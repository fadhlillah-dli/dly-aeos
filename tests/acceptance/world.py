"""Builders that drive the integrated control plane through its public surfaces (part of the trusted
suite; no test functions). Human decisions go through the HTTP API as the owner; registry rows that a
catalog import would create (a worker route) are registered as CANDIDATE and activated by a human.
"""

from __future__ import annotations

import importlib
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from aeos.core.timeutil import utcnow
from tests.conftest import AuthedClient

GIT_TIMEOUT = 60  # seconds; every child process in this suite is bounded (no hung test run)


def seed_catalog(db: Session) -> None:
    """`aeos seed`: design catalog (manifest, roles, model registry, research, self project)."""
    from aeos.cli import SEEDERS

    for name in SEEDERS:
        importlib.import_module(name).run(db)
    db.commit()


def caps_by_name(client: AuthedClient) -> dict[str, dict[str, Any]]:
    r = client.get("/api/doctor/capabilities")
    assert r.status_code == 200, r.text
    return {c["capability"]: c for c in r.json()["items"]}


def checks_by_id(client: AuthedClient) -> dict[str, dict[str, Any]]:
    r = client.get("/api/doctor/checks")
    assert r.status_code == 200, r.text
    return {c["check_id"]: c for c in r.json()["items"]}


def register_free_route(db: Session, key: str, *, model_id: str = "vendor/acc-model:free", provider: str = "openrouter",
                        endpoint: str | None = None, fallback_chain: list[str] | None = None, is_free: str = "YES",
                        pricing: dict[str, Any] | None = None) -> Any:
    """A worker route as a catalog import registers it: CANDIDATE, never routable until a human activates it."""
    from aeos.models.routing import ModelRoute
    from aeos.routing.service import compute_config_hash

    now = utcnow()
    r = ModelRoute(route_key=key, pool="WORKER", provider=provider, model_id=model_id, endpoint=endpoint,
                   status="CANDIDATE", eligible_domains=["PERSONAL"], allowed_data_classes=["PUBLIC", "INTERNAL"],
                   supports_tools="YES", is_free=is_free, pricing=pricing or {}, context_window=131072,
                   pricing_checked_at=now, terms_checked_at=now, availability="AVAILABLE", mode="LIVE",
                   origin="CATALOG_DISCOVERED", fallback_chain=fallback_chain or [], priority=10)
    db.add(r)
    db.flush()
    r.config_hash = compute_config_hash(r)
    db.commit()
    return r


def activate_route(client: AuthedClient, key: str, *, mode: str = "APPROVED", budget_decision_id: str | None = None
                   ) -> dict[str, Any]:
    """Owner reviews the route (exact config hash) and activates it: ROUTE_ACTIVATION decision."""
    route = client.get(f"/api/routes/{key}").json()
    cfg = route.get("config_hash") or route.get("route", {}).get("config_hash")
    body: dict[str, Any] = {"mode": mode, "rationale": "reviewed terms and eval for acceptance",
                            "expected_config_hash": cfg}
    if budget_decision_id:
        body["budget_decision_id"] = budget_decision_id
    r = client.post(f"/api/routes/{key}/activate", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def create_project(client: AuthedClient, *, domain: str = "PERSONAL", name: str = "Acceptance App",
                   **extra: Any) -> dict[str, Any]:
    r = client.post("/api/projects", json={"domain": domain, "name": name, **extra})
    assert r.status_code == 200, r.text
    return r.json()["project"]


def human_exception(db: Session, *, actor_user_id: str, target_type: str, target_id: str, details: dict[str, Any],
                    domain: str = "PERSONAL", rationale: str = "owner exception for the acceptance pilot",
                    target_hash: str | None = None, expires_in: timedelta | None = None) -> Any:
    """An owner EXCEPTION decision recorded through the policy facade (the same registry the API uses)."""
    from aeos.policy import facade as policy

    d = policy.record_decision(db, kind="EXCEPTION", actor_user_id=actor_user_id, target_type=target_type,
                               target_id=target_id, target_hash=target_hash, domain=domain, rationale=rationale,
                               details=details, expires_at=(utcnow() + expires_in) if expires_in else None)
    db.commit()
    return d


def owner_id(db: Session) -> str:
    from sqlalchemy import select

    from aeos.models.identity import User

    return db.scalar(select(User.id).where(User.username == "owner"))


# ------------------------------------------------------------------------------------------------
# planning pipeline (the official claude child process is the only fake)


def task(key: str, *, deps: list[str] | None = None, paths: list[str] | None = None, feature: str = "F1",
         **overrides: Any) -> dict[str, Any]:
    t: dict[str, Any] = {
        "key": key, "feature_key": feature, "title": f"Task {key}", "goal": f"Implement {key}",
        "role_key": "feature-implementer", "input_artifacts": ["PRD F1"], "expected_outputs": ["code", "tests"],
        "acceptance_criteria": [f"{key} works"], "depends_on": deps or [],
        "allowed_paths": paths if paths is not None else [f"src/{key.lower()}/**"], "forbidden_paths": [],
        "tools_allowed": [], "egress_allowed": [],
        "capability_scope": {"operations": [], "paths": [], "notes": ""}, "capability_ttl_seconds": 0,
        "data_classification": "INTERNAL",
        "model_route_policy": {"pool": "WORKER", "requires_tools": True, "min_context_tokens": 0, "notes": ""},
        "reviewer_policy": {"required": True, "reviewer_role": "", "checkpoint": ""},
        "verifier_policy": {"required": True, "suites": [], "negative_tests_required": True},
        "limits": {"max_attempts": 2, "timeout_seconds": 1800, "max_output_tokens": 16000},
        "stop_conditions": [], "evidence_requirements": [], "risk_class": "R1", "artifact_schema_version": "1",
    }
    t.update(overrides)
    return t


def plan_content(name: str = "Acceptance App", *, tasks: list[dict[str, Any]] | None = None,
                 note: str = "") -> dict[str, Any]:
    from aeos.planning import contracts

    c = contracts.empty_plan_content(name)
    c["overview"]["overview"] = f"Ringkasan {name} {note}".strip()
    c["milestones"] = [{"key": "M1", "title": "Slice pertama", "exit_evidence": "verifier report", "order": 1}]
    c["features"] = [{"key": "F1", "title": "Fitur satu", "requirement": "R1", "priority": "P1",
                      "acceptance": ["AC1"], "depends_on": [], "milestone": "M1"}]
    c["tasks"] = tasks if tasks is not None else [task("T1")]
    c["requirements"] = [{
        "feature_key": "F1", "title": "PRD F1", "overview": "", "problem": "", "background": "", "goals": [],
        "non_goals": [], "business_requirements": [], "functional_requirements": [], "non_functional_requirements": [],
        "user_stories": [], "acceptance_criteria": ["AC1"], "user_impact": "", "dependencies": [], "assumptions": [],
        "risks": [], "success_metrics": [], "rollout_considerations": "", "related_work": [],
    }]
    return c


class PlanningResponder:
    """Answers the four planning stages by their JSON schema title (what the official client is given)."""

    def __init__(self, content: dict[str, Any] | None = None, *, verdict: str = "PASS-STATIC") -> None:
        self.content = content or plan_content()
        self.verdict = verdict
        self.prompts: list[str] = []

    def __call__(self, argv: list[str], prompt: str) -> Any:
        import copy

        from tests.acceptance.fakes import FakeClaude

        self.prompts.append(prompt)
        title = FakeClaude.schema_of(argv).get("title", "")
        if title.endswith("plan-content/1"):
            return copy.deepcopy(self.content)
        if title.endswith("challenge-findings/1"):
            return {"summary": "tidak ada temuan kritis", "overall_severity": "LOW", "contradictions": [],
                    "missing_dependencies": [], "risks": [], "scope_issues": [], "other_findings": []}
        if title.endswith("synthesis/1"):
            synthesized = copy.deepcopy(self.content)  # a synthesis is a NEW version (different hash)
            synthesized["overview"]["overview"] = (synthesized["overview"]["overview"] + " (disintesis)").strip()
            return {"summary": "disintesis", "content": synthesized, "resolutions": []}
        if title.endswith("final-review/1"):
            return {"review": {"outcome": "ok", "change_summary": "", "architecture_impact": "", "owner_auth": "owner",
                               "blast_radius": "", "baseline": "", "contradictions": [], "scope": ""},
                    "verify": {"claims": [], "test_ids": [], "environment": "", "old_behavior": "", "unknowns": []},
                    "verdict": self.verdict, "verdict_rationale": "static review only", "findings": []}
        raise AssertionError(f"unexpected schema {title}")


def plan_to_review(client: AuthedClient, project_id: str, *, prompt: str = "Aplikasi pencatat tugas pribadi "
                   "dengan ekspor CSV dan login lokal", attachment_ids: list[str] | None = None,
                   title: str = "Rencana acceptance") -> dict[str, Any]:
    """Create a plan and run the four-stage pipeline (needs a FakeClaude installed)."""
    from aeos.planning import runner

    r = client.post(f"/api/projects/{project_id}/plans",
                    json={"title": title, "prompt": prompt, "attachment_ids": attachment_ids or []})
    assert r.status_code == 201, r.text
    plan_id = r.json()["id"]
    run = client.post(f"/api/plans/{plan_id}/run")
    assert run.status_code == 202, run.text
    runner.join_all(60)
    detail = client.get(f"/api/plans/{plan_id}").json()
    assert detail["state"] == "READY_FOR_HUMAN_REVIEW", (detail["state"], detail.get("state_reason"))
    return detail


def approve_current(client: AuthedClient, plan_id: str, *, action: str = "APPROVE",
                    targets: list[dict[str, Any]] | None = None, rationale: str = "disetujui pemilik") -> dict[str, Any]:
    detail = client.get(f"/api/plans/{plan_id}").json()
    current = detail["versions"][-1]
    r = client.post(f"/api/plans/{plan_id}/decision", json={
        "action": action, "version_id": current["id"], "version_hash": current["manifest_hash"],
        "targets": targets or [], "rationale": rationale})
    assert r.status_code == 200, r.text
    return r.json()


def approved_plan(client: AuthedClient, monkeypatch: Any, project_id: str, *, content: dict[str, Any] | None = None,
                  action: str = "APPROVE", targets: list[dict[str, Any]] | None = None,
                  responder: Any = None) -> dict[str, Any]:
    """Full human flow: plan -> 4 supervisor stages (fake official client) -> owner approval of the exact hash."""
    from tests.acceptance.fakes import FakeClaude

    FakeClaude(monkeypatch, responder or PlanningResponder(content))
    detail = plan_to_review(client, project_id)
    decision = approve_current(client, detail["id"], action=action, targets=targets)
    return {"plan": client.get(f"/api/plans/{detail['id']}").json(), "decision": decision}


PUB_REPO = "octo/pub"
PUB_FOLDER = "FolderRoot0001"
# One provider vocabulary (aeos.portfolio.providers): publication targets are exactly {provider, root_id};
# Google Docs are files in the Drive folder (google-drive operations), the GitHub Issues root id is owner/repo.
PUBLISH_PROVIDERS = ("google-drive", "github-issues")
PUBLISH_TARGETS = [{"provider": "google-drive", "root_id": PUB_FOLDER},
                   {"provider": "github-issues", "root_id": PUB_REPO}]


def publication_ready(client: AuthedClient, db: Session, vault: Any, monkeypatch: Any, *,
                      content: dict[str, Any] | None = None) -> dict[str, Any]:
    """PERSONAL project with GitHub repo + Drive folder integration, owner credentials stored, write
    enablement decided by the owner, and a plan approved with Approve & Publish for those exact targets."""
    import json as _json

    from tests.acceptance.fakes import google_token_json

    vault.put("personal/github", "gh" + "p_" + "P9u8b7l6i5" * 4, actor="owner")
    vault.put("personal/google-oauth-client", _json.dumps({"installed": {"client_id": "cid",
                                                                         "client_secret": "csecret"}}), actor="owner")
    vault.put("personal/google-oauth-token", google_token_json(), actor="owner")
    project = create_project(client, name="Pub App", repository={"provider": "GITHUB", "owner": "octo", "name": "pub"})
    r = client.put(f"/api/projects/{project['id']}/integrations",
                   json={"provider": "GOOGLE_DRIVE", "purpose": "docs", "root_id": PUB_FOLDER})
    assert r.status_code == 200, r.text
    for provider in PUBLISH_PROVIDERS:
        r = client.post("/api/decisions", json={"kind": "WRITE_ENABLEMENT", "provider": provider, "enabled": True,
                                                "rationale": "owner enables writes for this pilot",
                                                "auth_scope": {"provider": provider}})
        assert r.status_code == 201, r.text
    approved = approved_plan(client, monkeypatch, project["id"], content=content, action="APPROVE_AND_PUBLISH",
                             targets=PUBLISH_TARGETS)
    return {"project": project, "plan": approved["plan"], "decision": approved["decision"]}


def preview_and_execute(client: AuthedClient, plan_id: str, *, targets: list[str] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"render_visuals": False}
    if targets:
        body["targets"] = targets
    pv = client.post(f"/api/plans/{plan_id}/publications/preview", json=body)
    assert pv.status_code == 200, pv.text
    pub = pv.json()
    ex = client.post(f"/api/publications/{pub['id']}/execute", json={
        "plan_version_id": pub["plan_version_id"], "approved_hash": pub["approved_hash"],
        "preview_hash": pub["preview_hash"]})
    assert ex.status_code == 200, ex.text
    return ex.json()


def run_outbox(db: Session, pub_id: str, *, passes: int = 3) -> dict[str, Any]:
    """What the publisher worker / `aeos publish run` does: process the outbox (fake clock not needed)."""
    from aeos.models.publishing import Publication
    from aeos.publisher import jobs

    for _ in range(passes):
        jobs.process_publication(db, pub_id)
    db.expire_all()
    return jobs.publication_view(db, db.get(Publication, pub_id))


# ------------------------------------------------------------------------------------------------
# workforce (worker admission prerequisites)


def doctor_ready(db: Session, capability: Any) -> list[str]:
    """CONTROLLED PRECONDITION: record Doctor results for every check of *capability* as passing.

    Used only where a §50 row is about scheduler/runtime semantics, not about readiness itself. On this
    host CAN_RUN_WORKER cannot become READY for real (no Docker containment), so the rows below prove
    admission/lease/cancel behaviour behind an explicitly fixture-made gate. The rows are labelled
    ``observed_evidence.fixture = acceptance`` and expire after one hour. Like a real Doctor run, they
    must be written AFTER the owner decisions/route changes they depend on: those changes mark dependent
    results STALE (§08), and a STALE result never counts as passing."""
    from aeos.core.enums import Capability
    from aeos.doctor.capabilities import checks_for_capability
    from aeos.doctor.checks import REGISTRY, load_all
    from aeos.models.inventory import CheckResult

    load_all()
    now = utcnow()
    ids = checks_for_capability(Capability(capability))
    for cid in ids:
        spec = REGISTRY[cid]
        status = "VERIFIED" if "VERIFIED" in spec.ok_statuses else (sorted(spec.ok_statuses) or ["UNKNOWN"])[0]
        db.add(CheckResult(check_id=cid, title=spec.title, group=spec.group, component_id=spec.component_id,
                           scope=dict(spec.scope), status=status, severity="INFO",
                           summary="acceptance fixture: controlled precondition (not a real probe)",
                           observed_evidence={"fixture": "acceptance"}, last_checked_at=now,
                           expires_at=now + timedelta(hours=1), invalidators=list(spec.invalidators),
                           fingerprint=None, error_code=None, remediation={}, requires_destructive=False,
                           requires_paid_write=False))
    db.commit()
    return ids


def fake_opencode(monkeypatch: Any, version: str = "1.19.3") -> None:
    """The OpenCode binary is an external tool: report it installed at the reviewed pin."""
    from aeos.worker_runtime import opencode

    status = {"status": "INSTALLED", "binary": "opencode.exe", "version": version, "pinned_version": version,
              "checked_at": utcnow().isoformat(), "functional": "NOT_PROVEN", "evidence": "acceptance fake binary"}
    monkeypatch.setattr(opencode, "opencode_status", lambda configured=None, refresh=False: dict(status))


def owner_exception(client: AuthedClient, *, target_type: str, target_id: str, details: dict[str, Any],
                    days: int = 7, rationale: str = "owner exception for the acceptance pilot",
                    allowed_operations: list[str] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "EXCEPTION", "target_type": target_type, "target_id": target_id, "rationale": rationale,
        "expires_at": (utcnow() + timedelta(days=days)).isoformat(), "auth_scope": {"domain": "PERSONAL"},
        "details": details}
    if allowed_operations:
        body["allowed_operations"] = allowed_operations
    r = client.post("/api/decisions", json=body)
    assert r.status_code == 201, r.text
    return r.json()


WORKER_ROUTE = "worker.acc-free"


def worker_ready(client: AuthedClient, db: Session, monkeypatch: Any, *, tasks: list[dict[str, Any]] | None = None,
                 project_name: str = "Worker App", platform: bool = False, responder: Any = None,
                 targets: list[dict[str, Any]] | None = None, cover_r3: bool = False) -> dict[str, Any]:
    """Approved plan with compiled TaskSpecs and every admission prerequisite decided by the owner:
    an activated free worker route, the ADR-0051 process-sandbox exception, the implementer agent
    enabled through the API, and the plan executed (tasks QUEUED -> READY). ``platform`` plans on the
    seeded DLY AEOS self-build project (§09) instead of a new project. ``targets`` are the exact targets
    the owner's plan approval binds (e.g. ``{"provider": "github-repo", "repo": owner/name}``: delivery of
    an accepted task as a PR is authorised only by an APPROVAL whose operations include ``create_pr``)."""
    from aeos.core.enums import Capability

    seed_catalog(db)
    fake_opencode(monkeypatch)
    register_free_route(db, WORKER_ROUTE)
    activate_route(client, WORKER_ROUTE)
    # ``cover_r3``: the owner's exception explicitly covers R3 (protected-path) work under the process sandbox
    # (ADR-0051 amendment 2026-10-19); without it R3 waits for the container sandbox.
    owner_exception(client, target_type="worker_sandbox", target_id="process",
                    details={"allow_process_sandbox": True},
                    allowed_operations=["run_worker_process_sandbox", "run_worker_process_sandbox_r3"] if cover_r3
                    else None)
    # The controlled precondition is written AFTER the owner decisions it depends on: recording a
    # ROUTE_ACTIVATION / EXCEPTION decision marks the dependent Doctor results STALE (§08 invalidation), so a
    # result recorded before them would (correctly) no longer count - exactly as a real recheck must follow.
    doctor_ready(db, Capability.CAN_RUN_WORKER)
    r = client.post("/api/workforce/agents/opencode-implementer/enable", json={"reason": "pilot"})
    assert r.status_code == 200, r.text
    if platform:
        from aeos.portfolio import facade as portfolio

        p = portfolio.platform_project(db)
        assert p is not None, "aeos seed registers the DLY AEOS platform project"
        project = client.get(f"/api/projects/{p.id}").json()["project"]
    else:
        project = create_project(client, name=project_name)
    approved = approved_plan(client, monkeypatch, project["id"], content=plan_content(tasks=tasks),
                             responder=responder, targets=targets)
    plan = approved["plan"]
    ex = client.post(f"/api/plans/{plan['id']}/execute")
    assert ex.status_code == 200, ex.text
    dag = client.get(f"/api/plans/{plan['id']}/dag").json()
    task_ids = {n["key"] if "key" in n else n.get("task_key"): n.get("task_id") or n.get("id") for n in dag["nodes"]}
    return {"project": project, "plan": plan, "execute": ex.json(), "dag": dag, "tasks": task_ids}


def admit(db: Session) -> list[str]:
    """One scheduler admission pass (what the scheduler loop does each tick)."""
    from aeos.workforce import scheduler

    leased = scheduler.admit_ready_tasks(db)
    db.commit()
    return leased


def git_repo(path: Any, files: dict[str, str], *, origin: str | None = None) -> str:
    """A real local git repository (git is a core bootstrap dependency, §07)."""
    import subprocess

    path.mkdir(parents=True, exist_ok=True)

    def g(*args: str) -> str:
        return subprocess.run(["git", "-c", "core.hooksPath=", "-c", "commit.gpgsign=false", *args], cwd=path,
                              capture_output=True, text=True, check=True, timeout=GIT_TIMEOUT).stdout.strip()

    g("init", "-q", "-b", "main")
    g("config", "user.email", "acceptance@example.invalid")
    g("config", "user.name", "Acceptance")
    for rel, text in files.items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    g("add", "-A")
    g("commit", "-q", "-m", "init")
    if origin:
        g("remote", "add", "origin", origin)
    return g("rev-parse", "HEAD")


def manifest_with_pin(tmp_path: Any, component_id: str, pin: str) -> Any:
    """A copy of the reviewed InstallManifest in which the owner re-declares *component_id*'s approved pin
    (the desired-state change ``aeos inventory`` loads with ``apply_manifest``)."""
    from pathlib import Path

    from aeos.core.paths import repo_root

    text = (Path(repo_root()) / "install-manifest" / "manifest.yaml").read_text(encoding="utf-8")
    start = text.index(f"  - component_id: {component_id}\n")
    old = "    approved_pin: null\n"
    at = text.index(old, start)
    out = Path(tmp_path) / f"manifest-{component_id}-{pin}.yaml"
    out.write_text(text[:at] + f'    approved_pin: "{pin}"\n' + text[at + len(old):], encoding="utf-8", newline="\n")
    return out
