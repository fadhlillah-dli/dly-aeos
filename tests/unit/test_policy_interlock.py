"""External-write interlock (§26): fail closed, all reasons collected.

§50 rows: "Bootstrap belum selesai tetapi publish diminta" (denied), "VIEWER memanggil write endpoint",
"Approval PlanVersion lama"/"approval lama tidak dapat dipakai setelah perubahan hash", OFFICE default
deny (§23), "Agent mencoba direct push main/merge API" (human-only operation)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from aeos.core.enums import Domain
from aeos.core.hashing import content_hash
from aeos.core.timeutil import utcnow
from aeos.models.audit import AuditEvent
from aeos.models.identity import SystemSetting, User
from aeos.models.planning import Plan, PlanVersion
from aeos.planning import service
from aeos.policy import decisions as registry
from aeos.policy import enablement
from aeos.policy.facade import WriteIntent, check_external_write
from tests.conftest import create_admin, create_viewer
from tests.unit.test_planning_support import make_project, sample_content

DOC_TARGET = {"provider": "google-docs", "root_id": "F1"}
CONF_TARGET = {"provider": "confluence", "space": "ENG", "parent_id": "123"}


def _approved_plan(db, owner, project, *, targets, action="APPROVE_AND_PUBLISH", mode="LIVE"):
    content = sample_content()
    plan = Plan(project_id=project.id, domain=project.domain, title="p", state="READY_FOR_HUMAN_REVIEW")
    db.add(plan)
    db.flush()
    v = PlanVersion(plan_id=plan.id, project_id=project.id, domain=project.domain, version=1, content=content,
                    manifest_hash=content_hash(content), mode=mode)
    db.add(v)
    db.flush()
    plan.current_version_id = v.id
    res = service.decide(db, plan.id, action=action, version_id=v.id, version_hash=v.manifest_hash, targets=targets,
                         rationale="ok", actor_user_id=owner.id)
    db.commit()
    return plan, v, res["decision"]["id"]


def _enable(db, owner, provider, domain):
    registry.record_decision(db, kind="WRITE_ENABLEMENT", actor_user_id=owner.id, target_type="provider_write",
                             target_id=provider, domain=domain, allowed_operations=["create_doc"],
                             auth_scope={"provider": provider}, rationale="enable writes", details={"enabled": True})
    enablement.set_flag(db, provider, True)
    db.commit()


@pytest.fixture
def owner(db):
    return create_admin(db)


@pytest.fixture
def personal(db, owner):
    project = make_project(db)
    plan, v, decision_id = _approved_plan(db, owner, project, targets=[DOC_TARGET])
    _enable(db, owner, "google-docs", "PERSONAL")
    intent = WriteIntent(
        actor_user_id=owner.id, project_id=project.id, domain="PERSONAL", provider="google-docs",
        operation="create_doc", target={"provider": "google-docs", "root_id": "F1", "logical_id": "00-product-home"},
        approved_plan_version_id=v.id, approved_hash=v.manifest_hash, decision_id=decision_id,
        capability_expires_at=utcnow() + timedelta(minutes=10),
    )
    return {"project": project, "plan": plan, "version": v, "decision_id": decision_id, "intent": intent}


def _codes(result):
    return {r.split(":", 1)[0] for r in result.reasons}


def test_full_valid_personal_write_is_allowed(db, personal):
    res = check_external_write(db, personal["intent"])
    assert res.allowed is True and res.reasons == []
    assert res.decision is not None and res.decision.id == personal["decision_id"]
    audit = db.query(AuditEvent).filter(AuditEvent.action == "policy.interlock.check").all()
    assert audit and audit[-1].result == "SUCCESS"


def test_target_naming_another_provider_is_denied(db, personal):
    """A target dict that names a different provider than the write never passes the scope check."""
    res = check_external_write(db, replace(personal["intent"], target={"provider": "github-repo", "root_id": "F1"}))
    assert not res.allowed and _codes(res) == {"TARGET_PROVIDER_MISMATCH"}


def test_approval_not_made_by_a_human_is_denied(db, personal):
    """§19/§20: the approval behind a write must be a live human decision. A row whose actor kind is
    anything else (forged/tampered, ADR-recorded, agent) is refused even if every binding matches."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    from aeos.models.decisions import Decision

    # Tampering with an existing decision's actor kind is refused by the DB (immutable binding column).
    db.get(Decision, personal["decision_id"]).actor_kind = "AGENT"
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    # A forged row inserted with every binding copied but a non-human actor kind is refused by the interlock.
    orig = db.get(Decision, personal["decision_id"])
    cols = {c.name: getattr(orig, c.name) for c in Decision.__table__.columns if c.name != "id"}
    orig.status = "SUPERSEDED"
    forged = Decision(**{**cols, "actor_kind": "AGENT"})
    db.add(forged)
    db.commit()
    res = check_external_write(db, replace(personal["intent"], decision_id=forged.id))
    assert not res.allowed and "DECISION_ACTOR_NOT_HUMAN" in _codes(res)


def test_bootstrap_incomplete_denied(db, personal):
    db.get(SystemSetting, "bootstrap.admin_created").value = False
    db.commit()
    res = check_external_write(db, personal["intent"])
    assert not res.allowed and "BOOTSTRAP_INCOMPLETE" in _codes(res)


def test_viewer_actor_denied(db, personal):
    viewer = create_viewer(db)
    res = check_external_write(db, replace(personal["intent"], actor_user_id=viewer.id))
    assert not res.allowed and "ACTOR_NOT_AUTHORISED" in _codes(res)


def test_inactive_or_unknown_actor_denied(db, personal):
    res = check_external_write(db, replace(personal["intent"], actor_user_id="agent:worker-1"))
    assert "ACTOR_NOT_ACTIVE" in _codes(res)


def test_admin_outside_domain_scope_denied(db, owner, personal):
    """§20: SUPER_ADMIN is not automatically AUTH over every domain."""
    user = db.get(User, owner.id)
    user.domain_scope = ["OFFICE"]
    db.commit()
    res = check_external_write(db, personal["intent"])
    assert "ACTOR_DOMAIN_SCOPE" in _codes(res)


def test_domain_mismatch_denied(db, personal):
    res = check_external_write(db, replace(personal["intent"], domain="OFFICE"))
    assert {"DOMAIN_MISMATCH", "PROVIDER_NOT_ALLOWED"} <= _codes(res)


def test_wrong_provider_denied(db, personal):
    res = check_external_write(db, replace(personal["intent"], provider="jira", operation="create_issue"))
    assert "PROVIDER_NOT_ALLOWED" in _codes(res)


def test_write_disabled_and_missing_enablement_decision(db, personal):
    enablement.set_flag(db, "google-docs", False)
    db.commit()
    assert "WRITE_DISABLED" in _codes(check_external_write(db, personal["intent"]))
    enablement.set_flag(db, "google-docs", True)
    registry.invalidate_decisions(db, target_type="provider_write", target_id="google-docs", reason="revoked")
    db.commit()
    res = check_external_write(db, personal["intent"])
    # invalidating the enabling decision also turns the flag off (fail closed)
    assert "WRITE_DISABLED" in _codes(res) and enablement.flag(db, "google-docs") is False


def test_wrong_hash_denied(db, personal):
    res = check_external_write(db, replace(personal["intent"], approved_hash="sha256:" + "1" * 64))
    assert {"APPROVED_HASH_MISMATCH", "DECISION_HASH_MISMATCH"} <= _codes(res)


def test_expired_decision_denied(db, personal):
    from aeos.models.decisions import Decision

    d = db.get(Decision, personal["decision_id"])
    d.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    res = check_external_write(db, personal["intent"])
    assert not res.allowed and any(r.startswith("DECISION_NOT_ACTIVE") and "EXPIRED" in r for r in res.reasons)


def test_invalidated_decision_denied(db, personal):
    registry.invalidate_decisions(db, target_type="plan_version", target_id=personal["version"].id, reason="x")
    db.commit()
    res = check_external_write(db, personal["intent"])
    assert {"DECISION_NOT_ACTIVE", "VERSION_NOT_APPROVED"} <= _codes(res)


def test_operation_not_allowed_and_target_out_of_scope(db, personal):
    assert "OPERATION_UNKNOWN" in _codes(check_external_write(db, replace(personal["intent"], operation="delete_doc")))
    res = check_external_write(db, replace(personal["intent"], target={"provider": "google-docs", "root_id": "F2"}))
    assert "TARGET_NOT_APPROVED" in _codes(res)


def test_operation_not_in_approval_denied(db, owner):
    project = make_project(db)
    _plan, v, did = _approved_plan(db, owner, project, targets=[{"provider": "github-issues", "repo": "o/r"}])
    _enable(db, owner, "google-docs", "PERSONAL")
    intent = WriteIntent(actor_user_id=owner.id, project_id=project.id, domain="PERSONAL", provider="google-docs",
                         operation="create_doc", target=DOC_TARGET, approved_plan_version_id=v.id,
                         approved_hash=v.manifest_hash, decision_id=did,
                         capability_expires_at=utcnow() + timedelta(minutes=5))
    assert {"OPERATION_NOT_APPROVED", "TARGET_NOT_APPROVED"} <= _codes(check_external_write(db, intent))


def test_mock_plan_version_is_never_published(db, owner):
    """§35/§49: fixture (MOCK) plan content is never written externally, even when approved."""
    project = make_project(db)
    _plan, v, did = _approved_plan(db, owner, project, targets=[DOC_TARGET], mode="MOCK")
    _enable(db, owner, "google-docs", "PERSONAL")
    intent = WriteIntent(actor_user_id=owner.id, project_id=project.id, domain="PERSONAL", provider="google-docs",
                         operation="create_doc", target=DOC_TARGET, approved_plan_version_id=v.id,
                         approved_hash=v.manifest_hash, decision_id=did,
                         capability_expires_at=utcnow() + timedelta(minutes=5))
    res = check_external_write(db, intent)
    assert not res.allowed and _codes(res) == {"VERSION_NOT_LIVE"}


def test_human_only_merge_is_always_denied(db, personal):
    res = check_external_write(db, replace(personal["intent"], provider="github-repo", operation="merge_pr",
                                           target={"provider": "github-repo", "repo": "o/r"}))
    assert "HUMAN_ONLY_OPERATION" in _codes(res)


def test_capability_missing_or_expired_denied(db, personal):
    assert "CAPABILITY_MISSING" in _codes(check_external_write(db, replace(personal["intent"], capability_expires_at=None)))
    expired = replace(personal["intent"], capability_expires_at=utcnow() - timedelta(seconds=5))
    assert "CAPABILITY_EXPIRED" in _codes(check_external_write(db, expired))


def test_all_reasons_are_collected(db):
    """No bootstrap, unknown actor/project, bad provider: every failing condition is reported."""
    intent = WriteIntent(actor_user_id="nobody", project_id="missing", domain="PERSONAL", provider="jira",
                         operation="create_issue", target={}, approved_plan_version_id=None, approved_hash=None,
                         decision_id=None)
    res = check_external_write(db, intent)
    assert not res.allowed
    assert {"BOOTSTRAP_INCOMPLETE", "ACTOR_NOT_ACTIVE", "PROJECT_UNKNOWN", "PROVIDER_NOT_ALLOWED", "TARGET_MISSING",
            "WRITE_DISABLED", "APPROVED_VERSION_MISSING", "CAPABILITY_MISSING"} <= _codes(res)


@pytest.fixture
def office(db, owner):
    project = make_project(db, domain=Domain.OFFICE)
    plan, v, did = _approved_plan(db, owner, project, targets=[CONF_TARGET])
    _enable(db, owner, "confluence", "OFFICE")
    intent = WriteIntent(actor_user_id=owner.id, project_id=project.id, domain="OFFICE", provider="confluence",
                         operation="create_page", target={**CONF_TARGET, "logical_id": "00-product-home"},
                         approved_plan_version_id=v.id, approved_hash=v.manifest_hash, decision_id=did,
                         capability_expires_at=utcnow() + timedelta(minutes=5))
    return intent


def test_office_without_company_authorisation_denied(db, office):
    res = check_external_write(db, office)
    assert {"OFFICE_NOT_AUTHORISED", "ATLASSIAN_DEPLOYMENT_UNKNOWN"} <= _codes(res)


def test_office_with_authorisation_and_known_deployment_allowed(db, owner, office):
    registry.record_decision(
        db, kind="POLICY", actor_user_id=owner.id, target_type="office_policy", target_id="OFFICE", domain="OFFICE",
        allowed_operations=[], auth_scope={"company": "ACME"}, rationale="company letter 2026-10",
        details={"office_authorization": True, "company_reference": "letter", "allowed_providers": ["confluence"],
                 "office_policy": {"atlassian_deployment": "CLOUD"}})
    db.commit()
    res = check_external_write(db, office)
    assert res.allowed, res.reasons
    other = replace(office, provider="jira", operation="create_issue", target={"provider": "jira", "project_key": "ENG"})
    assert "OFFICE_PROVIDER_NOT_AUTHORISED" in _codes(check_external_write(db, other))
