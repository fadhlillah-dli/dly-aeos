"""Freshness of what ``aeos serve`` serves after a ``git pull`` (owner request R28, LESSONS L23).

``git pull`` updates the source, but ``frontend/dist`` is a build output and the running server keeps the code it
imported at start. This module is the single owner of both questions:

* **UI bundle** (``bundle_state``): in a git checkout the Vite build record ``dist/aeos-build.json`` must name the
  committed ``frontend/`` tree of HEAD and a clean build; the comparison is ``release.ui_correspondence`` with
  commit ``HEAD`` (one implementation for checkouts and release builds). An extracted release uses the verdict its
  provenance recorded at build time (the same function, run when the release was built). No record, a dirty build or
  no git -> UNVERIFIED, never FRESH.
* **Self-heal at start** (``ensure_ui_fresh``): ``aeos serve`` rebuilds a STALE bundle once, only when
  ``frontend/node_modules`` was installed from the current lockfile and ``frontend/`` has no local change; it never
  runs ``npm ci``/``npm install`` (AGENTS.md: no auto-install) and never blocks the start.
* **Code drift** (``code_drift``): ``aeos serve`` records the commit it loaded (``<data>/run/serve.json``); a later
  HEAD that changes backend-relevant paths means the server runs old code until it is restarted.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil

from aeos.core.config import Settings
from aeos.core.errors import AeosError
from aeos.core.timeutil import utcnow
from aeos.operations._util import read_json, write_json_atomic

UI_BUILD_TIMEOUT_SECONDS = 300
UI_BUILD_LOG = "ui-build.log"
RUN_FILE = Path("run") / "serve.json"
# Paths whose change needs a server restart; everything else (docs, frontend source, CI files) does not.
BACKEND_PATHS = ("backend", "requirements.lock", "pyproject.toml")

# (argv, cwd, timeout seconds, log file) -> exit code; raises subprocess.TimeoutExpired / OSError.
BuildRunner = Callable[[list[str], Path, float, Path], int]

# In-process record of the serve start per data dir (the reload child reads the run file instead).
SERVE_STATE: dict[str, dict[str, Any]] = {}


def _git(repo: Path, *args: str, timeout: float = 30) -> str | None:
    """stdout of ``git -C repo args`` or None on any failure (no git, not a checkout, unknown object)."""
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout,
                              check=False, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def frontend_head_tree(repo: Path) -> str | None:
    """``git rev-parse HEAD:frontend`` (the committed frontend source tree), None outside a git checkout."""
    return _git(repo, "rev-parse", "HEAD:frontend")


# ----------------------------------------------------------------------------------------- UI bundle


def bundle_state(settings: Settings, repo: Path, dist: Path | None = None) -> dict[str, Any]:
    """Whether the Control Center bundle in *dist* (default ``<repo>/frontend/dist``) belongs to the code in *repo*.

    ``status``: FRESH | STALE | UNVERIFIED | MISSING; ``basis``: checkout | release | none; ``reason`` explains
    every status other than FRESH."""
    from aeos.operations import release

    dist = dist if dist is not None else repo / "frontend" / "dist"
    if not (dist / "index.html").is_file():
        return {"status": "MISSING", "basis": "none", "reason": "no built bundle (frontend/dist/index.html)"}
    release_id = release.release_id_of_root(settings, repo)
    if release_id is not None:
        try:
            prov = release.load_provenance(settings, release_id)
        except AeosError:
            prov = {}
        ui = release._ui_summary(prov.get("ui"))
        if ui is None:
            return {"status": "UNVERIFIED", "basis": "release", "release_id": release_id,
                    "reason": "the release provenance records no UI bundle"}
        detail = {"basis": "release", "release_id": release_id, "bundle_frontend_tree": ui.get("frontend_tree")}
        if ui.get("commit_correspondence") == "VERIFIED":
            return {"status": "FRESH", **detail}
        return {"status": "UNVERIFIED", **detail,
                "reason": f"release UI bundle correspondence {ui.get('commit_correspondence')}: {ui.get('reason')}"}
    head_tree = frontend_head_tree(repo)
    if head_tree is None:
        return {"status": "UNVERIFIED", "basis": "none",
                "reason": "not a git checkout (no HEAD:frontend): the bundle cannot be compared with a commit"}
    try:
        corr, detail = release.ui_correspondence(repo, "HEAD", dist)
    except AeosError as exc:
        return {"status": "UNVERIFIED", "basis": "checkout", "head_frontend_tree": head_tree,
                "reason": f"{exc.code}: {exc.message}"}
    out: dict[str, Any] = {"basis": "checkout", "head_frontend_tree": head_tree,
                           "bundle_frontend_tree": detail.get("bundle_frontend_tree"),
                           "built_at": detail.get("built_at"), "clean": detail.get("clean")}
    if corr == "VERIFIED":
        return {"status": "FRESH", **out}
    if corr == "MISMATCH":
        bundle = str(detail.get("bundle_frontend_tree") or "none")[:12]
        return {"status": "STALE", **out, "reason": f"bundle built from frontend tree {bundle}, HEAD has "
                                                     f"{head_tree[:12]}"}
    return {"status": "UNVERIFIED", **out, "reason": str(detail.get("reason") or "no build record")}


def lockfile_matches_install(frontend: Path) -> tuple[bool, str]:
    """Whether ``node_modules`` was installed from the current ``package-lock.json``.

    npm writes ``node_modules/.package-lock.json`` (the hidden lockfile) on install. Every installed package must
    have the version and integrity the lockfile names, nothing may be installed that the lockfile no longer has, and
    a lockfile package missing from the install must be optional (platform binaries of other hosts)."""
    try:
        lock = json.loads((frontend / "package-lock.json").read_text(encoding="utf-8")).get("packages") or {}
        hidden = json.loads((frontend / "node_modules" / ".package-lock.json").read_text(encoding="utf-8"))
        installed = hidden.get("packages") or {}
    except (OSError, ValueError, AttributeError) as exc:
        return False, f"cannot compare node_modules with package-lock.json ({type(exc).__name__})"
    wanted = {k: v for k, v in lock.items() if k}
    extra = sorted(set(installed) - set(wanted))
    if extra:
        return False, f"node_modules has packages the lockfile no longer lists ({extra[0]})"
    for name, entry in wanted.items():
        have = installed.get(name)
        if have is None:
            if not (entry.get("optional") or entry.get("devOptional")):
                return False, f"lockfile package not installed ({name})"
            continue
        if have.get("version") != entry.get("version") or have.get("integrity") != entry.get("integrity"):
            return False, f"installed {name} differs from the lockfile"
    return True, "node_modules matches package-lock.json"


def frontend_source_clean(repo: Path) -> tuple[bool, str]:
    status = _git(repo, "status", "--porcelain", "--untracked-files=normal", "--", "frontend")
    if status is None:
        return False, "git status of frontend/ failed"
    if status:
        return False, "frontend/ has local changes (a build would not match a commit)"
    return True, "frontend/ is clean"


def _run_build(argv: list[str], cwd: Path, timeout: float, log_path: Path) -> int:
    """Run the build with its output in *log_path*; on timeout the whole process tree is killed."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as log:
        proc = subprocess.Popen(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                parent = psutil.Process(proc.pid)
                procs = [*parent.children(recursive=True), parent]
                for p in procs:
                    try:
                        p.kill()
                    except psutil.Error:
                        pass
                psutil.wait_procs(procs, timeout=5)
            except psutil.Error:
                pass
            raise


# The runner and the PATH lookup ``ensure_ui_fresh`` uses; tests replace them (tests/conftest.py refuses a real
# npm build).
BUILD_RUNNER: BuildRunner = _run_build
WHICH: Callable[[str], str | None] = shutil.which


def _ui_record(status: str, state: dict[str, Any], reason: str | None = None, **extra: Any) -> dict[str, Any]:
    """What /api/health shows (no absolute path: the log is named relative to the data dir)."""
    rec: dict[str, Any] = {"status": status, "reason": reason if reason is not None else state.get("reason"),
                           "basis": state.get("basis"), "bundle_frontend_tree": state.get("bundle_frontend_tree"),
                           "head_frontend_tree": state.get("head_frontend_tree"), "checked_at": utcnow().isoformat()}
    rec.update(extra)
    return rec


def ensure_ui_fresh(settings: Settings, repo: Path, *, remedy: str, enabled: bool = True,
                    which: Callable[[str], str | None] | None = None, timeout: float = UI_BUILD_TIMEOUT_SECONDS,
                    echo: Callable[[str], None] = print) -> dict[str, Any]:
    """Rebuild a STALE bundle once before the server starts, when that is safe; never raises, never blocks.

    *remedy* is the operator's way out, printed and reported when the bundle stays STALE.

    Returns the ``ui_build`` record: FRESH (nothing to do), REBUILT, STALE (not rebuilt or the build failed; the old
    bundle is served), UNVERIFIED or MISSING (left as they are, with the reason)."""
    try:
        state = bundle_state(settings, repo)
    except Exception as exc:  # never block the start on the check itself
        return _ui_record("UNVERIFIED", {}, f"freshness check failed ({type(exc).__name__})")
    if state["status"] != "STALE":
        return _ui_record(state["status"], state)
    frontend = repo / "frontend"
    blocked: str | None = None
    npm = (which or WHICH)("npm")  # Windows: npm.cmd through PATHEXT
    if not enabled:
        blocked = "--no-ui-build"
    elif not (frontend / "node_modules").is_dir():
        blocked = "frontend/node_modules is missing (npm ci is never run automatically)"
    else:
        ok, why = lockfile_matches_install(frontend)
        if not ok:
            blocked = f"{why}; run npm ci first"
        else:
            ok, why = frontend_source_clean(repo)
            if not ok:
                blocked = why
            elif npm is None:
                blocked = "npm not found on PATH"
    if blocked is not None:
        reason = f"{state['reason']}; not rebuilt: {blocked}"
        echo(f"WARNING: Control Center bundle is STALE ({reason}). Serving the old bundle. Remedy: {remedy}")
        return _ui_record("STALE", state, reason, remedy=remedy)
    log_path = settings.layout.logs / UI_BUILD_LOG
    echo(f"Control Center bundle is stale ({state['reason']}); running 'npm run build' once "
         f"(timeout {int(timeout)}s, log <data>/logs/{UI_BUILD_LOG}).")
    failure: str | None = None
    try:
        code = BUILD_RUNNER([str(npm), "run", "build"], frontend, timeout, log_path)
        if code != 0:
            failure = f"npm run build exited with {code}"
    except subprocess.TimeoutExpired:
        failure = f"npm run build timed out after {int(timeout)}s"
    except Exception as exc:
        failure = f"npm run build could not run ({type(exc).__name__}: {exc})"
    if failure is None:
        after = bundle_state(settings, repo)
        if after["status"] == "FRESH":
            return _ui_record("REBUILT", after, None, log=f"<data>/logs/{UI_BUILD_LOG}")
        failure = f"the build finished but the bundle is {after['status']}: {after.get('reason')}"
    reason = f"{state['reason']}; rebuild failed: {failure}"
    echo(f"WARNING: {reason}. Serving the old bundle. Log: <data>/logs/{UI_BUILD_LOG}. Remedy: {remedy}")
    return _ui_record("STALE", state, reason, remedy=remedy, log=f"<data>/logs/{UI_BUILD_LOG}")


# ----------------------------------------------------------------------------------------- serve record / drift


def run_file(settings: Settings) -> Path:
    return settings.layout.root / RUN_FILE


def record_serve_start(settings: Settings, repo: Path, *, ui_build: dict[str, Any] | None,
                       reload: bool = False) -> dict[str, Any]:
    """Record which commit this ``aeos serve`` process loaded (checkout mode), for ``code_drift`` and health."""
    try:
        create_time: float | None = psutil.Process().create_time()
    except psutil.Error:
        create_time = None
    rec: dict[str, Any] = {"pid": os.getpid(), "create_time": create_time, "port": settings.port,
                           "started_at": utcnow().isoformat(), "mode": "checkout", "repo": str(repo.resolve()),
                           "commit": _git(repo, "rev-parse", "HEAD"), "reload": reload, "ui_build": ui_build}
    SERVE_STATE[str(settings.data_dir)] = rec
    try:
        write_json_atomic(run_file(settings), rec)
    except OSError:
        pass  # the in-process record still serves health; code_drift then says NOT_APPLICABLE
    return rec


def _alive(rec: dict[str, Any]) -> bool:
    try:
        proc = psutil.Process(int(rec.get("pid") or 0))
        created = proc.create_time()
    except (psutil.Error, ValueError, TypeError):
        return False
    recorded = rec.get("create_time")
    return recorded is None or abs(float(recorded) - created) < 1.0


def serve_record(settings: Settings) -> dict[str, Any] | None:
    """This process's serve record: in-process, or the run file when it names this process or its parent (the
    ``--reload`` worker is a child of the ``aeos serve`` process)."""
    rec = SERVE_STATE.get(str(settings.data_dir))
    if rec is not None:
        return rec
    rec = read_json(run_file(settings))
    if rec and rec.get("pid") in (os.getpid(), os.getppid()):
        return rec
    return None


def health_ui_build(settings: Settings) -> dict[str, Any]:
    rec = serve_record(settings)
    ui = (rec or {}).get("ui_build")
    if isinstance(ui, dict):
        return {k: ui.get(k) for k in ("status", "reason", "basis", "bundle_frontend_tree", "head_frontend_tree",
                                       "checked_at", "remedy", "log")}
    return {"status": "NOT_RECORDED", "reason": "this server was not started by 'aeos serve' from a checkout"}


def code_drift(settings: Settings) -> dict[str, Any]:
    """Does the running ``aeos serve`` (checkout mode) run the code its checkout's HEAD has?

    ``status``: CURRENT | OUTSIDE_BACKEND (HEAD moved, no backend-relevant change) | DRIFT | NOT_RUNNING | RELOAD |
    UNKNOWN."""
    rec = read_json(run_file(settings))
    if not rec:
        return {"status": "NOT_RUNNING", "reason": "no 'aeos serve' start is recorded in this data dir"}
    base = {"loaded_commit": rec.get("commit"), "started_at": rec.get("started_at"), "pid": rec.get("pid")}
    if not _alive(rec):
        return {"status": "NOT_RUNNING", **base, "reason": "the recorded 'aeos serve' process is not running"}
    if rec.get("reload"):
        return {"status": "RELOAD", **base, "reason": "--reload reloads changed backend code itself"}
    repo = Path(str(rec.get("repo") or ""))
    head = _git(repo, "rev-parse", "HEAD") if rec.get("repo") else None
    loaded = rec.get("commit")
    out = {**base, "head_commit": head}
    if not loaded or not head:
        return {"status": "UNKNOWN", **out, "reason": "the loaded or the current commit cannot be read (not a git "
                                                      "checkout?)"}
    if loaded == head:
        return {"status": "CURRENT", **out}
    names = _git(repo, "diff", "--name-only", str(loaded), head, "--", *BACKEND_PATHS)
    if names is None:
        return {"status": "UNKNOWN", **out, "reason": f"cannot diff the loaded commit {str(loaded)[:12]} with HEAD "
                                                      "(history rewritten?)"}
    changed = [n for n in names.splitlines() if n]
    if not changed:
        return {"status": "OUTSIDE_BACKEND", **out}
    return {"status": "DRIFT", **out, "changed": changed[:20], "changed_count": len(changed),
            "reason": f"restart aeos serve to load {head[:12]}"}
