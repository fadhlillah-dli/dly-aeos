"""Parent side of parser isolation (§05 step 5, §25).

Each attachment is parsed by a fresh ``python -I`` process (``parse_worker.py``) with:
* a minimal environment (no AEOS/credential variables, no PYTHON* variables);
* a private temporary working directory;
* a wall-clock timeout, a memory cap (process-tree RSS, polled with psutil on every OS; plus
  self-imposed RLIMIT_AS/CPU/FSIZE in the worker on POSIX) and an output-size cap that is also
  polled while the worker runs (Windows has no RLIMIT_FSIZE);
* process-tree kill on any violation (the Windows venv launcher spawns a child interpreter).

Everything the worker returns is untrusted: error messages and metadata can echo file content
(e.g. a YAML error quoting the offending line), so they are redacted before they are persisted.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from aeos.core.redaction import redact, redact_text

WORKER_PATH = Path(__file__).resolve().with_name("parse_worker.py")
PARSE_TIMEOUT_SECONDS = 30.0
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_MEMORY_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_CHARS = 2_000_000


@dataclass
class ParseOutcome:
    status: str  # PARSED | FAILED | SKIPPED
    text: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    error_code: str = ""
    duration_ms: int = 0


def worker_command(src: Path, dst: Path, kind: str, max_chars: int) -> list[str]:
    """Command line for the isolated worker (tests may monkeypatch this)."""
    return [sys.executable, "-I", str(WORKER_PATH), str(src), str(dst), kind, str(max_chars)]


def _minimal_env(tmp: Path) -> dict[str, str]:
    env = {"TEMP": str(tmp), "TMP": str(tmp), "TMPDIR": str(tmp), "HOME": str(tmp), "LANG": "C.UTF-8",
           "PYTHONIOENCODING": "utf-8"}
    if sys.platform == "win32":
        for k in ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS"):
            if os.environ.get(k):
                env[k] = os.environ[k]
        sysroot = os.environ.get("SYSTEMROOT", r"C:\Windows")
        env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), str(Path(sysroot) / "System32")])
        env["USERPROFILE"] = str(tmp)
    else:
        env["PATH"] = "/usr/bin:/bin"
    return env


def kill_tree(pid: int) -> None:
    """Kill *pid* and all its descendants; tolerant of processes exiting concurrently."""
    try:
        parent = psutil.Process(pid)
    except psutil.Error:
        return
    children: list[psutil.Process] = []
    with contextlib.suppress(psutil.Error):
        children = parent.children(recursive=True)
    procs = [*children, parent]
    for p in procs:
        with contextlib.suppress(psutil.Error):
            p.kill()
    with contextlib.suppress(psutil.Error):
        psutil.wait_procs(procs, timeout=5)


def _tree_rss(pid: int) -> int:
    try:
        parent = psutil.Process(pid)
        procs = [parent, *parent.children(recursive=True)]
    except psutil.Error:
        return 0
    total = 0
    for p in procs:
        with contextlib.suppress(psutil.Error):
            total += p.memory_info().rss
    return total


def _output_size(dst: Path) -> int:
    total = 0
    for f in (dst, dst.with_suffix(".partial")):
        with contextlib.suppress(OSError):
            total = max(total, f.stat().st_size)
    return total


def run_parser(
    src: Path, kind: str, *, max_chars: int = DEFAULT_MAX_CHARS, timeout: float | None = None,
    max_output_bytes: int = MAX_OUTPUT_BYTES, max_memory_bytes: int = MAX_MEMORY_BYTES,
) -> ParseOutcome:
    timeout = PARSE_TIMEOUT_SECONDS if timeout is None else timeout
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="aeos-parse-") as tmpdir:
        tmp = Path(tmpdir)
        dst = tmp / "result.json"
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
            "cwd": str(tmp), "env": _minimal_env(tmp),
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            # No preexec_fn (unsafe with threads); the worker sets its own rlimits on start.
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(worker_command(src, dst, kind, max_chars), **kwargs)
        except OSError as exc:
            return ParseOutcome("FAILED", error=f"parser could not start: {exc.__class__.__name__}",
                                error_code="PARSER_UNAVAILABLE")
        deadline = started + timeout
        violation = ""
        while True:
            try:
                proc.wait(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() > deadline:
                violation = "PARSE_TIMEOUT"
            elif _tree_rss(proc.pid) > max_memory_bytes:
                violation = "PARSE_MEMORY_LIMIT"
            elif _output_size(dst) > max_output_bytes:
                violation = "PARSE_OUTPUT_TOO_LARGE"
            if violation:
                kill_tree(proc.pid)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
                break
        duration = int((time.monotonic() - started) * 1000)
        if violation == "PARSE_TIMEOUT":
            return ParseOutcome("FAILED", error=f"parser exceeded {timeout:.0f}s and was killed",
                                error_code=violation, duration_ms=duration)
        if violation == "PARSE_OUTPUT_TOO_LARGE":
            return ParseOutcome("FAILED", error="parser output exceeded the size cap and the parser was killed",
                                error_code=violation, duration_ms=duration)
        if violation:
            return ParseOutcome("FAILED", error="parser exceeded its memory cap and was killed",
                                error_code=violation, duration_ms=duration)
        if not dst.is_file():
            return ParseOutcome("FAILED", error=f"parser produced no result (exit code {proc.returncode})",
                                error_code="PARSE_NO_OUTPUT", duration_ms=duration)
        if dst.stat().st_size > max_output_bytes:
            return ParseOutcome("FAILED", error="parser output exceeded the size cap",
                                error_code="PARSE_OUTPUT_TOO_LARGE", duration_ms=duration)
        try:
            data = json.loads(dst.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return ParseOutcome("FAILED", error="parser output is not valid JSON", error_code="PARSE_BAD_OUTPUT",
                                duration_ms=duration)
    status = data.get("status") if isinstance(data, dict) else None
    if status not in {"PARSED", "FAILED", "SKIPPED"}:
        return ParseOutcome("FAILED", error="parser output has an unexpected shape", error_code="PARSE_BAD_OUTPUT",
                            duration_ms=duration)
    text = data.get("text") if isinstance(data.get("text"), str) else ""
    raw_meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    meta = redact(raw_meta)  # e.g. json_error/yaml_error can quote a credential from the file
    error = redact_text(str(data.get("error") or ""))[:500]
    return ParseOutcome(status, text=text[:max_chars], meta=meta, error=error,
                        error_code="PARSE_ERROR" if status == "FAILED" else "", duration_ms=duration)
