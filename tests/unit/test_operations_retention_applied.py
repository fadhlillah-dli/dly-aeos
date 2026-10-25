"""§35 "Audit/evidence retention follows policy": the policy/retention.yaml rule is applied to the stored audit
and evidence records - every listed record carries its rule, a decided and elapsed rule marks it "expired by
policy (manual disposal only)", a legal hold or an undecided rule never does, and nothing is ever deleted."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from aeos.core.timeutil import utcnow
from aeos.models.audit import AuditEvent
from aeos.models.evidence import EvidenceArtifact
from aeos.models.operations_ext import OpsRetirement
from aeos.operations import retention
from tests.conftest import AuthedClient

DECIDED = """status: DECIDED
version: 2
legal_hold_blocks_disposal: true
data_classes:
  audit_events:
    PERSONAL: {retain_days: 30}
    OFFICE: {retain_days: null}
  evidence:
    PERSONAL: {retain_days: 30}
    OFFICE: {retain_days: null}
"""


@pytest.fixture
def decided_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "retention.yaml"
    path.write_text(DECIDED, encoding="utf-8")
    monkeypatch.setattr(retention, "policy_path", lambda root=None: path)
    return path


def _old_and_new(db, project_id: str | None = None):
    old = utcnow() - timedelta(days=40)
    rows = [AuditEvent(actor_type="user", actor_id="u", action="test.old", result="SUCCESS", domain="PERSONAL",
                       at=old, project_id=project_id),
            AuditEvent(actor_type="user", actor_id="u", action="test.new", result="SUCCESS", domain="PERSONAL"),
            AuditEvent(actor_type="system", actor_id="s", action="test.platform", result="SUCCESS", at=old)]
    common = dict(kind="LOG", origin="collector", collector_identity="c", trusted=True, content_hash="sha256:x",
                  domain="PERSONAL", created_at=old, project_id=project_id)
    evs = [EvidenceArtifact(claim="old", **common),
           EvidenceArtifact(claim="held", retention_policy="legal-hold", **common),
           EvidenceArtifact(claim="new", **{**common, "created_at": utcnow()})]
    db.add_all(rows + evs)
    db.commit()
    return rows, evs


def test_undecided_policy_marks_every_record_retained(personal_client: AuthedClient, db):
    _old_and_new(db)
    items = personal_client.get("/api/audit").json()["items"]
    assert items and all(i["retention"]["rule"] == "RETAIN" and not i["retention"]["expired_by_policy"]
                         for i in items)
    ev = personal_client.get("/api/evidence").json()["items"]
    assert ev and all(not e["retention"]["expired_by_policy"] for e in ev)
    summary = {i["data_class"]: i for i in personal_client.get("/api/ops/retention").json()["items"]}
    assert summary["audit_events"]["expired_by_policy"] == 0 and summary["audit_events"]["records"] >= 2
    assert summary["evidence"]["records"] == 3 and summary["attachments"]["evaluated"] is False


def test_decided_rule_marks_old_records_expired_for_manual_disposal_only(personal_client: AuthedClient, db,
                                                                         decided_policy):
    _old_and_new(db)
    audit = {i["action"]: i["retention"] for i in personal_client.get("/api/audit?action_prefix=test.").json()["items"]}
    assert audit["test.old"]["expired_by_policy"] and audit["test.old"]["disposal"] == "MANUAL_ONLY"
    assert audit["test.old"]["label"] == retention.EXPIRED_LABEL
    assert not audit["test.new"]["expired_by_policy"] and audit["test.new"]["retain_days"] == 30
    assert not audit["test.platform"]["expired_by_policy"], "a platform record has no domain rule: retained"
    history = {i["action"]: i["retention"] for i in
               personal_client.get("/api/control-center/history/audit?action=test.").json()["items"]}
    assert history["test.old"]["expired_by_policy"] and not history["test.new"]["expired_by_policy"]
    ev = {e["claim"]: e["retention"] for e in personal_client.get("/api/evidence?kind=LOG").json()["items"]}
    assert ev["old"]["expired_by_policy"] and not ev["new"]["expired_by_policy"]
    assert ev["held"]["legal_hold"] and not ev["held"]["expired_by_policy"], "legal hold blocks expiry"
    summary = {i["data_class"]: i for i in personal_client.get("/api/ops/retention").json()["items"]}
    assert summary["evidence"]["expired_by_policy"] == 1 and summary["audit_events"]["expired_by_policy"] == 1
    # nothing is deleted: the expired records are still stored
    assert db.query(AuditEvent).filter(AuditEvent.action == "test.old").count() == 1
    assert db.query(EvidenceArtifact).filter(EvidenceArtifact.claim == "old").count() == 1


def test_project_legal_hold_blocks_expiry_of_its_records(personal_client: AuthedClient, db, decided_policy):
    from aeos.models.portfolio import Project

    p = Project(project_key="held", name="Held", domain="PERSONAL", data_classification="INTERNAL")
    db.add(p)
    db.flush()
    db.add(OpsRetirement(project_id=p.id, legal_hold={"active": True, "reason": "sengketa"}))
    db.commit()
    _old_and_new(db, project_id=p.id)
    markers = retention.marker(retention.load_policy(), "audit_events", "PERSONAL", utcnow() - timedelta(days=40),
                               legal_hold=True)
    assert not markers["expired_by_policy"]
    counts = retention.applied_counts(db, retention.load_policy(), "PERSONAL")
    assert counts["audit_events"]["expired_by_policy"] == 0 and counts["evidence"]["expired_by_policy"] == 0
