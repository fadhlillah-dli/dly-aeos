"""API contract (master Header/Istilah "every claim is read with its scope, source, timestamp/freshness,
version/config and evidence", AGENTS.md list-endpoint convention, §31 G3 "contract" tests).

* Every GET list endpoint (``items`` in the body) and every GET claim endpoint (a JSON object that is not a
  static catalog or session state) states its ``source`` and freshness (``checked_at``/``generated_at``) -
  parameterless routes and every route with path parameters (each called with a real seeded record, which must
  answer 200). ``checked_at`` may be null ("never checked"); then ``generated_at`` says when the store was read,
  so no response is undated.
* Scope: a PERSONAL record named by any parameterised GET is 404/403 from an OFFICE session.
* Evidence stays per claim: no list response carries one page-wide ``evidence_level``.
* Every /api response names the version/config it was produced under (``X-AEOS-Version``,
  ``X-AEOS-Schema-Version``, ``X-AEOS-Settings-Hash``), including errors and security-middleware rejections.
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


def _ok_json(r) -> dict[str, Any]:
    assert r.status_code == 200, r.text
    return r.json()


def _detail_ids(db, client: AuthedClient) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """One real record behind every path parameter of a GET route (master Header/Istilah: every claim states its
    source and freshness - detail views as well as lists). Records come from the product's own write paths where
    one exists (API or service), otherwise from the canonical model rows those paths create."""
    from aeos.core.hashing import content_hash
    from aeos.evaluation import service as eval_service
    from aeos.models.decisions import Decision
    from aeos.models.evidence import EvidenceArtifact, ReviewRecord
    from aeos.models.portfolio import Project, RepositoryReference
    from aeos.models.publishing import DocumentRecord, Publication
    from aeos.models.workforce import TaskExecution, WorkerRun
    from aeos.operations import sli
    from aeos.research import service as research
    from tests.unit.test_workforce_support import make_plan, make_route

    ids = _seeded_ids(db)
    project = db.get(Project, ids["project_id"])
    plan, version, specs = make_plan(db, project, [{"task_key": "T1"}])
    plan.current_version_id = version.id
    run = WorkerRun(project_id=project.id, domain=project.domain, task_id=specs[0].id, execution_status="SUCCEEDED",
                    plan_version_id=version.id, task_key="T1")
    db.add_all([run, TaskExecution(task_id=specs[0].id, project_id=project.id, domain=project.domain, state="QUEUED",
                                   plan_id=plan.id, plan_version_id=version.id, task_key="T1")])
    db.flush()
    evidence = EvidenceArtifact(project_id=project.id, domain=project.domain, run_id=run.id, task_id=specs[0].id,
                                kind="TEST_REPORT", origin="verifier", collector_identity="verifier", trusted=True,
                                content_hash=content_hash("report"), evidence_level="E2", claim="tests ran")
    review = ReviewRecord(target_type="run", target_id=run.id, project_id=project.id, domain=project.domain,
                          run_id=run.id, task_id=specs[0].id, reviewer_kind="HUMAN", verdict="NEEDS_CHANGES")
    decision = Decision(kind="EXCEPTION", actor_user_id="owner", actor_kind="HUMAN", target_type="worker_sandbox",
                        target_id="process", domain=project.domain, rationale="contract test exception")
    doc = DocumentRecord(project_id=project.id, domain=project.domain, logical_doc_id="00-product-home", node="00",
                         title="Product home", body_markdown="# Home\n")
    pub = Publication(project_id=project.id, domain=project.domain, plan_version_id=version.id, plan_id=plan.id,
                      approved_hash=version.manifest_hash, decision_id="approval-decision")
    db.add_all([evidence, review, decision, doc, pub])
    make_route(db, key="worker.contract-free")
    # wave 7 (cluster D): a read-only investigation TaskSpec (its own detail view) and the seeded worker agents
    from aeos.workforce import seed as workforce_seed

    _iplan, _ipv, inv_specs = make_plan(db, project, [
        {"task_key": "INV", "allowed_paths": ["src/**"], "expected_outputs": ["investigation report per hypothesis"],
         "capability_scope": {"operations": ["investigation", "investigation.timebox=120", "investigation.repair=FIX",
                                             "hypothesis:h1=the constant is wrong"]}},
        {"task_key": "FIX", "allowed_paths": ["src/**"], "depends_on": ["INV"]}])
    db.add(TaskExecution(task_id=inv_specs[0].id, project_id=project.id, domain=project.domain, state="QUEUED",
                         plan_id=_iplan.id, plan_version_id=_ipv.id, task_key="INV"))
    workforce_seed.run(db)
    research.seed_registry(db, platform_repo_slug="fadhlillah-dli/dly-aeos")
    eval_service.seed_suites(db)
    repo = db.scalars(select(RepositoryReference).where(RepositoryReference.project_id == project.id)).first()
    db.commit()

    attachment = _ok_json(client.post(f"/api/projects/{project.id}/attachments",
                                      files={"file": ("notes.md", b"# Catatan\n", "text/markdown")}))
    pack = _ok_json(client.post(f"/api/projects/{project.id}/context-packs", json={"token_budget": 2000}))
    snapshot = _ok_json(client.post("/api/inventory/snapshots", json={"label": "contract"}))
    incident = _ok_json(client.post("/api/ops/incidents", json={"title": "Contract incident"}))
    proposal = _ok_json(client.post("/api/proposals", json={"kind": "COMPARE",
                                                           "sources": ["https://github.com/octo/tool"]}))
    eval_run = _ok_json(client.post("/api/eval/runs", json={
        "suite_key": "eval.docs-only-change", "topology": "A",
        "configuration": {"model_id": "glm-5.3-flash", "model_version": "UNKNOWN", "provider": "opencode-zen",
                          "provider_route": "opencode-zen/glm-flash-free", "harness": "opencode",
                          "harness_version": "1.4.0", "prompt_revision": "impl-v1", "tools": ["edit"],
                          "policy_version": "pol-1", "context_params": {}},
        "limits": {"max_attempts": 1, "max_minutes": 20, "max_cost_usd": 0}}))
    adr = _ok_json(client.get("/api/adrs"))["items"][0]
    entry = _ok_json(client.get("/api/research"))["items"][0]
    check = _ok_json(client.get("/api/doctor/checks"))["items"][0]

    ids.update({
        "plan_id": plan.id, "version_id": version.id, "task_id": specs[0].id, "run_id": run.id,
        "evidence_id": evidence.id, "review_id": review.id, "decision_id": decision.id, "doc_id": doc.id,
        "pub_id": pub.id, "route_key": "worker.contract-free", "attachment_id": attachment["attachment"]["id"]
        if "attachment" in attachment else attachment["id"], "pack_id": (pack.get("context_pack") or pack)["id"],
        "snapshot_id": snapshot["id"], "incident_id": incident["id"], "proposal_id": proposal["id"],
        "adr_key": adr["key"], "entry_id": entry["id"], "check_id": check.get("check_id") or check["id"],
        "sli_key": next(iter(sli.SLI_DEFS)), "agent_key": "opencode-implementer",
    })
    if repo is not None:
        ids["repository_id"] = repo.id
    per_route = {"/api/eval/runs/{run_id}": {"run_id": eval_run["id"]},
                 "/api/tasks/{task_id}/investigation": {"task_id": inv_specs[0].id}}
    return ids, per_route


def test_every_get_with_path_parameters_states_source_and_freshness(app, seeded_client: AuthedClient, db):
    """W6-001: no GET route with a path parameter is left outside the contract - each one is called with a real
    record, must answer 200, and its JSON body states source and freshness (detail views included)."""
    ids, per_route = _detail_ids(db, seeded_client)
    missing: list[str] = []
    not_exercised: list[str] = []
    checked = 0
    for method, path in _api_routes(app):
        params = re.findall(r"{(\w+)}", path)
        if method != "GET" or not params or path.endswith("/stream"):
            continue
        values = {**ids, **per_route.get(path, {})}
        if any(p not in values for p in params):
            not_exercised.append(f"{path} (no seeded {', '.join(p for p in params if p not in values)})")
            continue
        url = path
        for p in params:
            url = url.replace("{" + p + "}", values[p])
        r = seeded_client.get(url)
        assert all(r.headers.get(h) for h in CLAIM_HEADERS), path
        if r.status_code != 200:
            not_exercised.append(f"{path} -> {r.status_code} {r.text[:200]}")
            continue
        body = _json_object(r)
        if body is None:
            continue
        checked += 1
        if problems := _envelope_problems(body):
            missing.append(f"{path}: {', '.join(problems)}")
    assert not not_exercised, "GET routes with path parameters outside the contract:\n" + "\n".join(not_exercised)
    assert checked >= 44, f"only {checked} parameterised endpoints were exercised"
    assert not missing, "detail endpoints without source/freshness:\n" + "\n".join(missing)


# Path parameters naming a PERSONAL-domain record in _detail_ids (the others are platform records: ADRs, components,
# doctor checks, SLI definitions, model routes, the seeded research registry, platform eval runs).
DOMAIN_SCOPED_PARAMS = {"project_id", "plan_id", "version_id", "task_id", "run_id", "evidence_id", "review_id",
                        "decision_id", "doc_id", "pub_id", "attachment_id", "pack_id", "snapshot_id", "incident_id",
                        "proposal_id", "repository_id"}
# Answers about a PERSONAL id that carry nothing of the record, so OFFICE may get them: the eval comparison of a
# proposal lists only eval runs visible in the session domain and answers an unknown id the same way (NOT_PLANNED).
SCOPE_NEUTRAL: set[str] = {"/api/eval/comparisons/{proposal_id}"}


def test_detail_endpoints_are_domain_scoped(app, seeded_client: AuthedClient, db):
    """The scope half of the convention for every parameterised GET: a PERSONAL record is not readable from an
    OFFICE session (404/403, no metadata), whichever route names it (master §35/§50 Personal vs Office)."""
    ids, per_route = _detail_ids(db, seeded_client)
    seeded_client.select_domain(Domain.OFFICE)
    leaked: list[str] = []
    checked = 0
    for method, path in _api_routes(app):
        params = re.findall(r"{(\w+)}", path)
        if (method != "GET" or not params or path.endswith("/stream") or path in SCOPE_NEUTRAL
                or not any(p in DOMAIN_SCOPED_PARAMS for p in params) or path in per_route):
            continue
        url = path
        for p in params:
            url = url.replace("{" + p + "}", ids[p])
        r = seeded_client.get(url)
        checked += 1
        if r.status_code not in (403, 404):
            leaked.append(f"{path} -> {r.status_code}")
    assert checked >= 30, f"only {checked} domain-scoped endpoints were exercised"
    assert not leaked, "PERSONAL records readable from an OFFICE session:\n" + "\n".join(leaked)


def test_evidence_level_is_per_claim_never_page_wide(app, seeded_client: AuthedClient, db):
    """Evidence stays per claim (AGENTS.md): a list response carries evidence_level on its items, never one level
    for the whole page."""
    _detail_ids(db, seeded_client)
    page_wide: list[str] = []
    for path, r in _parameterless_gets(app, seeded_client):
        body = _json_object(r)
        if body is not None and isinstance(body.get("items"), list) and "evidence_level" in body:
            page_wide.append(path)
    assert not page_wide, "list endpoints with a page-wide evidence_level:\n" + "\n".join(page_wide)


def test_middleware_rejections_carry_the_claim_headers(app, settings):
    """A request refused by the security middleware (bad Host -> 421, foreign Origin -> 403) still names the
    version/config it was refused under, like every other /api response."""
    from fastapi.testclient import TestClient

    with TestClient(app, base_url="http://testserver") as c:
        bad_host = c.get("/api/health", headers={"Host": "evil.example"})
        bad_origin = c.post("/api/auth/login", json={"username": "x", "password": "y"},
                            headers={"Origin": "http://evil.example"})
    assert bad_host.status_code == 421 and bad_host.json()["error"]["code"] == "HOST_NOT_ALLOWED"
    assert bad_origin.status_code == 403 and bad_origin.json()["error"]["code"] == "ORIGIN_NOT_ALLOWED"
    for r in (bad_host, bad_origin):
        assert all(r.headers.get(h) for h in CLAIM_HEADERS), dict(r.headers)


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
