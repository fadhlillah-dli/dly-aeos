"""Verifier-owned adversarial probes of the DLY AEOS HTTP boundary (§22, §26, §31 G2; W6-063).

Copied by the verifier into its run directory (as ``tests/aeos_verifier_pack/test_probe_api_boundary.py`` next to
the trusted ``tests/conftest.py``) and run against the *candidate* code when the change has ``api`` or ``auth``
impact. Unlike the implementer's own tests these cases are authored by the verifier and enumerate every operation
the candidate app publishes in its OpenAPI document, so a new or changed route is probed without anyone writing a
test for it:

* refusal of unauthenticated requests on every operation except the documented public ones;
* refusal of cross-origin writes (Origin check) and of non-loopback Host headers (DNS rebinding);
* VIEWER is read-only on every write operation (backend enforcement, not UI hiding);
* edge/invalid input: malformed JSON, path-traversal identifiers and hostile login payloads never produce a
  server error (5xx) or a success.

Parametrisation is over static data only, so the collected test ids never depend on candidate code.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.conftest import ADMIN_PW, VIEWER_PW, AuthedClient, create_admin, create_viewer, make_app

# Operations reachable without a session by design (§26: login + readiness of the bootstrap, health probe).
PUBLIC_OPERATIONS = frozenset({("get", "/api/health"), ("get", "/api/auth/bootstrap-status"),
                               ("post", "/api/auth/login")})
# Writes a VIEWER may still perform on its own session (aeos.api.deps.SELF_SERVICE_PATHS in the trusted copy).
SELF_SERVICE = frozenset({"/api/auth/logout", "/api/auth/password", "/api/session/context"})
CAPABILITY_TOKEN_PREFIX = "/broker/"  # capability-token routes: never session-authenticated
SAFE = frozenset({"get", "head", "options"})
HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})
TRAVERSAL_ID = "..%2F..%2F..%2Fetc%2Fpasswd"
HOSTILE_LOGINS: tuple[Any, ...] = (
    {"username": "A" * (1024 * 1024), "password": "B" * (1024 * 1024)},  # oversized
    {"username": "own\x00er", "password": "x\x00"},  # NUL bytes
    {"username": ["owner"], "password": {"$ne": ""}},  # type confusion / operator injection
    {"username": "' OR '1'='1", "password": "' OR '1'='1"},  # SQL-shaped
    {"username": "../../etc/passwd", "password": "%00"},  # traversal-shaped
)


def _url(path: str, fill: str = "x") -> str:
    return re.sub(r"\{[^}]+\}", fill, path)


def _operations(app: Any) -> list[tuple[str, str]]:
    ops: list[tuple[str, str]] = []
    for path, item in sorted((app.openapi().get("paths") or {}).items()):
        for method in sorted(item):
            if method.lower() in HTTP_METHODS:
                ops.append((method.lower(), path))
    return ops


def _code(resp: Any) -> str | None:
    if not resp.headers.get("content-type", "").startswith("application/json"):
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    return (body.get("error") or {}).get("code") if isinstance(body, dict) else None


@pytest.fixture
def boundary(settings: Any, db: Any) -> Iterator[tuple[Any, TestClient, list[tuple[str, str]]]]:
    app = make_app(settings)
    with TestClient(app) as client:
        ops = _operations(app)
        assert ops, "the candidate app publishes no operations"
        yield app, client, ops


def test_every_operation_refuses_an_unauthenticated_request(boundary: Any) -> None:
    _app, client, ops = boundary
    failures = []
    for method, path in ops:
        if (method, path) in PUBLIC_OPERATIONS:
            continue
        r = client.request(method.upper(), _url(path), json={} if method not in SAFE else None)
        if r.status_code not in (401, 403):
            failures.append(f"{method.upper()} {path} -> {r.status_code} {_code(r)}")
    assert not failures, failures


def test_every_write_refuses_a_cross_origin_request(boundary: Any) -> None:
    _app, client, ops = boundary
    failures = []
    for method, path in ops:
        if method in SAFE:
            continue
        r = client.request(method.upper(), _url(path), json={}, headers={"Origin": "http://evil.example"})
        if r.status_code != 403 or _code(r) != "ORIGIN_NOT_ALLOWED":
            failures.append(f"{method.upper()} {path} -> {r.status_code} {_code(r)}")
    assert not failures, failures


def test_every_operation_refuses_a_non_loopback_host(settings: Any, db: Any) -> None:
    app = make_app(settings)
    failures = []
    with TestClient(app, base_url="http://attacker.example:8787") as client:
        for method, path in _operations(app):
            r = client.request(method.upper(), _url(path), json={} if method not in SAFE else None)
            if r.status_code != 421:
                failures.append(f"{method.upper()} {path} -> {r.status_code}")
    assert not failures, failures


def test_viewer_is_refused_on_every_write(boundary: Any, db: Any) -> None:
    _app, client, ops = boundary
    create_admin(db)
    create_viewer(db)
    viewer = AuthedClient(client)
    viewer.login("viewer", VIEWER_PW)
    failures = []
    checked = 0
    for method, path in ops:
        if method in SAFE or path in SELF_SERVICE or (method, path) in PUBLIC_OPERATIONS:
            continue
        r = client.request(method.upper(), _url(path), json={}, headers={"X-CSRF-Token": viewer.csrf or ""})
        checked += 1
        expected = (401, "COOKIE_AUTH_REJECTED") if path.startswith(CAPABILITY_TOKEN_PREFIX) else (403, "READ_ONLY_ROLE")
        if (r.status_code, _code(r)) != expected:
            failures.append(f"{method.upper()} {path} -> {r.status_code} {_code(r)}")
    assert checked > 0
    assert not failures, failures


def test_malformed_json_never_causes_a_server_error(boundary: Any, db: Any) -> None:
    _app, client, ops = boundary
    create_admin(db)
    admin = AuthedClient(client)
    admin.login("owner", ADMIN_PW)
    admin.post("/api/session/context", json={"domain": "PERSONAL"})
    failures = []
    for method, path in ops:
        if method in SAFE or path in ("/api/auth/logout", "/api/auth/password"):
            continue
        r = client.request(method.upper(), _url(path), content=b'{"not json',
                           headers={"X-CSRF-Token": admin.csrf or "", "Content-Type": "application/json"})
        if r.status_code >= 500:
            failures.append(f"{method.upper()} {path} -> {r.status_code} {_code(r)}")
    assert not failures, failures


def test_path_traversal_identifiers_are_refused(boundary: Any, db: Any) -> None:
    _app, client, ops = boundary
    create_admin(db)
    admin = AuthedClient(client)
    admin.login("owner", ADMIN_PW)
    admin.post("/api/session/context", json={"domain": "PERSONAL"})
    failures = []
    checked = 0
    for method, path in ops:
        if method != "get" or "{" not in path:
            continue
        checked += 1
        r = client.get(_url(path, TRAVERSAL_ID))
        if not 400 <= r.status_code < 500:
            failures.append(f"GET {path} -> {r.status_code} {_code(r)}")
    assert checked > 0
    assert not failures, failures


@pytest.mark.parametrize("index", range(len(HOSTILE_LOGINS)))
def test_hostile_login_payload_is_refused_without_server_error(boundary: Any, db: Any, index: int) -> None:
    _app, client, _ops = boundary
    create_admin(db)
    r = client.post("/api/auth/login", json=HOSTILE_LOGINS[index])
    assert 400 <= r.status_code < 500, (r.status_code, _code(r))
    assert "set-cookie" not in {k.lower() for k in r.headers}
