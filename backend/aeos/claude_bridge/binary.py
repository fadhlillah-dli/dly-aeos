"""Locate and probe the official Claude Code binary; parse the native auth mode (§12, §08).

Only the unmodified official binary with the owner's native claude.ai login (Max) is accepted.
``claude auth status`` runs with the same scrubbed environment the bridge uses, so its answer is
what a bridge child would actually see. Anything other than ``loggedIn=true``,
``authMethod=claude.ai`` and ``apiProvider=firstParty`` is BLOCKED — never an API fallback.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aeos.claude_bridge.env import child_env
from aeos.core.config import Settings
from aeos.core.hashing import file_sha256
from aeos.core.redaction import redact_text

REQUIRED_AUTH_METHOD = "claude.ai"
REQUIRED_API_PROVIDER = "firstParty"
EXPECTED_SUBSCRIPTION = "max"
_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")
_PROBE_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class BinaryInfo:
    found: bool
    path: Path | None = None
    source: str = ""  # settings.claude_binary | PATH
    version: str | None = None
    fingerprint: str | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def logical_path(self) -> str:
        """§37: local paths can be sensitive — the UI gets a logical name, not the full path."""
        if not self.path:
            return "UNKNOWN"
        return f"{self.path.name} ({self.source})"


@dataclass(frozen=True)
class AuthInfo:
    status: str  # VERIFIED | DEGRADED | BLOCKED | UNKNOWN
    logged_in: bool | None = None
    auth_method: str | None = None
    api_provider: str | None = None
    subscription_type: str | None = None
    email: str | None = None  # the owner's own account e-mail; never tokens
    reasons: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "loggedIn": self.logged_in,
            "authMethod": self.auth_method,
            "apiProvider": self.api_provider,
            "subscriptionType": self.subscription_type,
            "email": self.email,
            "reasons": list(self.reasons),
        }


def _is_batch_shim(path: Path) -> bool:
    return path.suffix.lower() in {".cmd", ".bat", ".ps1"}


def locate_binary(settings: Settings) -> BinaryInfo:
    """Find the official binary. On Windows only a real ``claude.exe`` is accepted: npm ``.cmd``
    shims go through cmd.exe, whose quoting cannot safely carry the JSON schema argument."""
    candidates: list[tuple[Path, str]] = []
    if settings.claude_binary:
        candidates.append((Path(settings.claude_binary).expanduser(), "settings.claude_binary"))
    else:
        names = ("claude.exe", "claude") if sys.platform == "win32" else ("claude",)
        for name in names:
            found = shutil.which(name)
            if found:
                candidates.append((Path(found), "PATH"))
                break
    if not candidates:
        return BinaryInfo(found=False, reasons=["official Claude Code binary not found on PATH"])
    path, source = candidates[0]
    if not path.is_file():
        return BinaryInfo(found=False, path=path, source=source, reasons=[f"configured binary does not exist ({source})"])
    if sys.platform == "win32" and _is_batch_shim(path):
        return BinaryInfo(
            found=False, path=path, source=source,
            reasons=["only a script shim (.cmd/.bat/.ps1) was found; install the native build "
                     "(`claude install` or `winget install Anthropic.ClaudeCode`) so arguments are not re-parsed by cmd.exe"],
        )
    return BinaryInfo(found=True, path=path, source=source)


def neutral_cwd() -> str:
    """Working directory for probes: never the controller's cwd (it may be a repository whose
    project settings/hooks the client would read, S05). The OS temp dir holds no project config."""
    return tempfile.gettempdir()


def _run(argv: list[str], *, timeout: float = 30.0) -> tuple[int, str, str]:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace", env=child_env(),
        stdin=subprocess.DEVNULL, timeout=timeout, check=False, cwd=neutral_cwd(), **kwargs,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


_fp_cache: dict[tuple[str, int, int], str] = {}
_fp_lock = threading.Lock()


def fingerprint(path: Path) -> str | None:
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_size, int(st.st_mtime))
    with _fp_lock:
        if key in _fp_cache:
            return _fp_cache[key]
    fp = file_sha256(path)
    with _fp_lock:
        _fp_cache[key] = fp
    return fp


def probe_version(info: BinaryInfo) -> BinaryInfo:
    if not info.found or info.path is None:
        return info
    try:
        rc, out, err = _run([str(info.path), "--version"], timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return BinaryInfo(found=False, path=info.path, source=info.source,
                          reasons=[f"binary could not be executed: {type(exc).__name__}"])
    m = _VERSION_RE.search(out)
    reasons = [] if rc == 0 and m else [f"`claude --version` failed (exit {rc}): {redact_text(err.strip())[:200]}"]
    return BinaryInfo(found=rc == 0 and bool(m), path=info.path, source=info.source,
                      version=m.group(1) if m else None, fingerprint=fingerprint(info.path), reasons=reasons)


def parse_auth_status(rc: int, stdout: str, stderr: str = "") -> AuthInfo:
    """Interpret ``claude auth status`` JSON. Pure function (unit-tested with fixtures)."""
    try:
        data = json.loads(stdout.strip() or "{}")
    except json.JSONDecodeError:
        return AuthInfo(status="UNKNOWN", reasons=[f"`claude auth status` output is not JSON (exit {rc})"])
    if not isinstance(data, dict):
        return AuthInfo(status="UNKNOWN", reasons=["`claude auth status` returned an unexpected shape"])
    logged_in = data.get("loggedIn")
    method = data.get("authMethod")
    provider = data.get("apiProvider")
    sub = data.get("subscriptionType")
    email = data.get("email") if isinstance(data.get("email"), str) else None
    reasons: list[str] = []
    if logged_in is not True:
        reasons.append("not logged in: run `claude` and complete the official claude.ai login (/login)")
    if method != REQUIRED_AUTH_METHOD:
        reasons.append(f"authMethod is {method!r}; only the native claude.ai (Max) login is allowed - "
                       "API keys/console/third-party auth are never used as a fallback")
    if provider != REQUIRED_API_PROVIDER:
        reasons.append(f"apiProvider is {provider!r}; Bedrock/Vertex/Foundry/other providers are not allowed")
    if reasons:
        return AuthInfo(status="BLOCKED", logged_in=logged_in if isinstance(logged_in, bool) else None,
                        auth_method=method, api_provider=provider, subscription_type=sub, email=email, reasons=reasons)
    if sub != EXPECTED_SUBSCRIPTION:
        return AuthInfo(status="DEGRADED", logged_in=True, auth_method=method, api_provider=provider,
                        subscription_type=sub, email=email,
                        reasons=[f"subscriptionType is {sub!r} (expected 'max'): supervisor model availability and "
                                 "limits differ; Fable may be unavailable"])
    return AuthInfo(status="VERIFIED", logged_in=True, auth_method=method, api_provider=provider,
                    subscription_type=sub, email=email)


def probe_auth(info: BinaryInfo) -> AuthInfo:
    if not info.found or info.path is None:
        return AuthInfo(status="UNKNOWN", reasons=["binary not available"])
    try:
        rc, out, err = _run([str(info.path), "auth", "status"], timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return AuthInfo(status="UNKNOWN", reasons=[f"`claude auth status` failed: {type(exc).__name__}"])
    return parse_auth_status(rc, out, err)


@dataclass(frozen=True)
class Probe:
    binary: BinaryInfo
    auth: AuthInfo
    checked_at: float

    @property
    def usable(self) -> bool:
        return self.binary.found and self.auth.status in {"VERIFIED", "DEGRADED"}

    @property
    def reasons(self) -> list[str]:
        return [*self.binary.reasons, *self.auth.reasons]


_probe_lock = threading.Lock()
_probe_cache: dict[str, Probe] = {}


def probe(settings: Settings, *, refresh: bool = False) -> Probe:
    """Binary + version + fingerprint + auth, cached briefly (status page polling)."""
    key = settings.claude_binary or "<PATH>"
    now = time.monotonic()
    with _probe_lock:
        cached = _probe_cache.get(key)
    if cached and not refresh and now - cached.checked_at < _PROBE_TTL_SECONDS:
        return cached
    info = probe_version(locate_binary(settings))
    auth = probe_auth(info) if info.found else AuthInfo(status="UNKNOWN", reasons=["binary not available"])
    result = Probe(binary=info, auth=auth, checked_at=now)
    with _probe_lock:
        _probe_cache[key] = result
    return result


def clear_probe_cache() -> None:
    with _probe_lock:
        _probe_cache.clear()
