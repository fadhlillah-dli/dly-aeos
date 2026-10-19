"""Audit trail integrity (master §13 AuditEvent actor/run/target, §21 workers may not change final
audit/evidence)."""

from __future__ import annotations

import uuid

from aeos.audit import service as audit
from aeos.verification.scope import check_scope
from tests.conftest import AuthedClient


def test_audit_event_records_the_run_it_belongs_to(db, admin_client: AuthedClient):
    run_id = str(uuid.uuid4())
    ev = audit.record(db, actor_type="worker", actor_id="w1", action="worker.result", target_type="task",
                      target_id="t1", run_id=run_id)
    lifted = audit.record(db, actor_type="service", actor_id="verifier", action="verify.done",
                          details={"run_id": run_id, "status": "PASS"})
    db.commit()
    assert ev.run_id == run_id and lifted.run_id == run_id  # run ids are never PII-mangled
    assert {e.seq for e in audit.list_events(db, run_id=run_id)} == {ev.seq, lifted.seq}
    body = admin_client.get(f"/api/audit?run_id={run_id}").json()
    assert {i["run_id"] for i in body["items"]} == {run_id} and body["checked_at"] and body["mode"] == "LIVE"


def test_audit_and_event_code_changes_are_high_impact_and_security_reviewed():
    """A self-build patch to the audit trail or event stream gets the high-impact test scope and a separate
    security review (§21, §31); quarantine (protected paths) stays the owner's AGENTS.md decision."""
    r = check_scope(["backend/aeos/audit/service.py", "backend/aeos/events/service.py"], allowed=["**"],
                    forbidden=[], protected=[]).to_dict()
    assert r["impact"] == "HIGH"
    assert set(r["high_impact_paths"]) == {"backend/aeos/audit/service.py", "backend/aeos/events/service.py"}
    assert set(r["security_sensitive_paths"]) == {"backend/aeos/audit/service.py", "backend/aeos/events/service.py"}
