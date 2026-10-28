"""Shared helpers for module-E tests (workforce / broker / worker_runtime).

Other modules' implementation modules do not exist in this worktree; ``FakeModules`` injects
minimal deterministic stand-ins into ``sys.modules`` so the frozen facades resolve. Tests that
check fail-closed behaviour simply do not install them.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import types
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.hashing import content_hash
from aeos.core.ids import new_id
from aeos.core.timeutil import utcnow
from aeos.models.cost import BudgetReservation, UsageLedgerEntry
from aeos.models.decisions import Decision
from aeos.models.evidence import EvidenceArtifact
from aeos.models.planning import Plan, PlanVersion, TaskSpec
from aeos.models.portfolio import Project, RepositoryReference
from aeos.models.routing import ModelRoute
from aeos.models.workforce import AgentDefinition
from aeos.routing.service import compute_config_hash as _route_config_hash  # real one, before any fake
from tests.conftest import template_dir


@dataclass
class FakeModules:
    approved: dict[str, str] = field(default_factory=dict)  # plan_id -> plan_version_id
    accepted: set[str] = field(default_factory=set)  # task ids whose acceptance is valid
    capability: str = "READY"
    route_key: str | None = None
    reserve_error: str | None = None
    office_allowed: bool = False
    interlock_allowed: bool = True
    evidence: bool = True
    route_use_problem: str | None = None
    calls: dict[str, int] = field(default_factory=dict)

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def install(self, monkeypatch: pytest.MonkeyPatch, *, skip: tuple[str, ...] = ()) -> FakeModules:
        mods: dict[str, types.ModuleType] = {}
        fake = self

        planning = types.ModuleType("aeos.planning.service")

        def get_approved_version(session: Session, plan_id: str):
            vid = fake.approved.get(plan_id)
            return session.get(PlanVersion, vid) if vid else None

        planning.get_approved_version = get_approved_version
        mods["aeos.planning.service"] = planning

        acc = types.ModuleType("aeos.verification.acceptance")
        acc.acceptance_is_valid = lambda session, task_id, **kw: task_id in fake.accepted
        acc.latest_acceptance = lambda session, task_id: None
        mods["aeos.verification.acceptance"] = acc

        if fake.evidence:
            col = types.ModuleType("aeos.verification.collector")

            def collect_evidence(session: Session, *, kind, origin, collector_identity, claim, content=None,
                                 evidence_level="E0", subject_type="", subject_id="", project_id=None, domain=None,
                                 run_id=None, task_id=None, environment=None, classification="INTERNAL", mode="LIVE",
                                 **_ignored):  # frozen facade passes impl=... via **locals()
                ev = EvidenceArtifact(kind=kind, origin=origin, collector_identity=collector_identity, claim=claim,
                                      trusted=origin in {"verifier", "collector", "doctor"},
                                      content_hash=content_hash(str(content)), evidence_level=evidence_level,
                                      subject_type=subject_type, subject_id=subject_id, project_id=project_id,
                                      domain=domain, run_id=run_id, task_id=task_id, environment=environment or {},
                                      mode=mode)
                session.add(ev)
                session.flush()
                return ev

            col.collect_evidence = collect_evidence
            mods["aeos.verification.collector"] = col

        doc = types.ModuleType("aeos.doctor.capabilities")

        def capability_state(session: Session, capability, *, domain=None, project_id=None, **_kw):
            from aeos.doctor.facade import CapabilityState

            return CapabilityState(capability=capability, status=fake.capability,
                                   reasons=[] if fake.capability == "READY" else ["fake doctor says not ready"])

        doc.capability_state = capability_state
        doc.invalidate = lambda session, invalidator, reason="": 0
        mods["aeos.doctor.capabilities"] = doc

        routing = types.ModuleType("aeos.routing.service")

        def select_route(session: Session, *, task, preferred_routes=None):
            from aeos.core.errors import Blocked
            from aeos.routing.facade import RouteDecision

            if not fake.route_key:
                raise Blocked("no approved route", code="NO_ROUTE")
            route = session.scalar(select(ModelRoute).where(ModelRoute.route_key == fake.route_key))
            return RouteDecision(route=route, eligible=[fake.route_key])

        def eligible_routes(session: Session, **kw):
            from aeos.routing.facade import RouteDecision

            return RouteDecision(route=None, eligible=[fake.route_key] if fake.route_key else [])

        routing.select_route = select_route
        routing.eligible_routes = eligible_routes
        # per-request route authorisation (broker §23): the real rule is tested with the real routing module
        routing.route_use_problem = lambda session, route: fake.route_use_problem
        routing.client_restriction_problem = lambda route: None  # the real rule: test_w7c1_route_request_shape.py
        # decision C per request (free OpenRouter WORKER route, PUBLIC task): the real rule is test_routing_free_worker.py
        routing.free_worker_problem = lambda session, route, run_id=None: None
        mods["aeos.routing.service"] = routing

        cost = types.ModuleType("aeos.cost.service")

        def reserve(session: Session, req):
            from aeos.core.errors import Blocked

            fake._count("reserve")
            if fake.reserve_error:
                raise Blocked(fake.reserve_error, code="BUDGET_EXCEEDED")
            r = BudgetReservation(budget_id="fake-budget", run_id=req.run_id, reserved_usd=req.est_usd,
                                  reserved_tokens=req.est_tokens, reserved_requests=req.est_requests)
            session.add(r)
            session.flush()
            return r

        def release(session: Session, reservation_id: str):
            fake._count("release")
            r = session.get(BudgetReservation, reservation_id)
            if r is not None:
                r.status = "RELEASED"

        def record_usage(session: Session, **kw):
            fake._count("record_usage")
            kw.pop("reservation_id", None)
            kw.pop("partial", None)
            e = UsageLedgerEntry(**{k: v for k, v in kw.items() if k not in ("session", "impl")})
            session.add(e)
            session.flush()
            return e

        def set_quota_state(session: Session, *, provider, state, source, scope="*", details=None, observed_at=None,
                            resets_at=None):
            from aeos.models.cost import QuotaState

            fake._count("set_quota_state")
            row = session.scalar(select(QuotaState).where(QuotaState.provider == provider, QuotaState.scope == scope))
            if row is None:
                row = QuotaState(provider=provider, scope=scope)
                session.add(row)
            row.state, row.source, row.details = state, source, details or {}
            row.observed_at, row.resets_at = observed_at or utcnow(), resets_at
            session.flush()
            return row

        cost.reserve, cost.release, cost.record_usage = reserve, release, record_usage
        cost.set_quota_state = set_quota_state
        mods["aeos.cost.service"] = cost

        dec = types.ModuleType("aeos.policy.decisions")

        def record_decision(session: Session, **kw):
            d = Decision(**{k: v for k, v in kw.items() if k not in ("session", "impl") and v is not None})
            session.add(d)
            session.flush()
            return d

        def find_active_decision(session: Session, *, kind, target_type, target_id, target_hash=None, operation=None,
                                 **_ignored):
            return session.scalar(select(Decision).where(Decision.kind == kind, Decision.target_type == target_type,
                                                         Decision.target_id == target_id, Decision.status == "ACTIVE")
                                  .order_by(Decision.created_at.desc()))

        dec.record_decision = record_decision
        dec.find_active_decision = find_active_decision
        dec.invalidate_decisions = lambda session, **kw: 0
        mods["aeos.policy.decisions"] = dec

        office = types.ModuleType("aeos.policy.office")
        office.office_policy = lambda session: {"external_worker_allowed": fake.office_allowed,
                                                "approved_worker_routes": []}
        mods["aeos.policy.office"] = office

        inter = types.ModuleType("aeos.policy.interlock")

        def check_external_write(session: Session, intent):
            from aeos.policy.facade import InterlockResult

            fake._count("interlock")
            return InterlockResult(allowed=fake.interlock_allowed, reasons=[] if fake.interlock_allowed else ["denied"])

        inter.check_external_write = check_external_write
        mods["aeos.policy.interlock"] = inter

        for name, mod in mods.items():
            if name not in skip:
                monkeypatch.setitem(sys.modules, name, mod)
        if "host" not in skip:
            # §22 admission reserves each run's enforced memory cap (4 GB by default) against the host capacity: a
            # deterministic 32 GB host keeps scheduler tests independent of the CI runner's RAM (7 GB on macOS).
            import psutil

            monkeypatch.setattr(psutil, "virtual_memory",
                                lambda: types.SimpleNamespace(total=32 * 1024 ** 3, available=16 * 1024 ** 3))
        return self


# ----------------------------------------------------------------------------- builders
def make_project(db: Session, *, domain: str = "PERSONAL", key: str | None = None, platform: bool = False,
                 classification: str = "INTERNAL") -> Project:
    p = Project(project_key=key or f"proj-{new_id()[:8]}", name="Test project", domain=domain, is_platform=platform,
                data_classification=classification)
    db.add(p)
    db.flush()
    return p


def make_plan(db: Session, project: Project, tasks: list[dict[str, Any]]) -> tuple[Plan, PlanVersion, list[TaskSpec]]:
    plan = Plan(project_id=project.id, domain=project.domain, title="Plan", state="APPROVED")
    db.add(plan)
    db.flush()
    content = {"tasks": tasks}
    pv = PlanVersion(plan_id=plan.id, project_id=project.id, domain=project.domain, version=1, content=content,
                     manifest_hash=content_hash(content))
    db.add(pv)
    db.flush()
    specs = []
    for t in tasks:
        spec = {"plan_version_id": pv.id, **t}
        ts = TaskSpec(plan_version_id=pv.id, plan_version_hash=pv.manifest_hash, project_id=project.id,
                      domain=project.domain, task_key=t["task_key"], title=t.get("title", t["task_key"]),
                      goal=t.get("goal", "do the thing"), depends_on=t.get("depends_on", []),
                      # ADR-0033: a TaskSpec always names its write scope (an empty scope is never admitted)
                      allowed_paths=t.get("allowed_paths", [f"src/{t['task_key']}/**"]),
                      forbidden_paths=t.get("forbidden_paths", []),
                      expected_outputs=t.get("expected_outputs", ["code"]),
                      tools_allowed=t.get("tools_allowed", []), egress_allowed=t.get("egress_allowed", []),
                      limits=t.get("limits", {}), capability_scope=t.get("capability_scope", {}),
                      acceptance_criteria=t.get("acceptance_criteria", ["AC1"]),
                      input_artifacts=t.get("input_artifacts", []), base_commit=t.get("base_commit"),
                      capability_ttl_seconds=t.get("capability_ttl_seconds", 600), spec_hash=content_hash(spec),
                      **({"role_key": t["role_key"]} if t.get("role_key") else {}),
                      **({"data_classification": t["data_classification"]} if "data_classification" in t else {}),
                      **({"verifier_policy": t["verifier_policy"]} if "verifier_policy" in t else {}))
        db.add(ts)
        specs.append(ts)
    db.flush()
    plan.approved_version_id = pv.id
    return plan, pv, specs


def make_route(db: Session, *, key: str = "worker.test-free", provider: str = "openrouter",
               model_id: str = "vendor/test-model:free", status: str = "APPROVED", domains: list[str] | None = None,
               is_free: str = "YES", endpoint: str | None = None, mode_params: dict | None = None,
               pricing: dict | None = None, data_classes: list[str] | None = None) -> ModelRoute:
    r = ModelRoute(route_key=key, pool="WORKER", provider=provider, model_id=model_id, status=status,
                   eligible_domains=domains if domains is not None else ["PERSONAL"], is_free=is_free,
                   endpoint=endpoint, mode_params=mode_params or {}, pricing=pricing or {},
                   # a route that may carry the default task class (ADR-0133: the executor and the broker refuse a
                   # task or context pack whose class the route does not allow); a test states [] explicitly if needed
                   allowed_data_classes=data_classes if data_classes is not None else ["PUBLIC", "INTERNAL"])
    db.add(r)
    db.flush()
    if status in ("APPROVED", "TRIAL"):
        # what a human activation records: a decision bound to the route's exact configuration hash —
        # the broker re-checks it on every proxied request (§23)
        r.config_hash = _route_config_hash(r)
        db.add(Decision(kind="ROUTE_ACTIVATION" if status == "APPROVED" else "TRIAL", actor_user_id="owner",
                        actor_kind="HUMAN", target_type="model_route", target_id=key, target_hash=r.config_hash,
                        domain="PERSONAL", rationale="test activation", allowed_operations=["route.use"]))
        db.flush()
    return r


def seed_and_enable_implementer(db: Session) -> AgentDefinition:
    from aeos.workforce import seed

    seed.run(db)
    a = db.scalar(select(AgentDefinition).where(AgentDefinition.agent_key == "opencode-implementer"))
    a.enabled = True
    db.flush()
    return a


def make_owner(db: Session, username: str = "owner-x") -> str:
    from aeos.models.identity import User

    u = User(username=username, password_hash="x", role="SUPER_ADMIN")
    db.add(u)
    db.flush()
    return u.id


def process_sandbox_exception(db: Session, *, domain: str = "PERSONAL", allow: bool = True,
                              expires_in: int | None = None) -> Decision:
    d = Decision(kind="EXCEPTION", actor_user_id="owner", actor_kind="HUMAN", target_type="worker_sandbox",
                 target_id="process", domain=domain, rationale="pilot on owner laptop",
                 details={"allow_process_sandbox": allow},
                 expires_at=(utcnow() + timedelta(seconds=expires_in)) if expires_in is not None else None)
    db.add(d)
    db.flush()
    return d


_GIT_TEMPLATES: dict[tuple[tuple[str, str], ...], tuple[Path, str]] = {}


def _init_git_repo_uncached(path: Path, files: dict[str, str]) -> str:
    path.mkdir(parents=True, exist_ok=True)

    def g(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, check=True).stdout.strip()

    g("init", "-q", "-b", "main")
    g("config", "user.email", "test@example.invalid")
    g("config", "user.name", "Test")
    g("config", "commit.gpgsign", "false")
    for rel, text in files.items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    g("add", "-A")
    g("commit", "-q", "-m", "init")
    return g("rev-parse", "HEAD")


def init_git_repo(path: Path, files: dict[str, str] | None = None) -> str:
    """One real ``git init`` + commit per distinct file set and process, then a plain directory copy per call
    (same files, same ``init`` commit and identity; the copy is a normal ``.git`` directory, never a link)."""
    files = files or {"README.md": "hello\n"}
    if path.exists() and any(path.iterdir()):
        return _init_git_repo_uncached(path, files)
    key = tuple(sorted(files.items()))
    if key not in _GIT_TEMPLATES:
        template = template_dir("aeos-gittpl-")
        _GIT_TEMPLATES[key] = (template, _init_git_repo_uncached(template, files))
    template, sha = _GIT_TEMPLATES[key]
    path.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template, path, dirs_exist_ok=True)
    return sha


def attach_repo(db: Session, project: Project, repo: Path) -> RepositoryReference:
    ref = RepositoryReference(project_id=project.id, provider="GITHUB", full_name="owner/repo",
                              local_path_ref=str(repo), default_branch="main")
    db.add(ref)
    db.flush()
    return ref


SLEEP_TREE = (
    "import subprocess, sys, time\n"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "print(g.pid, flush=True)\n"
    "time.sleep(120)\n"
)
