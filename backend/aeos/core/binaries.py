"""One lookup rule for an executable AEOS runs: the inventory reconciler, the Doctor (every binary) and the vault
runtime share it.

Order: explicit settings override (``~`` expanded; authoritative, no fall-through when it is unusable), then the
user-level install in ``<data>/bin`` (``<name>.exe`` on Windows; only for binaries an ``aeos ... install`` places
there, the others pass ``data_dir=None``), then PATH (``which(name)``, the same call for every caller). A candidate
counts only if it is a regular file and, off Windows, has the execute bit; a directory or a non-executable file in
``<data>/bin`` falls through to PATH. Callers inject ``is_executable`` / ``which`` so hermetic tests (and the runner
abstraction of the probes) stay in control; the defaults are the real filesystem and ``shutil.which``.

``exe_name`` / ``data_bin_dir`` are also what the installer (``aeos.secrets.installer``) writes with, so the place
that installs and the place that looks cannot drift apart.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple


def is_executable_file(path: str | os.PathLike[str]) -> bool:
    """A regular file that can be run (a directory, or off Windows a file without the execute bit, is not)."""
    p = Path(path)
    return p.is_file() and (sys.platform == "win32" or os.access(p, os.X_OK))


def exe_name(name: str) -> str:
    """The file name of an executable on this host (``<name>.exe`` on Windows)."""
    return name + (".exe" if sys.platform == "win32" else "")


def data_bin_dir(data_dir: Path) -> Path:
    """The user-level install directory (``<data>/bin``) that ``aeos ... install`` writes to."""
    return Path(data_dir) / "bin"


class BinaryLookup(NamedTuple):
    """``path`` is None when nothing usable was found. ``lookup`` is the step that decided: ``settings`` | ``data-dir``
    | ``PATH`` when found or when the settings override was unusable (authoritative); ``data-dir/PATH`` or ``PATH``
    (the steps tried) when nothing was found. ``rejected`` is the unusable settings path (``~`` expanded), else None."""

    path: str | None
    lookup: str
    rejected: str | None = None


def locate_binary(
    name: str,
    *,
    configured: str | os.PathLike[str] | None = None,
    data_dir: Path | None = None,
    is_executable: Callable[[str], bool] = is_executable_file,
    which: Callable[[str], str | None] | None = None,
) -> BinaryLookup:
    """See the module docstring. ``data_dir=None`` skips the ``<data>/bin`` step."""
    if configured:
        path = str(Path(configured).expanduser())
        if is_executable(path):
            return BinaryLookup(path, "settings")
        return BinaryLookup(None, "settings", path)
    if data_dir is not None:
        local = str(data_bin_dir(data_dir) / exe_name(name))
        if is_executable(local):
            return BinaryLookup(local, "data-dir")
    found = (which or shutil.which)(name)  # resolved at call time so tests can patch shutil.which
    if found:
        return BinaryLookup(found, "PATH")
    return BinaryLookup(None, "PATH" if data_dir is None else "data-dir/PATH")
