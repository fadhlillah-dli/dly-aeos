"""Run a child process with stdin input, a hard timeout and process-tree kill (Windows/macOS/Linux)."""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil


@dataclass
class ProcResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int
    killed_pids: list[int] = field(default_factory=list)


def kill_process_tree(pid: int, *, timeout: float = 5.0) -> list[int]:
    """Kill *pid* and every descendant (children first). Returns the pids that were signalled."""
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return []
    try:
        procs = parent.children(recursive=True)
    except psutil.Error:
        procs = []
    procs.append(parent)
    killed: list[int] = []
    for p in procs:
        try:
            p.kill()
            killed.append(p.pid)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:  # pragma: no cover - only when the OS refuses
            continue
    psutil.wait_procs(procs, timeout=timeout)
    return killed


def run_process(argv: list[str], *, stdin_text: str, cwd: Path, env: dict[str, str], timeout: float) -> ProcResult:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    started = time.monotonic()
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(cwd), env=env,
        text=True, encoding="utf-8", errors="replace", **kwargs,
    )
    killed: list[int] = []
    try:
        out, err = proc.communicate(input=stdin_text, timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        killed = kill_process_tree(proc.pid)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - kill already sent
            proc.kill()
            out, err = "", ""
    return ProcResult(
        returncode=proc.returncode, stdout=out or "", stderr=err or "", timed_out=timed_out,
        duration_ms=int((time.monotonic() - started) * 1000), killed_pids=killed,
    )
