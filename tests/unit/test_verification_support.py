"""Shared helpers for the verification/delivery tests (no tests in this module).

Other modules (workforce, claude_bridge, doctor, broker, planning, policy) are built in parallel; the
tests install small in-memory fakes for their implementation modules through ``sys.modules`` so the
frozen facades resolve to them.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from aeos.core.enums import Domain
from aeos.core.hashing import content_hash
from aeos.models.planning import Plan, PlanVersion, TaskSpec
from aeos.models.portfolio import Project, RepositoryReference
from aeos.models.workforce import WorkerRun

GIT = shutil.which("git")
requires_git = pytest.mark.skipif(GIT is None, reason="git executable not available")


def git(repo: Path, *args: str) -> str:
    cp = subprocess.run(
        [GIT or "git", "-c", "user.name=AEOS Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return cp.stdout.strip()


def write_files(root: Path, files: dict[str, str | None]) -> None:
    for rel, text in files.items():
        p = root / rel
        if text is None:
            if p.exists():
                p.unlink()
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")


GOOD_CALC = "def add(a, b):\n    return a + b\n"
BUGGY_CALC = "def add(a, b):\n    return a - b\n"
TRUSTED_TEST = (
    "from calc import add\n\n\n"
    "def test_add():\n    assert add(2, 3) == 5\n\n\n"
    "def test_add_negative():\n    assert add(-1, 1) == 0\n"
)
OWN_TEST = "from calc import add\n\n\ndef test_own():\n    assert add(1, 1) == 2\n"

BASE_FILES: dict[str, str | None] = {
    "backend/calc.py": GOOD_CALC,
    "tests/__init__.py": "",
    "tests/unit/__init__.py": "",
    "tests/unit/test_own.py": OWN_TEST,
    "tests/acceptance/__init__.py": "",
    "tests/acceptance/test_calc.py": TRUSTED_TEST,
    "docs/readme.md": "# demo\n",
}


def make_controller(root: Path, *, extra: dict[str, str | None] | None = None) -> Path:
    """A tiny trusted controller checkout with its own acceptance suite."""
    files = {
        "backend/calc.py": GOOD_CALC,
        "tests/__init__.py": "",
        "tests/acceptance/__init__.py": "",
        "tests/acceptance/test_calc.py": TRUSTED_TEST,
    }
    files.update(extra or {})
    write_files(root, files)
    return root


@dataclass
class World:
    project: Project
    plan: Plan
    version: PlanVersion
    task: TaskSpec
    run: WorkerRun


def make_world(
    db, *, domain: str = "PERSONAL", is_platform: bool = True, project_key: str = "dly-aeos",
    allowed_paths: list[str] | None = None, forbidden_paths: list[str] | None = None,
    verifier_policy: dict[str, Any] | None = None, evidence_requirements: list[Any] | None = None,
    risk_class: str = "R1", run_state: str = "REVIEW", workspace: str | None = None,
    base: str | None = "0" * 40, head: str | None = "1" * 40, mode: str = "LIVE", repo: str | None = None,
) -> World:
    project = Project(project_key=project_key, name=project_key, domain=domain, is_platform=is_platform)
    db.add(project)
    db.flush()
    if repo:
        db.add(RepositoryReference(project_id=project.id, provider="GITHUB", full_name=repo,
                                   default_branch="main", role="platform" if is_platform else "project"))
    plan = Plan(project_id=project.id, domain=domain, title="plan")
    db.add(plan)
    db.flush()
    content = {"title": "plan", "tasks": ["t1"]}
    version = PlanVersion(plan_id=plan.id, project_id=project.id, domain=domain, version=1, content=content,
                          manifest_hash=content_hash(content))
    db.add(version)
    db.flush()
    spec = {"task_key": "t1", "allowed": allowed_paths}
    task = TaskSpec(
        plan_version_id=version.id, plan_version_hash=version.manifest_hash, project_id=project.id, domain=domain,
        task_key="t1", title="Implement slice", goal="Add a feature", base_commit=base,
        allowed_paths=allowed_paths if allowed_paths is not None else ["backend/**", "tests/unit/**", "docs/**"],
        forbidden_paths=forbidden_paths or [], verifier_policy=verifier_policy or {},
        evidence_requirements=evidence_requirements or [], risk_class=risk_class,
        acceptance_criteria=["add works"], spec_hash=content_hash(spec),
    )
    db.add(task)
    db.flush()
    run = WorkerRun(task_id=task.id, project_id=project.id, domain=domain, attempt=1, execution_status=run_state,
                    workspace_ref=workspace, base_commit=base, head_commit=head, branch="aeos/t1-attempt-1",
                    worker_reported_status="DONE", exit_code=0, mode=mode)
    db.add(run)
    db.flush()
    db.commit()
    return World(project, plan, version, task, run)


def make_candidate(settings, world: World, *, head_files: dict[str, str | None], name: str = "run-1",
                   base_files: dict[str, str | None] | None = None) -> tuple[Path, str, str]:
    """Create a git candidate inside the project's task-data dir; record base/head on run+task."""
    layout = settings.layout
    cand = layout.task_data(Domain(world.project.domain), world.project.project_key) / name
    cand.mkdir(parents=True)
    git(cand, "init", "-q")
    write_files(cand, base_files or BASE_FILES)
    git(cand, "add", "-A")
    git(cand, "commit", "-q", "-m", "base")
    base = git(cand, "rev-parse", "HEAD")
    write_files(cand, head_files)
    git(cand, "add", "-A")
    git(cand, "commit", "-q", "--allow-empty", "-m", "head")
    head = git(cand, "rev-parse", "HEAD")
    return cand, base, head


def bind_candidate(db, world: World, cand: Path, base: str, head: str) -> None:
    world.run.workspace_ref = str(cand)
    world.run.base_commit = base
    world.run.head_commit = head
    db.commit()


@dataclass
class FakeWorkforce:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeWorkforce:
        mod = types.ModuleType("aeos.workforce.scheduler")

        def update_run(session, run_id, *, actor, **fields):
            self.calls.append(("update_run", {"run_id": run_id, "actor": actor, **fields}))
            run = session.get(WorkerRun, run_id)
            for k, v in fields.items():
                setattr(run, k, v)
            session.flush()
            return run

        def transition_task(session, task_id, to_state, *, reason, actor, expected_from=None, reason_code=""):
            self.calls.append(("transition_task", {"task_id": task_id, "to": to_state, "reason": reason,
                                                   "expected_from": expected_from, "reason_code": reason_code}))
            return types.SimpleNamespace(task_id=task_id, state=to_state)

        def reopen_for_reverification(session, task_id, *, reason, actor):
            self.calls.append(("reopen_for_reverification", {"task_id": task_id, "reason": reason, "actor": actor}))
            return types.SimpleNamespace(task_id=task_id, state="READY")

        def request_repair(session, task_id, *, findings, review_record_id, actor):
            self.calls.append(("request_repair", {"task_id": task_id, "findings": findings,
                                                  "review_record_id": review_record_id, "actor": actor}))
            return types.SimpleNamespace(task_id=task_id, state="READY")

        def mark_accepted(session, task_id, run_id, *, acceptance_id, actor):
            self.calls.append(("mark_accepted", {"task_id": task_id, "run_id": run_id,
                                                 "acceptance_id": acceptance_id, "actor": actor}))
            return types.SimpleNamespace(task_id=task_id, state="DONE")

        mod.update_run = update_run
        mod.transition_task = transition_task
        mod.request_repair = request_repair
        mod.mark_accepted = mark_accepted
        mod.reopen_for_reverification = reopen_for_reverification
        monkeypatch.setitem(sys.modules, "aeos.workforce.scheduler", mod)
        return self

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def of(self, name: str) -> list[dict[str, Any]]:
        return [c[1] for c in self.calls if c[0] == name]


def install_module(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def remove_module(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make ``import name`` raise ImportError (simulates a module not built yet)."""
    monkeypatch.setitem(sys.modules, name, None)


def fake_secret(prefix: str = "gh" + "p_", n: int = 36) -> str:
    """Credential-shaped test value built at runtime so repository secret scans stay clean."""
    return prefix + "A1b2C3d4E5" * (n // 10 + 1)


HEAD = "a" * 40
BASE = "b" * 40


def record_verification(db, world: World, *, status: str = "PASS", head: str = HEAD, base: str = BASE,
                        artifact_hash: str = "sha256:" + "c" * 64, changed: list[str] | None = None,
                        mode: str = "LIVE", config_hash: str | None = None, ruff: str = "PASS", leak: str = "PASS"):
    """Record evidence the way the verifier does (origin verifier) without running subprocesses."""
    from aeos.verification import collector
    from aeos.verification.scope import check_scope
    from aeos.verification.verifier import current_config_hash

    changed = changed if changed is not None else ["backend/extra.py"]
    world.run.head_commit = head
    world.run.base_commit = base
    env = {"commit": head, "base_commit": base, "tree": "d" * 40, "artifact_hash": artifact_hash,
           "config_hash": config_hash or current_config_hash(world.task), "suite_hash": "sha256:" + "f" * 64,
           "verifier": "aeos-verifier/test"}
    scope = check_scope(changed, allowed=["**"], forbidden=[], protected=[])
    common = dict(origin="verifier", collector_identity="aeos-verifier/test", subject_type="run",
                  subject_id=world.run.id, project_id=world.project.id, domain=world.project.domain,
                  run_id=world.run.id, task_id=world.task.id, environment=env, mode=mode)
    sc = collector.collect_evidence(db, kind="SCOPE_CHECK", claim="scope", content={**scope.to_dict(),
                                    "name_status": [{"status": "A", "path": p} for p in changed], "numstat": {}},
                                    evidence_level="E1", **common)
    df = collector.collect_evidence(db, kind="DIFF", claim="diff", content="+++ b/backend/extra.py\n+X = 1\n",
                                    evidence_level="E1", **common)
    tr = collector.collect_evidence(db, kind="TEST_REPORT", claim="trusted suite",
                                    content={"suite": "trusted-acceptance", "status": status}, evidence_level="E2",
                                    **common)
    report = collector.collect_evidence(
        db, kind="VERIFICATION_REPORT", claim=f"verification {status}", evidence_level="E2",
        content={"status": status, "summary": "test", "trusted_suite_status": status, "ruff_status": ruff,
                 "scope_status": "PASS", "leak_scan_status": leak, "tests_passed": 3, "tests_failed": 0,
                 "tests_skipped": 0, "docs_only": scope.docs_only, "impact": "HIGH" if scope.high_impact else "NORMAL",
                 "security_sensitive": bool(scope.security_sensitive), "evidence_refs": [sc.id, df.id, tr.id],
                 "not_proven": [], "residual_risks": ["same-user process"]}, **common)
    db.commit()
    return report


NR_COVERED = {k: {"status": "COVERED", "note": "ok"} for k in (
    "existing_behavior", "callers", "contract_consumers", "authn_authz", "fallback_order", "state_data", "recovery",
    "integration")}


def make_review(db, world: World, report, *, verdict: str = "PASS-CONTROLLED", status: str = "COMPLETED",
                mode: str = "LIVE", target_hash: str | None = None, kind: str = "CLAUDE",
                non_regression: dict[str, Any] | None = None):
    from aeos.models.evidence import ReviewRecord

    rec = ReviewRecord(target_type="worker_run", target_id=world.run.id,
                       target_hash=target_hash or report.environment["artifact_hash"], project_id=world.project.id,
                       domain=world.project.domain, run_id=world.run.id, task_id=world.task.id,
                       reviewer_kind=kind, verdict=verdict, status=status, mode=mode,
                       non_regression=non_regression if non_regression is not None else NR_COVERED)
    db.add(rec)
    db.commit()
    return rec


def review_payload(verdict: str = "PASS-CONTROLLED", findings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    nr = {k: {"status": "COVERED", "note": "ok"} for k in (
        "existing_behavior", "callers", "contract_consumers", "authn_authz", "fallback_order", "state_data",
        "recovery", "integration")}
    return {
        "review": {"outcome": "adds mul()", "change": "new module", "why": "feature", "architecture":
                   {"upstream": [], "downstream": []}, "owner_auth": "owner", "blast_radius": "low",
                   "baseline": "main", "contradictions": [], "scope": {"in_scope": True, "notes": ""}},
        "verify": {"claims": [{"claim": "tests pass", "evidence_refs": ["x"], "status": "SUPPORTED"}],
                   "test_ids": ["tests.acceptance.test_calc::test_add"], "environment": {"commit": HEAD, "config": "c"},
                   "old_behavior": "unchanged", "unknowns": []},
        "non_regression": nr,
        "verdict": verdict,
        "verdict_rationale": "evidence supports the change",
        "findings": findings or [],
    }


@dataclass
class FakeBridge:
    result: Any = None
    requests: list[Any] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    manual_result: Any = None

    def install(self, monkeypatch: pytest.MonkeyPatch, *, manual: bool = True) -> FakeBridge:
        def run_structured(request):
            self.requests.append(request)
            return self.result

        install_module(monkeypatch, "aeos.claude_bridge.bridge", run_structured=run_structured,
                       capability_test=lambda: {"status": "TEST"})
        if manual:
            def write_manual_package(request, *, package_id):
                self.packages.append(package_id)
                return Path("manual") / package_id

            install_module(monkeypatch, "aeos.claude_bridge.manual", write_manual_package=write_manual_package,
                           read_manual_result=lambda package_id: self.manual_result)
        else:
            remove_module(monkeypatch, "aeos.claude_bridge.manual")
        return self


def claude_result(structured: dict[str, Any] | None, *, ok: bool = True, quota: bool = False, mode: str = "NATIVE",
                  model: str | None = "claude-fable-5-1", error_code: str | None = None):
    from aeos.claude_bridge.facade import ClaudeResult

    return ClaudeResult(ok=ok, structured=structured, text="", model_requested="fable", model_resolved=model,
                        engine_version="2.1.0", session_id="s1", usage={}, duration_ms=5, error_code=error_code,
                        quota_exhausted=quota, mode=mode)


def install_doctor(monkeypatch: pytest.MonkeyPatch, status: str = "READY", reasons: list[str] | None = None) -> None:
    from aeos.doctor.facade import CapabilityState

    def capability_state(session, capability, *, domain=None, project_id=None, **_kw):
        return CapabilityState(capability=capability, status=status, reasons=reasons or [])

    install_module(monkeypatch, "aeos.doctor.capabilities", capability_state=capability_state,
                   invalidate=lambda session, *, invalidator, reason="": 0)


def install_planning(monkeypatch: pytest.MonkeyPatch, approved: Any) -> None:
    install_module(monkeypatch, "aeos.planning.service", get_approved_version=lambda session, plan_id: approved)
