"""Feature flags and export snapshots (master §35 Control Center extras, §41, §50).

§50 rows: "Trace berisi secret/PII -> Redaction/policy menolak atau membatasi ... ekspor";
"Snapshot export dibuka setelah runtime berubah -> scope/time/hash jelas; tidak disebut live";
"Viewer mengakses detail/search/export inventory Office -> backend scope diterapkan";
"Config/model/provider berubah setelah doctor -> check terkait kedaluwarsa" (flag change invalidates).
"""

from __future__ import annotations

import json

from sqlalchemy import select

from aeos.core.enums import Domain
from aeos.models.audit import AuditEvent, Event
from aeos.models.decisions import Decision
from aeos.models.evidence import EvidenceArtifact
from aeos.models.inventory import ComponentObservation, ComponentReference
from aeos.models.operations_ext import OpsExportSnapshot
from aeos.models.portfolio import Project
from tests.conftest import VIEWER_PW, create_admin, create_viewer
from tests.unit.test_operations_fakes import fake_doctor

SECRET = "ghp_" + "Z" * 36


def test_flags_list_defaults_and_set_is_audited(personal_client, db, monkeypatch):
    invalidations = fake_doctor(monkeypatch, {})
    body = personal_client.get("/api/ops/flags").json()
    flags = {f["key"]: f for f in body["items"]}
    assert flags["ops.sli_loop_enabled"]["value"] is True and flags["ops.sli_loop_enabled"]["is_default"]
    r = personal_client.put("/api/ops/flags/ops.sli_loop_enabled", json={"value": False, "reason": "maintenance"})
    assert r.status_code == 200, r.text
    assert r.json()["readiness_invalidation"]["status"] == "DONE"
    assert invalidations == ["flag:ops.sli_loop_enabled"]
    audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "ops.flag.set"))
    assert audit.details["previous"] is True and audit.details["new"] is False
    assert db.scalar(select(Event).where(Event.kind == "ops.flag.changed")) is not None
    after = {f["key"]: f for f in personal_client.get("/api/ops/flags").json()["items"]}
    assert after["ops.sli_loop_enabled"]["value"] is False and after["ops.sli_loop_enabled"]["version"] == 1


def test_flag_validation_and_unknown_keys(personal_client):
    assert personal_client.put("/api/ops/flags/does.not.exist", json={"value": True}).status_code == 404
    assert personal_client.put("/api/ops/flags/ops.sli_loop_enabled", json={"value": "yes"}).status_code == 422
    assert personal_client.put("/api/ops/flags/ops.sli_loop_interval_seconds", json={"value": 5}).status_code == 422


def test_flag_change_without_doctor_reports_unavailable(personal_client):
    r = personal_client.put("/api/ops/flags/ui.advanced_view_default", json={"value": True})
    assert r.status_code == 200
    assert r.json()["readiness_invalidation"]["status"] == "UNAVAILABLE"


def test_viewer_cannot_change_flags(client, db):
    create_admin(db)
    create_viewer(db)
    client.login("viewer", VIEWER_PW)
    r = client.put("/api/ops/flags/ops.sli_loop_enabled", json={"value": False})
    assert r.status_code == 403 and r.json()["error"]["code"] == "READ_ONLY_ROLE"


def _seed_two_domains(db):
    p = Project(project_key="alpha", name="Alpha", domain="PERSONAL")
    o = Project(project_key="omega", name="Omega Office", domain="OFFICE")
    db.add_all([p, o])
    db.flush()
    db.add(Decision(kind="APPROVAL", actor_user_id="u", target_type="plan_version", target_id="pv-p",
                    project_id=p.id, domain="PERSONAL", target_hash="sha256:p"))
    db.add(Decision(kind="APPROVAL", actor_user_id="u", target_type="plan_version", target_id="pv-o",
                    project_id=o.id, domain="OFFICE", target_hash="sha256:o"))
    # legacy/trace row written without redaction (bypassing audit.record) -> export must scrub it
    db.add(AuditEvent(actor_type="worker", actor_id="w", action="trace.tool_output", result="SUCCESS",
                      project_id=p.id, domain="PERSONAL", details={"stdout": f"export GH={SECRET}"}))
    db.add(AuditEvent(actor_type="user", actor_id="u", action="office.thing", result="SUCCESS",
                      project_id=o.id, domain="OFFICE", details={"x": "office-only"}))
    db.add(EvidenceArtifact(kind="LOG", origin="verifier", collector_identity="v", content_hash="sha256:e1",
                            project_id=p.id, domain="PERSONAL", inline={"line": f"token {SECRET}"}))
    db.add(EvidenceArtifact(kind="LOG", origin="verifier", collector_identity="v", content_hash="sha256:e2",
                            project_id=o.id, domain="OFFICE"))
    db.add(ComponentReference(component_id="claude-code", kind="tool", name="Claude Code", scope={}))
    db.add(ComponentReference(component_id="jira", kind="integration", name="Jira", scope={"domains": ["OFFICE"]}))
    db.add(ComponentObservation(component_id="claude-code", dimension="installation", status="INSTALLED",
                                observer="doctor"))
    db.commit()
    return p, o


def test_export_is_scoped_redacted_and_registered(personal_client, db):
    _seed_two_domains(db)
    for kind in ("audit", "decisions", "evidence", "inventory", "architecture"):
        r = personal_client.get(f"/api/ops/export?kind={kind}")
        assert r.status_code == 200, (kind, r.text)
        body = r.json()
        text = json.dumps(body)
        assert body["mode"] == "SNAPSHOT" and "bukan status live" in body["notice"]
        assert body["content_hash"].startswith("sha256:") and body["snapshot_id"] and body["generated_at"]
        assert body["scope"]["domain"] == "PERSONAL"
        assert SECRET not in text, kind
        assert "office-only" not in text and "pv-o" not in text and "Jira" not in text, kind
        if kind in ("audit", "evidence"):
            assert body["redaction"]["fields_redacted"] >= 1, kind
    inv = personal_client.get("/api/ops/export?kind=inventory").json()
    comp = next(i for i in inv["items"] if i["component_id"] == "claude-code")
    assert comp["observations"][0]["status"] == "INSTALLED"
    assert db.scalar(select(OpsExportSnapshot).where(OpsExportSnapshot.kind == "audit")) is not None
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "ops.export")) is not None
    listed = personal_client.get("/api/ops/exports").json()["items"]
    assert len(listed) >= 6 and all(s["content_hash"] for s in listed)


def test_export_download_and_cross_domain_project(personal_client, db):
    _p, o = _seed_two_domains(db)
    r = personal_client.get("/api/ops/export?kind=decisions&download=true")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert personal_client.get(f"/api/ops/export?kind=audit&project_id={o.id}").status_code == 404
    assert personal_client.get("/api/ops/export?kind=secrets").status_code == 422


def test_office_export_sees_office_only(admin_client, db):
    _seed_two_domains(db)
    admin_client.select_domain(Domain.OFFICE)
    text = json.dumps(admin_client.get("/api/ops/export?kind=decisions").json())
    assert "pv-o" in text and "pv-p" not in text
