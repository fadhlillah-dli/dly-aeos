"""`aeos project reclassify`: the owner path that changes a project's data classification (ADR-0133).

Owner decision 2026-10-28: the DLY AEOS platform project (seeded INTERNAL) becomes PUBLIC, so PUBLIC-only routes may
take its tasks. Raising a class is allowed with an audit record; lowering is a SUPER_ADMIN POLICY decision with an
audit record and an event naming old class, new class, reason and actor; an OFFICE project is never lowered (master
s23); in-flight work keeps its class; the HTTP PATCH never changes the class. Each refusal asserts its own message,
next to a near miss that is allowed.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from aeos.core.enums import Domain
from aeos.core.errors import ValidationFailed
from aeos.core.hashing import content_hash
from aeos.models.audit import AuditEvent, Event
from aeos.models.decisions import Decision
from aeos.models.planning import Plan, PlanVersion, TaskSpec
from aeos.models.portfolio import Project
from aeos.planning import dag
from tests.conftest import AuthedClient, create_admin, create_viewer
from tests.unit.test_planning_support import make_project, sample_content, task

REASON = "owner approved in chat 2026-10-28: the repository is public on GitHub"
ACTION = "portfolio.project.reclassify"
EVENT = "portfolio.project.reclassified"


def _cli(*args: str):
    from aeos.cli import app

    return CliRunner().invoke(app, ["project", "reclassify", *args])


def _reclassify(project: str, to: str, *, domain: str = "PERSONAL", actor: str = "owner", reason: str = REASON):
    return _cli("--project", project, "--domain", domain, "--to", to, "--reason", reason, "--actor", actor)


def _project(db, project_id: str) -> Project:
    db.expire_all()
    p = db.get(Project, project_id)
    assert p is not None
    return p


def _decisions(db) -> list[Decision]:
    db.expire_all()
    return list(db.scalars(select(Decision).where(Decision.target_type == "project_classification")
                           .order_by(Decision.created_at)))


def _audits(db, result: str | None = None) -> list[AuditEvent]:
    db.expire_all()
    q = select(AuditEvent).where(AuditEvent.action == ACTION)
    if result:
        q = q.where(AuditEvent.result == result)
    return list(db.scalars(q.order_by(AuditEvent.seq)))


def _events(db) -> list[Event]:
    db.expire_all()
    return list(db.scalars(select(Event).where(Event.kind == EVENT).order_by(Event.seq)))


def _version(db, project: Project, content: dict[str, Any]) -> PlanVersion:
    plan = Plan(project_id=project.id, domain=project.domain, title="t", state="APPROVED")
    db.add(plan)
    db.flush()
    v = PlanVersion(plan_id=plan.id, project_id=project.id, domain=project.domain, version=1, content=content,
                    manifest_hash=content_hash(content), mode="LIVE")
    db.add(v)
    db.flush()
    plan.current_version_id = plan.approved_version_id = v.id
    db.commit()
    return v


def _classes(db, version: PlanVersion) -> dict[str, str]:
    db.expire_all()
    return {t.task_key: t.data_classification
            for t in db.scalars(select(TaskSpec).where(TaskSpec.plan_version_id == version.id))}


# ---------------------------------------------------------------- lowering needs a SUPER_ADMIN


def test_lowering_without_super_admin_is_refused(db):
    create_admin(db)
    create_viewer(db)
    p = make_project(db, key="lower-viewer")
    r = _reclassify("lower-viewer", "PUBLIC", actor="viewer")
    assert r.exit_code == 1, r.output
    assert "LOWERING_NEEDS_SUPER_ADMIN" in r.output and "only an active SUPER_ADMIN may record it" in r.output
    assert "Nothing was changed." in r.output and r.output.isascii()
    assert _project(db, p.id).data_classification == "INTERNAL"
    assert _decisions(db) == [] and _events(db) == []
    denied = _audits(db, "DENIED")
    assert len(denied) == 1 and denied[0].details["code"] == "LOWERING_NEEDS_SUPER_ADMIN"
    # near miss: the same lowering by the SUPER_ADMIN is recorded
    ok = _reclassify("lower-viewer", "PUBLIC")
    assert ok.exit_code == 0, ok.output
    assert _project(db, p.id).data_classification == "PUBLIC"


def test_viewer_cannot_raise_either_and_unknown_actor_is_refused(db):
    """VIEWER is read-only (master s02): a raise needs no decision, but it still needs an actor who may write."""
    create_admin(db)
    create_viewer(db)
    p = make_project(db, key="raise-viewer", classification="PUBLIC")
    r = _reclassify("raise-viewer", "INTERNAL", actor="viewer")
    assert r.exit_code == 1 and "RECLASSIFY_READ_ONLY_ACTOR" in r.output and "(read-only)" in r.output
    r = _reclassify("raise-viewer", "INTERNAL", actor="nobody")
    assert r.exit_code == 2 and "--actor nobody is not an active user" in r.output
    assert _project(db, p.id).data_classification == "PUBLIC" and _events(db) == []


# ---------------------------------------------------------------- OFFICE is never lowered


def test_office_lowering_is_refused(db):
    create_admin(db)
    p = make_project(db, key="office-lower", domain=Domain.OFFICE)
    r = _reclassify("office-lower", "PUBLIC", domain="OFFICE")
    assert r.exit_code == 1, r.output
    assert "OFFICE_LOWERING_DENIED" in r.output and "is never lowered by this command" in r.output
    assert "company's authorisation" in r.output
    assert _project(db, p.id).data_classification == "INTERNAL" and _decisions(db) == []
    denied = _audits(db, "DENIED")
    assert len(denied) == 1 and denied[0].details["code"] == "OFFICE_LOWERING_DENIED"
    assert denied[0].domain == "OFFICE"
    # near miss: raising the OFFICE project is allowed (audit record, no decision)
    ok = _reclassify("office-lower", "CONFIDENTIAL", domain="OFFICE")
    assert ok.exit_code == 0, ok.output
    assert _project(db, p.id).data_classification == "CONFIDENTIAL" and _decisions(db) == []


# ---------------------------------------------------------------- lowering records decision + audit + event


def test_lowering_records_decision_audit_and_event(db, secrets_memory):
    from aeos.portfolio.seed import run

    admin = create_admin(db)
    seeded = run(db)
    db.commit()
    pid = seeded["project_id"]
    assert _project(db, pid).data_classification == "INTERNAL"  # the seeded platform project
    r = _reclassify("dly-aeos", "PUBLIC")
    assert r.exit_code == 0, r.output
    assert r.output.isascii() and "Project dly-aeos (PERSONAL): INTERNAL -> PUBLIC (lowered)." in r.output
    p = _project(db, pid)
    assert p.data_classification == "PUBLIC" and p.domain == "PERSONAL"

    [d] = _decisions(db)
    assert (d.kind, d.target_type, d.target_id, d.target_version, d.status) == (
        "POLICY", "project_classification", pid, "PUBLIC", "ACTIVE")
    assert d.actor_user_id == admin.id and d.actor_kind == "HUMAN" and d.domain == "PERSONAL"
    assert d.rationale == REASON and d.allowed_operations == ["lower_data_classification"]
    assert d.details["from_classification"] == "INTERNAL" and d.details["to_classification"] == "PUBLIC"
    assert d.details["actor_username"] == "owner"
    assert d.target_hash == content_hash({"project_id": pid, "from": "INTERNAL", "to": "PUBLIC"})
    assert f"Recorded POLICY decision {d.id} on project_classification:{pid}" in r.output

    [a] = _audits(db, "SUCCESS")
    assert a.actor_id == admin.id and a.reason == REASON and a.target_id == pid
    assert (a.details["from"], a.details["to"], a.details["direction"]) == ("INTERNAL", "PUBLIC", "LOWER")
    assert a.details["actor_username"] == "owner" and a.details["decision_id"] == d.id
    [e] = _events(db)
    assert (e.payload["from"], e.payload["to"], e.payload["actor"], e.payload["reason"]) == (
        "INTERNAL", "PUBLIC", "owner", REASON)
    assert e.payload["decision_id"] == d.id and e.domain == "PERSONAL"
    # the registry records the decision itself as well (decision.record / decision.recorded)
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "decision.record",
                                              AuditEvent.target_id == d.id)) is not None
    assert "Actor owner; reason: " + REASON in r.output
    # no --approval-ref: recorded as absent and said so, never invented
    assert "Owner approval: not given (--approval-ref)" in r.output
    assert d.details["approval_ref"] is None and a.details["approval_ref"] is None
    assert "owner rule R43" in d.details["actor_check"] and a.details["actor_check"] == d.details["actor_check"]
    assert "In-flight work keeps its class" in r.output
    assert "Only plans and task DAGs compiled after this change use PUBLIC as their floor" in r.output

    # running it again is a no-op that records nothing
    again = _reclassify("dly-aeos", "PUBLIC")
    assert again.exit_code == 0 and "is already PUBLIC; nothing was recorded" in again.output
    assert len(_decisions(db)) == 1 and len(_audits(db)) == 1 and len(_events(db)) == 1


def _owner_command() -> list[str]:
    """The exact block of docs/OWNER_ACTIONS.md 2.13, as zsh would split it (continuations joined)."""
    import shlex

    from aeos.core.paths import repo_root

    text = (repo_root() / "docs" / "OWNER_ACTIONS.md").read_text(encoding="utf-8")
    section = text.split("### 2.13 ", 1)[1].split("\n### ", 1)[0]
    block = section.split("```bash\n", 1)[1].split("```", 1)[0]
    argv = shlex.split(block.replace("\\\n", " "))
    assert argv[:3] == ["./aeos", "project", "reclassify"], argv
    return argv[1:]


def test_owner_command_dry_run_on_the_seeded_platform_project(db, secrets_memory):
    """L13: the documented owner command, run in-process against a temporary database seeded like a real one."""
    from aeos.cli import app
    from aeos.portfolio.seed import run

    create_admin(db)
    pid = run(db)["project_id"]
    db.commit()
    argv = [("owner" if a == "<admin>" else a) for a in _owner_command()]
    r = CliRunner().invoke(app, argv)
    assert r.exit_code == 0, r.output
    assert "Project dly-aeos (PERSONAL): INTERNAL -> PUBLIC (lowered)." in r.output
    assert _project(db, pid).data_classification == "PUBLIC"
    [d] = _decisions(db)
    assert d.rationale == "owner decision 2026-10-28: the repository is public" and d.target_version == "PUBLIC"
    # how the owner approved is in the decision and the audit record (ADR-0133 decision 8)
    assert d.details["approval_ref"] == "chat 2026-10-28, owner rule R43"
    [a] = _audits(db, "SUCCESS")
    assert a.details["approval_ref"] == "chat 2026-10-28, owner rule R43" and a.details["decision_id"] == d.id
    assert _events(db)[0].payload["approval_ref"] == "chat 2026-10-28, owner rule R43"
    assert "Owner approval: chat 2026-10-28, owner rule R43" in r.output


def test_reason_is_required_and_lowercase_class_is_accepted(db):
    create_admin(db)
    p = make_project(db, key="short-reason")
    r = _reclassify("short-reason", "PUBLIC", reason="ok")
    assert r.exit_code == 1 and "REASON_TOO_SHORT" in r.output
    assert _project(db, p.id).data_classification == "INTERNAL" and _decisions(db) == []
    ok = _reclassify("short-reason", "public")  # near miss: case does not matter
    assert ok.exit_code == 0, ok.output
    assert _project(db, p.id).data_classification == "PUBLIC"


# ---------------------------------------------------------------- raising: audit only, ends the lowering


def test_raising_is_allowed_with_an_audit_record_only(db):
    admin = create_admin(db)
    p = make_project(db, key="raise-me", classification="PUBLIC")
    r = _reclassify("raise-me", "CONFIDENTIAL")
    assert r.exit_code == 0, r.output
    assert "PUBLIC -> CONFIDENTIAL (raised; audit record, no decision needed)." in r.output
    assert _project(db, p.id).data_classification == "CONFIDENTIAL"
    assert _decisions(db) == []
    [a] = _audits(db, "SUCCESS")
    assert (a.details["direction"], a.actor_id, a.reason) == ("RAISE", admin.id, REASON)
    [e] = _events(db)
    assert (e.payload["from"], e.payload["to"], e.payload["actor"]) == ("PUBLIC", "CONFIDENTIAL", "owner")


def test_raise_after_lowering_ends_the_lowering_decision(db):
    create_admin(db)
    p = make_project(db, key="up-down")
    assert _reclassify("up-down", "PUBLIC").exit_code == 0
    [d] = _decisions(db)
    r = _reclassify("up-down", "INTERNAL")
    assert r.exit_code == 0, r.output
    assert f"Earlier lowering decision(s) {d.id}: INVALIDATED." in r.output
    [d] = _decisions(db)
    assert d.status == "INVALIDATED" and "classification raised to INTERNAL by owner" in (d.invalidation_reason or "")
    assert _project(db, p.id).data_classification == "INTERNAL"


def test_revoking_the_lowering_decision_restores_the_class(db):
    """`aeos policy revoke`: consumers fail closed at once. Here the consumer is the project's class."""
    from aeos.policy.decisions import revoke_decision

    admin = create_admin(db)
    p = make_project(db, key="revoke-me", classification="CONFIDENTIAL")
    assert _reclassify("revoke-me", "INTERNAL").exit_code == 0
    assert _reclassify("revoke-me", "PUBLIC").exit_code == 0
    first, second = _decisions(db)
    # near miss: the newer lowering superseded the first one without restoring anything
    assert first.status == "SUPERSEDED" and second.status == "ACTIVE"
    assert _project(db, p.id).data_classification == "PUBLIC"
    revoke_decision(db, second.id, actor_user_id=admin.id, reason="owner withdrew the approval")
    db.commit()
    assert _project(db, p.id).data_classification == "INTERNAL"  # the class the revoked decision lowered from
    restored = [a for a in _audits(db, "SUCCESS") if a.details.get("trigger") == "decision_inactive"]
    assert len(restored) == 1 and restored[0].details["decision_id"] == second.id
    assert _events(db)[-1].payload["to"] == "INTERNAL"


# ---------------------------------------------------------------- in-flight work keeps its class


def test_new_plan_gets_public_tasks_while_an_existing_taskspec_keeps_internal(db):
    create_admin(db)
    project = make_project(db, key="dag-floor")
    public_tasks = [task("T1", data_classification="PUBLIC"),
                    task("T2", deps=["T1"], feature="F2", data_classification="PUBLIC")]
    before = _version(db, project, sample_content("Before", tasks=public_tasks))
    dag.compile_version(db, before, actor="tester")
    db.commit()
    assert _classes(db, before) == {"T1": "INTERNAL", "T2": "INTERNAL"}  # the INTERNAL project floor lifts them

    r = _reclassify("dag-floor", "PUBLIC")
    assert r.exit_code == 0, r.output
    assert "2 existing TaskSpec(s) (2 open), by class INTERNAL=2; none is changed" in r.output
    assert _classes(db, before) == {"T1": "INTERNAL", "T2": "INTERNAL"}  # in-flight work is not downgraded
    assert dag.compile_version(db, before, actor="tester")["created"] is False  # never recompiled

    project = _project(db, project.id)
    after = _version(db, project, sample_content("After", tasks=[
        task("T1", data_classification="PUBLIC"),
        task("T2", deps=["T1"], feature="F2", data_classification="INTERNAL")]))
    dag.compile_version(db, after, actor="tester")
    db.commit()
    # PUBLIC where the new plan says PUBLIC; near miss: a task its plan marks INTERNAL stays INTERNAL
    assert _classes(db, after) == {"T1": "PUBLIC", "T2": "INTERNAL"}


# ---------------------------------------------------------------- unknown class, domains, PATCH, generic path


def test_unknown_class_is_refused(db):
    create_admin(db)
    p = make_project(db, key="unknown-class")
    r = _reclassify("unknown-class", "SECRET")
    assert r.exit_code == 1, r.output
    assert "INVALID_CLASSIFICATION" in r.output and "unknown data classification 'SECRET'" in r.output
    assert _project(db, p.id).data_classification == "INTERNAL"
    assert _decisions(db) == [] and _audits(db) == [] and _events(db) == []
    ok = _reclassify("unknown-class", "RESTRICTED")  # near miss: a known class
    assert ok.exit_code == 0, ok.output


def test_other_domain_project_is_not_found_and_other_domain_is_untouched(db):
    """L12: a PERSONAL reclassification names no OFFICE project and changes nothing in OFFICE."""
    create_admin(db)
    personal = make_project(db, key="mine-personal")
    office = make_project(db, key="mine-office", domain=Domain.OFFICE)
    r = _reclassify("mine-personal", "PUBLIC", domain="OFFICE")
    assert r.exit_code == 1 and "Project mine-personal not found in OFFICE." in r.output
    r = _reclassify(personal.id, "PUBLIC")  # by id
    assert r.exit_code == 0, r.output
    assert _project(db, office.id).data_classification == "INTERNAL"
    assert {a.domain for a in _audits(db)} == {"PERSONAL"} and {e.domain for e in _events(db)} == {"PERSONAL"}
    assert all(d.domain == "PERSONAL" for d in _decisions(db))
    r = _cli("--project", "mine-personal", "--to", "PUBLIC", "--reason", REASON, "--actor", "owner")
    assert r.exit_code == 2 and "--domain PERSONAL or --domain OFFICE" in r.output


def test_patch_does_not_change_the_classification(personal_client: AuthedClient, db):
    from tests.unit.test_portfolio_support import create_project

    pid = create_project(personal_client, name="Patch Class")["id"]
    r = personal_client.patch(f"/api/projects/{pid}", json={"data_classification": "PUBLIC"})
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "FIELD_NOT_EDITABLE" and "aeos project reclassify" in err["message"]
    assert err["details"]["use"] == "aeos project reclassify"
    assert _project(db, pid).data_classification == "INTERNAL"
    # near miss: an editable field still patches
    assert personal_client.patch(f"/api/projects/{pid}", json={"name": "Patch Class 2"}).status_code == 200


def test_generic_policy_decision_cannot_target_a_project_classification(db):
    from aeos.policy import recording

    admin = create_admin(db)
    p = make_project(db, key="generic-path")
    with pytest.raises(ValidationFailed) as exc:
        recording.record_generic_decision(
            db, actor_user_id=admin.id, domain=Domain.PERSONAL, kind="POLICY", rationale="lower it quietly",
            auth_scope={"x": "y"}, project_id=p.id, target_type="project_classification", target_id=p.id)
    assert exc.value.code == "DEDICATED_ENDPOINT_REQUIRED" and "aeos project reclassify" in exc.value.message
