"""One lookup rule for an executable AEOS runs: the inventory reconciler, the Doctor and the vault runtime share it.

Order: explicit settings override (``~`` expanded; authoritative, no fall-through when it is unusable), then the
user-level install in ``<data>/bin`` (``<name>.exe`` on Windows), then PATH. A candidate counts only if it is a
regular file and, off Windows, has the execute bit; a directory or a non-executable file in ``<data>/bin`` falls
through to PATH. Callers inject ``is_executable`` / ``which`` so hermetic tests (and the runner abstraction of the
probes) stay in control; the defaults are the real filesystem and ``shutil.which``.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path


def is_executable_file(path: str | os.PathLike[str]) -> bool:
    """A regular file that can be run (a directory, or off Windows a file without the execute bit, is not)."""
    p = Path(path)
    return p.is_file() and (sys.platform == "win32" or os.access(p, os.X_OK))


def locate_binary(
    name: str,
    *,
    configured: str | os.PathLike[str] | None = None,
    data_dir: Path | None = None,
    is_executable: Callable[[str], bool] = is_executable_file,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[str | None, str]:
    """(path or None, lookup) with lookup in ``settings`` | ``data-dir`` | ``PATH``.

    ``data_dir=None`` skips the ``<data>/bin`` step (only binaries that an ``aeos ... install`` places there use it)."""
    if configured:
        path = str(Path(configured).expanduser())
        return (path if is_executable(path) else None), "settings"
    if data_dir is not None:
        local = str(Path(data_dir) / "bin" / (name + (".exe" if sys.platform == "win32" else "")))
        if is_executable(local):
            return local, "data-dir"
    return which(name), "PATH"
