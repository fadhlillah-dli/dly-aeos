"""Trusted acceptance suite identity (§09, §19, §21).

The trusted suite is read from the **trusted controller checkout** (``aeos.core.paths.repo_root()``),
never from a candidate workspace. Its identity is the hash of every suite file (plus the shared
``tests/__init__.py`` / ``tests/conftest.py`` fixtures it reuses). The verifier records this hash in
every piece of evidence so a changed grader is visible and invalidates acceptance (config hash).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from aeos.core.errors import Blocked
from aeos.core.hashing import content_hash, file_sha256
from aeos.core.paths import repo_root

SUITE_DIR = ("tests", "acceptance")
SUPPORT_FILES = ("tests/__init__.py", "tests/conftest.py")
_IGNORED_SUFFIXES = {".pyc", ".pyo"}


def trusted_controller_root() -> Path:
    """Root of the trusted (released/seed) controller checkout. Tests may monkeypatch this."""
    return repo_root()


def trusted_suite_files(root: Path | None = None) -> list[tuple[str, Path]]:
    root = (root or trusted_controller_root()).resolve()
    suite_dir = root.joinpath(*SUITE_DIR)
    if not suite_dir.is_dir():
        raise Blocked("trusted acceptance suite not found in the controller checkout",
                      code="NO_TRUSTED_SUITE", details={"expected": "/".join(SUITE_DIR)})
    files: list[tuple[str, Path]] = []
    for rel in SUPPORT_FILES:
        p = root / rel
        if p.is_file():
            files.append((rel, p))
    for p in sorted(suite_dir.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or p.suffix in _IGNORED_SUFFIXES:
            continue
        files.append((p.relative_to(root).as_posix(), p))
    return files


def hash_files(files: list[tuple[str, Path]]) -> str:
    return content_hash(sorted([rel, file_sha256(p)] for rel, p in files))


def trusted_suite_hash(root: Path | None = None) -> str:
    return hash_files(trusted_suite_files(root))


def copy_trusted_suite(dest: Path, root: Path | None = None) -> tuple[list[tuple[str, Path]], str]:
    """Copy the trusted suite into a verifier-owned directory; return (copied files, hash of copies)."""
    copied: list[tuple[str, Path]] = []
    for rel, src in trusted_suite_files(root):
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        copied.append((rel, target))
    # Make sure package markers exist so `tests.conftest` stays importable from the copy.
    for pkg in ("tests", "tests/acceptance"):
        marker = dest / pkg / "__init__.py"
        if not marker.exists():
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("", encoding="utf-8")
    return copied, hash_files(copied)
