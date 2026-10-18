"""Dashboard Readiness API (§08 layer 2): RBAC/CSRF, events/audit, domain scope, startup pass.

§50 rows covered: "VIEWER memanggil write endpoint langsung" (403), "Tool opsional atau Office belum
ready" (dashboard data still available; Office checks hidden from Personal sessions).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from aeos.core.enums import Domain
from aeos.doctor.checks import host as host_mod
from aeos.inventory.probes import set_runner_override
from aeos.models.audit import AuditEvent, Event
from aeos.models.doctor_ext import DoctorRun
from tests.conftest import VIEWER_PW, create_admin, create_viewer
from tests.unit.test_inventory_fakes import base_runner


@pytest.fixture
def fake_host(monkeypatch):
    r = base_runner()
    set_runner_override(r)
    monkeypatch.setattr(host_mod, "tcp_in_use", lambda h, p, timeout=1.0: False)
    monkeypatch.setattr(host_mod, "can_bind", lambda h, p: True)
    yield r
    set_runner_override(None)


def test_checks_list_before_any_run_is_unknown_not_green(personal_client, fake_host):
    body = personal_client.get("/api/doctor/checks").json()
    assert body["items"] and all(i["never_run"] for i in body["items"])
    assert all(i["effective_status"] == "UNKNOWN" for i in body["items"])
    assert body["last_run"] is None and body["source"] and body["mode"] == "LIVE"
    ids = {i["check_id"] for i in body["items"]}
    assert "adapter.office_jira" not in ids and "adapter.github_token" in ids  # domain scope
    caps = personal_client.get("/api/doctor/capabilities").json()
    statuses = {c["capability"]: c["status"] for c in caps["items"]}
    assert len(statuses) == 9 and "READY" not in statuses.values()
    assert statuses["CAN_PLAN_OFFICE"] == "NOT_APPLICABLE"
    assert any(h["label"] == "Merge ke main" for h in caps["human_only"])


def test_admin_run_records_audit_events_and_history(personal_client, db, fake_host):
    r = personal_client.client.post("/api/doctor/run", json={})
    assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_FAILED"
    r = personal_client.post("/api/doctor/run", json={"group": "HOST"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert {i["group"] for i in body["items"]} == {"HOST"}
    assert body["run"]["counts"] and body["capabilities"]
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "doctor.run")) is not None
    assert db.scalar(select(Event).where(Event.kind == "doctor.run.completed")) is not None
    assert db.scalar(select(Event).where(Event.kind == "doctor.check")) is not None
    hist = personal_client.get("/api/doctor/checks/host.platform/history").json()
    assert hist["items"] and hist["check"]["group"] == "HOST"
    assert personal_client.get("/api/doctor/checks/nope/history").status_code == 404
    assert personal_client.post("/api/doctor/run", json={"group": "NOPE"}).status_code == 422
    full = personal_client.post("/api/doctor/run", json={}).json()
    assert {s["check_id"] for s in full["skipped"]} >= {"containment.report"}  # functional only
    listed = personal_client.get("/api/doctor/checks").json()
    platform = next(i for i in listed["items"] if i["check_id"] == "host.platform")
    assert platform["never_run"] is False and platform["component_link"] is None
    claude = next(i for i in listed["items"] if i["check_id"] == "claude.binary")
    assert claude["component_link"] == "/tools/claude-code" and claude["remediation"]


def test_viewer_can_read_but_not_run(client, db, fake_host):
    create_admin(db)
    create_viewer(db)
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    assert client.get("/api/doctor/checks").status_code == 200
    assert client.get("/api/doctor/capabilities").status_code == 200
    r = client.post("/api/doctor/run", json={})
    assert r.status_code == 403 and r.json()["error"]["code"] == "READ_ONLY_ROLE"


def test_office_session_sees_office_checks_only(personal_client, fake_host):
    personal_client.select_domain(Domain.OFFICE)
    ids = {i["check_id"] for i in personal_client.get("/api/doctor/checks").json()["items"]}
    assert "adapter.office_jira" in ids and "adapter.github_token" not in ids
    caps = {c["capability"]: c for c in personal_client.get("/api/doctor/capabilities").json()["items"]}
    assert caps["CAN_PLAN_PERSONAL"]["status"] == "NOT_APPLICABLE"
    assert caps["CAN_USE_OFFICE_INTEGRATIONS"]["status"] == "BLOCKED"
    assert "OFFICE default deny" in caps["CAN_USE_OFFICE_INTEGRATIONS"]["reasons"][0]


def test_startup_pass_runs_once_in_background(settings, db, fake_host, monkeypatch):
    from aeos.doctor import worker

    monkeypatch.setattr(worker, "STARTUP_DELAY_SECONDS", 0.0)
    worker.start(settings)
    worker.stop()
    db.expire_all()
    run = db.scalar(select(DoctorRun).where(DoctorRun.trigger == "startup"))
    assert run is not None and run.functional is False
