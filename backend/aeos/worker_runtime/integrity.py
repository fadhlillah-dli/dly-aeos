"""Controller-checkout integrity around every worker run (§09 Zone 1 "read-only for workers", ADR-0051).

Under the WEAK process sandbox the operating system does not stop a worker process from writing the
running controller checkout (its code, ``policy/``, the acceptance suite). Without a shell OpenCode's own
tools are confined to the worktree (``external_directory: deny``), so this is defence in depth against a
tool-permission bug: the controller's runtime files (all backend code, the served UI build, launchers,
scripts, config/contract files) and the trust-relevant protected-path list are fingerprinted before the
worker starts and compared after it exits (§09 "a worker cannot load its changes into the active
controller": any controller file would be loaded on the next restart). Any change fails and quarantines
the run, raises a HIGH audit/event, and is never loaded silently. An owner editing the checkout during a
run trips the check as well (fail closed). Writes to the data-dir database are NOT detectable this
way (the controller writes it during the run) - that residual risk is stated in
``sandbox.PROCESS_RESIDUAL_RISKS``; the container sandbox never mounts the data dir.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from aeos.core.protected_paths import PLATFORM_PROTECTED_PATHS
from aeos.worker_runtime.scope import matches_any, static_prefix

# What the running controller loads besides the protected list (review 2026-10-19: a worker-written
# backend/aeos/models/*.py or frontend/dist bundle would be loaded on the next restart just the same).
CONTROLLER_RUNTIME_PATHS: tuple[str, ...] = (
    "backend/**", "frontend/dist/**", "scripts/**", "aeos", "aeos.ps1", "config/**", "contracts/**",
)
CONTROLLER_PATTERNS: tuple[str, ...] = tuple(dict.fromkeys(PLATFORM_PROTECTED_PATHS + CONTROLLER_RUNTIME_PATHS))
MAX_FILES = 20_000
MAX_FILE_BYTES = 8 * 1024 * 1024
_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".pytest_cache", ".ruff_cache", ".mypy_cache"}


def _iter_files(root: Path, patterns: tuple[str, ...]):
    seen: set[str] = set()
    for pat in patterns:
        prefix = static_prefix(pat)
        start = root / prefix if prefix else root
        if start.is_file() or start.is_symlink():
            rel = start.relative_to(root).as_posix()
            if rel not in seen and matches_any(rel, patterns, case_insensitive=True):
                seen.add(rel)
                yield rel, start
            continue
        if not start.is_dir():
            # a glob such as scripts/bootstrap.* : scan the parent directory only
            parent = start.parent if prefix else root
            if parent.is_dir() and parent != root:
                for child in parent.iterdir():
                    rel = child.relative_to(root).as_posix()
                    if child.is_file() and rel not in seen and matches_any(rel, patterns, case_insensitive=True):
                        seen.add(rel)
                        yield rel, child
            continue
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in filenames:
                full = Path(dirpath) / name
                rel = full.relative_to(root).as_posix()
                if rel in seen or not matches_any(rel, patterns, case_insensitive=True):
                    continue
                seen.add(rel)
                yield rel, full


def _digest(path: Path) -> str:
    try:
        if path.is_symlink():
            return "symlink:" + os.readlink(path)
        if path.stat().st_size > MAX_FILE_BYTES:
            st = path.stat()
            return f"large:{st.st_size}:{st.st_mtime_ns}"
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        return f"unreadable:{type(exc).__name__}"


def snapshot(root: Path | None = None, patterns: tuple[str, ...] = CONTROLLER_PATTERNS) -> dict[str, Any]:
    """Fingerprint of the runtime and trust-relevant files of the running controller checkout."""
    from aeos.core.paths import repo_root

    base = (root or repo_root()).resolve()
    files: dict[str, str] = {}
    truncated = False
    for rel, full in _iter_files(base, patterns):
        if len(files) >= MAX_FILES:
            truncated = True
            break
        files[rel] = _digest(full)
    return {"root": str(base), "files": files, "truncated": truncated, "patterns": list(patterns)}


def compare(before: dict[str, Any] | None) -> list[dict[str, str]]:
    """Changed/added/removed controller files since ``before`` (empty = untouched)."""
    if not before:
        return []
    after = snapshot(Path(before["root"]), tuple(before.get("patterns") or CONTROLLER_PATTERNS))
    old, new = before.get("files") or {}, after["files"]
    out: list[dict[str, str]] = []
    for rel in sorted(set(old) | set(new)):
        if rel not in new:
            out.append({"path": rel, "change": "removed"})
        elif rel not in old:
            out.append({"path": rel, "change": "added"})
        elif old[rel] != new[rel]:
            out.append({"path": rel, "change": "modified"})
    return out
