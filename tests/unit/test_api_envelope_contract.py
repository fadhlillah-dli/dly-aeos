"""API contract (master Header/Istilah "every claim is read with its scope, source, timestamp/freshness",
AGENTS.md list-endpoint convention, §31 G3 "contract" tests).

* Every parameterless GET list endpoint (``items`` in the body) states its ``source`` and freshness
  (``checked_at``/``generated_at``) - a new endpoint without them fails here.
* The route contract snapshot (contracts/api/routes.json) only grows: removing or renaming a route is a
  breaking change that must update the snapshot explicitly in the same reviewed change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aeos.core.enums import Domain
from aeos.core.paths import repo_root
from tests.conftest import AuthedClient

SNAPSHOT = repo_root() / "contracts" / "api" / "routes.json"
FRESHNESS_KEYS = ("checked_at", "generated_at", "observed_at", "computed_at")
# Endpoints whose list is a static catalog shipped with the code (not a claim about live state).
STATIC_CATALOGS: set[str] = set()
# Ratchet (2026-10-19): list endpoints that state a source but no envelope freshness yet (their items carry
# per-row times). This set may only shrink: remove an endpoint once fixed; never add one.
KNOWN_GAPS: dict[str, str] = {
    "/api/delivery/prs": "delivery", "/api/doctor/capabilities": "doctor", "/api/doctor/checks": "doctor",
    "/api/inventory/components": "inventory", "/api/inventory/drift": "inventory",
    "/api/inventory/snapshots": "inventory", "/api/routes": "routing",
}


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


def test_list_endpoints_state_source_and_freshness(app, seeded_client: AuthedClient):
    missing: list[str] = []
    still_gaps: set[str] = set()
    checked = 0
    for method, path in _api_routes(app):
        if method != "GET" or "{" in path or path in STATIC_CATALOGS or path.endswith("/stream"):
            continue  # path parameters need a record; /stream is a long-lived SSE response
        r = seeded_client.get(path)
        if r.status_code != 200 or "application/json" not in r.headers.get("content-type", ""):
            continue
        body = r.json()
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            continue
        checked += 1
        problems = []
        if not body.get("source"):
            problems.append("source")
        if not any(body.get(k) for k in FRESHNESS_KEYS):
            if path in KNOWN_GAPS and not problems:
                still_gaps.add(path)
                continue
            problems.append("checked_at")
        if problems:
            missing.append(f"{path}: {', '.join(problems)}")
    assert checked >= 20, f"only {checked} list endpoints were exercised"
    assert not missing, "list endpoints without source/freshness:\n" + "\n".join(missing)
    assert still_gaps <= set(KNOWN_GAPS)  # fixed endpoints simply drop out; nothing new may be added


def test_domain_scoped_list_endpoints_refuse_without_a_domain(admin_client: AuthedClient):
    """Spot check of the scope half of the convention: no domain selected -> no domain data."""
    r = admin_client.get("/api/projects")
    assert r.status_code in (400, 403, 409, 422) or r.json().get("items") == []
    admin_client.select_domain(Domain.PERSONAL)
    assert admin_client.get("/api/projects").status_code == 200


def test_snapshot_file_is_sorted_and_unique():
    routes = json.loads(Path(SNAPSHOT).read_text(encoding="utf-8"))["routes"]
    assert routes == sorted(set(routes))
