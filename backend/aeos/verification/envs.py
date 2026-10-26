"""Per-candidate dependency environment (§09 Zone 2 "dependencies dan test environment terisolasi"; W6-031).

When a candidate changes its Python dependency set, the trusted suite also runs in a virtual environment built
in the verifier's private run directory from the **candidate's own lockfile**:

1. ``python -m venv --without-pip`` (the controller interpreter's version) in ``<run dir>/ve``;
2. an install step, separate from every test step: ``pip --python <venv> install --require-hashes
   --only-binary=:all: --no-deps -r <lockfile>`` - hash-pinned wheels only, so no candidate build/setup code
   runs while installing. This is the one step with package-index egress (the process runner does not enforce
   an egress boundary; stated). Requirement files that carry pip options (other indexes, includes, editables)
   are refused instead of honoured;
3. the environment's ``dependency_set_hash``: the distributions installed in its ``site-packages``, read by the
   verifier itself from their metadata files right after the install (no process of the candidate environment
   reports it, so nothing it imports can change the listing). Start-up hooks the install brought (``.pth`` files
   with ``import`` lines, which the interpreter runs at start, before the verifier's result seal loads) are listed
   and stated;
4. the trusted acceptance suite against the candidate with that interpreter, ``-m "not integration and not
   network"``, the trusted test harness (pytest and its requirements, copied from the controller) first on
   ``PYTHONPATH`` so the candidate's pins cannot replace the grader's runner, under the verifier's result seal.

The venv creation, the install and the suite each run in their own kill group and are recorded in the verifier
runner's step list (``dependency_env_venv`` - the controller's own stdlib, a trusted-only step -,
``dependency_env_install``, ``dependency_env``): a process left behind is a survivor (QUARANTINED). The venv
creation and the install run the controller interpreter in isolated mode (``-I``: no ``PYTHONPATH``, no working
directory on ``sys.path``), so a module an earlier candidate step left in the run directory cannot replace ``venv``
or ``pip`` - the trusted-only classification of the venv step holds whatever the run directory contains.

The container runner keeps the verifier image's dependency set (no per-candidate environment there): NOT_RUN,
stated. Lockfiles without hashes, non-pip lock formats and failed installs are NOT_RUN with the reason - the
change then stays a blocking NOT-PROVEN (§09 Zone 2), never a pass.
"""

from __future__ import annotations

import importlib.metadata
import re
import shutil
import sys
import sysconfig
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
MAX_METADATA_BYTES = 1024 * 1024
MAX_LISTED_HOOKS = 50


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
        return self.record(step or (seal.name if seal else ""), res)


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


def venv_command(ve: Path) -> list[str]:
    """The venv creation: the controller's own stdlib ``venv`` in isolated mode (``-I`` ignores ``PYTHONPATH`` and
    keeps the working directory off ``sys.path``: nothing in the run directory is imported - a trusted-only step)."""
    return [sys.executable, "-I", "-m", "venv", "--without-pip", str(ve)]


def pip_command(py: Path, reqs: list[str]) -> list[str]:
    """The install: the controller's pip in isolated mode (no module of the run directory can stand in for pip),
    hash-pinned wheels only, no dependency resolution beyond the lockfile."""
    return [sys.executable, "-I", "-m", "pip", "--python", str(py), "install", "--isolated", "--no-input",
            "--disable-pip-version-check", "--no-cache-dir", "--require-hashes", "--only-binary=:all:", "--no-deps",
            *PIP_INDEX_ARGS, *reqs]


def site_dirs(ve: Path) -> list[Path]:
    """The ``site-packages`` directories of a venv this interpreter created (``venv`` install scheme)."""
    base = {"base": str(ve), "platbase": str(ve), "installed_base": str(ve), "installed_platbase": str(ve)}
    out: list[Path] = []
    for key in ("purelib", "platlib"):
        p = Path(sysconfig.get_path(key, scheme="venv", vars=base))
        if p not in out:
            out.append(p)
    return out


def installed_pins(dirs: list[Path]) -> tuple[list[str], list[str]]:
    """(``name==version`` of every distribution installed in *dirs*, problems), read by the verifier from the
    metadata files (no candidate process). A metadata file that is unreadable or oversized is a problem: the
    listing - and so the dependency set identity - is then incomplete."""
    pins: set[str] = set()
    problems: list[str] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for info in sorted(d.iterdir()):
            if not info.is_dir() or not info.name.endswith((".dist-info", ".egg-info")):
                continue
            meta = info / ("METADATA" if info.name.endswith(".dist-info") else "PKG-INFO")
            try:
                if meta.stat().st_size > MAX_METADATA_BYTES:
                    problems.append(f"{info.name}: metadata larger than {MAX_METADATA_BYTES} bytes")
                    continue
                dist = importlib.metadata.PathDistribution(info)
                name = str((dist.metadata.get_all("Name") or [""])[0] or "").strip().lower()
                version = str(dist.version or "").strip()
            except (OSError, ValueError, KeyError, TypeError) as exc:
                problems.append(f"{info.name}: metadata unreadable ({type(exc).__name__})")
                continue
            if name:
                pins.add(f"{name}=={version}")
            else:
                problems.append(f"{info.name}: metadata names no distribution")
    return sorted(pins), problems


def startup_hooks(dirs: list[Path]) -> list[dict[str, Any]]:
    """``.pth`` files in *dirs*: the interpreter processes them when it starts and runs every line that begins with
    ``import`` - before any ``-p`` plugin, so before the verifier's result seal reads its key (stated)."""
    hooks: list[dict[str, Any]] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for pth in sorted(d.glob("*.pth")):
            try:
                # utf-8-sig: the interpreter strips a byte-order mark before it looks for ``import`` lines
                lines = pth.read_bytes()[:MAX_METADATA_BYTES].decode("utf-8-sig", errors="replace").splitlines()
            except OSError:
                lines = ["import ?"]  # unreadable: assume it runs code (fail closed)
            runs = any(ln.startswith(("import ", "import\t")) for ln in lines)
            hooks.append({"file": pth.name, "executes_code": runs})
    return hooks[:MAX_LISTED_HOOKS]


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
    rn = ctx.runner
    base_env = scrubbed_env(pythonpath=[wd / "c"], home=wd / "h", tmp=wd / "t", data_dir=wd / "d",
                            disable_plugin_autoload=True)
    # the venv (controller stdlib, isolated mode) and the install run in their own kill groups, recorded in the
    # runner's step list
    mk = rn.tool(venv_command(ve), cwd=wd, env=base_env, timeout=300, step="dependency_env_venv")
    py = _venv_python(ve)
    if mk.returncode != 0 or not py.is_file():
        return not_run(f"python -m venv failed (exit {mk.returncode}): {mk.tail(300)}")
    install_env = {k: v for k, v in base_env.items() if k not in _PROXY_KEYS}  # the install step's registry egress
    install_env["PYTHONPATH"] = ""
    reqs = [a for p in pip_files for a in ("-r", str(ctx.snapshot.joinpath(*p.split("/"))))]
    pip = rn.tool(pip_command(py, reqs), cwd=wd, env=install_env, timeout=INSTALL_TIMEOUT,
                  step="dependency_env_install")
    if pip.timed_out or pip.returncode != 0:
        return not_run(f"pip install --require-hashes failed (exit {pip.returncode}, timed out {pip.timed_out}): "
                       f"{pip.tail(600)}", install_tail=pip.tail(2000))
    # the dependency set identity is read by the verifier itself, before anything of the environment has run
    sites = site_dirs(ve)
    pins, listing_problems = installed_pins(sites)
    hooks = startup_hooks(sites)
    dep_hash = content_hash(sorted(pins))
    try:
        harness = build_harness(wd / "hx")
    except (OSError, importlib.metadata.PackageNotFoundError) as exc:
        return not_run(f"trusted test harness could not be prepared ({type(exc).__name__})",
                       dependency_set_hash=dep_hash)
    # its steps are recorded in the verifier runner's step list, so a process outliving one reaches the tamper
    # window's kill-group check like every other candidate step (aeos.verification.runner)
    vr = VenvRunner(workdir=wd, interpreter=str(py), harness=[wd / "hx"], kind="process+candidate-venv",
                    steps=rn.steps)
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
    status = "FAIL" if problems else ("NOT_PROVEN" if skipped or report is None or not report.passed
                                      or listing_problems else "PASS")
    running_hooks = [h["file"] for h in hooks if h["executes_code"]]
    stated: list[str] = []
    if running_hooks:
        stated.append(f"the candidate environment has {len(running_hooks)} start-up hook(s) that run code when its "
                      f"interpreter starts ({', '.join(running_hooks[:5])}): they run before the verifier's result "
                      "seal loads, so the seal of the candidate-environment run does not cover them")
    content = {"suite": "trusted-acceptance-candidate-env", "status": status, "lockfiles": pip_files,
               "dependency_set_hash": dep_hash, "distributions": pins[:500], "harness": harness,
               "listing": {"source": "metadata files of the venv site-packages, read by the verifier after the "
                                     "install (no candidate process)", "problems": listing_problems[:20]},
               "startup_hooks": hooks,
               "install": {"command": "pip install --require-hashes --only-binary=:all: --no-deps",
                           "exit_code": pip.returncode, "duration_s": pip.duration_s,
                           "kill_group": pip.containment or "none", "survivors": list(pip.survivors)[:10],
                           "egress": "package index reachable for this step only; not enforced on the process runner"},
               "report": report.to_dict() if report else None, "exit_code": res.returncode, "timed_out": res.timed_out,
               "parse_error": parse_error, "output_tail": res.tail(3000)}
    ev = StepEvidence("TEST_REPORT", f"Trusted acceptance suite in the per-candidate dependency environment "
                                     f"({dep_hash[:19]}): {status}", content, "E2", "dependency_env",
                      {"candidate_dependency_set_hash": dep_hash})
    details = {"dependency_set_hash": dep_hash, "lockfiles": pip_files, "distributions": len(pins),
               "startup_hooks": running_hooks[:10], "report": report.to_dict(include_cases=False) if report else None}
    if status == "FAIL":
        return StepResult("dependency_env", "FAIL", problems[0], details, [ev], reasons=problems, not_proven=stated)
    if status == "NOT_PROVEN":
        msg = (f"dependency set of the candidate environment could not be read completely "
               f"({'; '.join(listing_problems[:2])}); dependency change NOT-PROVEN" if listing_problems else
               f"trusted suite in the candidate environment did not pass completely ({skipped} skipped); "
               "dependency change NOT-PROVEN")
        return StepResult("dependency_env", "NOT_PROVEN", msg, details, [ev], not_proven=[msg, *stated])
    return StepResult("dependency_env", "PASS",
                      f"trusted suite passed in an isolated environment built from {', '.join(pip_files)} "
                      f"(dependency set {dep_hash[:19]}, {len(pins)} distributions)", details, [ev],
                      not_proven=stated)
