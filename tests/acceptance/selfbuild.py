"""Self-build world (§09, §19, §34): a worker attempt on the DLY AEOS platform project, verified by the
REAL independent verifier and reviewed through the REAL bridge (part of the trusted suite; no tests).

* The candidate workspace is created by the worker runtime itself (``create_workspace`` /
  ``collect_changes`` / ``commit_candidate``) from a small source repository; the "worker change" is
  written into that workspace exactly where OpenCode would write it.
* The trusted acceptance suite used by the verifier is a tiny controller grader in a temporary
  controller checkout (``aeos.verification.suite.trusted_controller_root`` is the documented test seam);
  running THIS suite inside the verifier would recurse.
* CAN_REVIEW_WITH_MAX is evaluated by the real Readiness Doctor against the fake host (official claude
  binary present, native Max login) with the capability test answered by the fake claude process.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from tests.acceptance.fakes import FakeClaude, ok
from tests.acceptance.world import PlanningResponder, admit, plan_content, task, worker_ready
from tests.conftest import AuthedClient

# collected with the trusted controller code first (expected ids), so imports of candidate code are lazy
GRADER_PASS = "def test_add():\n    from aeos.feature.calc import add\n\n    assert add(1, 2) == 3\n"
BASE_FILES = {"README.md": "# platform candidate\n", "backend/aeos/__init__.py": "",
              "backend/aeos/feature/__init__.py": ""}
GOOD_CHANGE = {"backend/aeos/feature/calc.py": "def add(a, b):\n    return a + b\n"}
NON_REGRESSION_KEYS = ("existing_behavior", "callers", "contract_consumers", "authn_authz", "fallback_order",
                       "state_data", "recovery", "integration")


def review_output(verdict: str = "PASS-CONTROLLED", findings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "review": {"outcome": "acceptance criteria met", "change": "adds add()", "why": "task T1",
                   "architecture": {"upstream": [], "downstream": []}, "owner_auth": "owner", "blast_radius": "low",
                   "baseline": "no add() before", "contradictions": [], "scope": {"in_scope": True, "notes": ""}},
        "verify": {"claims": [{"claim": "trusted suite passed", "evidence_refs": [], "status": "SUPPORTED"}],
                   "test_ids": ["tests/acceptance/test_grader.py::test_add"],
                   "environment": {"commit": "", "config": ""}, "old_behavior": "", "unknowns": []},
        "non_regression": {k: {"status": "COVERED", "note": "verifier evidence"} for k in NON_REGRESSION_KEYS},
        "verdict": verdict, "verdict_rationale": "independent verifier passed", "findings": findings or [],
    }


class SupervisorResponder:
    """Answers planning stages, the doctor capability test and checkpoint reviews by their schema."""

    def __init__(self, *, content: dict[str, Any] | None = None, review: dict[str, Any] | None = None) -> None:
        self.planning = PlanningResponder(content)
        self.review = review or review_output()
        self.reviews = 0

    def __call__(self, argv: list[str], prompt: str) -> Any:
        schema = FakeClaude.schema_of(argv)
        if str(schema.get("title", "")).startswith("aeos.planning."):
            return self.planning(argv, prompt)
        if "non_regression" in (schema.get("properties") or {}):
            self.reviews += 1
            return self.review
        return {"ok": True}  # doctor capability test


def trusted_controller(tmp_path: Path, monkeypatch: Any, grader: str = GRADER_PASS) -> Path:
    from aeos.verification import suite

    root = tmp_path / "controller"
    (root / "tests" / "acceptance").mkdir(parents=True)
    (root / "backend" / "aeos").mkdir(parents=True)  # the platform's own package (candidates may change it)
    (root / "backend" / "aeos" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "acceptance" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "acceptance" / "test_grader.py").write_text(grader, encoding="utf-8")
    monkeypatch.setattr(suite, "trusted_controller_root", lambda: root)
    return root


def review_ready(client: AuthedClient, host: Any, settings: Any) -> None:
    """Real doctor run: official client present + native login (fake host) + capability test (fake process)."""
    import json

    host.install("claude", "2.1.302 (Claude Code)\n")
    host.outputs[("claude", ("auth", "status", "--json"))] = ok(json.dumps(
        {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"}))
    settings.claude_binary = None
    # only the groups CAN_REVIEW_WITH_MAX reads (other capabilities' results are left untouched)
    assert client.post("/api/doctor/run", json={"group": ["HOST", "RUNTIME", "LOCAL_AUTH"]}).status_code == 200
    assert client.post("/api/doctor/run", json={"group": "CLAUDE_NATIVE", "functional": True}).status_code == 200


def self_build(client: AuthedClient, db: Session, monkeypatch: Any, tmp_path: Path, host: Any, settings: Any, *,
               changes: dict[str, str] | None = None, allowed: list[str] | None = None, grader: str = GRADER_PASS,
               review: dict[str, Any] | None = None, extra_tasks: list[dict[str, Any]] | None = None,
               bypass_runtime_scope: bool = False, targets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Platform-project task leased, worker change committed by the runtime, run in REVIEW.

    The runtime's own bookkeeping after an exit 0 (``executor._finalize``) is reproduced with the
    runtime's real scope check (``worker_runtime.scope.check_scope`` against the platform protected
    paths): protected hits mark the run quarantined. ``bypass_runtime_scope`` skips that first line of
    defence on purpose, for rows that prove the independent verifier catches the candidate on its own."""
    from sqlalchemy import select

    from aeos.core.enums import Domain
    from aeos.models.portfolio import Project
    from aeos.models.workforce import WorkerRun
    from aeos.worker_runtime import workspace as wsmod
    from aeos.worker_runtime.scope import PLATFORM_PROTECTED_PATHS, check_scope
    from aeos.workforce import facade as workforce
    from aeos.workforce import scheduler
    from tests.acceptance.world import git_repo

    responder = SupervisorResponder(review=review)
    tasks = [task("T1", paths=allowed or ["backend/aeos/feature/**"]), *(extra_tasks or [])]
    responder.planning.content = plan_content(tasks=tasks)
    # Self-build tasks that target protected paths are R3 (planning/dag): the owner covers R3 explicitly here.
    w = worker_ready(client, db, monkeypatch, tasks=tasks, platform=True, responder=responder, targets=targets,
                     cover_r3=True)
    run = db.get(WorkerRun, admit(db)[0])
    fake = FakeClaude(monkeypatch, responder)
    controller = trusted_controller(tmp_path, monkeypatch, grader)
    source = tmp_path / "platform-src"
    git_repo(source, BASE_FILES)
    project = db.scalar(select(Project).where(Project.id == run.project_id))
    ws = wsmod.create_workspace(settings.layout, domain=Domain(project.domain), project_key=project.project_key,
                                run_id=run.id, task_key=run.task_key, source_repo=source, base_commit=None,
                                isolated=True)
    for rel, text in (changes if changes is not None else GOOD_CHANGE).items():
        target = ws.repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    collected = wsmod.collect_changes(ws)
    scope = check_scope(collected["changes"], allowed=[*(allowed or ["backend/aeos/feature/**"])], forbidden=[],
                        protected=PLATFORM_PROTECTED_PATHS)
    if not bypass_runtime_scope:
        assert not scope.violations, f"the runtime would fail this attempt (SCOPE_VIOLATION): {scope.violations}"
    # the runtime commits with the owner's identity of the project's code host (executor._commit_identity_record,
    # AGENTS.md invariant 11); without one it would commit as itself and the broker would refuse the push
    from aeos.worker_runtime import executor as runtime_executor

    ident = runtime_executor._commit_identity_record(db, settings, project)
    head = wsmod.commit_candidate(ws, f"aeos: {run.task_key}", identity=(ident.pop("_name"), ident.pop("_email")))
    # what the runtime records after the worker exited 0 (executor._finalize)
    run.workspace_ref, run.git_dir_ref = str(ws.repo), str(ws.git_dir)
    run.base_commit, run.head_commit, run.branch = ws.base_commit, head, ws.branch
    run.exit_code = 0
    run.touched_paths = [c.path for c in collected["changes"]]
    if not bypass_runtime_scope:
        run.quarantined = bool(scope.violations or scope.protected_hits)
        run.quarantine_reasons = [*scope.violations, *scope.protected_hits]
    db.commit()
    workforce.transition_task(db, run.task_id, "RUNNING", reason="worker bound", actor="service:worker_runtime")
    scheduler.set_run_status(db, db.get(WorkerRun, run.id), "RUNNING", reason="worker bound",
                             actor="service:worker_runtime")
    workforce.transition_task(db, run.task_id, "REVIEW", reason="worker exit 0", actor="service:worker_runtime")
    scheduler.set_run_status(db, db.get(WorkerRun, run.id), "REVIEW", reason="exit 0", actor="service:worker_runtime")
    db.commit()
    return {**w, "run_id": run.id, "task_id": run.task_id, "workspace": ws, "head": head, "controller": controller,
            "fake": fake, "responder": responder, "collected": collected}


def verify(client: AuthedClient, run_id: str) -> dict[str, Any]:
    r = client.post(f"/api/runs/{run_id}/verify", json={"request_repair": True})
    assert r.status_code == 200, r.text
    return r.json()


def review(client: AuthedClient, run_id: str) -> dict[str, Any]:
    r = client.post(f"/api/runs/{run_id}/review", json={"reviewer": "FEATURE"})
    assert r.status_code == 200, r.text
    return r.json()


def claude_code_envelope(structured: dict[str, Any], model: str = "claude-opus-5-5") -> dict[str, Any]:
    """What `claude -p --output-format json` prints for the manual package: the result envelope whose
    ``modelUsage`` proves which Claude model answered (§15 — a bare JSON object proves nothing)."""
    import json

    return {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(structured),
            "structured_output": structured, "session_id": "manual-package-session", "duration_ms": 1200,
            "modelUsage": {model: {"inputTokens": 1500, "outputTokens": 900}}}


def manual_review(client: AuthedClient, run_id: str, structured: dict[str, Any] | None = None) -> dict[str, Any]:
    """§12 fallback (the path while the native bridge is not proven - the self-build world runs no Doctor
    capability test): the checkpoint package is run by the owner in official Claude Code and its JSON
    result envelope is submitted back."""
    waiting = review(client, run_id)["review"]
    assert waiting["status"] == "NEEDS_DECISION", waiting
    r = client.post(f"/api/reviews/{waiting['id']}/manual-result",
                    json={"structured": claude_code_envelope(structured or review_output())})
    assert r.status_code == 200, r.text
    return r.json()
