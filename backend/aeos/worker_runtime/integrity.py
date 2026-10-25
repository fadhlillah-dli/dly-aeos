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

The controller **interpreter** is part of what the next start loads as well (re-audit 2026-10-19: the
editable-install finder/``.pth`` that maps ``aeos`` to the checkout and every installed dependency live in
the venv's site-packages, outside the checkout patterns). ``interpreter_roots`` adds them: startup hooks
(every top-level ``*.pth``, ``__editable__*`` finder, ``sitecustomize``/``usercustomize``, the platform's own
``dly_aeos-*.dist-info``/``*.egg-link`` and ``pyvenv.cfg``) are content-hashed; every other file under
site-packages and the venv's scripts directory is fingerprinted by size + modification time (cheap enough
for every run). Residual risks stated honestly: a deliberate writer that restores size and mtime of an
ordinary dependency file, and bytecode under ``__pycache__`` (the controller itself compiles lazily during
a run, so those files are not fingerprinted), are not detected; only the container sandbox prevents writes.
"""

from __future__ import annotations

import hashlib
import os
import sys
import sysconfig
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


INTERPRETER_PREFIX = "interpreter:"
MAX_INTERPRETER_FILES = 200_000
# Loaded/executed by every interpreter start (site.py) or naming where the platform package is imported from.
_STARTUP_NAMES = ("sitecustomize.py", "usercustomize.py", "pyvenv.cfg")
_PLATFORM_DISTS = ("dly_aeos", "dly-aeos", "aeos")


def interpreter_roots() -> list[tuple[str, Path]]:
    """(label, directory) of the controller interpreter's install locations the next start loads: the venv
    (or system) site-packages, the scripts/bin directory of the launchers and the venv root (pyvenv.cfg)."""
    paths = sysconfig.get_paths()
    out: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for label, key in (("site-packages", "purelib"), ("platlib", "platlib"), ("scripts", "scripts")):
        raw = paths.get(key)
        if not raw:
            continue
        p = Path(raw)
        try:
            norm = str(p.resolve()).lower() if sys.platform == "win32" else str(p.resolve())
        except OSError:
            continue
        if norm in seen or not p.is_dir():
            continue
        seen.add(norm)
        out.append((label, p))
    if sys.prefix != sys.base_prefix and (Path(sys.prefix) / "pyvenv.cfg").is_file():
        out.append(("venv", Path(sys.prefix)))
    return out


_PLATFORM_NORMALISED = frozenset(d.replace("-", "_").lower() for d in _PLATFORM_DISTS)


def _startup_critical(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    if len(parts) == 1 and (name.endswith(".pth") or name.startswith("__editable__") or name in _STARTUP_NAMES
                            or name.endswith(".egg-link")):
        return True
    top = parts[0].lower()
    if not top.endswith((".dist-info", ".egg-info")):
        return False
    # the platform's own install metadata (names where "aeos" is imported from): the dist name is the
    # segment before the first "-" (e.g. "dly_aeos-0.1.0.dist-info" -> "dly_aeos")
    dist_name = top.rsplit(".", 1)[0].split("-", 1)[0].replace("-", "_")
    return dist_name in _PLATFORM_NORMALISED


def _interpreter_files(label: str, root: Path, files: dict[str, str]) -> bool:
    """Add one interpreter root to *files*; False when the file cap was reached."""
    if label == "venv":  # only the venv marker; its Lib/ and Scripts/ are the other roots
        cfg = root / "pyvenv.cfg"
        files[f"{INTERPRETER_PREFIX}venv/pyvenv.cfg"] = _digest(cfg)
        return True
    stack = [root]
    flat = label == "scripts"  # launchers live at the top level of Scripts/ or bin/
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if not flat and entry.name not in _SKIP_DIRS:
                        stack.append(Path(entry.path))
                    continue
                rel = Path(entry.path).relative_to(root).as_posix()
                key = f"{INTERPRETER_PREFIX}{label}/{rel}"
                if _startup_critical(rel) and label != "scripts":
                    files[key] = _digest(Path(entry.path))
                else:
                    st = entry.stat(follow_symlinks=False)
                    files[key] = f"stat:{st.st_size}:{st.st_mtime_ns}"
            except (OSError, ValueError):
                continue
            if len(files) >= MAX_INTERPRETER_FILES:
                return False
    return True


def snapshot(root: Path | None = None, patterns: tuple[str, ...] = CONTROLLER_PATTERNS, *,
             interpreter: bool | None = None) -> dict[str, Any]:
    """Fingerprint of the runtime and trust-relevant files of the running controller checkout and (by default
    for the real controller checkout) of the controller interpreter's site-packages/scripts."""
    from aeos.core.paths import repo_root

    base = (root or repo_root()).resolve()
    if interpreter is None:
        interpreter = root is None
    files: dict[str, str] = {}
    truncated = False
    for rel, full in _iter_files(base, patterns):
        if len(files) >= MAX_FILES:
            truncated = True
            break
        files[rel] = _digest(full)
    roots: list[tuple[str, Path]] = interpreter_roots() if interpreter else []
    for label, iroot in roots:
        if not _interpreter_files(label, iroot, files):
            truncated = True
            break
    return {"root": str(base), "files": files, "truncated": truncated, "patterns": list(patterns),
            "interpreter": bool(interpreter), "interpreter_roots": {label: str(p) for label, p in roots}}


def compare(before: dict[str, Any] | None) -> list[dict[str, str]]:
    """Changed/added/removed controller files since ``before`` (empty = untouched)."""
    if not before:
        return []
    after = snapshot(Path(before["root"]), tuple(before.get("patterns") or CONTROLLER_PATTERNS),
                     interpreter=bool(before.get("interpreter")))
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
