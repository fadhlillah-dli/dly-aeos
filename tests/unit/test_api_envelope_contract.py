"""API contract (master Header/Istilah "every claim is read with its scope, source, timestamp/freshness,
version/config and evidence", AGENTS.md list-endpoint convention, §31 G3 "contract" tests).

* Every GET list endpoint (``items`` in the body) and every GET claim endpoint (a JSON object that is not a
  static catalog or session state) states its ``source`` and freshness (``checked_at``/``generated_at``) -
  parameterless routes and the project/component-scoped routes alike. ``checked_at`` may be null ("never
  checked"); then ``generated_at`` says when the store was read, so no response is undated.
* Every /api response names the version/config it was produced under (``X-AEOS-Version``,
  ``X-AEOS-Schema-Version``, ``X-AEOS-Settings-Hash``), including errors.
* The route contract snapshot (contracts/api/routes.json) only grows: removing or renaming a route is a
  breaking change that must update the snapshot explicitly in the same reviewed change.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from aeos.core.enums import Domain
from aeos.core.paths import repo_root
from tests.conftest import AuthedClient

SNAPSHOT = repo_root() / "contracts" / "api" / "routes.json"
FRESHNESS_KEYS = ("checked_at", "generated_at", "observed_at", "computed_at")
# Bodies that are a static catalog shipped with the code (not a claim about live state).
STATIC_CATALOGS: set[str] = {"/api/decisions/shapes", "/api/docs/product1-structure", "/api/intake/policy",
                             "/api/proposals/meta"}
# Not claims about the system: session/auth state, the event cursor, the liveness probe (it reports version).
NOT_CLAIMS: set[str] = {"/api/auth/bootstrap-status", "/api/auth/session", "/api/events", "/api/health"}
# The freshness ratchet is closed (2026-10-19, was 7 endpoints): it must stay empty.
KNOWN_GAPS: dict[str, str] = {}
CLAIM_HEADERS = ("X-AEOS-Version", "X-AEOS-Schema-Version", "X-AEOS-Settings-Hash")


def api_routes(app) -> list[tuple[str, str]]:
    """(METHOD, path) of every /api route, from the OpenAPI schema (app.routes nests included routers)."""
    out = []
    for path, ops in app.openapi()["paths"].items():
        if not path.startswith("/api/"):
            continue
        for m in ops:
            if m.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                out.append((m.upper(), path))
    return sorted(set(out))


_api_routes = api_routes


def test_route_contract_only_grows(app):
    current = {f"{m} {p}" for m, p in _api_routes(app)}
    snapshot = set(json.loads(SNAPSHOT.read_text(encoding="utf-8"))["routes"])
    removed = sorted(snapshot - current)
    assert not removed, f"routes removed/renamed without updating {SNAPSHOT.name}: {removed}"


@pytest.fixture
def seeded_client(personal_client: AuthedClient, db, secrets_memory) -> AuthedClient:
    for mod in ("aeos.policy.seed", "aeos.inventory.seed", "aeos.portfolio.seed"):
        try:
            __import__(mod, fromlist=["run"]).run(db)
        except Exception:  # a seeder that cannot run here only means fewer rows; the contract still applies
            db.rollback()
    db.commit()
    return personal_client


def _envelope_problems(body: dict[str, Any]) -> list[str]:
    if "bomFormat" in body:  # CycloneDX document: its own format states freshness in metadata.timestamp
        return [] if (body.get("metadata") or {}).get("timestamp") else ["metadata.timestamp"]
    problems = []
    if not body.get("source"):
        problems.append("source")
    if not any(body.get(k) for k in FRESHNESS_KEYS):
        problems.append("checked_at/generated_at")
    return problems


def _json_object(r) -> dict[str, Any] | None:
    if r.status_code != 200 or "application/json" not in r.headers.get("content-type", ""):
        return None
    body = r.json()
    return body if isinstance(body, dict) else None


def _parameterless_gets(app, client: AuthedClient):
    for method, path in _api_routes(app):
        if method != "GET" or "{" in path or path.endswith("/stream"):
            continue  # path parameters: see the scoped test; /stream is a long-lived SSE response
        yield path, client.get(path)


def test_list_endpoints_state_source_and_freshness(app, seeded_client: AuthedClient):
    missing: list[str] = []
    checked = 0
    for path, r in _parameterless_gets(app, seeded_client):
        body = _json_object(r)
        if body is None or not isinstance(body.get("items"), list) or path in STATIC_CATALOGS:
            continue
        checked += 1
        if problems := _envelope_problems(body):
            missing.append(f"{path}: {', '.join(problems)}")
    assert checked >= 50, f"only {checked} list endpoints were exercised"
    assert not missing, "list endpoints without source/freshness:\n" + "\n".join(missing)
    assert KNOWN_GAPS == {}, "the freshness ratchet is closed; fix the endpoint instead of listing it"


def test_claim_endpoints_state_source_freshness_and_version(app, seeded_client: AuthedClient):
    """Non-list claims (status/summary objects) carry the same envelope, and every response the version."""
    missing: list[str] = []
    unversioned: list[str] = []
    checked = 0
    for path, r in _parameterless_gets(app, seeded_client):
        if any(not r.headers.get(h) for h in CLAIM_HEADERS):
            unversioned.append(f"{path} ({r.status_code})")
        body = _json_object(r)
        if body is None or isinstance(body.get("items"), list) or path in STATIC_CATALOGS | NOT_CLAIMS:
            continue
        checked += 1
        if problems := _envelope_problems(body):
            missing.append(f"{path}: {', '.join(problems)}")
    assert checked >= 15, f"only {checked} claim endpoints were exercised"
    assert not missing, "claim endpoints without source/freshness:\n" + "\n".join(missing)
    assert not unversioned, "responses without version/config headers:\n" + "\n".join(unversioned)


def _seeded_ids(db) -> dict[str, str]:
    from aeos.models.inventory import ComponentReference
    from aeos.models.portfolio import Project

    ids: dict[str, str] = {}
    project = db.scalars(select(Project).where(Project.domain == Domain.PERSONAL.value)).first()
    if project is not None:
        ids["project_id"] = project.id
    comp = db.scalars(select(ComponentReference).order_by(ComponentReference.component_id)).first()
    if comp is not None:
        ids["component_id"] = comp.component_id
    return ids


def test_project_and_component_scoped_endpoints_state_source_and_freshness(app, seeded_client: AuthedClient, db):
    """Routes with path parameters, exercised with the seeded PERSONAL project and a seeded component."""
    ids = _seeded_ids(db)
    assert set(ids) == {"project_id", "component_id"}, ids
    missing: list[str] = []
    checked = 0
    for method, path in _api_routes(app):
        params = re.findall(r"{(\w+)}", path)
        if method != "GET" or not params or any(p not in ids for p in params) or path.endswith("/stream"):
            continue
        url = path
        for p in params:
            url = url.replace("{" + p + "}", ids[p])
        r = seeded_client.get(url)
        assert all(r.headers.get(h) for h in CLAIM_HEADERS), path
        body = _json_object(r)
        if body is None:
            continue
        checked += 1
        if problems := _envelope_problems(body):
            missing.append(f"{path}: {', '.join(problems)}")
    assert checked >= 15, f"only {checked} scoped endpoints were exercised"
    assert not missing, "scoped endpoints without source/freshness:\n" + "\n".join(missing)


def test_version_and_settings_headers_match_the_runtime_config(app, seeded_client: AuthedClient, settings):
    """The settings hash is the one recorded at promotion (runtime_config), also on errors; never on the SPA."""
    from aeos import SCHEMA_VERSION, __version__
    from aeos.operations.release import runtime_config

    expected = runtime_config(settings, None)["settings_hash"]
    for r in (seeded_client.get("/api/projects"), seeded_client.get("/api/plans/does-not-exist"),
              seeded_client.get("/api/does-not-exist")):
        assert r.headers["X-AEOS-Version"] == __version__
        assert r.headers["X-AEOS-Schema-Version"] == SCHEMA_VERSION
        assert r.headers["X-AEOS-Settings-Hash"] == expected
    assert seeded_client.get("/api/plans/does-not-exist").status_code == 404
    assert "X-AEOS-Settings-Hash" not in seeded_client.get("/").headers


def test_settings_hash_changes_with_the_configuration(settings):
    from fastapi.testclient import TestClient

    from aeos.api.app import create_app

    def header(s) -> str:
        with TestClient(create_app(s, testing=True, init_db=False), base_url="http://testserver") as c:
            return c.get("/api/health").headers["X-AEOS-Settings-Hash"]

    assert header(settings) == header(settings.model_copy())
    assert header(settings) != header(settings.model_copy(update={"session_idle_minutes": 5}))


def test_domain_scoped_list_endpoints_refuse_without_a_domain(admin_client: AuthedClient):
    """Spot check of the scope half of the convention: no domain selected -> no domain data."""
    r = admin_client.get("/api/projects")
    assert r.status_code in (400, 403, 409, 422) or r.json().get("items") == []
    admin_client.select_domain(Domain.PERSONAL)
    assert admin_client.get("/api/projects").status_code == 200


def test_snapshot_file_is_sorted_and_unique():
    routes = json.loads(Path(SNAPSHOT).read_text(encoding="utf-8"))["routes"]
    assert routes == sorted(set(routes))
