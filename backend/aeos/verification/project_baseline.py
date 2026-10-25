"""Project test baseline at the inventoried commit (§05 step 6 "inventaris repo, kontrak, dependensi, baseline tes";
§34 "Baseline ditetapkan sebelum perubahan"; W6-013).

For an existing project the inventory (``aeos.portfolio.inventory``) is read-only and runs nothing. This step runs
the repository's **own existing tests** at the exact ``inventory_commit`` and records the result as a trusted
TEST_REPORT the inventory and planning can cite (``inventory.tests.baseline`` -> ``PlanningContext.repo_inventory``):

* the commit's tree is materialised through git plumbing into a private run directory (never the live checkout);
* candidate-style isolation applies: the same runner selection as the verifier (``runner.select_runner``: the
  container when its boundary is proven, else WEAK; OFFICE requires the container and is BLOCKED without it) and
  the same tamper window (controller checkout/interpreter + DB trust tables) around the project's code;
* Python tests (the inventory's test directories) run with pytest under the verifier-owned ini
  (``-m "not integration and not network"``; the project's own pytest configuration is not used - stated), a
  package with vitest runs ``npm ci --ignore-scripts`` + vitest with the verifier's config (process runner only);
* the report stores the commit, the suite identity (selection + hash of the selected test files), the passing,
  failing and skipped test ids, the runner and its residual risks. A step that cannot run is NOT_RUN with the
  concrete reason; a failing test in the baseline is recorded as such (a measurement, not a verdict).

Provider CI check-runs for that commit are not imported here (the delivery gates read provider CI on PR heads).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.errors import Blocked
from aeos.core.hashing import content_hash, file_sha256
from aeos.events import service as events
from aeos.models.evidence import EvidenceArtifact
from aeos.verification import collector, runner, tamper
from aeos.verification.procs import GitError, GitReader, SnapshotError
from aeos.verification.steps import parse_report

ORIGIN = "verifier"
SUITE = "project-baseline"
DEFAULT_TIMEOUT = 1800.0
MAX_TIMEOUT = 7200.0
MIN_TIMEOUT = 60.0
MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
MAX_IDS = 5000
MARK_EXPR = "not integration and not network"
PYTEST_INI = """[pytest]
markers =
    integration: needs local tools; skipped when absent
    acceptance: trusted acceptance suite
    network: needs internet access
junit_family = xunit2
junit_logging = no
filterwarnings =
    ignore::DeprecationWarning
"""
_PY_TEST_GLOBS = ("test_*.py", "*_test.py")
NOT_RUN_REASON = ("the project's tests have not been run at this commit; run the project test baseline "
                  "(aeos verify project-baseline <project id> / POST /api/projects/{id}/test-baseline)")


def _identity(snapshot: Path, dirs: list[str]) -> dict[str, Any]:
    files: list[tuple[str, str]] = []
    for d in dirs:
        for pattern in _PY_TEST_GLOBS:
            for p in sorted((snapshot / d).rglob(pattern)):
                if p.is_file() and "node_modules" not in p.parts:
                    files.append((p.relative_to(snapshot).as_posix(), file_sha256(p)))
    for conftest in sorted(snapshot.rglob("conftest.py")):
        rel = conftest.relative_to(snapshot).as_posix()
        if any(rel.startswith(d + "/") for d in dirs) or rel == "conftest.py":
            files.append((rel, file_sha256(conftest)))
    files = sorted(set(files))
    return {"kind": "project-own-tests", "selection": dirs, "mark_expr": MARK_EXPR, "files": len(files),
            "files_hash": content_hash(files),
            "config": "verifier-owned pytest ini; the project's own pytest configuration is not used"}


def python_test_dirs(snapshot: Path, inventory: dict[str, Any] | None) -> list[str]:
    """Top-level test directories of the inventory (or the conventional ones) that hold Python tests."""
    names: list[str] = []
    for d in ((inventory or {}).get("tests") or {}).get("directories") or []:
        top = str(d).split("/", 1)[0]
        if top and top not in names:
            names.append(top)
    for conventional in ("tests", "test"):
        if conventional not in names:
            names.append(conventional)
    out = []
    for name in names:
        root = snapshot / name
        if root.is_dir() and any(next(root.rglob(g), None) is not None for g in _PY_TEST_GLOBS):
            out.append(name)
    return out[:10]


def _pythonpath(snapshot: Path) -> Path:
    for cand in ("backend", "src"):
        if (snapshot / cand).is_dir():
            return snapshot / cand
    return snapshot


def _vitest_package(snapshot: Path) -> Path | None:
    import json

    for pkg in sorted(snapshot.rglob("package.json"))[:20]:
        if "node_modules" in pkg.parts or not (pkg.parent / "package-lock.json").is_file():
            continue
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        if "vitest" in deps:
            return pkg.parent
    return None


def baseline_summary(session: Session, project_id: str, commit: str | None) -> dict[str, Any]:
    """The latest trusted project baseline for *commit* (for ``inventory.tests.baseline``), else NOT_RUN."""
    if not commit:
        return {"status": "NOT_RUN", "commit": None, "reason": "inventory has no commit"}
    rows = session.scalars(select(EvidenceArtifact).where(
        EvidenceArtifact.project_id == project_id, EvidenceArtifact.kind == "TEST_REPORT",
        EvidenceArtifact.origin == ORIGIN, EvidenceArtifact.trusted.is_(True))
        .order_by(EvidenceArtifact.created_at.desc()).limit(200))
    for ev in rows:
        env = ev.environment or {}
        if env.get("project_baseline_commit") != commit:
            continue
        data = collector.read_json(ev) or {}
        py = data.get("python") or {}
        return {"status": data.get("status", "UNKNOWN"), "commit": commit, "evidence_id": ev.id,
                "evidence_level": ev.evidence_level, "checked_at": ev.created_at.isoformat(),
                "suite_identity": data.get("suite_identity"), "passed": py.get("passed"), "failed": py.get("failed"),
                "skipped": py.get("skipped"), "failing_sample": (py.get("failing") or [])[:20],
                "javascript": {k: (data.get("javascript") or {}).get(k) for k in ("status", "passed", "failed")},
                "runner": data.get("runner", {}).get("verifier_runner"),
                "isolation": data.get("runner", {}).get("verifier_isolation"),
                "source": "verifier project baseline (TEST_REPORT evidence)"}
    return {"status": "NOT_RUN", "commit": commit, "reason": NOT_RUN_REASON}


class _Guard:
    """The verifier's tamper window around the project's code (controller files + DB trust tables)."""

    def __init__(self, session: Session):
        from aeos.worker_runtime import facade as worker

        self.before: dict[str, Any] | None = None
        self.error: str | None = None
        try:
            self.before = worker.controller_integrity_snapshot()
        except Exception as exc:  # unverifiable: reported as modified (fail closed)
            self.error = type(exc).__name__
        bind = session.get_bind()
        engine = bind.engine if isinstance(bind, Connection) else bind
        self.window = tamper.Window(engine)

    def close(self) -> dict[str, Any]:
        from aeos.worker_runtime import facade as worker

        if self.before is None:
            changed = [{"path": "*", "change": f"integrity snapshot failed ({self.error})"}]
        else:
            try:
                changed = worker.controller_integrity_compare(self.before)
            except Exception as exc:
                changed = [{"path": "*", "change": f"integrity check failed ({type(exc).__name__})"}]
        db = self.window.close()
        return {"controller_changed": changed[:50], "trust_tables": db,
                "status": "FAIL" if changed or db.get("status") == "FAIL" else
                ("UNKNOWN" if db.get("status") == "UNKNOWN" else "PASS")}


def _run_python(rn: Any, snapshot: Path, workdir: Path, dirs: list[str], timeout: float) -> dict[str, Any]:
    ini = workdir / "pytest.ini"
    ini.write_text(PYTEST_INI, encoding="utf-8")
    junit = workdir / "r" / "project-baseline.xml"
    junit.parent.mkdir(parents=True, exist_ok=True)
    args = ["-m", "pytest", *[str(snapshot / d) for d in dirs], "-c", str(ini), "--rootdir", str(snapshot),
            "--confcutdir", str(snapshot), "-p", "no:cacheprovider", "-q", "--junitxml", str(junit), "-m", MARK_EXPR,
            "--basetemp", rn.basetemp("pb")]
    res = rn.python(args, pythonpath=_pythonpath(snapshot), autoload=True, timeout=timeout)
    report, parse_error = parse_report(junit)
    out: dict[str, Any] = {"exit_code": res.returncode, "timed_out": res.timed_out, "duration_s": res.duration_s,
                           "parse_error": parse_error, "output_tail": res.tail(3000)}
    if res.timed_out:
        out.update(status="TIMED_OUT", summary=f"project tests timed out after {timeout:.0f}s")
    if report is None:
        out.setdefault("status", "NOT_RUN")
        out.setdefault("summary", f"pytest produced no report ({parse_error}; exit {res.returncode})")
        return out
    passing = sorted(c.key for c in report.cases if c.outcome == "passed")
    out.update(passed=report.passed, failed=report.failures + report.errors, skipped=report.skipped,
               passing=passing[:MAX_IDS], failing=sorted(report.failing_keys)[:MAX_IDS],
               skipped_ids=sorted(report.skipped_keys)[:MAX_IDS], truncated=len(passing) > MAX_IDS)
    if "status" not in out:
        out["status"] = "FAIL" if out["failed"] else ("PASS" if report.passed else "NOT_RUN")
        out["summary"] = (f"{report.passed} passed, {out['failed']} failed/errored, {report.skipped} skipped "
                          f"(exit {res.returncode})")
    return out


def run_project_baseline(session: Session, project_id: str, *, actor: str, actor_type: str = "user",
                         principal: Any = None, timeout_seconds: float | None = None) -> dict[str, Any]:
    """Run the project's existing tests at its inventory commit; writes evidence + audit (caller commits)."""
    from aeos.portfolio import facade as portfolio

    project, repo, path = portfolio.repository_checkout(session, project_id, principal=principal,
                                                        action="verification.project_baseline")
    commit = repo.inventory_commit
    if not commit:
        raise Blocked("inventory the repository first: the baseline runs at the inventoried commit",
                      code="INVENTORY_REQUIRED")
    timeout = max(MIN_TIMEOUT, min(float(timeout_seconds or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    try:
        git = GitReader(path)
        sha = git.resolve_commit(commit)
    except GitError as exc:
        raise Blocked(f"the inventory commit cannot be read: {exc}", code="COMMIT_UNRESOLVED") from exc
    workdir = runner.new_workdir()
    content: dict[str, Any] = {"suite": SUITE, "commit": sha, "repository": {"id": repo.id, "provider": repo.provider,
                                                                            "full_name": repo.full_name}}
    status = "NOT_RUN"
    level = "E1"
    rn: Any = None
    try:
        snapshot = workdir / "s"
        snapshot.mkdir()
        try:
            info = git.export_tree(sha, snapshot, max_bytes=MAX_SNAPSHOT_BYTES)
            skipped = info.get("skipped")
            content["snapshot"] = {"files": info.get("files"),
                                   "skipped": len(skipped) if isinstance(skipped, list) else 0}
        except (SnapshotError, GitError) as exc:
            content.update(status="BLOCKED", reason=f"commit could not be materialised safely: {exc}")
            raise _Stop from exc
        try:
            rn = runner.select_runner(session, domain=project.domain, workdir=workdir, run_id=f"baseline-{repo.id}",
                                      policy={}, run_sandbox=None)
        except runner.RunnerError as exc:
            content.update(status="BLOCKED", reason=exc.message, blocked_code=exc.code)
            raise _Stop from exc
        content["runner"] = {**rn.describe(), "residual_risks": list(rn.residual_risks)}
        dirs = python_test_dirs(snapshot, repo.inventory if isinstance(repo.inventory, dict) else None)
        content["suite_identity"] = _identity(snapshot, dirs)
        pkg = _vitest_package(snapshot)
        guard = _Guard(session)
        try:
            content["python"] = (_run_python(rn, snapshot, workdir, dirs, timeout) if dirs else
                                 {"status": "NOT_RUN", "summary": "no Python test directory at this commit"})
            if pkg is not None:
                from aeos.verification import frontend

                js = frontend.run_package_tests(rn, pkg, workdir)
                js["package_root"] = pkg.relative_to(snapshot).as_posix() or "."
                js.pop("tests", None)
                content["javascript"] = js
            else:
                content["javascript"] = {"status": "NOT_RUN", "summary": "no package with vitest + package-lock.json"}
        finally:
            content["tamper"] = guard.close()
        parts = [content["python"].get("status"), content["javascript"].get("status")]
        if content["tamper"]["status"] == "FAIL":
            status = "TAMPERED"
        elif "TIMED_OUT" in parts:
            status = "TIMED_OUT"
        elif "FAIL" in parts:
            status = "FAIL"
        elif "PASS" in parts:
            status = "PASS"
        else:
            status = "NOT_RUN"
        level = "E2" if status in ("PASS", "FAIL") else "E1"
        content["status"] = status
    except _Stop:
        status = str(content.get("status") or "BLOCKED")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        short = runner.short_basetemp(workdir, "pb")
        if short is not None:
            shutil.rmtree(short, ignore_errors=True)
    py = content.get("python") or {}
    claim = (f"Project test baseline of {repo.full_name} at {sha[:12]}: {status} "
             f"({py.get('passed', 0)} passed, {py.get('failed', 0)} failed, {py.get('skipped', 0)} skipped)")
    ev = collector.collect_evidence(
        session, kind="TEST_REPORT", origin=ORIGIN, collector_identity="aeos-verifier/project-baseline", claim=claim,
        content=content, evidence_level=level, subject_type="repository", subject_id=repo.id, project_id=project.id,
        domain=project.domain, environment={"project_baseline_commit": sha, "repository_id": repo.id,
                                            "suite_identity_hash": (content.get("suite_identity") or {}).get(
                                                "files_hash"),
                                            "verifier_runner": rn.kind if rn is not None else None,
                                            "verifier_isolation": rn.isolation if rn is not None else None})
    if status == "TAMPERED":
        audit.record(session, actor_type="system", actor_id=ORIGIN, action="verification.controller_modified",
                     result="FAILED", target_type="repository", target_id=repo.id, project_id=project.id,
                     domain=project.domain, reason="project code changed the controller or control-plane records "
                                                   "during the baseline run", details={"evidence_id": ev.id})
    audit.record(session, actor_type=actor_type, actor_id=actor, action="verification.project_baseline",
                 result="FAILED" if status in ("BLOCKED", "TAMPERED") else "SUCCESS", target_type="repository",
                 target_id=repo.id, project_id=project.id, domain=project.domain,
                 details={"status": status, "commit": sha, "evidence_id": ev.id})
    events.emit(session, source=ORIGIN, kind="verification.project_baseline", entity_type="repository",
                entity_id=repo.id, entity_version=sha, project_id=project.id, domain=project.domain,
                payload={"status": status, "evidence_id": ev.id, "commit": sha})
    return {**baseline_summary(session, project.id, sha), "status": status, "evidence_id": ev.id}


class _Stop(Exception):
    """Internal: the baseline could not start; recorded as BLOCKED evidence."""
