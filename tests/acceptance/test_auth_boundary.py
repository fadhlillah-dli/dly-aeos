"""§50 "VIEWER memanggil write endpoint langsung" (denied by the backend), no default credentials (§26),
CSRF + origin checks on writes, DNS-rebinding host check, unauthenticated reads refused (§06, §26)."""

from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from aeos.api.deps import SELF_SERVICE_PATHS
from tests.conftest import ADMIN_PW, VIEWER_PW, AuthedClient, create_admin, create_viewer

pytestmark = pytest.mark.acceptance

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
UNAUTHENTICATED_BY_DESIGN = {"/api/auth/login"}
# Capability-token routes (§25/§26): never session-authenticated; a session cookie must be refused.
CAPABILITY_TOKEN_PREFIX = "/broker/"
COMMON_DEFAULTS = [("admin", "admin"), ("admin", "password"), ("root", "root"), ("aeos", "aeos"), ("owner", "owner")]


def test_no_default_credentials(client: AuthedClient):
    r = client.get("/api/auth/bootstrap-status")
    assert r.status_code == 200 and r.json() == {"bootstrap_complete": False}
    for username, password in COMMON_DEFAULTS:
        r = client.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 401, (username, r.status_code)


def test_default_passwords_rejected_even_after_bootstrap(client: AuthedClient, db):
    create_admin(db)
    for username, password in COMMON_DEFAULTS:
        assert client.post("/api/auth/login", json={"username": username, "password": password}).status_code in (401, 429)
    assert client.post("/api/auth/login", json={"username": "owner", "password": ADMIN_PW}).status_code == 200


def test_write_without_csrf_token_rejected(admin_client: AuthedClient):
    r = admin_client.client.post("/api/session/context", json={"domain": "PERSONAL"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_FAILED"


def test_cross_origin_write_rejected(admin_client: AuthedClient):
    r = admin_client.post("/api/session/context", json={"domain": "PERSONAL"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "ORIGIN_NOT_ALLOWED"


def test_dns_rebinding_host_rejected(app):
    with TestClient(app, base_url="http://attacker.example:8787") as c:
        assert c.get("/api/health").status_code == 421


def test_unauthenticated_reads_refused(client: AuthedClient):
    for url in ("/api/auth/session", "/api/audit", "/api/events", "/api/evidence"):
        assert client.get(url).status_code == 401, url


def _unsafe_routes(app) -> dict[str, set[str]]:
    """Every unsafe (path, methods) pair: OpenAPI paths plus directly registered APIRoutes."""
    found: dict[str, set[str]] = {}
    for path, ops in (app.openapi().get("paths") or {}).items():
        methods = {m.upper() for m in ops} & UNSAFE
        if methods:
            found.setdefault(path, set()).update(methods)
    for route in app.routes:
        if isinstance(route, APIRoute) and route.methods & UNSAFE:
            found.setdefault(route.path, set()).update(route.methods & UNSAFE)
    return found


def test_viewer_is_denied_on_every_write_route(client: AuthedClient, db, app):
    """Enumerates every registered unsafe route (loop, not parametrize: ids stay candidate-independent)."""
    create_admin(db)
    create_viewer(db)
    client.login("viewer", VIEWER_PW)
    assert client.post("/api/session/context", json={"domain": "PERSONAL"}).status_code == 200  # self-service
    failures = []
    checked = 0
    for path, methods in sorted(_unsafe_routes(app).items()):
        if path in SELF_SERVICE_PATHS or path in UNAUTHENTICATED_BY_DESIGN:
            continue
        url = re.sub(r"\{[^}]+\}", "x", path)
        for method in sorted(methods):
            r = client.client.request(method, url, json={}, headers={"X-CSRF-Token": client.csrf or ""})
            checked += 1
            code = r.json().get("error", {}).get("code") if r.headers.get("content-type", "").startswith(
                "application/json") else None
            if path.startswith(CAPABILITY_TOKEN_PREFIX):
                if r.status_code != 401 or code != "COOKIE_AUTH_REJECTED":
                    failures.append(f"{method} {path} -> {r.status_code} {code} (cookie auth must be refused)")
                continue
            if r.status_code != 403 or code != "READ_ONLY_ROLE":
                failures.append(f"{method} {path} -> {r.status_code} {code}")
    assert checked > 0
    assert not failures, failures
