"""Emergency stop / incident containment (master §33 incident response, §35 operator controls).

Each step reports its own result; a missing module makes its step FAILED with a reason and the stop
is never reported COMPLETED. §50 rows: "Cancel saat ada child process -> capability dicabut dan
seluruh proses dihentikan; status/evidence konsisten", "VIEWER memanggil write endpoint -> ditolak",
"Dashboard Personal mencoba run Office -> backend menolak" (Office run ids hidden from Personal).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from aeos.core.enums import Domain
from aeos.core.errors import Forbidden, ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.models.audit import AuditEvent
from aeos.models.evidence import EvidenceArtifact
from aeos.models.operations_ext import OpsAlert
from aeos.models.routing import CapabilityGrant
from aeos.models.workforce import WorkerRun
from aeos.operations import emergency
from tests.conftest import VIEWER_PW, create_admin, create_viewer
from tests.unit.test_operations_fakes import block_module, fake_broker, fake_verification, fake_workforce


def _active_state(db):
    runs = [WorkerRun(project_id="p1", domain="PERSONAL", execution_status="RUNNING"),
            WorkerRun(project_id="p2", domain="PERSONAL", execution_status="LEASED"),
            WorkerRun(project_id="p3", domain="OFFICE", execution_status="RUNNING"),
            WorkerRun(project_id="p1", domain="PERSONAL", execution_status="DONE")]
    db.add_all(runs)
    db.flush()
    for i, r in enumerate(runs[:3]):
        db.add(CapabilityGrant(token_hash=f"h{i}", run_id=r.id, project_id=r.project_id, domain=r.domain,
                               kind="MODEL_PROXY", expires_at=utcnow() + timedelta(hours=1)))
    db.commit()
    return runs


def test_emergency_stop_all_steps_succeed(db, monkeypatch):
    fw = fake_workforce(monkeypatch)
    broker_calls = fake_broker(monkeypatch)
    evidence = fake_verification(monkeypatch)
    runs = _active_state(db)
    report = emergency.execute(db, actor_type="user", actor_id="owner", reason="runaway worker spending",
                               incident_owner="owner", blast_radius="worker routes", visible_domain="PERSONAL")
    db.commit()
    steps = {s["step"]: s for s in report["steps"]}
    assert report["status"] == "COMPLETED", report
    assert [s["step"] for s in report["steps"]] == ["pause_scheduling", "revoke_capabilities", "cancel_runs",
                                                     "preserve_evidence", "audit"]
    assert fw.paused is True
    assert broker_calls[0]["all_active"] is True and steps["revoke_capabilities"]["remaining_active"] == 0
    assert set(fw.cancelled) == {runs[0].id, runs[1].id, runs[2].id}
    assert steps["cancel_runs"]["remaining_active"] == 0
    assert {r["run_id"] for r in steps["cancel_runs"]["runs"]} == {runs[0].id, runs[1].id}
    assert steps["cancel_runs"]["hidden_other_domain"] == 1  # Office run id not shown to Personal
    assert evidence and evidence[0].kind == "INCIDENT_CONTAINMENT" and evidence[0].trusted
    assert db.get(WorkerRun, runs[3].id).execution_status == "DONE"  # finished runs untouched
    audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "ops.emergency_stop"))
    assert audit is not None and audit.result == "SUCCESS"
    assert db.scalar(select(OpsAlert).where(OpsAlert.kind == "emergency_stop")).severity == "CRITICAL"
    assert emergency.last_emergency(db)["incident_id"] == report["incident_id"]


def test_emergency_stop_missing_modules_fail_each_step(db, monkeypatch):
    for name in ("aeos.workforce.scheduler", "aeos.broker.grants", "aeos.verification.collector"):
        block_module(monkeypatch, name)
    _active_state(db)
    report = emergency.execute(db, actor_type="cli", actor_id="op", reason="containment drill")
    db.commit()
    steps = {s["step"]: s for s in report["steps"]}
    assert report["status"] == "PARTIAL"  # audit still succeeds; never COMPLETED
    for name in ("pause_scheduling", "revoke_capabilities", "cancel_runs", "preserve_evidence"):
        assert steps[name]["status"] == "FAILED", name
        assert "unavailable" in steps[name]["reason"]
    assert steps["audit"]["status"] == "SUCCEEDED"
    assert db.scalar(select(EvidenceArtifact)) is None
    remaining = db.scalars(select(WorkerRun).where(WorkerRun.execution_status.in_(["RUNNING", "LEASED"]))).all()
    assert len(remaining) == 3  # honest: nothing was cancelled
    audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "ops.emergency_stop"))
    assert audit.result == "FAILED"


def test_partial_cancel_reported(db, monkeypatch):
    fw = fake_workforce(monkeypatch)
    fake_broker(monkeypatch)
    fake_verification(monkeypatch)
    runs = _active_state(db)
    fw.fail_runs.add(runs[1].id)
    report = emergency.execute(db, actor_type="cli", actor_id="op", reason="partial kill check")
    cancel = next(s for s in report["steps"] if s["step"] == "cancel_runs")
    assert cancel["status"] == "PARTIAL" and cancel["failed"] == 1 and cancel["remaining_active"] == 1
    assert report["status"] == "PARTIAL"


def test_reason_required(db):
    with pytest.raises(ValidationFailed):
        emergency.execute(db, actor_type="cli", actor_id="op", reason="x")


def test_token_is_single_use_bound_and_expiring(db):
    tok = emergency.prepare(db, user_id="u1")["token"]
    with pytest.raises(Forbidden):
        emergency.consume_token(db, user_id="u2", token=tok)
    emergency.consume_token(db, user_id="u1", token=tok)
    with pytest.raises(Forbidden):
        emergency.consume_token(db, user_id="u1", token=tok)
    tok2 = emergency.prepare(db, user_id="u1")["token"]
    row = db.get(emergency.SystemSetting, emergency.PENDING_KEY)
    row.value = {**row.value, "expires_at": (utcnow() - timedelta(seconds=1)).isoformat()}
    db.flush()
    with pytest.raises(Forbidden):
        emergency.consume_token(db, user_id="u1", token=tok2)


def test_api_emergency_stop_flow(client, db, monkeypatch):
    fake_workforce(monkeypatch)
    fake_broker(monkeypatch)
    fake_verification(monkeypatch)
    create_admin(db)
    create_viewer(db)
    client.login("viewer", VIEWER_PW)
    assert client.post("/api/ops/emergency-stop/prepare").status_code == 403
    from tests.conftest import ADMIN_PW

    client.login("owner", ADMIN_PW)
    client.select_domain(Domain.PERSONAL)
    r = client.post("/api/ops/emergency-stop", json={"token": "x" * 20, "reason": "no prepare"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
    token = client.post("/api/ops/emergency-stop/prepare").json()["token"]
    r = client.post("/api/ops/emergency-stop", json={"token": token, "reason": "dashboard drill"})
    assert r.status_code == 200 and r.json()["status"] == "COMPLETED", r.text
    r = client.post("/api/ops/emergency-stop", json={"token": token, "reason": "replay"})
    assert r.status_code == 403
    last = client.get("/api/ops/emergency-stop/last").json()
    assert last["last"]["status"] == "COMPLETED" and last["scheduling"]["status"] == "PAUSED"
