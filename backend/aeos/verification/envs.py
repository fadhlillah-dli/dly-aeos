"""Per-candidate dependency environment (§09 Zone 2 "dependencies dan test environment terisolasi"; W6-031).

When a candidate changes its Python dependency set, the trusted suite also runs in a virtual environment built
in the verifier's private run directory from the **candidate's own lockfile**:

1. ``python -m venv --without-pip`` (the controller interpreter's version) in ``<run dir>/ve``;
2. an install step, separate from every test step: ``pip --python <venv> install --require-hashes
   --only-binary=:all: --no-deps -r <lockfile>`` - hash-pinned wheels only, so no candidate build/setup code
   runs while installing. This is the one step with package-index egress (the process runner does not enforce
   an egress boundary; stated). Requirement files that carry pip options (other indexes, includes, editables)
   are refused instead of honoured;
3. the trusted acceptance suite against the candidate with that interpreter, ``-m "not integration and not
   network"``, the trusted test harness (pytest and its requirements, copied from the controller) first on
   ``PYTHONPATH`` so the candidate's pins cannot replace the grader's runner;
4. the environment's ``dependency_set_hash`` (its distributions) is recorded.

The container runner keeps the verifier image's dependency set (no per-candidate environment there): NOT_RUN,
stated. Lockfiles without hashes, non-pip lock formats and failed installs are NOT_RUN with the reason - the
change then stays a blocking NOT-PROVEN (§09 Zone 2), never a pass.
"""

from __future__ import annotations

import importlib.metadata
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aeos.core.hashing import content_hash
from aeos.verification import runner as runner_mod
from aeos.verification.procs import ProcResult, run_process, scrubbed_env
from aeos.verification.seal import PLUGIN_DIR, StepSeal
from aeos.verification.steps import (
    StepContext,
    StepEvidence,
    StepResult,
    last_json_line,
    pack_text,
    run_pytest,
)

# pip's configured default index unless set (tests point this at a local wheel directory with --no-index)
PIP_INDEX_ARGS: list[str] = []
INSTALL_TIMEOUT = 900.0
HARNESS_ROOT_DISTS = ("pytest",)
_REQ_FILE = re.compile(r"(^|/)requirements[\w.-]*\.(txt|lock)$", re.IGNORECASE)
_NON_PIP_LOCKS = ("poetry.lock", "uv.lock", "Pipfile.lock", "pyproject.toml", "pdm.lock")
_ALLOWED_OPTION = re.compile(r"^--hash[=\s]")
_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
MARK_EXPR = "not integration and not network"


@dataclass
class VenvRunner(runner_mod.ProcessRunner):
    """The process runner with the per-candidate interpreter; the trusted harness directory precedes the venv."""

    interpreter: str = ""
    harness: list[Path] = field(default_factory=list)

    def python(self, args: list[str], *, pythonpath: Path, autoload: bool, timeout: float,
               seal: StepSeal | None = None, step: str = "") -> ProcResult:
        """Like ``ProcessRunner.python`` (own kill group, recorded step, the verifier's result-seal plugin first on
        ``PYTHONPATH`` of a sealed step), with the per-candidate interpreter."""
        wd = self.workdir
        paths = [*([wd / PLUGIN_DIR] if seal is not None else []), pythonpath, *self.harness]
        env = scrubbed_env(pythonpath=paths, home=wd / "h", tmp=wd / "t", data_dir=wd / "d",
                           disable_plugin_autoload=not autoload)
        if seal is not None:
            env.update(seal.child_env(str(seal.keyfile)))
        res = run_process([self.interpreter, *args], cwd=self.cwd(), env=env, timeout=timeout, contain=True)
        self.steps.append({"step": step or (seal.name if seal else ""), "exit_code": res.returncode,
                           "timed_out": res.timed_out, "duration_s": res.duration_s,
                           "containment": res.containment, "survivors": list(res.survivors),
                           "containment_errors": list(res.containment_errors)})
        return res


def requirement_files(snapshot: Path, changed: list[str]) -> tuple[list[str], list[str]]:
    """(pip requirement lockfiles to install, changed Python dependency files that are not pip lockfiles)."""
    pip_files = sorted(p for p in changed if _REQ_FILE.search(p) and snapshot.joinpath(*p.split("/")).is_file())
    if (snapshot / "requirements.lock").is_file() and "requirements.lock" not in pip_files:
        pip_files.insert(0, "requirements.lock")
    other = sorted(p for p in changed if p.rsplit("/", 1)[-1] in _NON_PIP_LOCKS)
    return pip_files, other


def lockfile_problems(path: Path) -> list[str]:
    """Lines pip --require-hashes would refuse, or options the verifier does not honour."""
    problems: list[str] = []
    logical: list[str] = []
    buf = ""
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.split(" #", 1)[0].strip() if not raw.lstrip().startswith("#") else ""
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        if buf.strip():
            logical.append(buf.strip())
        buf = ""
    if buf.strip():
        logical.append(buf.strip())
    for line in logical:
        if line.startswith("-"):
            if not _ALLOWED_OPTION.match(line):
                problems.append(f"pip option not honoured by the verifier: {line.split()[0][:40]}")
            continue
        if "--hash=" not in line and "--hash " not in line:
            problems.append(f"requirement without --hash: {line.split()[0][:60]}")
    return problems


def _closure(roots: tuple[str, ...]) -> list[importlib.metadata.Distribution]:
    from packaging.requirements import Requirement

    seen: dict[str, importlib.metadata.Distribution] = {}
    todo = list(roots)
    while todo:
        name = todo.pop()
        key = name.lower().replace("_", "-")
        if key in seen:
            continue
        try:
            dist = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            continue
        seen[key] = dist
        for req in dist.requires or []:
            try:
                r = Requirement(req)
            except Exception:  # noqa: S112 - unparsable metadata line: skipped
                continue
            if r.marker is None or r.marker.evaluate({"extra": ""}):
                todo.append(r.name)
    return list(seen.values())


def build_harness(dest: Path) -> list[str]:
    """Copy the controller's pytest and its requirements (packages + dist-info) into *dest*."""
    dest.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for dist in _closure(HARNESS_ROOT_DISTS):
        files = list(dist.files or [])
        tops = {str(f).replace("\\", "/").split("/", 1)[0] for f in files}
        for top in sorted(tops):
            if top in ("..", "") or top.endswith(".data"):
                continue
            src = Path(str(dist.locate_file(top)))
            target = dest / top
            if target.exists():
                continue
            if src.is_dir():
                shutil.copytree(src, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            elif src.is_file() and src.suffix == ".py":  # top-level modules (``py.py``); never ``.pth`` hooks
                shutil.copy2(src, target)
        copied.append(f"{dist.metadata['Name']}=={dist.version}")
    return sorted(copied)


def _venv_python(ve: Path) -> Path:
    return ve / ("Scripts" if sys.platform == "win32" else "bin") / ("python.exe" if sys.platform == "win32"
                                                                     else "python")


def run(ctx: StepContext, python_pins_changed: bool) -> StepResult | None:
    """Build the per-candidate environment and run the trusted suite in it (Python dependency changes only)."""
    if not python_pins_changed and not ctx.policy.get("isolated_dependencies"):
        return None
    pip_files, other = requirement_files(ctx.snapshot, ctx.changed)

    def not_run(reason: str, **extra: Any) -> StepResult:
        # stated by the verifier's blocking §09 Zone 2 line, which quotes this summary
        msg = f"per-candidate dependency environment not built: {reason}"
        return StepResult("dependency_env", "NOT_RUN", msg, {"lockfiles": pip_files, **extra})

    if not isinstance(ctx.runner, runner_mod.ProcessRunner):
        return not_run("the container runner uses the verifier image's dependency set (built from its own lock); "
                       "per-candidate environments are built on the process runner only")
    if not pip_files:
        return not_run("no pip requirement lockfile in the candidate"
                       + (f" ({', '.join(other[:3])} need an exported, hash-pinned requirements file)" if other else ""))
    problems = [f"{p}: {msg}" for p in pip_files for msg in lockfile_problems(ctx.snapshot.joinpath(*p.split("/")))]
    if problems:
        return not_run("pip --require-hashes needs every requirement hash-pinned and no extra pip options; "
                       + "; ".join(problems[:3]), problems=problems[:20])
    ve = ctx.workdir / "ve"
    wd = ctx.workdir
    base_env = scrubbed_env(pythonpath=[wd / "c"], home=wd / "h", tmp=wd / "t", data_dir=wd / "d",
                            disable_plugin_autoload=True)
    mk = run_process([sys.executable, "-m", "venv", "--without-pip", str(ve)], cwd=wd, env=base_env, timeout=300)
    py = _venv_python(ve)
    if mk.returncode != 0 or not py.is_file():
        return not_run(f"python -m venv failed (exit {mk.returncode}): {mk.tail(300)}")
    install_env = {k: v for k, v in base_env.items() if k not in _PROXY_KEYS}  # the install step's registry egress
    install_env["PYTHONPATH"] = ""
    reqs = [a for p in pip_files for a in ("-r", str(ctx.snapshot.joinpath(*p.split("/"))))]
    pip = run_process([sys.executable, "-m", "pip", "--python", str(py), "install", "--isolated", "--no-input",
                       "--disable-pip-version-check", "--no-cache-dir", "--require-hashes", "--only-binary=:all:",
                       "--no-deps", *PIP_INDEX_ARGS, *reqs], cwd=wd, env=install_env, timeout=INSTALL_TIMEOUT)
    if pip.timed_out or pip.returncode != 0:
        return not_run(f"pip install --require-hashes failed (exit {pip.returncode}, timed out {pip.timed_out}): "
                       f"{pip.tail(600)}", install_tail=pip.tail(2000))
    try:
        harness = build_harness(wd / "hx")
    except (OSError, importlib.metadata.PackageNotFoundError) as exc:
        return not_run(f"trusted test harness could not be prepared ({type(exc).__name__})")
    # its steps are recorded in the verifier runner's step list, so a process outliving one reaches the tamper
    # window's kill-group check like every other candidate step (aeos.verification.runner)
    vr = VenvRunner(workdir=wd, interpreter=str(py), harness=[wd / "hx"], kind="process+candidate-venv",
                    steps=ctx.runner.steps)
    listing = last_json_line(vr.python(["-c", pack_text("dist_list.py")], pythonpath=wd / "c", autoload=False,
                                       timeout=120, step="dependency_env_listing").stdout)
    pins = [p for p in listing.get("pins") or [] if isinstance(p, str)]
    dep_hash = content_hash(sorted(pins))
    if ctx.trusted_copy is None or ctx.ini is None:
        return not_run("trusted suite copy missing", dependency_set_hash=dep_hash)
    junit = wd / "r" / "venv-junit.xml"
    junit.parent.mkdir(parents=True, exist_ok=True)
    args = ["-m", "pytest", str(ctx.trusted_copy / "tests" / "acceptance"), "-c", str(ctx.ini), "--rootdir",
            str(ctx.trusted_copy), "--confcutdir", str(ctx.trusted_copy), "-p", "no:cacheprovider", "-q",
            "--junitxml", str(junit), "-m", MARK_EXPR, *ctx.basetemp("b8")]
    res, report, parse_error, seal_problems = run_pytest(
        ctx, "dependency_env", args, pythonpath=ctx.snapshot / "backend", autoload=False, junit=junit,
        label="trusted suite in the candidate environment", runner=vr)
    problems = []
    if res.timed_out:
        problems.append(f"trusted suite in the candidate environment timed out after {ctx.timeout:.0f}s")
    elif seal_problems:  # the junit/exit code cannot be trusted (verifier result seal): never a pass
        problems.extend(seal_problems[:5])
    elif report is None:
        problems.append(f"trusted suite in the candidate environment produced no report ({parse_error}; exit "
                        f"{res.returncode})")
    else:
        reported = {c.key for c in report.cases}
        missing = [k for k in ctx.expected if k not in reported]
        if report.failures or report.errors:
            problems.append(f"trusted suite in the candidate environment: {report.failures} failed, "
                            f"{report.errors} error(s)")
        if missing:
            problems.append(f"{len(missing)} trusted test(s) missing from the candidate-environment run")
    skipped = report.skipped if report is not None else 0
    status = "FAIL" if problems else ("NOT_PROVEN" if skipped or report is None or not report.passed else "PASS")
    content = {"suite": "trusted-acceptance-candidate-env", "status": status, "lockfiles": pip_files,
               "dependency_set_hash": dep_hash, "distributions": pins[:500], "harness": harness,
               "install": {"command": "pip install --require-hashes --only-binary=:all: --no-deps",
                           "exit_code": pip.returncode, "duration_s": pip.duration_s,
                           "egress": "package index reachable for this step only; not enforced on the process runner"},
               "report": report.to_dict() if report else None, "exit_code": res.returncode, "timed_out": res.timed_out,
               "parse_error": parse_error, "output_tail": res.tail(3000)}
    ev = StepEvidence("TEST_REPORT", f"Trusted acceptance suite in the per-candidate dependency environment "
                                     f"({dep_hash[:19]}): {status}", content, "E2", "dependency_env",
                      {"candidate_dependency_set_hash": dep_hash})
    details = {"dependency_set_hash": dep_hash, "lockfiles": pip_files, "distributions": len(pins),
               "report": report.to_dict(include_cases=False) if report else None}
    if status == "FAIL":
        return StepResult("dependency_env", "FAIL", problems[0], details, [ev], reasons=problems)
    if status == "NOT_PROVEN":
        msg = (f"trusted suite in the candidate environment did not pass completely ({skipped} skipped); "
               "dependency change NOT-PROVEN")
        return StepResult("dependency_env", "NOT_PROVEN", msg, details, [ev], not_proven=[msg])
    return StepResult("dependency_env", "PASS",
                      f"trusted suite passed in an isolated environment built from {', '.join(pip_files)} "
                      f"(dependency set {dep_hash[:19]}, {len(pins)} distributions)", details, [ev])
