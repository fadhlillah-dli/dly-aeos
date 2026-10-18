"""Emergency stop / incident containment (master §33 incident response, §35 operator controls).

Each step reports its own result; a missing module makes its step FAILED with a reason and the stop
is never reported COMPLETED. §50 rows: "Cancel saat ada child process -> capability dicabut dan
seluruh proses dihentikan; status/evidence konsisten", "VIEWER memanggil write endpoint -> ditolak",
"Dashboard Personal mencoba run Office -> backend menolak" (Office run ids hidden from Personal).
"""

from __future__ import annotations

import json
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
    assert set(fw.cancelled) == {runs[0].id, runs[1].id, runs[2].id}  # the stop itself is platform-wide
    assert steps["cancel_runs"]["remaining_active"] == 0
    assert {r["run_id"] for r in steps["cancel_runs"]["runs"]} == {runs[0].id, runs[1].id}
    # Personal view: Office run ids AND Office counts are not shown (§50 scope "termasuk counts")
    assert steps["cancel_runs"]["active_runs"] == 2 and steps["cancel_runs"]["cancelled"] == 2
    assert steps["revoke_capabilities"]["active_before"] == 2
    text = json.dumps(report)
    assert runs[2].id not in text and "by_domain" not in text and "OFFICE" not in text
    assert "hidden_other_domain" not in text and "remaining_total" not in text and "revoked_total" not in text
    assert {e.domain for e in evidence} == {"PERSONAL", "OFFICE"}  # one INCIDENT_CONTAINMENT per partition
    assert all(e.kind == "INCIDENT_CONTAINMENT" and e.trusted for e in evidence)
    office_ev = next(e for e in evidence if e.domain == "OFFICE")
    personal_ev = next(e for e in evidence if e.domain == "PERSONAL")
    assert runs[2].id in json.dumps(office_ev.inline) and runs[2].id not in json.dumps(personal_ev.inline)
    assert db.get(WorkerRun, runs[3].id).execution_status == "DONE"  # finished runs untouched
    audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "ops.emergency_stop"))
    assert audit is not None and audit.result == "SUCCESS"
    assert audit.domain is None and runs[2].id not in json.dumps(audit.details)
    assert all(set(s) <= {"step", "status", "reason"} for s in audit.details["steps"])  # statuses only
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
    cancel = next(s for s in report["steps"] if s["step"] == "cancel_runs")  # full CLI view
    assert cancel["status"] == "PARTIAL" and cancel["remaining_total"] == 1
    assert cancel["by_domain"]["PERSONAL"]["failed"] == 1 and cancel["by_domain"]["OFFICE"]["failed"] == 0
    assert report["status"] == "PARTIAL"


def test_failure_only_in_other_domain_is_not_hidden_but_not_detailed(db, monkeypatch):
    """An Office run that cannot be cancelled keeps the stop PARTIAL in a Personal session, with a
    neutral reason and without Office counts/ids; the Office view names the failure."""
    fw = fake_workforce(monkeypatch)
    fake_broker(monkeypatch)
    fake_verification(monkeypatch)
    runs = _active_state(db)
    fw.fail_runs.add(runs[2].id)  # the OFFICE run
    full = emergency.execute(db, actor_type="cli", actor_id="op", reason="office kill fails")
    personal = emergency.scope_report(full, "PERSONAL")
    office = emergency.scope_report(full, "OFFICE")
    p_cancel = next(s for s in personal["steps"] if s["step"] == "cancel_runs")
    o_cancel = next(s for s in office["steps"] if s["step"] == "cancel_runs")
    assert personal["status"] == "PARTIAL" and p_cancel["status"] == "PARTIAL"
    assert p_cancel["remaining_active"] == 0 and "domain lain" in p_cancel["reason"]
    assert runs[2].id not in json.dumps(personal)
    assert o_cancel["remaining_active"] == 1 and o_cancel["failed"] == 1
    assert "masih aktif" in o_cancel["reason"] and o_cancel["runs"][0]["run_id"] == runs[2].id


def test_status_only_view_without_domain(db, monkeypatch):
    fake_workforce(monkeypatch)
    fake_broker(monkeypatch)
    fake_verification(monkeypatch)
    runs = _active_state(db)
    report = emergency.execute(db, actor_type="user", actor_id="u", reason="no domain chosen",
                               visible_domain=emergency.STATUS_ONLY)
    assert report["status"] == "COMPLETED" and report["scope"] == "NO_DOMAIN_SELECTED"
    assert all(set(s) <= {"step", "status", "reason"} for s in report["steps"])
    assert not any(r.id in json.dumps(report) for r in runs)


def test_reason_is_redacted(db, monkeypatch):
    for name in ("aeos.workforce.scheduler", "aeos.broker.grants", "aeos.verification.collector"):
        block_module(monkeypatch, name)
    secret = "ghp_" + "S" * 36
    report = emergency.execute(db, actor_type="cli", actor_id="op", reason=f"leaked token {secret}")
    db.commit()
    assert secret not in json.dumps(report)
    assert secret not in json.dumps(emergency.last_emergency(db))


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


def test_api_report_is_scoped_to_the_session_domain(client, db, monkeypatch):
    """§35/§50: the stop cancels every active run (platform-wide), but a PERSONAL browser session never
    receives Office run ids or Office counts; the last-stop record carries statuses only."""
    fw = fake_workforce(monkeypatch)
    fake_broker(monkeypatch)
    fake_verification(monkeypatch)
    from tests.conftest import ADMIN_PW

    create_admin(db)
    runs = _active_state(db)
    client.login("owner", ADMIN_PW)
    client.select_domain(Domain.PERSONAL)
    token = client.post("/api/ops/emergency-stop/prepare").json()["token"]
    body = client.post("/api/ops/emergency-stop", json={"token": token, "reason": "scoped drill"}).json()
    assert body["status"] == "COMPLETED" and body["scope"] == "PERSONAL"
    assert runs[2].id in fw.cancelled  # the Office run was cancelled too
    text = json.dumps(body)
    assert runs[2].id not in text and "OFFICE" not in text
    cancel = next(s for s in body["steps"] if s["step"] == "cancel_runs")
    assert cancel["cancelled"] == 2 and {r["run_id"] for r in cancel["runs"]} == {runs[0].id, runs[1].id}
    last = client.get("/api/ops/emergency-stop/last").json()["last"]
    assert runs[2].id not in json.dumps(last)
    assert all(set(s) <= {"step", "status", "reason"} for s in last["steps"])
