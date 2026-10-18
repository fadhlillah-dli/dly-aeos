"""Inventory / System Map API: RBAC, CSRF, domain scope, refresh without install (§35, §38, §50).

§50 rows covered:
* "VIEWER memanggil write endpoint langsung" -> 403 from the backend.
* "Viewer mengakses detail/search/export inventory Office" -> cross-domain detail is 404, counts scoped.
* "Pengguna hanya meminta compare/review URL" / refresh -> read-only probes, no install, audit trail.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from aeos.core.enums import Domain
from aeos.inventory.probes import set_runner_override
from aeos.inventory.seed import run as seed_run
from aeos.models.audit import AuditEvent
from tests.conftest import VIEWER_PW, create_viewer
from tests.unit.test_inventory_fakes import base_runner


@pytest.fixture
def runner():
    r = base_runner()
    set_runner_override(r)
    yield r
    set_runner_override(None)


@pytest.fixture
def seeded_personal(personal_client, db):
    seed_run(db)
    db.commit()
    return personal_client


def test_domain_must_be_selected(admin_client, db):
    seed_run(db)
    db.commit()
    r = admin_client.get("/api/inventory/components")
    assert r.status_code == 409 and r.json()["error"]["code"] == "DOMAIN_NOT_SELECTED"


def test_list_detail_relations_map_and_coverage(seeded_personal):
    r = seeded_personal.get("/api/inventory/components", params={"page_size": 200})
    assert r.status_code == 200
    body = r.json()
    ids = {i["component_id"] for i in body["items"]}
    assert "claude-code" in ids and "jira" not in ids
    assert body["source"] and "checked_at" in body and body["mode"] == "DESIGN"
    d = seeded_personal.get("/api/inventory/components/claude-code").json()
    assert d["identity"]["friendly_name"].startswith("Claude Code")
    rel = seeded_personal.get("/api/inventory/components/claude-native-bridge/relations").json()
    assert any(x["target"] == "claude-code" for x in rel["outgoing"])
    for lens in ("DESIGN", "CONFIG", "OBSERVED"):
        m = seeded_personal.get("/api/inventory/map", params={"lens": lens})
        assert m.status_code == 200 and m.json()["lens"] == lens
    assert seeded_personal.get("/api/inventory/map", params={"lens": "LIVE"}).status_code == 422
    cov = seeded_personal.get("/api/inventory/coverage").json()
    assert any(g["code"] == "NO_RECONCILE" for g in cov["gaps"])
    br = seeded_personal.get("/api/inventory/blast-radius/sqlite-metadata-store").json()
    assert br["summary"]["affected"] > 0
    p = seeded_personal.get("/api/inventory/path", params={"from": "control-center-frontend", "to": "openbao"}).json()
    assert p["found"] is True


def test_cross_domain_detail_is_not_found(seeded_personal):
    assert seeded_personal.get("/api/inventory/components/jira").status_code == 404
    assert seeded_personal.get("/api/inventory/components/confluence/relations").status_code == 404
    assert seeded_personal.get("/api/inventory/blast-radius/bitbucket").status_code == 404
    seeded_personal.select_domain(Domain.OFFICE)
    assert seeded_personal.get("/api/inventory/components/github").status_code == 404
    assert seeded_personal.get("/api/inventory/components/jira").status_code == 200


def test_refresh_is_admin_only_csrf_protected_and_read_only(seeded_personal, db, runner):
    r = seeded_personal.client.post("/api/inventory/refresh", json={})  # no CSRF header
    assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_FAILED"
    r = seeded_personal.post("/api/inventory/refresh", json={"component_id": "git"})
    assert r.status_code == 200, r.text
    run = r.json()["run"]
    assert run["scope"] == {"component_id": "git"} and run["status"] in ("COMPLETED", "PARTIAL")
    # read-only probes only: version/status queries, no install/uninstall/write subcommands
    for call in runner.calls:
        assert not any(a in ("install", "uninstall", "push", "commit", "reset", "checkout") for a in call["argv"])
    audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "inventory.refresh"))
    assert audit is not None and audit.result == "SUCCESS"
    assert seeded_personal.post("/api/inventory/refresh", json={"component_id": "jira"}).status_code == 404
    full = seeded_personal.post("/api/inventory/refresh", json={})
    assert full.status_code == 200 and full.json()["run"]["scope"]["set"] == "core"
    drift = seeded_personal.get("/api/inventory/drift").json()
    assert drift["items"] and all(i["domain_scope"] in ("PLATFORM", "PERSONAL") for i in drift["items"])


def test_viewer_reads_but_cannot_write(client, db, runner):
    from tests.conftest import create_admin

    create_admin(db)
    create_viewer(db)
    seed_run(db)
    db.commit()
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    assert client.get("/api/inventory/components").status_code == 200
    for path, body in (("/api/inventory/refresh", {}), ("/api/inventory/snapshots", {"label": "x"})):
        r = client.post(path, json=body)
        assert r.status_code == 403 and r.json()["error"]["code"] == "READ_ONLY_ROLE"


def test_snapshot_endpoints_and_diff_route(seeded_personal):
    a = seeded_personal.post("/api/inventory/snapshots", json={"label": "A"})
    assert a.status_code == 200, a.text
    b = seeded_personal.post("/api/inventory/snapshots", json={"label": "B"}).json()
    lst = seeded_personal.get("/api/inventory/snapshots").json()
    assert {s["label"] for s in lst["items"]} == {"A", "B"}
    diff = seeded_personal.get("/api/inventory/snapshots/diff", params={"a": a.json()["id"], "b": b["id"]})
    assert diff.status_code == 200 and diff.json()["same_hash"] is True
    one = seeded_personal.get(f"/api/inventory/snapshots/{b['id']}").json()
    assert one["refs"]["scope"]["domain"] == "PERSONAL"
    seeded_personal.select_domain(Domain.OFFICE)
    assert seeded_personal.get(f"/api/inventory/snapshots/{b['id']}").status_code == 404
    assert seeded_personal.get("/api/inventory/snapshots").json()["items"] == []
