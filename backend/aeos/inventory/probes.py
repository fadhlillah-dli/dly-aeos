"""Read-only, time-bounded, non-LLM probe primitives shared by the Doctor and the reconciler (§08, §38).

Rules this module enforces:

* subprocesses use list argv (never ``shell=True``), a hard timeout, no stdin, and the full process
  tree is killed on timeout (psutil) — Windows ``.cmd`` shims included;
* HTTP probes go to loopback hosts only unless the caller explicitly opts into a remote, read-only
  request (the Doctor's ``--functional`` GitHub probe);
* nothing here scans the machine: callers name the exact binary/URL/file they probe;
* output that reaches storage is redacted and local paths are reduced to logical names.

Tests inject a fake runner with :func:`set_runner_override` (same pattern as the secrets provider).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from aeos.core.hashing import sha256_text
from aeos.core.redaction import redact_text

log = logging.getLogger("aeos.inventory.probes")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
PROVIDER_ENV_PREFIXES = ("ANTHROPIC_",)
PROVIDER_ENV_NAMES = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
MAX_OUTPUT_CHARS = 20_000


class ProbeRefused(Exception):
    """The probe would leave the allowed scope (e.g. a non-loopback URL)."""


@dataclass(frozen=True)
class RunResult:
    returncode: int | None
    stdout: str
    stderr: str
    error: str | None = None  # NOT_FOUND | TIMEOUT | OS_ERROR:<detail>
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None and self.returncode == 0


@dataclass(frozen=True)
class HttpResult:
    status: int | None
    body: Any = None
    text: str = ""
    error: str | None = None  # UNREACHABLE | TIMEOUT | REFUSED | HTTP_ERROR:<detail>
    headers: dict[str, str] = field(default_factory=dict)


class ProbeRunner(Protocol):
    def which(self, name: str) -> str | None: ...

    def run(self, argv: Sequence[str], *, timeout: float, env: Mapping[str, str] | None = None,
            cwd: Path | None = None) -> RunResult: ...

    def http_get(self, url: str, *, timeout: float, headers: Mapping[str, str] | None = None,
                 allow_remote: bool = False) -> HttpResult: ...

    def environ(self) -> Mapping[str, str]: ...

    def stat(self, path: str) -> dict[str, Any] | None: ...


def is_loopback_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").strip("[]").lower()
    except ValueError:
        return False
    return host in LOOPBACK_HOSTS


def _kill_tree(pid: int) -> None:
    try:
        import psutil

        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for p in [*children, parent]:
            try:
                p.kill()
            except psutil.Error:
                pass
        psutil.wait_procs([*children, parent], timeout=3)
    except Exception:  # pragma: no cover - best effort; the caller still reports TIMEOUT
        log.warning("could not kill process tree %s after probe timeout", pid)


class SystemProbeRunner:
    """Real implementation used outside tests."""

    def which(self, name: str) -> str | None:
        if not name or any(sep in name for sep in ("/", "\\")):
            return None
        return shutil.which(name)

    def run(self, argv: Sequence[str], *, timeout: float, env: Mapping[str, str] | None = None,
            cwd: Path | None = None) -> RunResult:
        start = time.monotonic()
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(
                list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=dict(env) if env is not None else None, cwd=str(cwd) if cwd else None, **kwargs,
            )
        except FileNotFoundError:
            return RunResult(None, "", "", "NOT_FOUND", 0)
        except OSError as exc:
            return RunResult(None, "", "", f"OS_ERROR:{type(exc).__name__}", 0)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc.pid)
            try:
                out, err = proc.communicate(timeout=5)
            except Exception:
                out, err = b"", b""
            return RunResult(None, _decode(out), _decode(err), "TIMEOUT", int((time.monotonic() - start) * 1000))
        return RunResult(proc.returncode, _decode(out), _decode(err), None, int((time.monotonic() - start) * 1000))

    def http_get(self, url: str, *, timeout: float, headers: Mapping[str, str] | None = None,
                 allow_remote: bool = False) -> HttpResult:
        if not allow_remote and not is_loopback_url(url):
            raise ProbeRefused(f"non-loopback probe refused: {urlparse(url).hostname}")
        try:
            # Loopback probes ignore proxy environment variables; explicit remote probes honour them.
            with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=allow_remote) as client:
                r = client.get(url, headers=dict(headers or {}))
        except httpx.TimeoutException:
            return HttpResult(None, error="TIMEOUT")
        except httpx.ConnectError:
            return HttpResult(None, error="UNREACHABLE")
        except httpx.HTTPError as exc:
            return HttpResult(None, error=f"HTTP_ERROR:{type(exc).__name__}")
        body: Any = None
        try:
            body = r.json()
        except ValueError:
            body = None
        return HttpResult(r.status_code, body, r.text[:MAX_OUTPUT_CHARS], None,
                          {k.lower(): v for k, v in r.headers.items() if k.lower() in ("x-github-sso", "x-oauth-scopes")})

    def environ(self) -> Mapping[str, str]:
        return dict(os.environ)

    def stat(self, path: str) -> dict[str, Any] | None:
        try:
            st = Path(path).stat()
        except OSError:
            return None
        return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _decode(data: bytes | None) -> str:
    if not data:
        return ""
    return data.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS]


_override: ProbeRunner | None = None


def set_runner_override(runner: ProbeRunner | None) -> None:
    """Tests inject a fake runner here."""
    global _override
    _override = runner


def get_runner() -> ProbeRunner:
    return _override if _override is not None else SystemProbeRunner()


# ---------------------------------------------------------------- helpers


def parse_version(text: str, regex: str | None = None) -> str | None:
    pattern = regex or r"(\d+\.\d+(?:\.\d+)?)"
    try:
        m = re.search(pattern, text or "")
    except re.error:
        return None
    if not m:
        return None
    return m.group(1) if m.groups() else m.group(0)


def version_tuple(v: str | None) -> tuple[int, ...]:
    if not v:
        return ()
    parts = re.findall(r"\d+", v)
    return tuple(int(p) for p in parts[:4])


def scrubbed_env(base: Mapping[str, str], prefixes: Sequence[str] = PROVIDER_ENV_PREFIXES,
                 names: Sequence[str] = PROVIDER_ENV_NAMES) -> dict[str, str]:
    """Child environment without provider credentials/routing variables (§12)."""
    return {k: v for k, v in base.items() if not k.upper().startswith(tuple(prefixes)) and k.upper() not in names}


def logical_location(path: str | os.PathLike[str] | None, *, repo: Path | None = None,
                     data_dir: Path | None = None) -> str | None:
    """Reduce a local path to a logical name (§37: paths may be sensitive)."""
    if path is None:
        return None
    try:
        p = Path(path)
        resolved = p.resolve() if p.is_absolute() else p
    except (OSError, ValueError):
        return "<path>"
    candidates: list[tuple[str, Path]] = []
    if repo is not None:
        candidates.append(("<repo>", repo))
    if data_dir is not None:
        candidates.append(("<data>", data_dir))
    for label, root in candidates:
        try:
            rel = resolved.relative_to(root.resolve())
            return f"{label}/{rel.as_posix()}" if str(rel) != "." else label
        except (ValueError, OSError):
            continue
    try:
        resolved.relative_to(Path.home().resolve())
        return f"<home>/.../{resolved.name}"
    except (ValueError, OSError, RuntimeError):
        pass
    return f"<system>/.../{resolved.name}" if resolved.name else "<system>"


def path_fingerprint(runner: ProbeRunner, path: str | None) -> dict[str, Any]:
    """Cheap identity of a binary/file (hash of path + size + mtime); no process is spawned."""
    if not path:
        return {"present": False}
    st = runner.stat(path)
    return {"present": st is not None, "path_sha": sha256_text(str(path))[:16], **(st or {})}


def clean_output(text: str, limit: int = 400) -> str:
    """First non-empty lines of tool output, redacted and ASCII-friendly for storage."""
    text = redact_text(text or "").strip()
    return text[:limit]


def strip_url_credentials(url: str) -> str:
    """Remove userinfo (tokens) from a git remote URL before storing it."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return "<invalid-url>"
    if parsed.username or parsed.password:
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc += f":{parsed.port}"
        parsed = parsed._replace(netloc=netloc)
        return parsed.geturl()
    return redact_text(url)


_REMOTE_RE = re.compile(r"(?:github\.com[:/])([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def github_slug_from_remote(url: str) -> str | None:
    m = _REMOTE_RE.search(url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None
