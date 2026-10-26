"""Evidence for ASVS 4.0.3 controls selected for the control plane that had no test yet (master §46 "test and trace per
relevant control"; contracts/security/asvs-selection.yaml). Each test names the control ids it proves."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.testclient import TestClient

from aeos.api.security import CSP
from tests.conftest import AuthedClient, make_app


def test_security_headers_are_on_every_response(client: AuthedClient):
    """V14.4.3 CSP, V14.4.4 nosniff, V14.4.6 Referrer-Policy, V14.4.7 no third-party framing, V8.2.1 no-store on the
    API, V14.4.1 a Content-Type on every response - on authenticated, refused and public responses alike."""
    for url in ("/api/health", "/api/auth/session", "/api/audit"):
        r = client.client.get(url)
        h = r.headers
        assert h["content-security-policy"] == CSP and "frame-ancestors 'none'" in CSP and "object-src 'none'" in CSP
        assert h["x-content-type-options"] == "nosniff", url
        assert h["referrer-policy"] == "no-referrer", url
        assert h["x-frame-options"] == "DENY", url
        assert h["cache-control"] == "no-store", url
        assert h["content-type"].startswith("application/json"), url
        # V10.2.2: no access to privacy-related browser features
        assert h["permissions-policy"] == "camera=(), microphone=(), geolocation=()", url


def test_cors_is_not_opened_to_a_foreign_or_null_origin(client: AuthedClient):
    """V14.5.3: no Access-Control-Allow-Origin for a foreign or "null" origin (no wildcard CORS); V13.2.3 / V4.2.2:
    a cookie-authenticated write from a foreign origin is refused before it reaches the route; V14.5.2: an allowed
    Origin header never authenticates anything on its own."""
    for origin in ("https://evil.example", "null"):
        r = client.client.get("/api/health", headers={"Origin": origin})
        assert "access-control-allow-origin" not in {k.lower() for k in r.headers}, origin
        w = client.client.post("/api/auth/logout", headers={"Origin": origin})
        assert w.status_code == 403 and w.json()["error"]["code"] == "ORIGIN_NOT_ALLOWED", origin
    assert client.client.get("/api/audit", headers={"Origin": "http://testserver"}).status_code == 401


def test_session_tokens_are_fresh_per_login_and_long(client: AuthedClient, db):
    """V3.2.1 a new token on every authentication; V3.2.2 at least 64 bits of entropy (256-bit token_urlsafe);
    V3.1.1 the token travels in an HttpOnly cookie, never in the URL or the JSON body."""
    from tests.conftest import ADMIN_PW, create_admin

    create_admin(db)
    tokens = []
    for _ in range(2):
        r = client.client.post("/api/auth/login", json={"username": "owner", "password": ADMIN_PW})
        assert r.status_code == 200
        token = client.client.cookies.get("aeos_session")
        assert token and token not in r.text and token not in str(r.url)
        tokens.append(token)
    assert tokens[0] != tokens[1] and all(len(t) >= 43 for t in tokens)


def test_the_web_tier_serves_only_the_built_ui(settings, tmp_path, monkeypatch):
    """V4.3.2 / V12.5.1: the SPA handler serves files from the built UI directory only - no directory listing, no
    repository metadata, nothing outside it by traversal; any other path gets the UI's index.html."""
    from aeos.api import app as app_mod

    root = tmp_path / "web"
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>AEOS UI</title>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\nrepositoryformatversion = 0\n", encoding="utf-8")
    (root / "secret.txt").write_text("OUTSIDE-THE-UI", encoding="utf-8")
    monkeypatch.setattr(app_mod, "_frontend_dist", lambda: dist)
    with TestClient(make_app(settings), base_url="http://testserver") as c:
        assert c.get("/assets/app.js").text == "console.log(1)"
        for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt", "/../.git/config", "/assets/",
                     "/.git/config"):
            r = c.get(path)
            assert "OUTSIDE-THE-UI" not in r.text and "[core]" not in r.text and "Index of" not in r.text, path


def test_the_asvs_trace_is_readable_by_every_role_and_never_claims_a_pass(client: AuthedClient, db):
    """§46 trace view (review W6-130): every ASVS 4.0.3 requirement with its disposition, readable by a VIEWER,
    TESTED only with named tests, the per-project level UNDECIDED (owner decision), never a pass claim."""
    from tests.conftest import VIEWER_PW, create_viewer

    assert client.client.get("/api/verification/asvs").status_code == 401
    create_viewer(db)
    client.login("viewer", VIEWER_PW)
    r = client.get("/api/verification/asvs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["asvs_version"] == body["catalog_version"] == "4.0.3" and body["application"] == "dly-aeos-control-plane"
    assert body["source"] and body["generated_at"] and body["checked_at"] is None
    assert len(body["items"]) == 286 and [c["chapter"] for c in body["chapters"]] == [f"V{n}" for n in range(1, 15)]
    assert sum(body["totals"].values()) == 286 and body["totals"]["deleted"] == 8
    assert all(c["unlisted"] == 0 for c in body["chapters"])  # complete: nothing silently dropped
    statuses = {i["status"] for i in body["items"]}
    assert statuses <= {"TESTED", "NOT_PROVEN", "NOT_SELECTED", "DELETED"} and "PASS" not in statuses
    assert all(i["tests"] for i in body["items"] if i["status"] == "TESTED")
    assert all(i.get("reason") for i in body["items"] if i["status"] in ("NOT_SELECTED", "DELETED"))
    assert body["per_project"]["status"] == "UNDECIDED"
    v1443 = next(i for i in body["items"] if i["id"] == "V14.4.3")
    assert v1443["status"] == "TESTED" and "tests/unit/test_asvs_controls.py" in v1443["tests"][0]


def test_an_unexpected_error_returns_a_generic_message(settings):
    """V7.4.1 / V7.4.3: an unhandled exception ends in the framework's last-resort handler - a generic 500 without the
    exception text or a stack trace (V14.3.2: no debug mode)."""
    app = make_app(settings)
    boom = APIRouter()

    @boom.get("/api/zz-asvs-boom")
    def _boom() -> dict[str, str]:
        raise RuntimeError("internal detail C:/Users/owner/secret-path")

    app.include_router(boom)
    app.router.routes.insert(0, app.router.routes.pop())  # ahead of the API catch-all (404 envelope)
    with TestClient(app, base_url="http://testserver", raise_server_exceptions=False) as c:
        r = c.get("/api/zz-asvs-boom")
    assert r.status_code == 500
    assert "internal detail" not in r.text and "Traceback" not in r.text and "secret-path" not in r.text
    assert app.debug is False
