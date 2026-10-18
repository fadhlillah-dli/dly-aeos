"""Internal helpers shared by the operations module (not a cross-module contract)."""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

from aeos.core.config import Settings
from aeos.core.errors import PolicyDenied

# Environment markers a worker sandbox / candidate build carries. Their presence means the caller is
# not the human operator, so human-only operations (promotion, live restore) are refused (§09, §40).
WORKER_CONTEXT_ENV = (
    "AEOS_RUN_ID",
    "AEOS_WORKER_RUN_ID",
    "AEOS_TASK_ID",
    "AEOS_CAPABILITY_TOKEN",
    "AEOS_SANDBOX",
    "AEOS_CANDIDATE_BUILD",
)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON via a temp file + ``os.replace`` so readers never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, default=str, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def port_in_use(host: str, port: int, timeout: float = 0.5) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            return sock.connect_ex((host, port)) == 0
        except OSError:
            return False


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def find_server_processes() -> list[dict[str, Any]]:
    """Processes that look like a running control plane (``aeos serve`` / uvicorn on aeos.api.app).

    Best effort: processes we may not inspect (AccessDenied) are skipped, so the port check stays
    the primary signal.
    """
    me = os.getpid()
    found: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            if proc.info["pid"] == me:
                continue
            cmd = [str(c) for c in (proc.info.get("cmdline") or [])]
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        tokens = [Path(c).name.lower() for c in cmd]
        is_aeos = any(t in ("aeos", "aeos.exe", "aeos.ps1") for t in tokens) or (
            "-m" in tokens and "aeos" in tokens
        )
        if (is_aeos and "serve" in tokens) or any("aeos.api.app" in c for c in cmd):
            found.append({"pid": proc.info["pid"]})
    return found


def server_status(settings: Settings) -> dict[str, Any]:
    busy = port_in_use(settings.host, settings.port)
    pids = find_server_processes()
    return {"host": settings.host, "port": settings.port, "port_busy": busy, "processes": pids,
            "running": busy or bool(pids)}


def refuse_worker_context(action: str) -> None:
    present = [k for k in WORKER_CONTEXT_ENV if os.environ.get(k)]
    if present:
        raise PolicyDenied(
            f"{action} is a human-only operation and is refused inside a worker/candidate context",
            code="HUMAN_ONLY_OPERATION",
            details={"markers": present},
        )


def ops_tmp_dir(settings: Settings, prefix: str) -> Path:
    """Scratch space inside the data directory (never the repo, never a world-readable location)."""
    base = settings.layout.root / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=base))


def _make_writable_and_retry(func: Callable[..., Any], path: str, *_args: Any) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass
    func(path)


def rmtree_robust(path: Path, attempts: int = 5) -> None:
    """Remove a directory tree; tolerate read-only files and short-lived Windows file locks."""
    for i in range(attempts):
        if not path.exists():
            return
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=_make_writable_and_retry)
            else:  # pragma: no cover - Python 3.11
                shutil.rmtree(path, onerror=_make_writable_and_retry)
            return
        except OSError:
            if i == attempts - 1:
                raise
            time.sleep(0.2 * (i + 1))


def minimal_child_env(extra: dict[str, str]) -> dict[str, str]:
    """Environment for child processes running candidate/release code: no AEOS_* overrides from the
    operator shell, no provider credentials (ANTHROPIC_API_KEY & co never reach the child)."""
    keep = {
        "PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR",
        "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "LANG",
        "LC_ALL", "VIRTUAL_ENV",
    }
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONNOUSERSITE"] = "1"
    env.update(extra)
    return env
