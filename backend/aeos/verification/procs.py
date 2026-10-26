"""Cross-platform subprocess helpers for the verifier (Windows, macOS, Linux).

* list arguments only (no shell), explicit timeouts, whole process tree killed on timeout (psutil); a step that
  executes candidate code runs in its own kill group (Windows Job Object / POSIX session, ``procgroup``) and
  whatever outlives it is reported and killed;
* a scrubbed child environment built from an allow-list — no credentials, no provider keys, no
  ``AEOS_*`` overrides; every per-user location a tool discovers through the environment is redirected
  into the verifier's run dir (``HOME``/``USERPROFILE``/``APPDATA``/``LOCALAPPDATA``/``XDG_*``,
  the platformdirs Windows overrides, ``GH_CONFIG_DIR``, docker/npm/claude config dirs), the Python keyring
  is the null backend, git reads no
  system/global config and has no credential helper or askpass and only the local ``file`` transport, no
  SSH agent socket, no D-Bus session bus, and outbound HTTP points at a dead proxy (an egress deterrent,
  not an enforcement claim). This stops credential discovery *through the environment*; code running as
  the same OS user that opens an absolute path or calls the OS credential API directly is not stopped
  (stated as residual risk; the container runner in ``aeos.verification.runner`` is the isolation);
* git plumbing that never runs hooks, fsmonitor, textconv, external diff or checkout filters.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import psutil

from aeos.core.redaction import contains_secret, redact_text
from aeos.verification.scope import unsafe_path_reason

_PASSTHROUGH_ENV = (
    "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "OS", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
)
DEAD_PROXY = "http://127.0.0.1:9"
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,250}$")
_CREATE_NO_WINDOW = 0x08000000
# Environment git needs to locate itself and the user's global config — nothing else. Secrets from
# the controller environment never reach a git process that reads an untrusted candidate repository.
_GIT_PASSTHROUGH_ENV = (
    *_PASSTHROUGH_ENV, "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "SYSTEMDRIVE", "TMP", "TEMP", "TMPDIR", "XDG_CONFIG_HOME",
)


@dataclass
class ProcResult:
    args: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float
    # contained steps only (``run_process(contain=True)``): processes of the step's kill group still alive after
    # its main process ended - listed, then killed (``aeos.verification.procgroup``) - and how the group was held
    survivors: list[dict[str, Any]] = field(default_factory=list)
    containment: str = ""
    containment_errors: list[str] = field(default_factory=list)
    # every result-seal line of stdout (``aeos.verification.seal``), kept in full even when the stdout tail above
    # is trimmed - a long failing run never loses the sealed stream (that would read as tampering)
    channel: str = ""

    def tail(self, limit: int = 8000) -> str:
        from aeos.verification.seal import strip_seal_lines

        text = strip_seal_lines(self.stdout or "") + ("\n" + self.stderr if self.stderr else "")
        return redact_text(text[-limit:])


def kill_tree(pid: int) -> None:
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    procs = [*parent.children(recursive=True), parent]
    for p in procs:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(procs, timeout=10)


def _creationflags() -> int:
    return _CREATE_NO_WINDOW if sys.platform == "win32" else 0


_SEAL_PREFIX = b"@@AEOS-VSEAL1 "  # aeos.verification.seal.PREFIX
MAX_CHANNEL_BYTES = 32 * 1024 * 1024
_MAX_PARTIAL_LINE = 1024 * 1024
# a contained step's output pipe still open after its whole kill group was killed: a process outside the group holds
# it (it escaped the group) - the runner reports that as a survivor that could not be killed
PIPE_HELD_ERROR = "step output pipe held open after the step ended (process outside the kill group)"


def _channel_lines(lines: list[bytes]) -> list[bytes]:
    return [ln[ln.index(_SEAL_PREFIX):].rstrip(b"\r") for ln in lines if _SEAL_PREFIX in ln]


def extract_channel(data: bytes) -> str:
    """The result-seal lines of a complete output buffer."""
    return b"\n".join(_channel_lines(data.split(b"\n"))).decode("utf-8", errors="replace")


class _PipeReader:
    """Drains one pipe on a thread (bounded: keeps the last ``limit`` bytes), collecting result-seal lines in full."""

    def __init__(self, pipe: Any, limit: int):
        self.pipe, self.limit = pipe, limit
        self.buf = bytearray()
        self.channel: list[bytes] = []
        self._channel_bytes = 0
        self._partial = bytearray()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _scan(self, chunk: bytes) -> None:
        self._partial += chunk
        if b"\n" not in chunk:
            if len(self._partial) > _MAX_PARTIAL_LINE and _SEAL_PREFIX not in self._partial:
                del self._partial[:-len(_SEAL_PREFIX)]  # an endless line without the prefix: keep a boundary only
            elif len(self._partial) > 4 * _MAX_PARTIAL_LINE:
                del self._partial[:-_MAX_PARTIAL_LINE]  # never unbounded (a sealed line is a few hundred bytes)
            return
        *lines, rest = bytes(self._partial).split(b"\n")
        self._partial = bytearray(rest)
        for ln in _channel_lines(lines):
            self._channel_bytes += len(ln)
            if self._channel_bytes <= MAX_CHANNEL_BYTES:
                self.channel.append(ln)

    def _run(self) -> None:
        try:
            fd = self.pipe.fileno()
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    if self._partial:
                        self._scan(b"\n")
                    return
                self._scan(chunk)
                self.buf += chunk
                if len(self.buf) > 2 * self.limit:
                    del self.buf[:-self.limit]
        except (OSError, ValueError):
            return

    def join(self, timeout: float) -> bool:
        self.thread.join(timeout)
        return not self.thread.is_alive()

    def text(self) -> str:
        return bytes(self.buf[-self.limit:]).decode("utf-8", errors="replace")

    def channel_text(self) -> str:
        return b"\n".join(self.channel).decode("utf-8", errors="replace")


def _run_contained(args: list[str], *, cwd: Path, env: dict[str, str], timeout: float, max_output: int,
                   start: float) -> ProcResult:
    """One step in its own kill group (``procgroup.StepGroup``): the main process is waited for (not its pipes),
    then every survivor of the group is listed and killed before the output is collected - a descendant holding
    the step's stdout open can neither outlive the step nor stall the verifier."""
    from aeos.verification.procgroup import StepGroup

    group = StepGroup()
    proc = group.popen(args, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, creationflags=_creationflags())
    out_r, err_r = _PipeReader(proc.stdout, max_output), _PipeReader(proc.stderr, max_output)
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        group.kill_all()
        kill_tree(proc.pid)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
    survivors = group.reap()
    errors = list(group.errors)
    if not (out_r.join(10) and err_r.join(10)):
        # a process outside the group (it escaped it) still holds the step's output pipes open
        errors.append(PIPE_HELD_ERROR)
        for pipe in (proc.stdout, proc.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass
    return ProcResult(
        args=[str(a) for a in args], returncode=None if timed_out else proc.returncode,
        stdout=out_r.text(), stderr=err_r.text(), timed_out=timed_out,
        duration_s=round(time.monotonic() - start, 3), survivors=survivors, containment=group.method,
        containment_errors=errors, channel=out_r.channel_text())


def run_process(args: list[str], *, cwd: Path, env: dict[str, str], timeout: float,
                max_output: int = 4_000_000, contain: bool = False) -> ProcResult:
    """Run *args* (no shell) with a timeout that kills the whole tree. ``contain=True`` (every step that executes
    candidate code on the process runner) runs it in its own kill group and reports its survivors."""
    start = time.monotonic()
    if contain:
        return _run_contained(args, cwd=cwd, env=env, timeout=timeout, max_output=max_output, start=start)
    proc = subprocess.Popen(
        args, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, creationflags=_creationflags(),
    )
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc.pid)
        out, err = proc.communicate()
    return ProcResult(
        args=[str(a) for a in args],
        returncode=None if timed_out else proc.returncode,
        stdout=out[-max_output:].decode("utf-8", errors="replace"),
        stderr=err[-max_output:].decode("utf-8", errors="replace"),
        timed_out=timed_out,
        duration_s=round(time.monotonic() - start, 3),
        channel=extract_channel(out) if _SEAL_PREFIX in out else "",
    )


# The keyring backend candidate code gets: answers "no password" for everything and stores nothing (the
# ``fail`` backend raised, which invited a fallback to ``keyring.set_keyring(<real backend>)`` in code).
NULL_KEYRING = "keyring.backends.null.Keyring"
EMPTY_GIT_CONFIG = ".gitconfig-empty"


def child_env_values(*, home: str, tmp: str, data_dir: str, pythonpath: str, empty_git_config: str,
                     sep: str = os.sep) -> dict[str, str]:
    """The explicit (non-passthrough) variables of a verifier child, for a host *or* a container layout.

    Every location a credential-bearing tool finds through the environment points into the run's own home
    (empty): gh (``GH_CONFIG_DIR``), git (no system/global config, ``credential.helper`` reset to empty, no
    askpass, only the local ``file`` transport), docker (``DOCKER_CONFIG``), npm, the Claude CLI, the Python
    keyring (null backend; its config dir is under the redirected XDG/LOCALAPPDATA), the D-Bus session bus
    (Secret Service) and the SSH agent (``SSH_AUTH_SOCK`` is never set)."""
    def j(*parts: str) -> str:
        return sep.join((home, *parts))

    return {
        "HOME": home,
        "USERPROFILE": home,
        "APPDATA": j("AppData", "Roaming"),
        "LOCALAPPDATA": j("AppData", "Local"),
        "XDG_CONFIG_HOME": j(".config"),
        "XDG_DATA_HOME": j(".local", "share"),
        "XDG_CACHE_HOME": j(".cache"),
        "XDG_STATE_HOME": j(".local", "state"),
        "XDG_RUNTIME_DIR": j(".run"),
        # Windows: platformdirs (and so aeos.core.paths.default_data_dir) asks the Known Folder API, which
        # ignores LOCALAPPDATA/APPDATA (it follows only a profile-relative registry entry through USERPROFILE);
        # these explicit overrides keep every platformdirs location - the AEOS data dir included - inside the
        # run home even when the owner relocated AppData/Documents (review 2026-10-19, K2). Ignored elsewhere.
        "WIN_PD_OVERRIDE_LOCAL_APPDATA": j("AppData", "Local"),
        "WIN_PD_OVERRIDE_APPDATA": j("AppData", "Roaming"),
        "WIN_PD_OVERRIDE_COMMON_APPDATA": j("ProgramData"),
        "WIN_PD_OVERRIDE_PERSONAL": j("Documents"),
        "WIN_PD_OVERRIDE_DOWNLOADS": j("Downloads"),
        "WIN_PD_OVERRIDE_DESKTOPDIRECTORY": j("Desktop"),
        "GH_CONFIG_DIR": j(".config", "gh"),
        "GH_PROMPT_DISABLED": "1",
        "GH_NO_UPDATE_NOTIFIER": "1",
        "DOCKER_CONFIG": j(".docker"),
        "NPM_CONFIG_USERCONFIG": j(".npmrc"),
        "CLAUDE_CONFIG_DIR": j(".claude"),
        "DBUS_SESSION_BUS_ADDRESS": "disabled:",
        "TMPDIR": tmp,
        "TEMP": tmp,
        "TMP": tmp,
        "PYTHONPATH": pythonpath,
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONHASHSEED": "0",
        "AEOS_DATA_DIR": data_dir,
        "AEOS_CONFIG": j("no-config.yaml"),
        "PYTHON_KEYRING_BACKEND": NULL_KEYRING,
        "HTTP_PROXY": DEAD_PROXY,
        "HTTPS_PROXY": DEAD_PROXY,
        "ALL_PROXY": DEAD_PROXY,
        "NO_PROXY": "127.0.0.1,localhost,testserver,::1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": empty_git_config,
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": "core.askPass",
        "GIT_CONFIG_VALUE_1": "",
        "GIT_CONFIG_KEY_2": "credential.interactive",
        "GIT_CONFIG_VALUE_2": "never",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "GCM_INTERACTIVE": "never",
        "GIT_ALLOW_PROTOCOL": "file",
        "GIT_TERMINAL_PROMPT": "0",
        "AEOS_VERIFIER_CHILD": "1",
    }


def scrubbed_env(*, pythonpath: list[Path], home: Path, tmp: Path, data_dir: Path,
                 disable_plugin_autoload: bool) -> dict[str, str]:
    """Allow-list environment for running candidate code under the verifier (process runner)."""
    env: dict[str, str] = {}
    for key in _PASSTHROUGH_ENV:
        val = os.environ.get(key)
        if val and not contains_secret(val):
            env[key] = val
    for d in (home, tmp, data_dir, home / "AppData" / "Roaming", home / "AppData" / "Local", home / ".config",
              home / ".config" / "gh", home / ".local" / "share", home / ".local" / "state", home / ".cache",
              home / ".run", home / ".docker", home / ".claude"):
        d.mkdir(parents=True, exist_ok=True)
    empty = home / EMPTY_GIT_CONFIG
    if not empty.is_file():
        empty.write_text("", encoding="utf-8")
    env.update(child_env_values(home=str(home), tmp=str(tmp), data_dir=str(data_dir),
                                pythonpath=os.pathsep.join(str(p) for p in pythonpath), empty_git_config=str(empty)))
    if sys.platform == "win32":
        drive, rest = os.path.splitdrive(str(home))
        env["HOMEDRIVE"] = drive or "C:"
        env["HOMEPATH"] = rest or "\\"
    if disable_plugin_autoload:
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return env


class GitError(RuntimeError):
    pass


class SnapshotError(RuntimeError):
    pass


def valid_sha(value: str | None) -> bool:
    return bool(value) and bool(_SHA_RE.match(value or ""))


def valid_branch(value: str | None) -> bool:
    """A plain branch name (no refspec syntax, traversal, revision operators or options)."""
    v = value or ""
    return bool(_BRANCH_RE.match(v)) and ".." not in v and "//" not in v and not v.endswith((".", "/", ".lock")) \
        and "@{" not in v


def git_env() -> dict[str, str]:
    """Allow-list environment for git plumbing (no credentials, no GIT_* overrides, no network)."""
    env: dict[str, str] = {}
    for key in _GIT_PASSTHROUGH_ENV:
        val = os.environ.get(key)
        if val and not contains_secret(val):
            env[key] = val
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",  # partial clones must not fetch missing objects (git >= 2.44)
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
    })
    return env


class GitReader:
    """Read-only git plumbing against an untrusted candidate repository."""

    def __init__(self, repo: Path, *, timeout: float = 120.0):
        git = shutil.which("git")
        if not git:
            raise GitError("git executable not found on PATH")
        self.git = git
        self.repo = repo
        self.timeout = timeout
        self.env = git_env()

    def _args(self, *args: str) -> list[str]:
        return [
            self.git, "-c", "core.fsmonitor=false", "-c", "core.quotepath=off", "-c", "diff.noprefix=false",
            "-c", "diff.mnemonicPrefix=false", "-c", "color.ui=false", "-c", "protocol.allow=never",
            "-c", "credential.helper=", "-C", str(self.repo), *args,
        ]

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        try:
            cp = subprocess.run(self._args(*args), capture_output=True, timeout=self.timeout, env=self.env,
                                stdin=subprocess.DEVNULL, creationflags=_creationflags(), check=False)
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {args[0]} timed out") from exc
        if check and cp.returncode != 0:
            raise GitError(f"git {args[0]} failed: {redact_text(cp.stderr.decode('utf-8', 'replace'))[:500]}")
        return cp

    def is_repository(self) -> bool:
        cp = self.run("rev-parse", "--is-inside-work-tree", check=False)
        return cp.returncode == 0 and cp.stdout.strip() == b"true"

    def resolve_commit(self, rev: str) -> str:
        if rev != "HEAD" and not valid_sha(rev):
            raise GitError(f"invalid commit identifier: {rev!r}")
        cp = self.run("rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}")
        return cp.stdout.decode().strip()

    def branch_head(self, branch: str) -> str:
        """Commit a local branch (``refs/heads/<branch>``) points to."""
        if not valid_branch(branch):
            raise GitError(f"invalid branch name: {branch!r}")
        cp = self.run("rev-parse", "--verify", "--quiet", "--end-of-options", f"refs/heads/{branch}^{{commit}}")
        return cp.stdout.decode().strip()

    def tree_of(self, commit: str) -> str:
        return self.run("rev-parse", "--verify", "--end-of-options", f"{commit}^{{tree}}").stdout.decode().strip()

    def is_ancestor(self, base: str, head: str) -> bool:
        return self.run("merge-base", "--is-ancestor", base, head, check=False).returncode == 0

    def changed_files(self, base: str, head: str) -> list[tuple[str, str]]:
        raw = self.run("diff", "--name-status", "--no-renames", "-z", "--no-ext-diff", "--no-textconv", base, head).stdout
        parts = raw.decode("utf-8", "surrogateescape").split("\x00")
        out: list[tuple[str, str]] = []
        i = 0
        while i + 1 < len(parts):
            status, path = parts[i], parts[i + 1]
            if status:
                out.append((status, path))
            i += 2
        return out

    def numstat(self, base: str, head: str) -> dict[str, dict[str, int | None]]:
        raw = self.run("diff", "--numstat", "--no-renames", "-z", "--no-ext-diff", "--no-textconv", base, head).stdout
        stats: dict[str, dict[str, int | None]] = {}
        for rec in raw.decode("utf-8", "surrogateescape").split("\x00"):
            if not rec.strip():
                continue
            fields = rec.split("\t")
            if len(fields) < 3:
                continue
            add, delete, path = fields[0], fields[1], fields[2]
            stats[path] = {"added": int(add) if add.isdigit() else None,
                           "deleted": int(delete) if delete.isdigit() else None}
        return stats

    def diff_bytes(self, base: str, head: str) -> bytes:
        return self.run("diff", "--no-renames", "--no-color", "--no-ext-diff", "--no-textconv", base, head).stdout

    def export_tree(self, commit: str, dest: Path, *, max_bytes: int) -> dict[str, object]:
        """Materialise exactly the blobs of *commit* into *dest* (no attributes/filters/hooks)."""
        listing = self.run("ls-tree", "-r", "-z", "--full-tree", commit).stdout
        entries: list[tuple[str, str, str, str]] = []
        for rec in listing.split(b"\x00"):
            if not rec:
                continue
            meta, _, raw_path = rec.partition(b"\t")
            mode, typ, sha = meta.decode().split(" ")
            try:
                path = raw_path.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SnapshotError("candidate tree contains a non-UTF-8 path") from exc
            entries.append((mode, typ, sha, path))

        skipped: list[dict[str, str]] = []
        seen_casefold: set[str] = set()
        total = 0
        written = 0
        proc = subprocess.Popen(self._args("cat-file", "--batch"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=self.env, creationflags=_creationflags())
        watchdog = threading.Timer(max(self.timeout, 300.0), lambda: kill_tree(proc.pid))
        watchdog.start()
        try:
            assert proc.stdin is not None and proc.stdout is not None
            for mode, typ, sha, path in entries:
                if typ != "blob":
                    skipped.append({"path": path, "reason": f"{typ} entry (submodule) not materialised"})
                    continue
                if mode == "120000":
                    skipped.append({"path": path, "reason": "symlink not materialised"})
                    continue
                pp = PurePosixPath(path)
                if pp.is_absolute() or any(seg in ("..", "") for seg in pp.parts) or "\\" in path or ":" in path:
                    raise SnapshotError(f"unsafe path in candidate tree: {path!r}")
                # Same verdict on every OS: Windows device names (CON, NUL, COM1…) would write to a device
                # and trailing dots/spaces alias other names there.
                reason = unsafe_path_reason(path)
                if reason:
                    raise SnapshotError(f"unsafe path in candidate tree: {path!r} ({reason})")
                folded = path.casefold()
                if folded in seen_casefold:
                    raise SnapshotError(f"case-colliding paths in candidate tree: {path!r}")
                seen_casefold.add(folded)
                proc.stdin.write(sha.encode() + b"\n")
                proc.stdin.flush()
                header = proc.stdout.readline().split()
                if len(header) != 3 or header[1] != b"blob":
                    raise SnapshotError(f"git cat-file returned an unexpected header for {path!r}")
                size = int(header[2])
                total += size
                if total > max_bytes:
                    raise SnapshotError("candidate tree exceeds the snapshot size limit")
                remaining, chunks = size, []
                while remaining:
                    chunk = proc.stdout.read(remaining)
                    if not chunk:
                        raise SnapshotError("git cat-file stream ended early")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                proc.stdout.read(1)  # trailing LF
                target = dest.joinpath(*pp.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    target.write_bytes(b"".join(chunks))
                except OSError as exc:
                    raise SnapshotError(f"cannot materialise {path!r} on this filesystem: {exc}") from exc
                if mode == "100755" and sys.platform != "win32":
                    target.chmod(0o755)
                written += 1
        finally:
            watchdog.cancel()
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                kill_tree(proc.pid)
        return {"files": written, "bytes": total, "skipped": skipped}
