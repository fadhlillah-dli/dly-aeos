"""Project test baseline at the inventoried commit (§05 step 6 "inventaris repo, kontrak, dependensi, baseline tes";
§34 "Baseline ditetapkan sebelum perubahan"; W6-013).

For an existing project the inventory (``aeos.portfolio.inventory``) is read-only and runs nothing. This step runs
the repository's **own existing tests** at the exact ``inventory_commit`` and records the result as a trusted
TEST_REPORT the inventory and planning can cite (``inventory.tests.baseline`` -> ``PlanningContext.repo_inventory``):

* the commit's tree is materialised through git plumbing into a private run directory (never the live checkout);
* candidate-style isolation applies: the same runner selection as the verifier (``runner.select_runner``: the
  container when its boundary is proven, else WEAK; OFFICE requires the container and is BLOCKED without it) and
  the same tamper window (controller checkout/interpreter + DB trust tables) around the project's code;
* the same step discipline as the verifier: every step runs in its own kill group (process runner) and a process
  that outlives its step is killed and makes the baseline TAMPERED (it could act after the window closed); a
  degraded kill group leaves the tamper status UNKNOWN; the pytest step runs under the verifier's result seal
  (``aeos.verification.seal``: a forged or inconsistent junit/exit code is TAMPERED, a missing sealed session end
  leaves the Python part NOT_RUN - no trusted measurement), and a project whose import path ships a top-level
  module that would shadow the test runner, the seal plugin or the standard library is not run under it (NOT_RUN,
  stated - the verifier refuses such candidates the same way);
* Python tests (the inventory's test directories) run with pytest under the verifier-owned ini
  (``-m "not integration and not network"``; the project's own pytest configuration is not used - stated), a
  package with vitest runs ``npm ci --ignore-scripts`` + vitest with the verifier's config (process runner only;
  vitest comes from the project's lockfile, so its report is the project toolchain's own and is not sealed);
* the report stores the commit, the suite identity (selection + hash of the selected test files), the passing,
  failing and skipped test ids, the runner and its residual risks. A step that cannot run is NOT_RUN with the
  concrete reason; a failing test in the baseline is recorded as such (a measurement, not a verdict).

Not done here, stated on every report (``stated``, ``STATED_LIMITS``): foreign trust-table rows found in the window
are reported and audited, not neutralised automatically as the verifier does; the baseline does not pause worker
admission. A tamper window that cannot prove integrity (UNKNOWN) is stated too, and the summary the inventory cites
carries it beside the measurement (``integrity``): a PASS baseline is never an integrity PASS by omission.

Provider CI check-runs for that commit are not imported here (the delivery gates read provider CI on PR heads).
"""

from __future__ import annotations

import shutil
import sys
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
from aeos.verification import collector, runner, seal, tamper
from aeos.verification.procs import GitError, GitReader, SnapshotError
from aeos.verification.steps import parse_report

ORIGIN = "verifier"
SUITE = "project-baseline"
IDENTITY = "aeos-verifier/project-baseline"
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
PYTHON_STEP = "project_baseline"  # the sealed pytest step (runner step list, result seal)
JS_STEP_PREFIX = "project_baseline_"  # kill-group step names of the npm ci / vitest steps
# what this baseline does not do, stated on every report (``stated``)
STATED_LIMITS = (
    "the project's own pytest configuration is not used: the verifier-owned ini selects "
    f"-m \"{MARK_EXPR}\"",
    "vitest comes from the project's lockfile: its report is the project toolchain's own and is not sealed by the "
    "verifier",
    "foreign trust-table rows written during the run are reported and audited only: unlike the verifier, the "
    "baseline does not neutralise them automatically and does not pause worker admission",
)
INTEGRITY_UNKNOWN = ("the tamper window could not prove that the project's code left the controller and the "
                     "control plane untouched (integrity UNKNOWN): the measurement is recorded, its integrity is "
                     "not proven")


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


def shadowing_modules(import_root: Path) -> list[str]:
    """Top-level modules on the project's import path that would replace the test runner, the verifier's seal plugin
    (and what they import before the seal reads its key) or a standard-library module. The project's own test
    packages (``tests``/``test``, ``conftest``: the conventional test directories this baseline runs, never imported
    by pytest's start-up) are expected there and are not counted."""
    from aeos.verification.verifier import HARNESS_MODULES, top_level_modules

    own_tests = {"tests", "test", "conftest"}
    harness = HARNESS_MODULES - own_tests - {"ruff", "xdist"}
    return [n for n in top_level_modules(import_root) if n not in own_tests
            and (n in harness or n in sys.stdlib_module_names or n in sys.builtin_module_names)]


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
    # only the baseline's own reports: the verifier's per-run TEST_REPORTs (several per verification) must never
    # push an existing baseline out of the window
    rows = session.scalars(select(EvidenceArtifact).where(
        EvidenceArtifact.project_id == project_id, EvidenceArtifact.kind == "TEST_REPORT",
        EvidenceArtifact.origin == ORIGIN, EvidenceArtifact.trusted.is_(True),
        EvidenceArtifact.collector_identity == IDENTITY, EvidenceArtifact.subject_type == "repository")
        .order_by(EvidenceArtifact.created_at.desc()).limit(200))
    for ev in rows:
        env = ev.environment or {}
        if env.get("project_baseline_commit") != commit:
            continue
        data = collector.read_json(ev) or {}
        py = data.get("python") or {}
        # the tamper window's verdict beside the measurement: UNKNOWN (a degraded kill group, an unreadable trust-table
        # check, a run that never opened the window) is shown as such - a PASS baseline is not an integrity PASS
        integrity = str((data.get("tamper") or {}).get("status") or "UNKNOWN")
        return {"status": data.get("status", "UNKNOWN"), "commit": commit, "evidence_id": ev.id,
                "integrity": integrity,
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

    def close(self, rn: Any = None) -> dict[str, Any]:
        """The window's verdict, with the runner's kill groups: a process that outlived its step (killed when the
        step ended) could have acted after the window closed - FAIL like a write; a degraded kill group cannot
        prove that none did - UNKNOWN."""
        from aeos.worker_runtime import facade as worker

        if self.before is None:
            changed = [{"path": "*", "change": f"integrity snapshot failed ({self.error})"}]
        else:
            try:
                changed = worker.controller_integrity_compare(self.before)
            except Exception as exc:
                changed = [{"path": "*", "change": f"integrity check failed ({type(exc).__name__})"}]
        db = self.window.close()
        survivors = list(rn.survivors()) if rn is not None else []
        gaps = list(rn.containment_gaps()) if rn is not None else []
        return {"controller_changed": changed[:50], "trust_tables": db,
                "step_containment": {"survivors": survivors[:20], "gaps": gaps[:20]},
                "status": "FAIL" if changed or db.get("status") == "FAIL" or survivors else
                ("UNKNOWN" if db.get("status") == "UNKNOWN" or gaps else "PASS")}


def _tamper_reason(content: dict[str, Any]) -> str:
    t = content.get("tamper") or {}
    what = [w for cond, w in (
        (t.get("controller_changed"), "changed the controller checkout/interpreter"),
        ((t.get("trust_tables") or {}).get("status") == "FAIL", "wrote control-plane records"),
        ((t.get("step_containment") or {}).get("survivors"), "left processes running after its step (killed)"),
        ((content.get("python") or {}).get("status") == "TAMPERED", "forged or broke its sealed test result"))
        if cond]
    return f"project code {' / '.join(what) or 'tampered'} during the baseline run"


def _run_python(rn: Any, snapshot: Path, workdir: Path, dirs: list[str], timeout: float) -> dict[str, Any]:
    import_root = _pythonpath(snapshot)
    shadowed = shadowing_modules(import_root)
    if shadowed:
        rel = import_root.relative_to(snapshot).as_posix() or "."
        return {"status": "NOT_RUN", "shadowed_modules": shadowed[:20],
                "summary": f"{rel} ships top-level module(s) {', '.join(shadowed[:5])} that would replace the test "
                           "runner, the verifier's result seal or the standard library: the tests are not run "
                           "(no sealed measurement)"}
    ini = workdir / "pytest.ini"
    ini.write_text(PYTEST_INI, encoding="utf-8")
    junit = workdir / "r" / "project-baseline.xml"
    junit.parent.mkdir(parents=True, exist_ok=True)
    # the verifier's result seal, loaded with -p before the project's conftest files and test modules are imported
    seal.install_plugin(workdir)
    step_seal = seal.StepSeal.new(workdir, PYTHON_STEP)
    args = ["-m", "pytest", "-p", seal.PLUGIN_MODULE, *[str(snapshot / d) for d in dirs], "-c", str(ini),
            "--rootdir", str(snapshot), "--confcutdir", str(snapshot), "-p", "no:cacheprovider", "-q", "--junitxml",
            str(junit), "-m", MARK_EXPR, "--basetemp", rn.basetemp("pb")]
    res = rn.python(args, pythonpath=import_root, autoload=True, timeout=timeout,
                    seal=step_seal, step=PYTHON_STEP)
    report, parse_error = parse_report(junit)
    consumed = step_seal.discard()
    sealed = seal.check_pytest(step_seal, stdout=res.stdout, returncode=res.returncode, timed_out=res.timed_out,
                               junit_path=junit, report=report, key_consumed=consumed, channel=res.channel)
    if sealed.status == "MISSING" and res.timed_out:
        sealed.problems = []  # the timeout is the finding; a killed session never ends sealed
    out: dict[str, Any] = {"exit_code": res.returncode, "timed_out": res.timed_out, "duration_s": res.duration_s,
                           "parse_error": parse_error, "output_tail": res.tail(3000), "result_seal": sealed.to_dict()}
    if res.timed_out:
        out.update(status="TIMED_OUT", summary=f"project tests timed out after {timeout:.0f}s")
        return out
    if sealed.status == "FAIL":
        out.update(status="TAMPERED", summary="result integrity: " + "; ".join(sealed.problems)[:500])
        return out
    if sealed.status != "PASS":
        out.update(status="NOT_RUN", summary="the verifier's result seal has no trustworthy sealed session end ("
                                             + "; ".join(sealed.problems)[:300] + "): no trusted measurement")
        return out
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
                                                                            "full_name": repo.full_name},
                               "stated": list(STATED_LIMITS)}
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

                js = frontend.run_package_tests(rn, pkg, workdir, prefix=JS_STEP_PREFIX)
                js["package_root"] = pkg.relative_to(snapshot).as_posix() or "."
                js.pop("tests", None)
                content["javascript"] = js
            else:
                content["javascript"] = {"status": "NOT_RUN", "summary": "no package with vitest + package-lock.json"}
        finally:
            content["tamper"] = guard.close(rn)
        if content["tamper"]["status"] == "UNKNOWN":
            content["stated"].append(INTEGRITY_UNKNOWN)
        parts = [content["python"].get("status"), content["javascript"].get("status")]
        if content["tamper"]["status"] == "FAIL" or "TAMPERED" in parts:
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
        session, kind="TEST_REPORT", origin=ORIGIN, collector_identity=IDENTITY, claim=claim,
        content=content, evidence_level=level, subject_type="repository", subject_id=repo.id, project_id=project.id,
        domain=project.domain, environment={"project_baseline_commit": sha, "repository_id": repo.id,
                                            "suite_identity_hash": (content.get("suite_identity") or {}).get(
                                                "files_hash"),
                                            "verifier_runner": rn.kind if rn is not None else None,
                                            "verifier_isolation": rn.isolation if rn is not None else None})
    if status == "TAMPERED":
        audit.record(session, actor_type="system", actor_id=ORIGIN, action="verification.controller_modified",
                     result="FAILED", target_type="repository", target_id=repo.id, project_id=project.id,
                     domain=project.domain, reason=_tamper_reason(content),
                     details={"evidence_id": ev.id, "survivors": ((content.get("tamper") or {}).get(
                         "step_containment") or {}).get("survivors", [])[:20]})
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
