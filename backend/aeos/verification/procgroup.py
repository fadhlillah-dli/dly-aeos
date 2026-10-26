"""Kill group of one verifier step on the WEAK process runner (§20 Verifier ENF, §21; review W6-054/W6-057).

A descendant of a verifier step that outlives the step (a detached grandchild started by candidate code) could
write the DB trust tables or the controller checkout *after* the tamper window has closed, undetected. Every step
that executes candidate code on the process runner therefore runs in its own kill group:

* Windows: a Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. The step's process is created suspended,
  assigned to the job and only then resumed, so every process it (or any descendant) starts belongs to the job; a
  process cannot leave a job that does not allow breakaway. The job's own accounting counts what is still alive.
* POSIX: a new session (``start_new_session``): the step leader's pid is the session and process-group id of every
  descendant that does not leave them.
* Everywhere: an environment marker (``AEOS_VERIFY_STEP``) every descendant inherits, swept with psutil.

When the step's main process has exited (or was killed on its timeout), whatever is still alive in the group - a
*survivor* - is listed and killed (Windows ``TerminateJobObject``; POSIX ``killpg`` plus the marker/session sweep)
before the verifier compares its tamper window; the verifier QUARANTINES a verification with any survivor.

A member still in the step leader's process group or session gets ``SURVIVOR_GRACE_SECONDS`` to finish exiting.
A member found only through the marker, having left both the process group and the session (``setsid``, a double
fork), detached itself: that is how a step escapes its kill group, not how a child finishes exiting. It is a
survivor at once, with no grace, and is killed. A grace would let a detached process sleep briefly, act after its
step has ended, then exit unseen; macOS CI showed exactly that. When the first sweep finds nothing on POSIX, the
sweep is repeated for ``SURVIVOR_SETTLE_SECONDS``, because a descendant spawned in the step's last moments may
not be readable yet. A step may name an allow-list of build-tool helper
processes (``helpers``: process names such as esbuild's service process, which ends when its parent's pipe closes):
while *only* allow-listed helpers are left they get ``helper_grace`` more seconds, and the ones that exited in it
are recorded (``helper_exits``), never hidden; a Windows console host (``conhost``) of such a helper waits with
them. A helper still alive after that, and every other process, is a survivor. A grace never weakens the tamper
window: a process that ends inside it acted while the window was still open.

The POSIX sweep reads every process on the host, one at a time, and one process that cannot be read never makes the
whole group unreadable. Reading the environment of a process the kernel is tearing down makes psutil raise
``SystemError`` on macOS (``sysctl(KERN_PROCARGS2)`` fails with errno 0); before this rule any unrelated process
exiting during a sweep reported the group as unreadable and QUARANTINED a clean verification, more often the busier
the host. A process whose state cannot be read is re-checked: once it has finished exiting (gone, or a zombie that
can no longer run code) it is no member; a process read normally on a later attempt is judged like any other; a
process still alive and still unreadable after ``UNREADABLE_RECHECK_SECONDS`` is a member (fail closed). Each sweep
builds fresh process objects from the pid list (never ``psutil.process_iter``'s cache, which can hold the identity of
an older process that used the same pid).

Residual risk (stated on the report, ``runner.PROCESS_RESIDUAL_RISKS``): a process created *outside* the step's tree
on the candidate's behalf - Windows WMI/Task Scheduler/COM out-of-process servers, a POSIX daemon that both leaves
the session and drops the marker from its environment, launchd/systemd user units - is not in the group and is not
seen; only the container runner (one PID namespace per step, removed with the container) excludes that.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import psutil

STEP_MARKER = "AEOS_VERIFY_STEP"
# a step's own children that are just exiting when the main process returns are not survivors
SURVIVOR_GRACE_SECONDS = 2.0
# POSIX: how long an empty first sweep is repeated before the group is taken to be empty
SURVIVOR_SETTLE_SECONDS = 1.0
# how long a process whose state cannot be read is re-checked before it counts as a member (fail closed); a process
# the kernel is tearing down has finished exiting long before this
UNREADABLE_RECHECK_SECONDS = 2.0
_MAX_LISTED = 50

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

    class _Accounting(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
            ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
            ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    _PID_SLOTS = 512

    class _PidList(ctypes.Structure):
        _fields_ = [("NumberOfAssignedProcesses", wintypes.DWORD), ("NumberOfProcessIdsInList", wintypes.DWORD),
                    ("ProcessIdList", ctypes.c_size_t * _PID_SLOTS)]

    class _Jobs:
        KILL_ON_JOB_CLOSE = 0x00002000
        EXTENDED_LIMIT_INFORMATION = 9
        BASIC_ACCOUNTING_INFORMATION = 1
        BASIC_PROCESS_ID_LIST = 3
        CREATE_SUSPENDED = 0x00000004

        def __init__(self) -> None:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                                      ctypes.c_void_p]
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            self.k32 = k32
            self.ntdll: Any = None
            try:
                self.ntdll = ctypes.WinDLL("ntdll")
                self.ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
            except (OSError, AttributeError):  # pragma: no cover
                self.ntdll = None

        def create(self) -> int:
            job = self.k32.CreateJobObjectW(None, None)
            if not job:
                raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
            info = _ExtendedLimit()
            info.BasicLimitInformation.LimitFlags = self.KILL_ON_JOB_CLOSE
            if not self.k32.SetInformationJobObject(job, self.EXTENDED_LIMIT_INFORMATION, ctypes.byref(info),
                                                    ctypes.sizeof(info)):
                err = ctypes.get_last_error()
                self.k32.CloseHandle(job)
                raise OSError(err, "SetInformationJobObject failed")
            return int(job)

        def assign(self, job: int, process_handle: int) -> None:
            if not self.k32.AssignProcessToJobObject(job, process_handle):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")

        def resume(self, process_handle: int) -> bool:
            return self.ntdll is not None and self.ntdll.NtResumeProcess(process_handle) == 0

        def active(self, job: int) -> int | None:
            acc = _Accounting()
            if not self.k32.QueryInformationJobObject(job, self.BASIC_ACCOUNTING_INFORMATION, ctypes.byref(acc),
                                                      ctypes.sizeof(acc), None):
                return None
            return int(acc.ActiveProcesses)

        def pids(self, job: int) -> list[int]:
            lst = _PidList()
            ok = self.k32.QueryInformationJobObject(job, self.BASIC_PROCESS_ID_LIST, ctypes.byref(lst),
                                                    ctypes.sizeof(lst), None)
            if not ok:  # ERROR_MORE_DATA still fills the first slots
                pass
            n = min(int(lst.NumberOfProcessIdsInList), _PID_SLOTS)
            return [int(lst.ProcessIdList[i]) for i in range(n)]

        def terminate(self, job: int) -> bool:
            return bool(self.k32.TerminateJobObject(job, 1))

        def close(self, job: int) -> None:
            self.k32.CloseHandle(job)

    try:
        _win = _Jobs()
    except OSError:  # pragma: no cover
        _win = None


def _describe(pid: int) -> dict[str, Any]:
    out: dict[str, Any] = {"pid": pid}
    try:
        p = psutil.Process(pid)
        out["name"] = p.name()[:80]
    except Exception:  # descriptive only: an unreadable name never decides membership
        out["name"] = "?"
    return out


def _finished(pid: int) -> bool:
    """*pid* has finished exiting: it is gone, or a zombie (it can no longer run code)."""
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:  # gone (``ZombieProcess`` is a subclass: a zombie)
        return True
    except Exception:  # unreadable: not known to have finished (fail closed)
        return False


def helper_key(name: str) -> str:
    """A process name as the helper allow-list compares it (case-insensitive, without a Windows ``.exe``)."""
    key = (name or "").strip().lower()
    return key[:-4] if key.endswith(".exe") else key


# Windows console hosts: a console helper the step started without a console of its own gets one, which lives exactly
# as long as its last client. It counts as a helper for the extra grace: alongside a non-helper there is no extra
# grace at all, and a console host still alive when the grace ends is a survivor like any other process.
CONSOLE_HOSTS = frozenset({"conhost"})


def _is_helper(pid: int, helpers: frozenset[str]) -> bool:
    if pid <= 0:  # an unnamed job member is never an allow-listed helper (fail closed)
        return False
    try:
        key = helper_key(psutil.Process(pid).name())
        return key in helpers or key in CONSOLE_HOSTS
    except psutil.NoSuchProcess:
        return True  # it ended right now: the next poll drops it from the group
    except psutil.Error:
        return False  # unreadable: no extra grace
    except Exception:  # an unexpected read error: decided by the state (finished exiting), else no extra grace
        return _finished(pid)


def _alive(p: psutil.Process) -> bool:
    try:
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


@dataclass
class StepGroup:
    """The kill group of one step. ``popen`` starts the process in it; ``reap`` lists and kills survivors."""

    marker: str = field(default_factory=lambda: uuid.uuid4().hex)
    method: str = "none"  # job-object | process-group | marker-sweep
    pid: int | None = None
    started: float = 0.0
    _job: int | None = None
    errors: list[str] = field(default_factory=list)
    # build-tool helper processes (``helper_key`` names) allowed ``helper_grace`` more seconds to exit after the
    # step's main process (see the module docstring); those that did are listed here
    helpers: frozenset[str] = frozenset()
    helper_grace: float = 0.0
    helper_exits: list[dict[str, Any]] = field(default_factory=list)

    def popen(self, args: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        env = dict(kwargs.pop("env", None) or {})
        env[STEP_MARKER] = self.marker
        self.started = time.time()
        suspended = False
        if sys.platform == "win32":
            flags = int(kwargs.pop("creationflags", 0) or 0)
            if _win is not None and _win.ntdll is not None:
                flags |= _win.CREATE_SUSPENDED
                suspended = True
            kwargs["creationflags"] = flags
        else:
            kwargs["start_new_session"] = True
        proc: subprocess.Popen[bytes] = subprocess.Popen(args, env=env, **kwargs)
        self.pid = proc.pid
        if sys.platform == "win32":
            if _win is not None:
                try:
                    self._job = _win.create()
                    _win.assign(self._job, int(proc._handle))  # type: ignore[attr-defined]
                    self.method = "job-object"
                except OSError as exc:
                    self.errors.append(f"job object unavailable ({exc.args[-1] if exc.args else exc})")
                    if self._job is not None:
                        _win.close(self._job)
                    self._job = None
                    self.method = "marker-sweep"
            if suspended and not _win.resume(int(proc._handle)):  # type: ignore[attr-defined]
                proc.kill()
                self.close()
                raise OSError("could not resume the suspended verifier step")
            if _win is None:
                self.method = "marker-sweep"
        else:
            self.method = "process-group"
        return proc

    # --- survivors ------------------------------------------------------------------------------------------
    def _posix_members(self) -> list[psutil.Process]:
        """Every live process of the group (session/process group of the step leader, or the step's marker). Each
        process is read on its own (``_member``): one that cannot be read never makes the whole sweep fail."""
        out: list[psutil.Process] = []
        me = os.getpid()
        for pid in psutil.pids():  # fresh objects: process_iter's cache can hold a reused pid's older identity
            if pid in (me, 0, self.pid):
                continue
            p = self._member(pid)
            if p is not None:
                out.append(p)
        return out

    def _member(self, pid: int) -> psutil.Process | None:
        """*pid* as a live member of the group, or None. A read that fails unexpectedly (macOS: psutil raises
        ``SystemError`` for a process the kernel is tearing down) is decided by the process's state, not by the
        error: finished exiting - no member; read normally on a later attempt - judged like any other; still alive
        and unreadable after ``UNREADABLE_RECHECK_SECONDS`` - a member (fail closed)."""
        deadline = time.monotonic() + UNREADABLE_RECHECK_SECONDS
        while True:
            try:
                return self._classify(pid)
            except Exception:
                if _finished(pid):
                    return None
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
        try:
            return psutil.Process(pid)
        except psutil.NoSuchProcess:
            return None

    def _classify(self, pid: int) -> psutil.Process | None:
        """The live member *pid*, or None. Answers the expected outcomes of reading another process (gone, a zombie,
        another user's); any other error propagates to ``_member``."""
        try:
            p = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return None
        member = False
        if sys.platform != "win32":
            lead = self.pid
            try:
                # same session/process group as the step leader, and started after the step (a recycled leader
                # pid of an unrelated, older group is never taken for the step's)
                member = lead is not None and (os.getpgid(pid) == lead or os.getsid(pid) == lead) \
                    and p.create_time() >= self.started - 1.0
            except (OSError, psutil.Error):
                member = False
        if not member:
            try:
                member = p.environ().get(STEP_MARKER) == self.marker
            except (psutil.Error, OSError, ValueError):
                return None
        return p if member and _alive(p) else None

    def _left_group(self, pid: int) -> bool:
        """POSIX: a member outside both the leader's process group and session detached itself (it was found through
        the marker only). Windows job members cannot leave the job, so this never applies there."""
        if sys.platform == "win32" or self.pid is None or pid <= 0:
            return False
        try:
            return os.getpgid(pid) != self.pid and os.getsid(pid) != self.pid
        except (ProcessLookupError, PermissionError, OSError):
            return False

    def _members(self) -> list[int]:
        if self.method == "job-object" and self._job is not None and _win is not None:
            active = _win.active(self._job)
            if active == 0:
                return []
            if active is not None:
                # the job says something is alive: its pids (or one unnamed entry - never "nothing", fail closed)
                return [pid for pid in _win.pids(self._job) if pid != self.pid] or [-1]
            # the job could not be queried: fall back to the environment-marker sweep
        return [p.pid for p in self._posix_members()]

    def kill_all(self) -> None:
        """Kill everything in the group now (timeout path); survivors are still listed by ``reap``."""
        if self.method == "job-object" and self._job is not None and _win is not None:
            _win.terminate(self._job)
            return
        if sys.platform != "win32" and self.pid is not None:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        for p in self._posix_members():
            try:
                p.kill()
            except psutil.Error:
                pass

    def reap(self, grace: float = SURVIVOR_GRACE_SECONDS) -> list[dict[str, Any]]:
        """After the step's main process ended: every process still alive in the group (after *grace*), each
        killed. Never raises; an unreadable group is reported as one survivor entry (fail closed)."""
        try:
            members = self._members()
            if not members and self.method != "job-object":
                settle = time.monotonic() + SURVIVOR_SETTLE_SECONDS
                while not members and time.monotonic() < settle:
                    time.sleep(0.1)
                    members = self._members()
            detached = {pid: _describe(pid) for pid in members if self._left_group(pid)}
            deadline = time.monotonic() + max(0.0, grace)
            while members and not detached and time.monotonic() < deadline:
                time.sleep(0.1)
                members = self._members()
                detached = {pid: _describe(pid) for pid in members if self._left_group(pid)}
            if members and self.helpers and self.helper_grace > 0 and not detached:
                waiting = {pid: _describe(pid) for pid in members if pid > 0}
                deadline = time.monotonic() + self.helper_grace
                while members and time.monotonic() < deadline and all(_is_helper(p, self.helpers) for p in members):
                    time.sleep(0.1)
                    members = self._members()
                self.helper_exits = [d for pid, d in waiting.items() if pid not in members][:_MAX_LISTED]
            survivors = [detached.get(pid) or (_describe(pid) if pid > 0 else {"pid": None, "name": "?"})
                         for pid in members[:_MAX_LISTED]]
            for d in survivors:
                if d.get("pid") in detached:
                    d["detached"] = True  # left the step's process group and session: no grace was given
            if members:
                self.kill_all()
                if self.method != "job-object":
                    procs = []
                    for pid in members:
                        try:
                            procs.append(psutil.Process(pid))
                        except psutil.Error:
                            continue
                    psutil.wait_procs(procs, timeout=5)
                else:
                    end = time.monotonic() + 5
                    while _win is not None and self._job is not None and (_win.active(self._job) or 0) > 0 \
                            and time.monotonic() < end:
                        time.sleep(0.05)
            return survivors
        except Exception as exc:  # pragma: no cover - never let the bookkeeping hide a survivor
            return [{"pid": None, "name": f"group unreadable ({type(exc).__name__})"}]
        finally:
            self.close()

    def close(self) -> None:
        if self._job is not None and _win is not None:
            _win.close(self._job)  # KILL_ON_JOB_CLOSE: nothing in the job outlives this handle
            self._job = None
