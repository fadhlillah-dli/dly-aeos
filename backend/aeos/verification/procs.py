"""Cross-platform subprocess helpers for the verifier (Windows, macOS, Linux).

* list arguments only (no shell), explicit timeouts, whole process tree killed on timeout (psutil);
* a scrubbed child environment built from an allow-list — no credentials, no provider keys, no
  ``AEOS_*`` overrides, ``HOME``/temp redirected into the verifier's work dir, keyring disabled and
  outbound HTTP pointed at a dead proxy as an egress deterrent (not an enforcement claim);
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
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import psutil

from aeos.core.redaction import contains_secret, redact_text

_PASSTHROUGH_ENV = (
    "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "OS", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
)
DEAD_PROXY = "http://127.0.0.1:9"
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_CREATE_NO_WINDOW = 0x08000000


@dataclass
class ProcResult:
    args: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float

    def tail(self, limit: int = 8000) -> str:
        text = (self.stdout or "") + ("\n" + self.stderr if self.stderr else "")
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


def run_process(args: list[str], *, cwd: Path, env: dict[str, str], timeout: float,
                max_output: int = 4_000_000) -> ProcResult:
    start = time.monotonic()
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
    )


def scrubbed_env(*, pythonpath: list[Path], home: Path, tmp: Path, data_dir: Path,
                 disable_plugin_autoload: bool) -> dict[str, str]:
    """Allow-list environment for running candidate code under the verifier."""
    env: dict[str, str] = {}
    for key in _PASSTHROUGH_ENV:
        val = os.environ.get(key)
        if val and not contains_secret(val):
            env[key] = val
    for d in (home, tmp, data_dir, home / "AppData" / "Roaming", home / "AppData" / "Local", home / ".config"):
        d.mkdir(parents=True, exist_ok=True)
    env.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "TMPDIR": str(tmp),
        "TEMP": str(tmp),
        "TMP": str(tmp),
        "PYTHONPATH": os.pathsep.join(str(p) for p in pythonpath),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONHASHSEED": "0",
        "AEOS_DATA_DIR": str(data_dir),
        "AEOS_CONFIG": str(home / "no-config.yaml"),
        "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
        "HTTP_PROXY": DEAD_PROXY,
        "HTTPS_PROXY": DEAD_PROXY,
        "ALL_PROXY": DEAD_PROXY,
        "NO_PROXY": "127.0.0.1,localhost,testserver,::1",
        "GIT_TERMINAL_PROMPT": "0",
        "AEOS_VERIFIER_CHILD": "1",
    })
    if disable_plugin_autoload:
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return env


class GitError(RuntimeError):
    pass


class SnapshotError(RuntimeError):
    pass


def valid_sha(value: str | None) -> bool:
    return bool(value) and bool(_SHA_RE.match(value or ""))


class GitReader:
    """Read-only git plumbing against an untrusted candidate repository."""

    def __init__(self, repo: Path, *, timeout: float = 120.0):
        git = shutil.which("git")
        if not git:
            raise GitError("git executable not found on PATH")
        self.git = git
        self.repo = repo
        self.timeout = timeout
        self.env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        self.env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_NOSYSTEM": "1"})

    def _args(self, *args: str) -> list[str]:
        return [
            self.git, "-c", "core.fsmonitor=false", "-c", "core.quotepath=off", "-c", "diff.noprefix=false",
            "-c", "diff.mnemonicPrefix=false", "-c", "color.ui=false", "-C", str(self.repo), *args,
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
