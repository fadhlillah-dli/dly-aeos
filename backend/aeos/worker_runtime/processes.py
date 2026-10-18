"""Worker process lifecycle with full process-tree control (§22 cancel, §25 kill controller, §50).

* Environment is rebuilt from a small allow-list; nothing secret-looking survives (names or
  values), HOME/USERPROFILE/APPDATA/LOCALAPPDATA/XDG_*/TEMP are redirected into the run sandbox.
* Windows: the process is created suspended, assigned to a Job Object with
  KILL_ON_JOB_CLOSE, then resumed — every descendant belongs to the job, and the whole job dies if
  the controller dies. POSIX: a new session/process group per run.
* ``kill_run_processes`` snapshots the descendant tree before terminating anything, terminates the
  job / process group, kills survivors via psutil, and finally sweeps for any process carrying the
  run's ``AEOS_RUN_ID`` marker (orphans re-parented away from the tree).
* The run -> pid registry is in memory; the pid and its psutil create_time are also persisted on
  ``WorkerRun`` so reconciliation after a controller crash can identify the exact old process.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import psutil

from aeos.core.redaction import contains_secret

log = logging.getLogger("aeos.worker_runtime")

RUN_MARKER = "AEOS_RUN_ID"
CREATE_TIME_TOLERANCE = 1.0

_ENV_ALLOW = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "PROCESSOR_LEVEL", "PROCESSOR_REVISION", "OS",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ",
}
# Names that must never reach a sandbox even if allow-listed by a caller.
_SECRET_NAME = re.compile(
    r"(?i)(TOKEN|SECRET|PASSW|API[_-]?KEY|PRIVATE|CREDENTIAL|AUTH|COOKIE|SESSION|ANTHROPIC|OPENAI|OPENROUTER|"
    r"GITHUB|^GH_|^AWS_|AZURE|GOOGLE|GCLOUD|^VAULT|^BAO_|SSH_AUTH_SOCK|SSH_AGENT|^AEOS_(?!RUN_ID$)|KEYRING|NPM_TOKEN)"
)
# Explicit sandbox variables we set ourselves (paths/flags only, never values of secrets).
_SANDBOX_SAFE_NAMES = {RUN_MARKER, "OPENCODE_CONFIG", "OPENCODE_DISABLE_AUTOUPDATE", "OPENCODE_DISABLE_MODELS_FETCH",
                       "OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_DISABLE_LSP_DOWNLOAD",
                       "OPENCODE_DISABLE_DEFAULT_PLUGINS", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL",
                       "GIT_TERMINAL_PROMPT", "NO_UPDATE_NOTIFIER", "NODE_OPTIONS_DISABLED"}


@dataclass(frozen=True)
class SandboxDirs:
    root: Path  # per-run directory (outside the worktree)
    home: Path
    tmp: Path
    config: Path

    @classmethod
    def create(cls, root: Path) -> SandboxDirs:
        dirs = cls(root=root, home=root / "home", tmp=root / "tmp", config=root / "config")
        for p in (dirs.root, dirs.home, dirs.tmp, dirs.config, dirs.home / "AppData" / "Roaming",
                  dirs.home / "AppData" / "Local", dirs.home / ".config", dirs.home / ".local" / "share",
                  dirs.home / ".cache", dirs.home / ".local" / "state"):
            p.mkdir(parents=True, exist_ok=True)
        (dirs.home / ".gitconfig-empty").write_text("", encoding="utf-8")
        return dirs


def is_secret_like(name: str, value: str) -> bool:
    if name in _SANDBOX_SAFE_NAMES:
        return contains_secret(value)
    return bool(_SECRET_NAME.search(name)) or contains_secret(value)


def build_sandbox_env(dirs: SandboxDirs, *, run_id: str, extra: dict[str, str] | None = None,
                      base: dict[str, str] | None = None) -> dict[str, str]:
    """Allow-listed environment with every home/config/temp location redirected into the sandbox."""
    src = dict(os.environ if base is None else base)
    env: dict[str, str] = {}
    for k, v in src.items():
        if k.upper() in _ENV_ALLOW and not is_secret_like(k, v):
            env[k] = v
    home = str(dirs.home)
    env.update({
        "HOME": home,
        "USERPROFILE": home,
        "APPDATA": str(dirs.home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(dirs.home / "AppData" / "Local"),
        "XDG_CONFIG_HOME": str(dirs.home / ".config"),
        "XDG_DATA_HOME": str(dirs.home / ".local" / "share"),
        "XDG_CACHE_HOME": str(dirs.home / ".cache"),
        "XDG_STATE_HOME": str(dirs.home / ".local" / "state"),
        "TEMP": str(dirs.tmp),
        "TMP": str(dirs.tmp),
        "TMPDIR": str(dirs.tmp),
        RUN_MARKER: run_id,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(dirs.home / ".gitconfig-empty"),
        "GIT_TERMINAL_PROMPT": "0",
        "NO_UPDATE_NOTIFIER": "1",
    })
    if sys.platform == "win32":
        drive, rest = os.path.splitdrive(home)
        env["HOMEDRIVE"] = drive or "C:"
        env["HOMEPATH"] = rest or "\\"
    for k, v in (extra or {}).items():
        env[k] = v
    # Final guarantee: nothing secret-looking, whatever a caller passed.
    return {k: v for k, v in env.items() if not is_secret_like(k, v)}


# ----------------------------------------------------------------------------- Windows job objects
_win: Any = None
if sys.platform == "win32":  # pragma: no branch
    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit), ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _Win:
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        JobObjectExtendedLimitInformation = 9
        CREATE_SUSPENDED = 0x00000004
        CREATE_NO_WINDOW = 0x08000000

        def __init__(self) -> None:
            self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.k32.CreateJobObjectW.restype = wintypes.HANDLE
            self.k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            self.k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            self.k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            self.k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            self.k32.CloseHandle.argtypes = [wintypes.HANDLE]
            try:
                self.ntdll = ctypes.WinDLL("ntdll")
                self.ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
            except (OSError, AttributeError):  # pragma: no cover
                self.ntdll = None

        def create_job(self) -> int:
            job = self.k32.CreateJobObjectW(None, None)
            if not job:
                raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
            info = _ExtendedLimit()
            info.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.k32.SetInformationJobObject(job, self.JobObjectExtendedLimitInformation,
                                                    ctypes.byref(info), ctypes.sizeof(info)):
                err = ctypes.get_last_error()
                self.k32.CloseHandle(job)
                raise OSError(err, "SetInformationJobObject failed")
            return int(job)

        def assign(self, job: int, process_handle: int) -> None:
            if not self.k32.AssignProcessToJobObject(job, process_handle):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")

        def resume(self, process_handle: int) -> bool:
            if self.ntdll is None:
                return False
            return self.ntdll.NtResumeProcess(process_handle) == 0

        def terminate(self, job: int) -> bool:
            return bool(self.k32.TerminateJobObject(job, 1))

        def close(self, job: int) -> None:
            self.k32.CloseHandle(job)

    try:
        _win = _Win()
    except OSError:  # pragma: no cover
        _win = None


# ----------------------------------------------------------------------------- registry
@dataclass
class ManagedProcess:
    run_id: str
    popen: subprocess.Popen[bytes]
    pid: int
    create_time: float
    job: int | None = None
    pgid: int | None = None
    stderr_path: Path | None = None
    _stderr_fh: IO[bytes] | None = field(default=None, repr=False)

    @property
    def stdout(self) -> IO[bytes] | None:
        return self.popen.stdout

    def wait(self, timeout: float | None = None) -> int:
        return self.popen.wait(timeout=timeout)

    def poll(self) -> int | None:
        return self.popen.poll()

    def close(self) -> None:
        if self._stderr_fh is not None:
            try:
                self._stderr_fh.close()
            except OSError:
                pass
        if self.job is not None and _win is not None and self.popen.poll() is not None:
            _win.close(self.job)
            self.job = None


_registry_lock = threading.Lock()
_registry: dict[str, list[ManagedProcess]] = {}


def registered(run_id: str) -> list[ManagedProcess]:
    with _registry_lock:
        return list(_registry.get(run_id, []))


def _register(mp: ManagedProcess) -> None:
    with _registry_lock:
        _registry.setdefault(mp.run_id, []).append(mp)


def unregister(run_id: str) -> None:
    with _registry_lock:
        items = _registry.pop(run_id, [])
    for mp in items:
        mp.close()


def spawn(run_id: str, argv: list[str], *, cwd: Path, env: dict[str, str], stderr_path: Path | None = None,
          capture_stdout: bool = True) -> ManagedProcess:
    """Start a worker process (list argv, never a shell) and register its tree root."""
    if not argv or not isinstance(argv, list):
        raise ValueError("argv must be a non-empty list")
    env = {**env, RUN_MARKER: run_id}
    stderr_fh: IO[bytes] | None = stderr_path.open("wb") if stderr_path else None
    kwargs: dict[str, Any] = {
        "cwd": str(cwd), "env": env, "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        "stderr": stderr_fh if stderr_fh is not None else subprocess.DEVNULL,
    }
    job: int | None = None
    suspended = False
    if sys.platform == "win32":
        flags = 0
        if _win is not None:
            flags |= _win.CREATE_NO_WINDOW
            if _win.ntdll is not None:
                flags |= _win.CREATE_SUSPENDED
                suspended = True
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    try:
        popen = subprocess.Popen(argv, **kwargs)
    except Exception:
        if stderr_fh:
            stderr_fh.close()
        raise
    if sys.platform == "win32" and _win is not None:
        try:
            job = _win.create_job()
            _win.assign(job, int(popen._handle))  # type: ignore[attr-defined]
        except OSError as exc:
            log.warning("job object assignment failed for run %s: %s (psutil tree kill still applies)", run_id, exc)
            if job is not None:
                _win.close(job)
            job = None
        if suspended and not _win.resume(int(popen._handle)):  # type: ignore[attr-defined]
            popen.kill()
            if job is not None:
                _win.close(job)
            if stderr_fh:
                stderr_fh.close()
            raise OSError("could not resume suspended worker process")
    try:
        create_time = psutil.Process(popen.pid).create_time()
    except psutil.Error:
        create_time = 0.0
    mp = ManagedProcess(run_id=run_id, popen=popen, pid=popen.pid, create_time=create_time, job=job,
                        pgid=popen.pid if sys.platform != "win32" else None, stderr_path=stderr_path,
                        _stderr_fh=stderr_fh)
    _register(mp)
    return mp


def is_alive(pid: int | None, create_time: float | None) -> bool:
    """True only if *pid* exists AND is the same process (create_time match) and not a zombie."""
    if not pid:
        return False
    try:
        p = psutil.Process(pid)
        if create_time and abs(p.create_time() - create_time) > CREATE_TIME_TOLERANCE:
            return False
        if p.status() == psutil.STATUS_ZOMBIE:
            return False
        # Windows has no zombie state: an exited process stays visible while any handle to it is open
        # (e.g. a Popen object), so ask for its exit status without blocking.
        try:
            p.wait(timeout=0)
        except psutil.TimeoutExpired:
            return True
        return False
    except psutil.Error:
        return False


def _same(p: psutil.Process, create_time: float | None) -> bool:
    try:
        return not create_time or abs(p.create_time() - create_time) <= CREATE_TIME_TOLERANCE
    except psutil.Error:
        return False


def _running(p: psutil.Process) -> bool:
    try:
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _marker_sweep(run_id: str) -> list[psutil.Process]:
    found: list[psutil.Process] = []
    me = os.getpid()
    for p in psutil.process_iter(["pid"]):
        if p.pid in (me, 0):
            continue
        try:
            if p.environ().get(RUN_MARKER) == run_id:
                found.append(p)
        except (psutil.Error, OSError, ValueError):
            continue
    return found


def _db_roots(run_id: str) -> list[tuple[int, float | None]]:
    try:
        from aeos.core.db import session_factory
        from aeos.models.workforce import WorkerRun

        with session_factory()() as s:
            run = s.get(WorkerRun, run_id)
            if run is not None and run.pid:
                return [(run.pid, run.pid_create_time)]
    except Exception as exc:  # DB unavailable: registry/marker sweep still apply
        log.info("kill_run_processes: DB lookup failed for %s: %s", run_id, exc)
    return []


def kill_run_processes(run_id: str, *, grace_seconds: float = 3.0, sweep: bool = True) -> dict[str, Any]:
    """Terminate the full process tree of a run. Idempotent; reports what was actually killed."""
    entries = registered(run_id)
    roots: list[tuple[int, float | None]] = [(m.pid, m.create_time) for m in entries] or _db_roots(run_id)
    methods: list[str] = []
    victims: dict[int, psutil.Process] = {}
    for pid, ctime in roots:
        try:
            root = psutil.Process(pid)
        except psutil.Error:
            continue
        if not _same(root, ctime):
            continue
        victims[root.pid] = root
        try:
            for child in root.children(recursive=True):  # snapshot BEFORE terminating anything
                victims[child.pid] = child
        except psutil.Error:
            pass
    for m in entries:
        if m.job is not None and _win is not None:
            if _win.terminate(m.job):
                methods.append("job-object")
        elif m.pgid is not None and sys.platform != "win32":
            try:
                os.killpg(m.pgid, signal.SIGKILL)
                methods.append("process-group")
            except (ProcessLookupError, PermissionError):
                pass
    if sweep and (roots or entries):  # a process was started for this run: catch re-parented orphans
        for p in _marker_sweep(run_id):
            victims.setdefault(p.pid, p)
    procs = list(victims.values())
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(procs, timeout=grace_seconds)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    _gone2, still = psutil.wait_procs(alive, timeout=grace_seconds)
    if procs:
        methods.append("psutil-tree")
    for m in entries:
        try:
            m.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    remaining = sorted(p.pid for p in still if _running(p))
    if not remaining:
        unregister(run_id)
    return {
        "run_id": run_id,
        "found": bool(roots) or bool(procs),
        "killed": sorted(p.pid for p in procs if p.pid not in remaining),
        "remaining": remaining,
        "methods": sorted(set(methods)),
    }
