"""Research & Technology Registry API (§45, §47; §50 "VIEWER memanggil write endpoint -> Ditolak",
"Pengguna hanya meminta compare/review URL -> Tidak install, activate ... atau kirim data proyek")."""

from __future__ import annotations

import httpx
import respx

from aeos.core.enums import Domain
from aeos.research import service as research
from tests.conftest import VIEWER_PW, AuthedClient, create_admin, create_viewer


def _seed(db):
    research.seed_registry(db, platform_repo_slug="fadhlillah-dli/dly-aeos")
    db.commit()


def test_list_filters_and_detail(personal_client: AuthedClient, db):
    _seed(db)
    body = personal_client.get("/api/research", params={"kind": "reference"}).json()
    assert len(body["items"]) == 64 and body["mode"] == "DESIGN" and body["checked_at"] is None
    assert "bukan bukti instalasi" in body["evidence_limit"]
    cand = personal_client.get("/api/research", params={"kind": "candidate", "lifecycle": "REFERENCE_ONLY"}).json()
    assert cand["items"] and all(i["lifecycle"] == "REFERENCE_ONLY" for i in cand["items"])
    one = cand["items"][0]
    detail = personal_client.get(f"/api/research/{one['id']}").json()
    assert detail["placement"] and "history" in detail
    assert personal_client.get("/api/research/nope").status_code == 404


def test_add_by_url_and_duplicate(personal_client: AuthedClient, db):
    with respx.mock(assert_all_called=False) as m:
        r = personal_client.post("/api/research", json={"url": "https://github.com/Foo/Bar"})
        assert r.status_code == 200, r.text
        assert r.json()["entry"]["lifecycle"] == "DISCOVERED" and not r.json()["duplicate"]
        r2 = personal_client.post("/api/research", json={"url": "github.com/foo/bar.git"})
        assert r2.json()["duplicate"] is True
        assert not [c for c in m.calls if c.request.url.host != "testserver"]


def test_check_upstream_endpoint(personal_client: AuthedClient, db):
    _seed(db)
    oc = next(i for i in personal_client.get("/api/research", params={"q": "anomalyco"}).json()["items"]
              if i["entry_key"] == "tool:opencode")
    with respx.mock(assert_all_called=False) as m:
        m.route(host="testserver").pass_through()
        m.get("https://api.github.com/repos/anomalyco/opencode").mock(
            return_value=httpx.Response(200, json={"full_name": "anomalyco/opencode", "archived": False, "fork": False}))
        m.get("https://api.github.com/repos/anomalyco/opencode/releases/latest").mock(
            return_value=httpx.Response(200, json={"tag_name": "v9"}))
        r = personal_client.post(f"/api/research/{oc['id']}/check-upstream")
    assert r.status_code == 200, r.text
    assert r.json()["check_status"] == "OK" and r.json()["latest_tag"] == "v9"


def test_viewer_cannot_add_or_check(client: AuthedClient, db):
    create_admin(db)
    create_viewer(db)
    _seed(db)
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    assert client.post("/api/research", json={"url": "https://github.com/a/b"}).status_code == 403
    first = client.get("/api/research").json()["items"][0]
    assert client.post(f"/api/research/{first['id']}/check-upstream").status_code == 403


def test_office_session_check_denied(admin_client: AuthedClient, db):
    _seed(db)
    admin_client.select_domain(Domain.OFFICE)
    oc = next(i for i in admin_client.get("/api/research", params={"q": "anomalyco"}).json()["items"]
              if i["entry_key"] == "tool:opencode")
    r = admin_client.post(f"/api/research/{oc['id']}/check-upstream")
    assert r.status_code == 403 and r.json()["error"]["code"] == "OFFICE_RESEARCH_DENIED"
