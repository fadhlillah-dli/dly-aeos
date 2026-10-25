"""Trusted screenshot collector (master §29, §10 Documentation & Visuals): the running app at a proven commit,
page text checked for secrets/PII before the image is stored, stored as trusted SCREENSHOT evidence that the
screenshot manifest and Product Home then reference by id. Fail closed on every check."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import respx
from sqlalchemy import select

from aeos.core.errors import Blocked, PolicyDenied, ValidationFailed
from aeos.docs_engine import screenshots as shots
from aeos.docs_engine import service
from aeos.docs_engine import visuals as vis
from aeos.models.evidence import EvidenceArtifact
from aeos.models.portfolio import ProjectIntegration
from tests.unit.test_publisher_support import install_fakes, make_plan, make_project
from tests.unit.test_publisher_support import schema_sample_content as sample

SHA = "0123456789abcdef0123456789abcdef01234567"
BASE = "https://staging.example.org"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def project(settings, db, monkeypatch):
    install_fakes(monkeypatch)
    p = make_project(db)
    db.add(ProjectIntegration(project_id=p.id, domain=p.domain, provider="deployment-target", purpose="staging",
                              root_id=BASE, config={}))
    db.commit()
    return p


class FakeBrowser:
    def __init__(self, pages: dict[str, tuple[str, str]] | None = None):
        self.pages = pages or {}
        self.calls: list[str] = []

    def __call__(self, url: str, viewport: tuple[int, int]) -> shots.PageCapture:
        self.calls.append(url)
        final, text = self.pages.get(url, (url, "Dashboard tugas: 3 terbuka"))
        return shots.PageCapture(final_url=final, text=text, png=PNG, http_status=200)


def _health(commit: str = SHA, status: str = "ok"):
    return httpx.Response(200, json={"status": status, "commit": commit})


def _screens(db):
    return list(db.scalars(select(EvidenceArtifact).where(EvidenceArtifact.kind == "SCREENSHOT")))


def test_capture_stores_trusted_screenshot_evidence_at_the_proven_commit(db, project):
    browser = FakeBrowser()
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/health").mock(return_value=_health())
        out = shots.capture_screenshots(db, project, environment="staging", commit=SHA, routes=["/", "/tasks"],
                                        actor="owner", capture=browser)
    db.commit()
    assert browser.calls == [f"{BASE}/", f"{BASE}/tasks"]
    assert [o["route"] for o in out] == ["/", "/tasks"]
    rows = _screens(db)
    assert len(rows) == 2
    for ev in rows:
        assert ev.trusted and ev.origin == "collector" and ev.collector_identity == "screenshot-collector:staging"
        assert ev.project_id == project.id and ev.domain == project.domain
        assert ev.environment["commit"] == SHA and ev.environment["name"] == "staging"
        assert ev.environment["reported_commit"] == SHA and ev.environment["viewport"] == "1440x900"
        assert ev.evidence_level == "E3"
    refs = [e for e in service.evidence_refs(db, project) if e.kind == "SCREENSHOT"]
    assert {e.id for e in refs} == {o["evidence_id"] for o in out} and all(e.commit == SHA for e in refs)
    manifest = vis.screenshot_manifest(project_id=project.id, commit=None, environment=None, evidence=[
        {"id": e.id, "kind": e.kind, "trusted": True, "origin": e.origin, "claim": e.claim,
         "environment": {"commit": e.commit, "name": e.environment}} for e in refs])
    assert manifest["status"] == "EVIDENCE_REFERENCED" and len(manifest["items"]) == 2


def test_product_home_references_the_captured_screenshots(db, settings, project):
    _plan, version = make_plan(db, project, sample())
    with respx.mock() as mock:
        mock.get(f"{BASE}/health").mock(return_value=_health())
        out = shots.capture_screenshots(db, project, environment="staging", commit=SHA, routes=["/"],
                                        actor="owner", capture=FakeBrowser())
    db.commit()
    result, _d, _s = service.render_version(db, settings, version, persist=False, render_visuals=False)
    assert out[0]["evidence_id"] in (result.by_id()["00-product-home"].body_markdown or "")


@pytest.mark.parametrize("health", [_health(commit="f" * 40), _health(status="degraded"),
                                    httpx.Response(503, json={"status": "down", "commit": SHA})])
def test_nothing_is_captured_unless_the_target_proves_the_commit_healthy(db, project, health):
    browser = FakeBrowser()
    with respx.mock() as mock:
        mock.get(f"{BASE}/health").mock(return_value=health)
        with pytest.raises(Blocked) as exc:
            shots.capture_screenshots(db, project, environment="staging", commit=SHA, routes=["/"], actor="owner",
                                      capture=browser)
    assert exc.value.code == "SCREENSHOT_COMMIT_NOT_PROVEN"
    assert browser.calls == [] and _screens(db) == []


@pytest.mark.parametrize("text", ["token ghp_" + "A" * 36 + " di halaman", "Hubungi +62 812-3456-7890",
                                  "email budi.santoso@example.com"])
def test_a_page_showing_a_secret_or_personal_data_is_refused_and_nothing_is_stored(db, project, text):
    browser = FakeBrowser({f"{BASE}/profile": (f"{BASE}/profile", text)})
    with respx.mock() as mock:
        mock.get(f"{BASE}/health").mock(return_value=_health())
        with pytest.raises(PolicyDenied) as exc:
            shots.capture_screenshots(db, project, environment="staging", commit=SHA, routes=["/", "/profile"],
                                      actor="owner", capture=browser)
    assert exc.value.code == "SCREENSHOT_SENSITIVE_CONTENT"
    assert _screens(db) == [], "all-or-nothing: the clean page captured before is not stored either"


def test_redirect_off_the_target_origin_is_refused(db, project):
    browser = FakeBrowser({f"{BASE}/": ("https://login.example.net/sso", "Masuk")})
    with respx.mock() as mock:
        mock.get(f"{BASE}/health").mock(return_value=_health())
        with pytest.raises(PolicyDenied) as exc:
            shots.capture_screenshots(db, project, environment="staging", commit=SHA, routes=["/"], actor="owner",
                                      capture=browser)
    assert exc.value.code == "SCREENSHOT_OFF_ORIGIN" and _screens(db) == []


@pytest.mark.parametrize(("kw", "code"), [
    ({"routes": ["//evil.example.com/x"]}, "ROUTES_INVALID"),
    ({"routes": ["https://evil.example.com/"]}, "ROUTES_INVALID"),
    ({"routes": []}, "ROUTES_INVALID"),
    ({"commit": "main"}, "SHA_INVALID"),
    ({"environment": "dev"}, "ENVIRONMENT_INVALID"),
    ({"viewport": "4k"}, "VIEWPORT_INVALID"),
])
def test_invalid_requests_are_refused_before_any_network_call(db, project, kw, code):
    args = {"environment": "staging", "commit": SHA, "routes": ["/"], **kw}
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValidationFailed) as exc:
            shots.capture_screenshots(db, project, actor="owner", capture=FakeBrowser(), **args)
        assert not mock.calls
    assert exc.value.code == code


def test_no_registered_target_blocks(db, project):
    with pytest.raises(Blocked) as exc:
        shots.capture_screenshots(db, project, environment="production", commit=SHA, routes=["/"], actor="owner",
                                  capture=FakeBrowser())
    assert exc.value.code == "NO_DEPLOYMENT_TARGET"


def test_visuals_status_names_the_collector(personal_client):
    body = personal_client.get("/api/docs/visuals/status").json()
    assert body["screenshot"] == "NOT_PROVEN" and body["screenshot_collector"]["command"] == "aeos docs screenshot"
    assert "no approved browser-automation" not in json.dumps(body)


# ---------------------------------------------------------------------------------------------- real browser


class _App(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib handler API
        if self.path == "/health":
            body, ctype = json.dumps({"status": "ok", "commit": SHA}).encode(), "application/json"
        else:
            body, ctype = b"<!doctype html><title>Contoh</title><h1>Daftar tugas</h1><p>3 terbuka</p>", "text/html"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.mark.integration
def test_real_headless_chromium_captures_the_running_app(db, settings, monkeypatch):
    """The default capture really drives Chromium (skips when Playwright/Chromium are not installed)."""
    pytest.importorskip("playwright.sync_api")
    install_fakes(monkeypatch)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _App)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        p = make_project(db, key="real-shot")
        db.add(ProjectIntegration(project_id=p.id, domain=p.domain, provider="deployment-target",
                                  purpose="staging", root_id=base, config={}))
        db.commit()
        try:
            out = shots.capture_screenshots(db, p, environment="staging", commit=SHA, routes=["/"], actor="owner",
                                            viewport="mobile")
        except Blocked as exc:
            if exc.code == "SCREENSHOT_BROWSER_MISSING":
                pytest.skip("Chromium for Playwright is not installed")
            raise
        db.commit()
    finally:
        server.shutdown()
    ev = db.get(EvidenceArtifact, out[0]["evidence_id"])
    from aeos.verification import collector

    data, integrity = collector.read_content(ev)
    assert integrity == "VERIFIED" and data is not None and data.startswith(b"\x89PNG")
    assert ev.environment["viewport"] == "390x844" and ev.trusted
