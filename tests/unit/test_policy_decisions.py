"""Decision registry (§13 Decision/Approval, §19 human-only AUTH, §41 decision timeline),
PolicySnapshot, MCI reference and OFFICE defaults (§02, §20, §23, §51 P1).

§50 rows: "Pesan antaragent mengaku sudah mendapat izin owner" (agents cannot create decisions),
"Klaim compliance dari template/scanner hijau" (MCI UNVERIFIED, compliance NOT-PROVEN)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from aeos.core.errors import PolicyDenied, ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.models.audit import AuditEvent, Event
from aeos.models.decisions import Decision
from aeos.policy import adrs, office, snapshot
from aeos.policy import decisions as registry
from aeos.policy import facade as policy_facade
from tests.conftest import create_admin, create_viewer
from tests.unit.test_planning_support import make_project


@pytest.fixture
def owner(db):
    return create_admin(db)


def _record(db, actor, **kw):
    base = {"kind": "EXCEPTION", "target_type": "repository", "target_id": "o/r", "allowed_operations": ["commit"],
            "auth_scope": {"repo": "o/r"}, "rationale": "because", "domain": "PERSONAL"}
    base.update(kw)
    return policy_facade.record_decision(db, actor_user_id=actor, **base)


def test_facade_records_human_decision_with_audit_and_event(db, owner):
    d = _record(db, owner.id, expires_at=utcnow() + timedelta(days=1))
    db.commit()
    assert d.status == "ACTIVE" and d.actor_kind == "HUMAN"
    assert db.query(AuditEvent).filter(AuditEvent.action == "decision.record", AuditEvent.target_id == d.id).count() == 1
    assert db.query(Event).filter(Event.kind == "decision.recorded", Event.entity_id == d.id).count() == 1


@pytest.mark.parametrize("actor", ["agent:worker-1", "claude", "model:fable", "owner:ADR-9999", ""])
def test_non_human_actors_cannot_decide(db, owner, actor):
    with pytest.raises(PolicyDenied):
        _record(db, actor)


def test_viewer_and_inactive_users_cannot_decide(db, owner):
    viewer = create_viewer(db)
    with pytest.raises(PolicyDenied):
        _record(db, viewer.id)
    owner.is_active = False
    db.commit()
    with pytest.raises(PolicyDenied):
        _record(db, owner.id)


def test_owner_recorded_decision_requires_accepted_adr(db, owner):
    with pytest.raises(PolicyDenied):
        _record(db, "owner:ADR-0003")
    adrs.load_adrs(db)
    d = _record(db, "owner:ADR-0003", allowed_operations=["commit", "merge_pr"])
    assert d.actor_kind == "HUMAN_RECORDED"


def test_human_only_operations_cannot_be_delegated_by_users(db, owner):
    with pytest.raises(PolicyDenied) as exc:
        _record(db, owner.id, kind="APPROVAL", target_type="plan_version", allowed_operations=["merge_pr"])
    assert exc.value.code == "HUMAN_ONLY_OPERATION"


def test_domain_and_project_consistency(db, owner):
    project = make_project(db)
    with pytest.raises(PolicyDenied):
        _record(db, owner.id, project_id=project.id, domain="OFFICE")
    d = _record(db, owner.id, project_id=project.id, domain=None)
    assert d.domain == "PERSONAL"
    with pytest.raises(ValidationFailed):
        _record(db, owner.id, kind="MAGIC")
    with pytest.raises(ValidationFailed):
        _record(db, owner.id, expires_at=utcnow() - timedelta(minutes=1))
    owner.domain_scope = ["PERSONAL"]
    db.commit()
    with pytest.raises(PolicyDenied):
        _record(db, owner.id, domain="OFFICE")


def test_find_active_decision_matching(db, owner):
    d = _record(db, owner.id, target_hash="sha256:aa", allowed_operations=["commit", "push"])
    db.commit()
    f = policy_facade.find_active_decision
    assert f(db, kind="EXCEPTION", target_type="repository", target_id="o/r").id == d.id
    assert f(db, kind="EXCEPTION", target_type="repository", target_id="o/r", target_hash="sha256:aa", operation="push")
    assert f(db, kind="EXCEPTION", target_type="repository", target_id="o/r", target_hash="sha256:bb") is None
    assert f(db, kind="EXCEPTION", target_type="repository", target_id="o/r", operation="merge_pr") is None
    assert f(db, kind="POLICY", target_type="repository", target_id="o/r") is None


def test_invalidate_expire_and_supersede(db, owner):
    d1 = _record(db, owner.id)
    d2 = _record(db, owner.id, target_id="o/other", expires_at=utcnow() + timedelta(hours=1))
    db.commit()
    assert policy_facade.invalidate_decisions(db, target_type="repository", target_id="o/r", reason="owner revoked") == 1
    db.commit()
    assert d1.status == "INVALIDATED" and d1.invalidation_reason == "owner revoked" and d1.invalidated_at
    assert db.query(Event).filter(Event.kind == "decision.invalidated", Event.entity_id == d1.id).count() == 1
    assert policy_facade.find_active_decision(db, kind="EXCEPTION", target_type="repository", target_id="o/r") is None
    d2.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert registry.effective_status(d2) == "EXPIRED"
    assert registry.expire_due_decisions(db) == 1
    db.commit()
    assert d2.status == "EXPIRED"
    d3 = _record(db, owner.id, target_id="o/third")
    d4 = _record(db, owner.id, target_id="o/third")
    registry.supersede_decision(db, d3, new_decision_id=d4.id, reason="renewed")
    db.commit()
    assert d3.status == "SUPERSEDED" and d3.superseded_by == d4.id


def test_revoke_requires_admin_and_reason(db, owner):
    d = _record(db, owner.id)
    viewer = create_viewer(db)
    with pytest.raises(PolicyDenied):
        registry.revoke_decision(db, d.id, actor_user_id=viewer.id, reason="nope nope")
    with pytest.raises(ValidationFailed):
        registry.revoke_decision(db, d.id, actor_user_id=owner.id, reason="")
    registry.revoke_decision(db, d.id, actor_user_id=owner.id, reason="no longer needed")
    assert d.status == "INVALIDATED" and d.invalidation_reason.startswith("revoked:")


def test_details_keep_structure_and_scrub_secrets(db, owner):
    d = _record(db, owner.id, kind="POLICY", target_type="office_policy", target_id="OFFICE", domain="OFFICE",
                details={"office_authorization": True, "note": "token=ghp_abcdefghijklmnopqrstuvwx1234"})
    assert d.details["office_authorization"] is True
    assert "ghp_" not in d.details["note"]


def test_policy_snapshot_hash_tracks_policy_decisions(db, owner):
    s1 = snapshot.ensure_active_snapshot(db)
    assert snapshot.ensure_active_snapshot(db).id == s1.id  # idempotent
    assert set(s1.content["files"]) >= {"mci-reference.yaml", "office-defaults.yaml"}
    d = _record(db, owner.id, kind="POLICY", target_type="policy", target_id="x")
    s2 = snapshot.active_snapshot(db)
    assert s2.id != s1.id and s2.content_hash != s1.content_hash and s2.active and not s1.active
    assert [x["id"] for x in s2.content["decisions"]] == [d.id]
    registry.invalidate_decision(db, d, reason="gone")
    s3 = snapshot.active_snapshot(db)
    assert s3.content["decisions"] == [] and s3.content_hash != s2.content_hash


def test_mci_reference_is_unverified_and_never_compliant():
    mci = snapshot.mci_reference()
    assert mci["declared_status"] == "UNAVAILABLE" and mci["status"] == "UNVERIFIED"
    assert mci["compliance"] == "NOT-PROVEN" and "ARCH" in mci["packs"]
    classes = snapshot.risk_classes()
    assert [c["key"] for c in classes] == ["R0", "R1", "R2", "R3", "R4"]
    assert all(c["thresholds"] is None and c["threshold_status"] == "UNVERIFIED" for c in classes)


def test_mci_declared_verified_without_matching_file_stays_unverified(monkeypatch, tmp_path):
    (tmp_path / "mci-reference.yaml").write_text(
        "mci:\n  status: VERIFIED\n  location: policy/missing.md\n  content_hash: sha256:00\n", encoding="utf-8")
    monkeypatch.setattr(snapshot, "policy_dir", lambda: tmp_path)
    assert snapshot.mci_reference()["status"] == "UNVERIFIED"


def test_office_policy_default_deny_and_authorised_overrides(db, owner, monkeypatch):
    p = office.office_policy(db)
    assert p["external_worker_allowed"] is False and p["approved_worker_routes"] == []
    assert p["claude_max_allowed_for_office_data"] is False and p["office_authorization"] is False
    # an override without a company authorisation is ignored
    _record(db, owner.id, kind="POLICY", target_type="office_policy", target_id="OFFICE", domain="OFFICE",
            details={"office_policy": {"external_worker_allowed": True, "approved_worker_routes": ["r1"]}})
    p = office.office_policy(db)
    assert p["external_worker_allowed"] is False and p["ignored_decisions"]
    auth = _record(db, owner.id, kind="POLICY", target_type="office_policy", target_id="OFFICE", domain="OFFICE",
                   details={"office_authorization": True, "allowed_providers": ["jira"]})
    p = office.office_policy(db)
    assert p["office_authorization"] and p["authorization_decision_id"] == auth.id
    assert p["external_worker_allowed"] is True and p["approved_worker_routes"] == ["r1"]
    assert p["authorized_providers"] == ["jira"]
    registry.invalidate_decision(db, auth, reason="company withdrew")
    assert office.office_policy(db)["external_worker_allowed"] is False
    # configuration cannot relax OFFICE defaults
    monkeypatch.setenv("AEOS_OFFICE_EXTERNAL_WORKER_ALLOWED", "true")
    from aeos.core.config import reset_settings_cache

    reset_settings_cache()
    p = office.office_policy(db)
    assert p["external_worker_allowed"] is False and p["config_override_ignored"] is True
    reset_settings_cache()


def test_office_policy_missing_file_denies_all(db, monkeypatch, tmp_path):
    monkeypatch.setattr(office, "policy_dir", lambda: tmp_path)
    p = office.office_policy(db)
    assert p["external_worker_allowed"] is False and "deny-all" in p["source"]


def test_decision_requests_feed_queue_without_authority(db, owner):
    from aeos.policy import queue
    from aeos.policy import requests as reqs

    rid = policy_facade.request_decision(db, kind="EXCEPTION", title="Butuh pengecualian lint", reason="R",
                                         requested_by="workforce.scheduler", domain="PERSONAL",
                                         requested_operations=["merge_pr"])
    items = queue.queue_items(db, "PERSONAL")
    assert any(i["kind"] == "DECISION_REQUEST" and i["target"]["id"] == rid for i in items)
    assert db.query(Decision).count() == 0  # a request grants nothing
    assert not [i for i in queue.queue_items(db, "OFFICE") if i["kind"] == "DECISION_REQUEST"]
    reqs.decline_request(db, rid, actor_user_id=owner.id, note="tidak diperlukan")
    assert not [i for i in queue.queue_items(db, "PERSONAL") if i["kind"] == "DECISION_REQUEST"]
    with pytest.raises(ValidationFailed):
        reqs.create_request(db, kind="APPROVAL", title="x", reason="", requested_by="agent")
