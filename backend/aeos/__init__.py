"""DLY AEOS — DLY AI Engineering Operating System (local-first control plane)."""

import os as _os

# CLI output must stay plain ASCII on every console (AGENTS.md). Typer's rich integration renders
# box-drawing panels and forces colour whenever GITHUB_ACTIONS/FORCE_COLOR is set, so it is disabled
# for this package before any typer import.
_os.environ.setdefault("TYPER_USE_RICH", "0")
_os.environ.setdefault("_TYPER_FORCE_DISABLE_TERMINAL", "1")

__version__ = "0.1.0"
SCHEMA_VERSION = "1"
