"""Domain-first (U-05, §04, §35). §50 rows: "Submit prompt tanpa domain" (backend refuses
domain-scoped work without an explicit human domain), "Prompt memerintahkan OFFICE menjadi PERSONAL"
(domain never changes), "Dashboard Personal mencoba run/evidence Office" (backend 404, no leak)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from aeos.events import service as events
from aeos.verification import facade as verification
from tests.acceptance.support import make_records
from tests.conftest import AuthedClient

pytestmark = pytest.mark.acceptance

DOMAIN_SCOPED_READS = ("/api/evidence", "/api/reviews", "/api/acceptance", "/api/verification/summary",
                       "/api/delivery/prs")


def test_domain_scoped_reads_require_explicit_domain(admin_client: AuthedClient):
    session = admin_client.get("/api/auth/session").json()
    assert session["context"]["domain"] is None, "domain is never pre-selected or inferred"
    for url in DOMAIN_SCOPED_READS:
        r = admin_client.get(url)
        assert r.status_code == 409 and r.json()["error"]["code"] == "DOMAIN_NOT_SELECTED", url


def test_invalid_domain_rejected(admin_client: AuthedClient):
    assert admin_client.post("/api/session/context", json={"domain": "HOME"}).status_code == 422
    assert admin_client.post("/api/session/context", json={}).status_code == 422


def test_project_domain_is_immutable(db):
    rec = make_records(db, domain="OFFICE", key="office-immutable")
    with pytest.raises((IntegrityError, OperationalError)):
        db.execute(text("UPDATE projects SET domain='PERSONAL' WHERE id=:i"), {"i": rec.project.id})
        db.commit()
    db.rollback()


def test_personal_session_cannot_see_office_records(personal_client: AuthedClient, db):
    office = make_records(db, domain="OFFICE", key="office-app")
    ev = verification.collect_evidence(db, kind="LOG", origin="collector", collector_identity="acc",
                                       claim="office-only log", content="office data", project_id=office.project.id)
    events.emit(db, source="acc", kind="acc.office", project_id=office.project.id, domain="OFFICE")
    db.commit()
    assert personal_client.get(f"/api/evidence/{ev.id}").status_code == 404
    assert all(i["id"] != ev.id for i in personal_client.get("/api/evidence").json()["items"])
    assert personal_client.post(f"/api/runs/{office.run.id}/verify", json={}).status_code == 404
    assert personal_client.get(f"/api/delivery/gates?task_id={office.task.id}").status_code == 404
    assert personal_client.get(f"/api/acceptance?task_id={office.task.id}").json()["items"] == []
    listed = personal_client.get("/api/events").json()["events"]
    assert all(e["domain"] != "OFFICE" for e in listed)
    r = personal_client.post("/api/session/context", json={"domain": "PERSONAL", "project_id": office.project.id})
    assert r.status_code == 404
