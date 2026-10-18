"""Read-only git access for repository references, inventory and retrieval (§24, §37).

Rules:
* only read commands (rev-parse, status, ls-tree, grep, cat-file, remote get-url) — never
  checkout, fetch, commit or anything that writes the repository;
* list arguments, no shell, hard timeouts and streamed output caps (Windows + macOS + Linux);
* inherited ``GIT_*`` variables are dropped so a caller's environment (hooks, worktrees, CI) can
  never redirect the command to another repository; ``core.fsmonitor`` is forced off because a
  repository-local fsmonitor hook would otherwise execute on ``git status``;
* content is read from the object database at an exact commit (``git cat-file blob <sha>:<path>``,
  no textconv/filters), so a symlink in the working tree cannot make AEOS read a file outside the
  repository.

Residual risk (ADR-0022): repository-local clean/smudge filter drivers may run during
``git status`` on racily-clean files; repositories are chosen explicitly by the human admin.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from aeos.core.errors import Blocked, ValidationFailed

DEFAULT_TIMEOUT = 20.0
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,199}$")


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: bytes
    stderr: str
    truncated: bool = False

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")


class GitUnavailable(Blocked):
    code = "GIT_UNAVAILABLE"


def git_binary() -> str:
    exe = shutil.which("git")
    if not exe:
        raise GitUnavailable("git executable not found on PATH; install Git and re-run `aeos doctor`")
    return exe


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C", "LANG": "C"})
    return env


def _popen_kwargs() -> dict[str, object]:
    if sys.platform == "win32":  # no console window flashes when the server runs detached
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def run_git(
    repo: Path, args: list[str], *, timeout: float = DEFAULT_TIMEOUT, max_output: int = MAX_OUTPUT_BYTES,
    check: bool = True,
) -> GitResult:
    """Run one read-only git command inside *repo*. Raises ``Blocked`` on timeout/unavailability and
    ``ValidationFailed`` on a non-zero exit when *check* is set.

    stdout is streamed and capped at *max_output* bytes: once the cap is reached the process is
    killed and the result is marked ``truncated`` (a huge blob or grep line is never buffered whole).
    """
    cmd = [git_binary(), "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "--no-pager",
           "-C", str(repo), *args]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=_env(), **_popen_kwargs())  # type: ignore[call-overload]
    except OSError as exc:
        raise GitUnavailable(f"git could not be started: {exc.__class__.__name__}") from exc
    timed_out = threading.Event()

    def _on_timeout() -> None:
        timed_out.set()
        with contextlib.suppress(OSError):
            proc.kill()

    err = bytearray()

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for chunk in iter(lambda: proc.stderr.read(8192), b""):  # type: ignore[union-attr]
            if len(err) < MAX_STDERR_BYTES:
                err.extend(chunk[: MAX_STDERR_BYTES - len(err)])

    timer = threading.Timer(timeout, _on_timeout)
    timer.daemon = True
    drain = threading.Thread(target=_drain_stderr, daemon=True)
    out = bytearray()
    truncated = False
    timer.start()
    drain.start()
    try:
        assert proc.stdout is not None
        for chunk in iter(lambda: proc.stdout.read(65536), b""):  # type: ignore[union-attr]
            room = max_output - len(out)
            if len(chunk) > room:
                out.extend(chunk[:room])
                truncated = True
                with contextlib.suppress(OSError):
                    proc.kill()
                break
            out.extend(chunk)
        try:
            proc.wait(timeout=max(1.0, timeout))
        except subprocess.TimeoutExpired:
            _on_timeout()
            proc.wait(timeout=5)
    finally:
        timer.cancel()
        drain.join(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            with contextlib.suppress(OSError, ValueError):
                if stream is not None:
                    stream.close()
    if timed_out.is_set() and not truncated:
        raise Blocked(f"git {args[0]} timed out after {timeout:.0f}s", code="GIT_TIMEOUT")
    # A process we killed because the output cap was reached is not a git failure.
    returncode = 0 if truncated else proc.returncode
    err_text = bytes(err).decode("utf-8", errors="replace").strip()
    if check and returncode != 0:
        raise ValidationFailed(f"git {args[0]} failed: {err_text[:300]}", code="GIT_FAILED",
                               details={"returncode": returncode})
    return GitResult(returncode, bytes(out), err_text, truncated)


def validate_commitish(value: str) -> str:
    """Accept a hex SHA or a simple ref name; reject option-like or path-like input."""
    v = (value or "").strip()
    if not v or v.startswith("-") or ".." in v or not (SHA_RE.match(v) or _REF_RE.match(v)):
        raise ValidationFailed(f"invalid commit reference: {value!r}", code="INVALID_COMMIT")
    return v


def toplevel(path: Path) -> Path:
    res = run_git(path, ["rev-parse", "--show-toplevel"], check=False)
    if res.returncode != 0:
        raise ValidationFailed("path is not inside a git repository", code="NOT_A_GIT_REPO",
                               details={"git": res.stderr[:200]})
    return Path(res.text.strip()).resolve()


def resolve_commit(repo: Path, commitish: str = "HEAD") -> str:
    ref = validate_commitish(commitish)
    res = run_git(repo, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], check=False)
    sha = res.text.strip()
    if res.returncode != 0 or not SHA_RE.match(sha):
        raise ValidationFailed(f"commit {commitish!r} not found in repository", code="COMMIT_NOT_FOUND")
    return sha


def current_branch(repo: Path) -> str | None:
    res = run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"], check=False)
    name = res.text.strip()
    return None if res.returncode != 0 or not name or name == "HEAD" else name


def is_dirty(repo: Path) -> tuple[bool, int]:
    """Working-tree dirty state (tracked + untracked) at workspace scope; read-only."""
    res = run_git(repo, ["status", "--porcelain=v1", "-z", "--ignore-submodules=all"], timeout=60)
    entries = [e for e in res.stdout.split(b"\0") if e.strip()]
    return bool(entries), len(entries)


def remote_url(repo: Path, name: str = "origin") -> str | None:
    res = run_git(repo, ["remote", "get-url", name], check=False)
    if res.returncode != 0:
        return None
    return res.text.strip() or None


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    kind: str  # blob|tree|commit
    sha: str
    size: int | None
    path: str

    @property
    def is_symlink(self) -> bool:
        return self.mode == "120000"

    @property
    def is_submodule(self) -> bool:
        return self.kind == "commit"


def ls_tree(repo: Path, commit: str, *, max_entries: int = 100_000) -> tuple[list[TreeEntry], bool]:
    """All entries of *commit* (recursive, with sizes). Returns (entries, truncated)."""
    res = run_git(repo, ["ls-tree", "-r", "-l", "-z", "--full-tree", commit], timeout=60)
    entries: list[TreeEntry] = []
    truncated = res.truncated
    for raw in res.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            meta, path = raw.split(b"\t", 1)
            mode, kind, sha, size = meta.decode().split()
        except ValueError:
            continue
        entries.append(TreeEntry(mode, kind, sha, int(size) if size.isdigit() else None,
                                 path.decode("utf-8", errors="replace")))
        if len(entries) >= max_entries:
            truncated = True
            break
    return entries, truncated


def show_blob(repo: Path, commit: str, path: str, *, max_bytes: int = 512 * 1024,
              complete_only: bool = False) -> bytes | None:
    """Content of *path* at *commit* from the object DB (never the working tree).

    ``git cat-file blob`` never runs textconv or clean/smudge filters. With *complete_only* a blob
    larger than *max_bytes* returns ``None`` instead of a truncated prefix.
    """
    if path.startswith(("/", "\\")) or ".." in Path(path).parts or ":" in path.split("/")[0]:
        raise ValidationFailed("invalid repository path", code="INVALID_REPO_PATH")
    res = run_git(repo, ["cat-file", "blob", f"{commit}:{path}"], check=False, max_output=max_bytes)
    if res.returncode != 0 or (complete_only and res.truncated):
        return None
    return res.stdout


def grep(
    repo: Path, commit: str, pattern: str, *, extended: bool = False, ignore_case: bool = True,
    max_count_per_file: int = 5, timeout: float = 30.0,
) -> list[tuple[str, int, str]]:
    """``git grep -n`` over the tree at *commit*. Returns (path, line, text) tuples."""
    args = ["grep", "-n", "-I", "--no-color", f"--max-count={max_count_per_file}"]
    args.append("-E" if extended else "-F")
    if ignore_case:
        args.append("-i")
    args += ["-e", pattern, commit, "--"]
    res = run_git(repo, args, check=False, timeout=timeout, max_output=8 * 1024 * 1024)
    if res.returncode not in (0, 1):  # 1 = no match
        raise ValidationFailed(f"git grep failed: {res.stderr[:200]}", code="GIT_GREP_FAILED")
    hits: list[tuple[str, int, str]] = []
    prefix = f"{commit}:"
    for line in res.text.splitlines():
        if line.startswith(prefix):
            line = line[len(prefix):]
        parts = line.split(":", 2)
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        hits.append((parts[0], int(parts[1]), parts[2]))
    return hits
