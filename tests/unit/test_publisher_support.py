"""Shared helpers for publisher/docs-engine tests (no tests here).

Other modules (planning service, policy decisions/interlock) are built in parallel, so these tests
install small, explicit fakes behind the frozen facades. The fakes are deliberately strict:
the interlock denies unless a PUBLICATION decision bound to the exact approved hash covers the
operation and write enablement is on (fail closed).
"""

from __future__ import annotations

import copy
import sys
import types
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.hashing import content_hash
from aeos.core.ids import new_id
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.models.decisions import Decision
from aeos.models.planning import Plan, PlanVersion, TaskSpec
from aeos.models.portfolio import Project, RepositoryReference
from aeos.publisher.providers.base import OpContext, OpResult, ProviderError, ProviderTimeout

SAMPLE_CONTENT: dict[str, Any] = {
    "product": {"name": "Contoh App", "vision": "Tugas tercatat rapi", "objectives": ["Catat tugas", "Lacak selesai"]},
    "summary": "Aplikasi contoh untuk mencatat tugas harian.",
    "scope": {"in": ["Pencatatan tugas"], "out": ["Aplikasi mobile"]},
    "team": {"product_owner": "Fadhlillah", "tech_lead": "Fadhlillah"},
    "stack": ["Python", "FastAPI", "React"],
    "criticality": "LOW",
    "features": [
        {"feature_key": "tasks", "title": "Kelola tugas", "summary": "CRUD tugas.",
         "acceptance": ["User dapat membuat tugas", "User dapat menandai selesai"], "priority": "P1",
         "milestone": "m1", "functional_requirements": ["Buat/ubah/hapus tugas"], "needs_tdd": True,
         "personal_data": False},
        {"feature_key": "auth", "title": "Login", "summary": "Login lokal.", "acceptance": ["User login dengan password"],
         "priority": "P1", "milestone": "m1", "needs_rfc": True, "personal_data": True},
    ],
    "milestones": [{"milestone_key": "m1", "title": "MVP", "exit_evidence": "Semua AC terbukti oleh verifier"}],
    "architecture": {
        "context": "Satu API dan satu SPA.",
        "components": [{"name": "API", "responsibility": "REST"}, {"name": "SPA", "responsibility": "UI"}],
        "integrations": ["GitHub"],
        "diagrams": [{"key": "context", "title": "Context", "kind": "context",
                      "mermaid": "flowchart TD\n  U[User] --> A[API]"}],
    },
    "risks": [{"id": "R1", "title": "Kebocoran data", "likelihood": "LOW", "impact": "HIGH", "mitigation": "Enkripsi"}],
    "security": {"authn": "Password Argon2id", "threats": ["Credential stuffing"]},
    "testing": {"strategy": "Unit + integration oleh verifier independen", "levels": ["unit", "integration"]},
}


def make_project(db: Session, *, domain: str = "PERSONAL", name: str = "Contoh App", key: str | None = None,
                 github_repo: str | None = "owner/contoh") -> Project:
    p = Project(project_key=key or f"contoh-{new_id()[:8]}", name=name, domain=domain, data_classification="INTERNAL")
    db.add(p)
    db.flush()
    if github_repo and domain == "PERSONAL":
        db.add(RepositoryReference(project_id=p.id, provider="GITHUB", full_name=github_repo,
                                   url=f"https://github.com/{github_repo}"))
    db.commit()
    return p


def make_plan(db: Session, project: Project, content: dict[str, Any] | None = None, *, approved: bool = True,
              tasks: list[dict[str, Any]] | None = None) -> tuple[Plan, PlanVersion]:
    plan = Plan(project_id=project.id, domain=project.domain, title="Rencana awal", state="APPROVED" if approved
                else "READY_FOR_HUMAN_REVIEW")
    db.add(plan)
    db.flush()
    v = add_version(db, plan, content if content is not None else copy.deepcopy(SAMPLE_CONTENT), tasks=tasks)
    if approved:
        approve(db, plan, v)
    db.commit()
    return plan, v


def add_version(db: Session, plan: Plan, content: dict[str, Any], *, tasks: list[dict[str, Any]] | None = None
                ) -> PlanVersion:
    n = len(list(db.scalars(select(PlanVersion).where(PlanVersion.plan_id == plan.id)))) + 1
    v = PlanVersion(plan_id=plan.id, project_id=plan.project_id, domain=plan.domain, version=n, content=content,
                    manifest_hash=content_hash({"content": content, "n": n}), created_at=utcnow())
    db.add(v)
    db.flush()
    default_tasks = tasks if tasks is not None else [
        {"task_key": "T1", "feature_key": "tasks", "title": "API tugas", "goal": "Endpoint CRUD tugas",
         "acceptance_criteria": ["POST /tasks membuat tugas"]},
        {"task_key": "T2", "feature_key": "auth", "title": "Login", "goal": "Login lokal",
         "acceptance_criteria": ["Login gagal dengan password salah"], "depends_on": ["T1"]},
    ]
    for t in default_tasks:
        db.add(TaskSpec(plan_version_id=v.id, plan_version_hash=v.manifest_hash, project_id=plan.project_id,
                        domain=plan.domain, task_key=t["task_key"], feature_key=t.get("feature_key"), title=t["title"],
                        goal=t.get("goal", t["title"]), acceptance_criteria=t.get("acceptance_criteria", []),
                        depends_on=t.get("depends_on", []), spec_hash=content_hash(t)))
    plan.current_version_id = v.id
    db.flush()
    return v


def approved_targets_for(db: Session, project_id: str) -> list[dict[str, str]]:
    """What the human approves with Approve & Publish: the project's configured targets ({provider, root_id})."""
    from aeos.publisher.targets import resolve_targets

    project = db.get(Project, project_id)
    return [{"provider": t.provider, "root_id": t.root_id or "unset"} for t in resolve_targets(db, project)]


def approve(db: Session, plan: Plan, version: PlanVersion, actor: str = "owner-user",
            targets: list[dict[str, str]] | None = None, action: str = "APPROVE_AND_PUBLISH") -> Decision:
    from aeos.policy import catalog

    for d in db.scalars(select(Decision).where(Decision.kind == "APPROVAL", Decision.target_type == "plan_version",
                                               Decision.status == "ACTIVE")):
        if d.project_id == plan.project_id:
            d.status = "SUPERSEDED"
    targets = approved_targets_for(db, plan.project_id) if targets is None else targets
    ops = sorted({op for t in targets for op in catalog.PUBLISH_OPERATIONS.get(t["provider"], ())})
    d = Decision(kind="APPROVAL", actor_user_id=actor, target_type="plan_version", target_id=version.id,
                 target_version=str(version.version), target_hash=version.manifest_hash, project_id=plan.project_id,
                 domain=plan.domain, allowed_operations=ops,
                 auth_scope={"plan_id": plan.id, "action": action, "targets": targets})
    db.add(d)
    plan.approved_version_id = version.id
    plan.state = "APPROVED"
    db.flush()
    return d


# ---------------------------------------------------------------------------------------------
# Facade fakes


@dataclass
class PolicyFake:
    write_enabled: bool = True
    deny_operations: set[str] = field(default_factory=set)
    calls: list[Any] = field(default_factory=list)


def _find_active(session: Session, *, kind: str, target_type: str, target_id: str, target_hash: str | None = None,
                 operation: str | None = None) -> Decision | None:
    now = utcnow()
    for d in session.scalars(select(Decision).where(Decision.kind == kind, Decision.target_type == target_type,
                                                    Decision.target_id == target_id, Decision.status == "ACTIVE")
                             .order_by(Decision.created_at.desc())):
        if target_hash is not None and d.target_hash != target_hash:
            continue
        if operation is not None and operation not in (d.allowed_operations or []):
            continue
        exp = ensure_aware(d.expires_at)
        if exp is not None and exp <= now:
            continue
        return d
    return None


def install_fakes(monkeypatch: pytest.MonkeyPatch, *, planning: bool = True, policy: bool = True) -> PolicyFake:
    state = PolicyFake()
    if planning:
        mod = types.ModuleType("aeos.planning.service")

        def get_approved_version(session: Session, plan_id: str) -> PlanVersion | None:
            plan = session.get(Plan, plan_id)
            if plan is None or not plan.approved_version_id:
                return None
            v = session.get(PlanVersion, plan.approved_version_id)
            if v is None:
                return None
            d = _find_active(session, kind="APPROVAL", target_type="plan_version", target_id=v.id,
                             target_hash=v.manifest_hash)
            return v if d is not None else None

        mod.get_approved_version = get_approved_version  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "aeos.planning.service", mod)
    if policy:
        dec = types.ModuleType("aeos.policy.decisions")

        # NOTE: the frozen facade calls ``impl(**locals())``, which also passes ``impl`` and ``session``
        # as keyword arguments; implementations must tolerate that (reported as a shared change request).
        def record_decision(**kw: Any) -> Decision:
            kw.pop("impl", None)
            session = kw.pop("session")
            d = Decision(kind=kw["kind"], actor_user_id=kw["actor_user_id"], target_type=kw["target_type"],
                         target_id=kw["target_id"], target_version=kw.get("target_version"),
                         target_hash=kw.get("target_hash"), allowed_operations=kw.get("allowed_operations") or [],
                         auth_scope=kw.get("auth_scope") or {}, project_id=kw.get("project_id"),
                         domain=kw.get("domain"), rationale=kw.get("rationale", ""), expires_at=kw.get("expires_at"),
                         details=kw.get("details") or {})
            session.add(d)
            session.flush()
            return d

        def find_active_decision(**kw: Any) -> Decision | None:
            kw.pop("impl", None)
            session = kw.pop("session")
            return _find_active(session, **kw)

        def invalidate_decisions(session: Session, *, target_type: str, target_id: str, reason: str,
                                 impl: Any = None) -> int:
            n = 0
            for d in session.scalars(select(Decision).where(Decision.target_type == target_type,
                                                            Decision.target_id == target_id,
                                                            Decision.status == "ACTIVE")):
                d.status, d.invalidation_reason, d.invalidated_at = "INVALIDATED", reason, utcnow()
                n += 1
            return n

        dec.record_decision = record_decision  # type: ignore[attr-defined]
        dec.find_active_decision = find_active_decision  # type: ignore[attr-defined]
        dec.invalidate_decisions = invalidate_decisions  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "aeos.policy.decisions", dec)

        inter = types.ModuleType("aeos.policy.interlock")

        def check_external_write(session: Session, intent: Any):
            from aeos.policy.facade import InterlockResult

            state.calls.append(intent)
            reasons = []
            if not state.write_enabled:
                reasons.append(f"write disabled for {intent.provider}")
            if intent.operation in state.deny_operations:
                reasons.append(f"operation {intent.operation} not allowed")
            d = session.get(Decision, intent.decision_id) if intent.decision_id else None
            if d is None or d.status != "ACTIVE" or d.kind != "PUBLICATION":
                reasons.append("no active PUBLICATION decision")
            else:
                if d.target_hash != intent.approved_hash or d.target_version != intent.approved_plan_version_id:
                    reasons.append("decision not bound to the approved version/hash")
                if intent.operation not in (d.allowed_operations or []):
                    reasons.append("operation not covered by the decision")
                if d.target_id != intent.extra.get("publication_id"):
                    reasons.append("decision bound to another publication")
                exp = ensure_aware(d.expires_at)
                if exp is None or exp <= utcnow():
                    reasons.append("decision expired")
            if intent.capability_expires_at is None:
                reasons.append("capability expiry unknown")
            return InterlockResult(not reasons, reasons, d)

        inter.check_external_write = check_external_write  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "aeos.policy.interlock", inter)
    return state


# ---------------------------------------------------------------------------------------------
# In-memory provider


class FakeProvider:
    """Records every call; external state lives in ``store`` keyed by logical id (the marker)."""

    name = "fake"

    def __init__(self) -> None:
        self.store: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail: dict[str, Exception] = {}  # logical_id -> exception raised once on execute
        self.timeout_after_create: set[str] = set()  # create succeeds remotely, response lost
        self.counter = 0

    def execute(self, ctx: OpContext) -> OpResult:
        self.calls.append((ctx.operation, ctx.logical_id))
        if ctx.logical_id in self.fail:
            raise self.fail.pop(ctx.logical_id)
        if ctx.operation.startswith("create") or ctx.operation in ("ensure_label", "upload_attachment"):
            self.counter += 1
            ext = f"ext-{self.counter}"
            self.store[ctx.logical_id] = {"id": ext, "parent": ctx.parent_external_id, "rev": "r1",
                                          "payload": ctx.payload}
            if ctx.logical_id in self.timeout_after_create:
                self.timeout_after_create.discard(ctx.logical_id)
                raise ProviderTimeout("simulated timeout after create")
            return OpResult(external_id=ext, external_url=f"https://example.invalid/{ext}", external_revision="r1",
                            kind="doc")
        if ctx.operation == "update_doc":
            cur = self.store.get(ctx.logical_id)
            if cur is None:
                raise ProviderError("missing")
            cur["rev"] = "r2"
            cur["payload"] = ctx.payload
            return OpResult(external_id=cur["id"], external_revision="r2", kind="doc")
        raise ProviderError("unsupported")

    def reconcile(self, ctx: OpContext) -> OpResult | None:
        self.calls.append(("reconcile", ctx.logical_id))
        cur = self.store.get(ctx.logical_id)
        if cur is None:
            return None
        return OpResult(external_id=cur["id"], external_revision=cur["rev"], kind="doc", response={"reconciled": True})

    def inspect(self, external_id: str):
        from aeos.publisher.providers.base import Inspection

        for cur in self.store.values():
            if cur["id"] == external_id:
                return Inspection(exists=True, revision=cur["rev"], text=cur.get("text", ""))
        return Inspection(exists=False)

    def close(self) -> None:
        pass


def use_fake_providers(monkeypatch: pytest.MonkeyPatch) -> dict[str, FakeProvider]:
    from aeos.publisher import providers

    fakes = {"google": FakeProvider(), "github-issues": FakeProvider(), "confluence": FakeProvider(),
             "jira": FakeProvider()}
    for fam, fake in fakes.items():
        monkeypatch.setitem(providers._overrides, fam, lambda _cfg, f=fake: f)
    return fakes


def seed_personal_secrets(secrets_memory: Any) -> None:
    secrets_memory.put("personal/google-oauth-client", '{"installed": {"client_id": "cid", "client_secret": "cs"}}',
                       actor="test")
    secrets_memory.put("personal/google-oauth-token", '{"token": "ya29.test-token-value-abcdefghijklmnop", '
                                                      '"refresh_token": "1//refresh-abcdefghijklmnopqrstuvwxyz", '
                                                      '"client_id": "cid", "client_secret": "cs", '
                                                      '"token_uri": "https://oauth2.googleapis.com/token", '
                                                      '"scopes": ["https://www.googleapis.com/auth/drive.file"], '
                                                      f'"expiry": "{(utcnow() + timedelta(hours=1)).isoformat()}"}}',
                       actor="test")
    secrets_memory.put("personal/github", "ghp_testtokenabcdefghijklmnopqrstuvwxyz0123", actor="test")


def confirm_of(pub: Any) -> dict[str, str]:
    return {"plan_version_id": pub.plan_version_id, "approved_hash": pub.approved_hash,
            "preview_hash": pub.preview_hash}
