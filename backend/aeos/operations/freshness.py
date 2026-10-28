"""Freshness of what ``aeos serve`` serves after a ``git pull`` (owner request R28, LESSONS L23).

``git pull`` updates the source, but ``frontend/dist`` is a build output and the running server keeps the code it
imported at start. This module is the single owner of both questions:

* **UI bundle** (``bundle_state``): in a git checkout the Vite build record ``dist/aeos-build.json`` must name the
  committed ``frontend/`` tree of HEAD and a clean build; the comparison is ``release.ui_correspondence`` with
  commit ``HEAD`` (one implementation for checkouts and release builds). An extracted release uses the verdict its
  provenance recorded at build time (the same function, run when the release was built). No record, a dirty build or
  no git -> UNVERIFIED, never FRESH.
* **Self-heal at start** (``ensure_ui_fresh``): ``aeos serve`` rebuilds a STALE, MISSING or UNVERIFIED bundle of a
  git checkout once, only when ``frontend/node_modules`` was installed from the current lockfile and ``frontend/`` has
  no local change; it builds into ``frontend/dist.next`` and swaps that in only after success (a failed build leaves
  the old bundle untouched), never runs ``npm ci``/``npm install`` (AGENTS.md: no auto-install) and never blocks the
  start.
* **Code drift** (``code_drift``): ``aeos serve`` records the commit it loaded (``<data>/run/serve.json``); a later
  HEAD that changes backend-relevant paths means the server runs old code until it is restarted.
* **Health block** (``health_freshness``): ``/api/health`` ``freshness`` = the build stamp of the bundle this server
  serves now (read from disk on every call) and the code drift of this process (cached for HEALTH_TTL_SECONDS so
  health stays cheap); the Control Center compares the stamp with the tree baked into the bundle it runs and shows
  a reload / restart banner.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
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


# The runner, the PATH lookup and the directory rename ``ensure_ui_fresh`` uses; tests replace them
# (tests/conftest.py refuses a real npm build).
BUILD_RUNNER: BuildRunner = _run_build
WHICH: Callable[[str], str | None] = shutil.which
RENAME: Callable[[Path, Path], None] = os.rename

# Vite empties its outDir when it starts writing, so a build killed or failing after that point would leave no
# index.html. ``aeos serve`` builds into NEXT_DIR and swaps it in only after the build succeeded (PREV_DIR holds the
# old bundle between the two renames). Both are gitignored, so they never make frontend/ dirty.
NEXT_DIR = "dist.next"
PREV_DIR = "dist.prev"
REBUILDABLE = frozenset({"STALE", "MISSING", "UNVERIFIED"})


def _ui_record(status: str, state: dict[str, Any], reason: str | None = None, **extra: Any) -> dict[str, Any]:
    """What /api/health shows (no absolute path: the log is named relative to the data dir)."""
    rec: dict[str, Any] = {"status": status, "reason": reason if reason is not None else state.get("reason"),
                           "basis": state.get("basis"), "bundle_frontend_tree": state.get("bundle_frontend_tree"),
                           "head_frontend_tree": state.get("head_frontend_tree"), "checked_at": utcnow().isoformat()}
    rec.update(extra)
    return rec


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _remove_tree(path: Path) -> str | None:
    """Delete *path* (a directory); the failure text, or None. Windows: a locked file makes this fail."""
    if not path.exists():
        return None
    try:
        shutil.rmtree(path)
    except OSError as exc:
        return _err(exc)
    return None


def _restore_prev(frontend: Path) -> tuple[bool, str | None]:
    """Put an old bundle moved aside by an interrupted swap (PREV_DIR) back when ``dist`` has no index.html.

    Needs no npm and no git, so it runs before the rebuild safety gate: a valid old bundle is served even with
    ``--no-ui-build``, out-of-date node_modules, a dirty ``frontend/`` or no npm. Returns (restored, failure)."""
    dist, prev = frontend / "dist", frontend / PREV_DIR
    if (dist / "index.html").is_file() or not (prev / "index.html").is_file():
        return False, None
    failed = _remove_tree(dist)
    if failed is None:
        try:
            RENAME(prev, dist)
        except OSError as exc:
            failed = _err(exc)
    if failed is not None:
        return False, f"could not restore the old bundle from frontend/{PREV_DIR} ({failed})"
    return True, None


def _clear_leftovers(frontend: Path) -> str | None:
    """Before a rebuild: delete what an earlier, interrupted rebuild left behind (PREV_DIR once ``dist`` has an
    index.html again, NEXT_DIR always). Returns the failure, or None."""
    nxt, prev = frontend / NEXT_DIR, frontend / PREV_DIR
    if prev.exists() and (frontend / "dist" / "index.html").is_file():
        failed = _remove_tree(prev)
        if failed is not None:
            return f"could not delete frontend/{PREV_DIR} left by an earlier rebuild ({failed})"
    failed = _remove_tree(nxt)
    if failed is not None:
        return f"could not delete frontend/{NEXT_DIR} left by an earlier build ({failed})"
    return None


def _swap_in(frontend: Path) -> tuple[str | None, str | None]:
    """Move NEXT_DIR to ``dist`` (the old ``dist`` via PREV_DIR, deleted afterwards). Returns (failure, note): on a
    failure every rename done so far is undone where possible; the note says what could not be cleaned up."""
    dist, nxt, prev = frontend / "dist", frontend / NEXT_DIR, frontend / PREV_DIR
    had_old = dist.exists()
    if had_old:
        try:
            RENAME(dist, prev)
        except OSError as exc:  # Windows: a file in dist is open (another server, an editor, a virus scanner)
            note = _remove_tree(nxt)
            return (f"could not move frontend/dist aside ({_err(exc)}); a file in it may be locked",
                    f"frontend/{NEXT_DIR} could not be deleted ({note})" if note else None)
    try:
        RENAME(nxt, dist)
    except OSError as exc:
        failure = f"could not move frontend/{NEXT_DIR} to frontend/dist ({_err(exc)})"
        if had_old:
            try:
                RENAME(prev, dist)
            except OSError as exc2:
                failure += f"; restoring the old bundle failed too ({_err(exc2)}), it is in frontend/{PREV_DIR}"
        return failure, None
    if had_old:
        failed = _remove_tree(prev)
        if failed is not None:
            return None, (f"the old bundle in frontend/{PREV_DIR} could not be deleted ({failed}); the next rebuild "
                          "deletes it")
    return None, None


def _blocked(repo: Path, enabled: bool, npm: str | None) -> str | None:
    """Why a rebuild is not safe now, or None. Never installs anything (AGENTS.md: no auto-install)."""
    frontend = repo / "frontend"
    if not enabled:
        return "--no-ui-build"
    if not (frontend / "node_modules").is_dir():
        return "frontend/node_modules is missing (npm ci is never run automatically)"
    ok, why = lockfile_matches_install(frontend)
    if not ok:
        return f"{why}; run npm ci first"
    ok, why = frontend_source_clean(repo)
    if not ok:
        return why
    if npm is None:
        return "npm not found on PATH"
    return None


def _serving(status: str) -> str:
    return ("No bundle to serve: the Control Center pages return FRONTEND_NOT_BUILT" if status == "MISSING"
            else "Serving the old bundle")


def ensure_ui_fresh(settings: Settings, repo: Path, *, remedy: str, enabled: bool = True,
                    which: Callable[[str], str | None] | None = None, timeout: float = UI_BUILD_TIMEOUT_SECONDS,
                    echo: Callable[[str], None] = print) -> dict[str, Any]:
    """Rebuild a STALE, MISSING or UNVERIFIED bundle of a git checkout once before the server starts, when that is
    safe; never raises, never blocks.

    The build goes to ``frontend/dist.next`` and replaces ``frontend/dist`` only after it succeeded and its build
    record matches HEAD, so a failed, killed or timed-out build leaves the old bundle untouched. *remedy* is the
    operator's way out, printed and reported when the bundle is not current.

    A MISSING bundle whose old copy an interrupted swap left in ``frontend/dist.prev`` is put back first, before the
    safety gate (restoring needs no npm), so it is served even when no rebuild is allowed.

    Returns the ``ui_build`` record: FRESH (nothing to do), REBUILT, or STALE / UNVERIFIED / MISSING (not rebuilt or
    the build failed, with the reason). The status always describes ``frontend/dist`` as it is on disk when this
    returns (MISSING exactly when ``frontend/dist/index.html`` is absent)."""
    from aeos.operations import release

    try:
        state = bundle_state(settings, repo)
    except Exception as exc:  # never block the start on the check itself
        return _ui_record("UNVERIFIED", {}, f"freshness check failed ({type(exc).__name__})")
    status = state["status"]
    if status not in REBUILDABLE:
        return _ui_record(status, state)
    # A release keeps the bundle its provenance pins: never touched here.
    if release.release_id_of_root(settings, repo) is not None:
        return _ui_record(status, state, **({"remedy": remedy} if status == "MISSING" else {}))
    frontend = repo / "frontend"
    notes: list[str] = []
    if status == "MISSING":
        # An interrupted swap left the old bundle in PREV_DIR: put it back before anything else (no npm needed), so a
        # valid bundle is served whether or not a rebuild is allowed; then judge what is on disk now.
        restored, failure = _restore_prev(frontend)
        if failure is not None:
            notes.append(failure)
        if restored:
            notes.append(f"the old bundle was restored from frontend/{PREV_DIR}")
            state, status = _state_now(settings, repo, status)
            if status not in REBUILDABLE:
                return _ui_record(status, state, note="; ".join(notes))
    note_kw = {"note": "; ".join(notes)} if notes else {}
    # Only a git checkout can be rebuilt and then proven: a tree without git (no HEAD:frontend) would be UNVERIFIED
    # again after any build.
    if frontend_head_tree(repo) is None:
        return _ui_record(status, state, **({"remedy": remedy} if status == "MISSING" else {}), **note_kw)
    npm = (which or WHICH)("npm")  # Windows: npm.cmd through PATHEXT
    blocked = _blocked(repo, enabled, npm)
    if blocked is not None:
        reason = f"{state['reason']}; not rebuilt: {blocked}"
        echo(f"WARNING: Control Center bundle is {status} ({reason}). {_serving(status)}. Remedy: {remedy}")
        return _ui_record(status, state, reason, remedy=remedy, **note_kw)
    log_path = settings.layout.logs / UI_BUILD_LOG
    log = f"<data>/logs/{UI_BUILD_LOG}"
    echo(f"Control Center bundle is {status} ({state['reason']}); running 'npm run build' once into "
         f"frontend/{NEXT_DIR} (timeout {int(timeout)}s, log {log}).")
    note: str | None = None
    failure = _clear_leftovers(frontend)
    if failure is None:
        try:
            code = BUILD_RUNNER([str(npm), "run", "build", "--", "--outDir", NEXT_DIR], frontend, timeout, log_path)
            if code != 0:
                failure = f"npm run build exited with {code}"
        except subprocess.TimeoutExpired:
            failure = f"npm run build timed out after {int(timeout)}s"
        except Exception as exc:
            failure = f"npm run build could not run ({_err(exc)})"
    if failure is None:
        built = bundle_state(settings, repo, frontend / NEXT_DIR)
        if built["status"] != "FRESH":
            failure = f"the build finished but frontend/{NEXT_DIR} is {built['status']}: {built.get('reason')}"
        else:
            failure, note = _swap_in(frontend)
    if failure is None:
        after = bundle_state(settings, repo)
        if after["status"] == "FRESH":
            notes += [note] if note else []
            extra = {"log": log, "previous_status": status, **({"note": "; ".join(notes)} if notes else {})}
            return _ui_record("REBUILT", after, None, **extra)
        failure = f"the swapped bundle is {after['status']}: {after.get('reason')}"
    else:
        cleanup = _remove_tree(frontend / NEXT_DIR)  # the old dist is untouched; drop the partial build
        if cleanup is not None:
            note = f"frontend/{NEXT_DIR} could not be deleted ({cleanup})"
    notes += [note] if note else []
    # whatever failed, report what is on disk now (never the status from before a restore or a swap): no index.html
    # means MISSING, not "serving the old bundle"
    final_state, final = _state_now(settings, repo, status)
    reason = f"{state['reason']}; rebuild failed: {failure}"
    echo(f"WARNING: {reason}. {_serving(final)}. Log: {log}. Remedy: {remedy}")
    return _ui_record(final, final_state, reason, remedy=remedy, log=log,
                      **({"note": "; ".join(notes)} if notes else {}))


def _state_now(settings: Settings, repo: Path, fallback: str) -> tuple[dict[str, Any], str]:
    """(state, status) of ``frontend/dist`` as it is on disk now; never raises (a failing check falls back to
    MISSING without index.html, else to *fallback*)."""
    try:
        state = bundle_state(settings, repo)
        return state, str(state["status"])
    except Exception as exc:
        missing = not (repo / "frontend" / "dist" / "index.html").is_file()
        status = "MISSING" if missing else fallback
        return {"status": status, "reason": f"freshness check failed ({type(exc).__name__})"}, status


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
                                       "checked_at", "remedy", "log", "previous_status", "note")}
    return {"status": "NOT_RECORDED", "reason": "this server was not started by 'aeos serve' from a checkout"}


def code_drift(settings: Settings, rec: dict[str, Any] | None = None, *, timeout: float = 30) -> dict[str, Any]:
    """Does the running ``aeos serve`` (checkout mode) run the code its checkout's HEAD has?

    *rec* is the serve record to judge (default: the run file, the last ``aeos serve`` started in this data dir; the
    health endpoint passes this process's own record). ``status``: CURRENT | OUTSIDE_BACKEND (HEAD moved, no
    backend-relevant change) | DRIFT | NOT_RUNNING | RELOAD | UNKNOWN."""
    rec = rec if rec is not None else read_json(run_file(settings))
    if not rec:
        return {"status": "NOT_RUNNING", "reason": "no 'aeos serve' start is recorded in this data dir"}
    base = {"loaded_commit": rec.get("commit"), "started_at": rec.get("started_at"), "pid": rec.get("pid")}
    if not _alive(rec):
        return {"status": "NOT_RUNNING", **base, "reason": "the recorded 'aeos serve' process is not running"}
    if rec.get("reload"):
        return {"status": "RELOAD", **base, "reason": "--reload reloads changed backend code itself"}
    repo = Path(str(rec.get("repo") or ""))
    head = _git(repo, "rev-parse", "HEAD", timeout=timeout) if rec.get("repo") else None
    loaded = rec.get("commit")
    out = {**base, "head_commit": head}
    if not loaded or not head:
        return {"status": "UNKNOWN", **out, "reason": "the loaded or the current commit cannot be read (not a git "
                                                      "checkout?)"}
    if loaded == head:
        return {"status": "CURRENT", **out}
    names = _git(repo, "diff", "--name-only", str(loaded), head, "--", *BACKEND_PATHS, timeout=timeout)
    if names is None:
        return {"status": "UNKNOWN", **out, "reason": f"cannot diff the loaded commit {str(loaded)[:12]} with HEAD "
                                                      "(history rewritten?)"}
    changed = [n for n in names.splitlines() if n]
    if not changed:
        return {"status": "OUTSIDE_BACKEND", **out}
    return {"status": "DRIFT", **out, "changed": changed[:20], "changed_count": len(changed),
            "reason": f"restart aeos serve to load {head[:12]}"}


# ----------------------------------------------------------------------------------------- /api/health block

HEALTH_TTL_SECONDS = 30.0
HEALTH_GIT_TIMEOUT_SECONDS = 5.0
CLOCK: Callable[[], float] = time.monotonic  # tests replace it
_HEALTH_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}  # data dir -> (when, code drift block)
_HEALTH_LOCK = threading.Lock()


def served_bundle(dist: Path | None) -> dict[str, Any]:
    """The build stamp of the bundle in *dist* (``aeos-build.json``, written by the Vite build): which committed
    frontend tree it was built from. No git call; ``frontend_tree`` is None without a bundle or a record."""
    if dist is None or not (dist / "index.html").is_file():
        return {"frontend_tree": None, "built_at": None, "clean": None, "reason": "no bundle is served"}
    info = read_json(dist / "aeos-build.json") or {}
    out = {"frontend_tree": info.get("frontend_tree"), "built_at": info.get("built_at"), "clean": info.get("clean")}
    return out if out["frontend_tree"] else {**out, "reason": "the served bundle has no build record"}


def _health_drift(settings: Settings) -> dict[str, Any]:
    rec = serve_record(settings)
    if rec is None:
        return {"status": "NOT_RECORDED", "reason": "this server was not started by 'aeos serve' from a checkout"}
    drift = code_drift(settings, rec, timeout=HEALTH_GIT_TIMEOUT_SECONDS)
    # the changed file names stay in the Doctor (runtime.code_drift); health says only whether and how many
    return {k: drift.get(k) for k in ("status", "loaded_commit", "head_commit", "changed_count", "reason")}


def health_freshness(settings: Settings, dist: Path | None) -> dict[str, Any]:
    """``/api/health`` ``freshness``: what this server serves and runs. Only the code drift (at most two bounded git
    calls) is cached, once per HEALTH_TTL_SECONDS per data dir, so a browser refetching health never makes the
    endpoint slow; the served bundle's stamp is a file read and is read on every call, so a page reloaded right after
    a rebuild of ``dist`` is compared with the bundle on disk, never with a cached older stamp."""
    key = str(settings.data_dir)
    now = CLOCK()
    with _HEALTH_LOCK:
        hit = _HEALTH_CACHE.get(key)
        if hit is None or now - hit[0] >= HEALTH_TTL_SECONDS:
            hit = (now, {"code_drift": _health_drift(settings), "checked_at": utcnow().isoformat()})
            _HEALTH_CACHE[key] = hit
    return {"served_bundle": served_bundle(dist), **hit[1], "max_age_seconds": int(HEALTH_TTL_SECONDS)}
