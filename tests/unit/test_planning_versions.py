"""Immutable PlanVersion/TaskSpec, human edits as new versions, approval binding (§14, §50 rows
"Approval PlanVersion lama", "Revisi baru muncul selama publish", "approval lama tidak dapat dipakai
setelah perubahan hash")."""

from __future__ import annotations

import copy

import pytest
from sqlalchemy import exc as sa_exc
from sqlalchemy import select

from aeos.core.errors import Conflict, PolicyDenied, ValidationFailed
from aeos.core.hashing import content_hash
from aeos.models.decisions import Decision
from aeos.models.planning import PlanVersion, TaskSpec
from aeos.planning import facade as planning_facade
from aeos.planning import service
from aeos.policy import decisions as registry
from tests.conftest import create_admin
from tests.unit.test_planning_support import FakeBridge, make_project

TARGET = {"provider": "google-drive", "root_id": "FOLDER-1"}


@pytest.fixture
def owner(db):
    return create_admin(db)


@pytest.fixture
def ready_plan(db, owner, monkeypatch, tmp_path):
    FakeBridge(monkeypatch, tmp_path)
    project = make_project(db)
    plan = service.create_plan(db, project=project, domain="PERSONAL", title="P", prompt="Aplikasi catatan pribadi lengkap",
                               attachment_ids=[], actor_user_id=owner.id)
    db.commit()
    service.run_pipeline(db, plan.id, actor=owner.id)
    db.refresh(plan)
    assert plan.state == "READY_FOR_HUMAN_REVIEW"
    return plan


def _current(db, plan):
    return db.get(PlanVersion, plan.current_version_id)


def _approve(db, owner, plan, version=None, action="APPROVE_AND_PUBLISH", targets=None):
    v = version or _current(db, plan)
    res = service.decide(db, plan.id, action=action, version_id=v.id, version_hash=v.manifest_hash,
                         targets=[TARGET] if targets is None else targets, rationale="ok", actor_user_id=owner.id)
    db.commit()
    return res


def test_plan_version_is_append_only(db, ready_plan):
    v = _current(db, ready_plan)
    v.content = {"tampered": True}
    with pytest.raises(sa_exc.DBAPIError, match="append-only"):
        db.commit()
    db.rollback()
    db.delete(db.get(PlanVersion, v.id))
    with pytest.raises(sa_exc.DBAPIError, match="append-only"):
        db.commit()
    db.rollback()
    assert content_hash(db.get(PlanVersion, v.id).content) == v.manifest_hash


def test_task_spec_is_append_only(db, owner, ready_plan):
    _approve(db, owner, ready_plan)
    spec = db.scalars(select(TaskSpec).where(TaskSpec.plan_version_id == ready_plan.current_version_id)).first()
    spec.goal = "changed by a worker"
    with pytest.raises(sa_exc.DBAPIError, match="append-only"):
        db.commit()
    db.rollback()


def test_decision_binding_columns_are_immutable(db, owner, ready_plan):
    res = _approve(db, owner, ready_plan)
    d = db.get(Decision, res["decision"]["id"])
    d.target_hash = "sha256:other"
    with pytest.raises(sa_exc.DBAPIError, match="immutable"):
        db.commit()
    db.rollback()


def test_approval_binds_exact_version_hash_targets_and_operations(db, owner, ready_plan):
    res = _approve(db, owner, ready_plan)
    v = _current(db, ready_plan)
    d = res["decision"]
    assert d["kind"] == "APPROVAL" and d["target_type"] == "plan_version" and d["target_id"] == v.id
    assert d["target_hash"] == v.manifest_hash and d["target_version"] == str(v.version)
    assert d["auth_scope"]["targets"] == [TARGET]
    assert set(d["allowed_operations"]) == {"compile_dag", "execute_tasks", "create_folder", "create_doc", "update_doc"}
    assert "merge_pr" not in d["allowed_operations"]
    assert ready_plan.state == "APPROVED" and ready_plan.approved_version_id == v.id
    assert res["compile"]["created"] is True and res["compile"]["tasks"] == 2
    assert planning_facade.get_approved_version(db, ready_plan.id).id == v.id


def test_stale_version_approval_rejected_with_latest_info(db, owner, ready_plan):
    """§50 'Approval PlanVersion lama': rejected; the latest revision is reported."""
    versions = list(db.scalars(select(PlanVersion).where(PlanVersion.plan_id == ready_plan.id).order_by(PlanVersion.version)))
    old, latest = versions[0], versions[-1]
    with pytest.raises(Conflict) as exc:
        _approve(db, owner, ready_plan, version=old)
    assert exc.value.code == "STALE_PLAN_VERSION"
    assert exc.value.details["latest_version_id"] == latest.id and exc.value.details["latest_hash"] == latest.manifest_hash
    with pytest.raises(Conflict):  # right version, wrong hash
        service.decide(db, ready_plan.id, action="APPROVE", version_id=latest.id, version_hash="sha256:" + "0" * 64,
                       targets=[], rationale="", actor_user_id=owner.id)
    assert db.scalar(select(Decision).where(Decision.kind == "APPROVAL")) is None


def test_human_edit_creates_new_version_and_invalidates_approval(db, owner, ready_plan):
    res = _approve(db, owner, ready_plan)
    v = _current(db, ready_plan)
    edited = copy.deepcopy(v.content)
    edited["overview"]["vision"] = "Visi baru dari pemilik"
    nv = service.create_human_version(db, ready_plan.id, content=edited, parent_version_id=v.id,
                                      rationale="perbaikan visi", actor_user_id=owner.id)
    db.commit()
    assert nv.source == "HUMAN_EDIT" and nv.parent_version_id == v.id and nv.version == v.version + 1
    assert nv.manifest_hash == content_hash(nv.content) != v.manifest_hash
    old = db.get(Decision, res["decision"]["id"])
    assert old.status == "INVALIDATED" and "superseded by plan version" in old.invalidation_reason
    assert ready_plan.approved_version_id is None and ready_plan.state == "READY_FOR_HUMAN_REVIEW"
    assert planning_facade.get_approved_version(db, ready_plan.id) is None
    # the old approval can no longer satisfy a lookup bound to the old hash (§35 acceptance)
    assert registry.find_active_decision(db, kind="APPROVAL", target_type="plan_version", target_id=v.id,
                                         target_hash=v.manifest_hash) is None


def test_verdict_is_tied_to_the_reviewed_version(db, owner, ready_plan):
    """The Fable verdict judged the synthesis version only; a later human edit is shown as not reviewed
    (a reviewer verdict is never silently reused for different content)."""
    detail = service.plan_detail(db, ready_plan)
    v = _current(db, ready_plan)
    assert detail["verdict_version_id"] == v.id and detail["current_version_reviewed"] is True
    edited = copy.deepcopy(v.content)
    edited["overview"]["vision"] = "Visi revisi pemilik"
    service.create_human_version(db, ready_plan.id, content=edited, parent_version_id=v.id, rationale="revisi visi",
                                 actor_user_id=owner.id)
    db.commit()
    detail = service.plan_detail(db, ready_plan)
    assert detail["verdict_version_id"] == v.id and detail["current_version_reviewed"] is False
    res = _approve(db, owner, ready_plan)  # the human may still decide; the decision records provenance
    assert res["decision"]["details"]["reviewed_version_id"] == v.id
    assert res["decision"]["details"]["version_source"] == "HUMAN_EDIT"
    # the fake bridge output is MOCK; a human edit of MOCK content stays MOCK (never becomes LIVE)
    assert res["decision"]["details"]["version_mode"] == "MOCK" and _current(db, ready_plan).mode == "MOCK"


def test_no_edit_or_redecision_while_publishing(db, owner, ready_plan):
    """§50 'Revisi baru muncul selama publish': while PUBLISHING no new version or decision can replace the
    approved one; the publisher keeps the exact approved version."""
    _approve(db, owner, ready_plan)
    v = _current(db, ready_plan)
    planning_facade.transition_plan(db, ready_plan.id, "PUBLISHING", reason="publisher started", actor="publisher")
    db.commit()
    edited = copy.deepcopy(v.content)
    edited["overview"]["vision"] = "Diubah saat publish"
    with pytest.raises(Conflict) as exc:
        service.create_human_version(db, ready_plan.id, content=edited, parent_version_id=v.id, rationale="ubah",
                                     actor_user_id=owner.id)
    assert exc.value.code == "PLAN_NOT_EDITABLE"
    with pytest.raises(Conflict):
        service.decide(db, ready_plan.id, action="REQUEST_REVISION", version_id=v.id, version_hash=v.manifest_hash,
                       targets=[], rationale="revisi saat publish", actor_user_id=owner.id)
    assert planning_facade.get_approved_version(db, ready_plan.id).id == v.id
    with pytest.raises(Conflict):  # illegal transition for the publisher too
        planning_facade.transition_plan(db, ready_plan.id, "APPROVED", reason="x", actor="publisher")


@pytest.mark.parametrize("target", ["APPROVED", "REJECTED", "REVISION_REQUESTED", "READY_FOR_HUMAN_REVIEW",
                                    "PRIMARY_PLANNING", "NEEDS_DECISION"])
def test_other_modules_cannot_set_human_decision_states(db, owner, ready_plan, target):
    """Human-only AUTH (§19, §20): the cross-module facade only moves publication states. APPROVED,
    REJECTED, ... require ``decide`` with a Decision bound to version + hash; NEEDS_DECISION only from a
    publication state."""
    with pytest.raises(Conflict) as exc:
        planning_facade.transition_plan(db, ready_plan.id, target, reason="publisher says so", actor="publisher")
    assert exc.value.code == "TRANSITION_NOT_DELEGATED" and ready_plan.state == "READY_FOR_HUMAN_REVIEW"
    assert planning_facade.get_approved_version(db, ready_plan.id) is None


def test_publisher_lifecycle_through_the_facade(db, owner, ready_plan):
    _approve(db, owner, ready_plan)
    for state in ("PUBLISHING", "PUBLICATION_PARTIAL", "NEEDS_DECISION", "PUBLISHING", "PUBLISHED"):
        planning_facade.transition_plan(db, ready_plan.id, state, reason="publisher", actor="publisher")
        assert ready_plan.state == state
    db.commit()


def test_publishing_requires_an_active_approval(db, owner, ready_plan):
    """The publisher cannot start/resume a publication when the approval stopped being ACTIVE, even if the
    plan state still says APPROVED (fail closed)."""
    res = _approve(db, owner, ready_plan)
    registry.invalidate_decisions(db, target_type="plan_version", target_id=ready_plan.approved_version_id,
                                  reason="revoked elsewhere", plan_hook=False)
    db.commit()
    assert ready_plan.state == "APPROVED" and db.get(Decision, res["decision"]["id"]).status == "INVALIDATED"
    with pytest.raises(Conflict) as exc:
        planning_facade.transition_plan(db, ready_plan.id, "PUBLISHING", reason="start", actor="publisher")
    assert exc.value.code == "NOT_APPROVED" and ready_plan.state == "APPROVED"


def test_human_edit_guards(db, owner, ready_plan):
    v = _current(db, ready_plan)
    with pytest.raises(Conflict) as exc:
        service.create_human_version(db, ready_plan.id, content=v.content, parent_version_id=v.parent_version_id,
                                     rationale="basis lama", actor_user_id=owner.id)
    assert exc.value.code == "STALE_PLAN_VERSION"
    with pytest.raises(ValidationFailed) as exc2:
        service.create_human_version(db, ready_plan.id, content=v.content, parent_version_id=v.id,
                                     rationale="tanpa perubahan", actor_user_id=owner.id)
    assert exc2.value.code == "NO_CHANGES"
    bad = copy.deepcopy(v.content)
    bad["unexpected"] = 1
    with pytest.raises(ValidationFailed) as exc3:
        service.create_human_version(db, ready_plan.id, content=bad, parent_version_id=v.id, rationale="rusak",
                                     actor_user_id=owner.id)
    assert exc3.value.code == "PLAN_SCHEMA_INVALID"
    cyclic = copy.deepcopy(v.content)
    cyclic["tasks"][0]["depends_on"] = [cyclic["tasks"][1]["key"]]
    with pytest.raises(ValidationFailed) as exc4:
        service.create_human_version(db, ready_plan.id, content=cyclic, parent_version_id=v.id, rationale="siklus",
                                     actor_user_id=owner.id)
    assert exc4.value.code == "DAG_INVALID"


def test_get_approved_version_ignores_newer_unapproved_version(db, owner, ready_plan):
    """§50 'Revisi baru muncul selama publish': the approved version is returned, never 'latest'."""
    _approve(db, owner, ready_plan)
    approved = _current(db, ready_plan)
    newer_content = copy.deepcopy(approved.content)
    newer_content["overview"]["overview"] = "revisi konkuren"
    db.add(PlanVersion(plan_id=ready_plan.id, project_id=ready_plan.project_id, domain=ready_plan.domain,
                       version=approved.version + 1, parent_version_id=approved.id, content=newer_content,
                       manifest_hash=content_hash(newer_content), source="IMPORT", created_by="concurrent"))
    db.commit()
    got = planning_facade.get_approved_version(db, ready_plan.id)
    assert got is not None and got.id == approved.id


def test_get_approved_version_fails_closed_without_active_approval(db, owner, ready_plan):
    res = _approve(db, owner, ready_plan)
    registry.revoke_decision(db, res["decision"]["id"], actor_user_id=owner.id, reason="owner revoked")
    db.commit()
    assert planning_facade.get_approved_version(db, ready_plan.id) is None
    assert ready_plan.state == "READY_FOR_HUMAN_REVIEW" and ready_plan.approved_version_id is None


def test_reapproval_supersedes_previous_approval(db, owner, ready_plan):
    first = _approve(db, owner, ready_plan, action="APPROVE", targets=[])
    second = _approve(db, owner, ready_plan, action="APPROVE_AND_PUBLISH")
    assert db.get(Decision, first["decision"]["id"]).status == "SUPERSEDED"
    assert db.get(Decision, second["decision"]["id"]).status == "ACTIVE"
    assert second["compile"]["created"] is False  # compile is idempotent per version


@pytest.mark.parametrize(("action", "targets", "code"), [
    ("APPROVE_AND_PUBLISH", [], "TARGET_MISSING"),
    ("APPROVE_AND_PUBLISH", [{"provider": "jira", "project_key": "X"}], "TARGET_INVALID"),
    ("APPROVE_AND_PUBLISH", [{"provider": "google-docs"}], "TARGET_NOT_EXACT"),
    ("APPROVE_AND_PUBLISH", [{"provider": "github-issues", "repo": "o/r"}], "TARGET_NOT_EXACT"),
    ("APPROVE", [TARGET], "TARGET_REQUIRES_PUBLISH"),
])
def test_approval_target_validation(db, owner, ready_plan, action, targets, code):
    v = _current(db, ready_plan)
    with pytest.raises(ValidationFailed) as exc:
        service.decide(db, ready_plan.id, action=action, version_id=v.id, version_hash=v.manifest_hash,
                       targets=targets, rationale="", actor_user_id=owner.id)
    assert exc.value.code == code


def test_approve_with_repo_target_binds_delivery_ops_only(db, owner, ready_plan):
    res = _approve(db, owner, ready_plan, action="APPROVE", targets=[{"provider": "github-repo", "repo": "o/r"}])
    assert set(res["decision"]["allowed_operations"]) == {"compile_dag", "execute_tasks", "push_branch", "create_pr"}


def test_revision_and_reject_need_rationale(db, owner, ready_plan):
    v = _current(db, ready_plan)
    with pytest.raises(ValidationFailed):
        service.decide(db, ready_plan.id, action="REJECT", version_id=v.id, version_hash=v.manifest_hash,
                       targets=[], rationale="", actor_user_id=owner.id)


def test_viewer_cannot_decide(db, owner, ready_plan):
    from tests.conftest import create_viewer

    viewer = create_viewer(db)
    v = _current(db, ready_plan)
    with pytest.raises(PolicyDenied):
        service.decide(db, ready_plan.id, action="APPROVE", version_id=v.id, version_hash=v.manifest_hash, targets=[],
                       rationale="", actor_user_id=viewer.id)


def test_diff_is_section_level(db, owner, ready_plan):
    versions = list(db.scalars(select(PlanVersion).where(PlanVersion.plan_id == ready_plan.id).order_by(PlanVersion.version)))
    d = service.diff_versions(db, ready_plan.id, versions[1].id, None)
    assert d["from"]["id"] == versions[0].id and d["to"]["id"] == versions[1].id
    assert "overview" in d["changed_sections"]
    overview = next(s for s in d["sections"] if s["section"] == "overview")
    assert overview["fields_changed"] == ["overview"]
    edited = copy.deepcopy(versions[1].content)
    edited["features"].append({"key": "F3", "title": "baru", "requirement": "", "priority": "P3", "acceptance": [],
                               "depends_on": [], "milestone": "M1"})
    edited["features"][0]["title"] = "diubah"
    sections = {s["section"]: s for s in service.diff_content(versions[1].content, edited)}
    assert sections["features"]["added"] == ["F3"] and sections["features"]["changed"] == ["F1"]
    assert sections["tasks"]["status"] == "unchanged"
