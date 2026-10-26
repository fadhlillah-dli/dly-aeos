"""Environment facts for a development run (scripts/gates.py calls this first; scripts/run_tests.sh stops on a bad
environment by itself).

    python scripts/preflight.py --facts      one `key: value` line per fact, ASCII only, exit 0 always

Facts: the Python path and version, whether it is the project venv, whether pytest and pytest-xdist are importable
for it, the node, npm and git versions, `core.hooksPath`, the login shell and whether `gh auth status` succeeds.
They are informational: a missing tool is reported as `not found`, never as an error, and nothing here can fail a
run. Nothing secret is read or printed: `gh auth status` is run only for its exit status and its output is dropped
(a token never reaches this script). Stdlib only."""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT_SECONDS = 20
NOT_FOUND = "not found"


def ascii_text(text: str) -> str:
    """Printable ASCII only (legacy Windows consoles): anything else becomes '?'."""
    return text.encode("ascii", "replace").decode("ascii")


def _run(argv: Sequence[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str] | None:
    """The finished process, or None when it cannot start or does not finish in time (a fact is never an error)."""
    try:
        return subprocess.run(list(argv), cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, check=False,
                              timeout=TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None


def _first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0].strip() if lines else ""


def tool_version(tool: str, flag: str = "--version") -> str:
    found = shutil.which(tool)
    if not found:
        return NOT_FOUND
    proc = _run([found, flag])
    if proc is None or proc.returncode != 0:
        return "found, version unknown"
    return _first_line(proc.stdout or proc.stderr) or "found, version unknown"


def module_importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def main_checkout(root: Path) -> Path | None:
    """The main checkout of a linked worktree (parent of the common `.git`), None for a bare repository or no git."""
    proc = _run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], root)
    if proc is None or proc.returncode != 0:
        return None
    common = Path(proc.stdout.strip())
    return common.parent if common.name == ".git" else None


def project_venv(root: Path = ROOT) -> str:
    """`yes (<path>)` when sys.prefix is the .venv of this checkout or of the main checkout, else `no`."""
    prefix = Path(sys.prefix).resolve()
    main = main_checkout(root)
    for base in (root, main):
        if base is not None and (base / ".venv").exists() and (base / ".venv").resolve() == prefix:
            return f"yes ({prefix})"
    return "no (sys.prefix is " + str(prefix) + ")"


def hooks_path(root: Path = ROOT) -> str:
    proc = _run(["git", "config", "--get", "core.hooksPath"], root)
    if proc is None:
        return "git not found"
    value = proc.stdout.strip()
    return value if proc.returncode == 0 and value else "(unset)"


def shell_name() -> str:
    return os.environ.get("SHELL") or os.environ.get("COMSPEC") or "(unknown)"


def gh_auth() -> str:
    """`ok` or `not authenticated`, `gh not found`. Only the exit status is used; the output is never kept."""
    gh = shutil.which("gh")
    if not gh:
        return "gh not found"
    proc = _run([gh, "auth", "status"])
    if proc is None:
        return "unknown (gh did not answer)"
    return "ok" if proc.returncode == 0 else "not authenticated"


def collect_facts(root: Path = ROOT) -> list[tuple[str, str]]:
    return [
        ("python", f"{sys.executable} ({sys.version.split()[0]})"),
        ("project_venv", project_venv(root)),
        ("pytest_importable", "yes" if module_importable("pytest") else "no"),
        ("xdist_importable", "yes" if module_importable("xdist") else "no"),
        ("node", tool_version("node")),
        ("npm", tool_version("npm")),
        ("git", tool_version("git")),
        ("core.hooksPath", hooks_path(root)),
        ("shell", shell_name()),
        ("gh_auth", gh_auth()),
    ]


def render_facts(facts: Sequence[tuple[str, str]]) -> str:
    return ascii_text("\n".join(f"{key}: {value}" for key, value in facts))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print environment facts for a development run.")
    parser.add_argument("--facts", action="store_true", help="print one 'key: value' line per fact (exit 0 always)")
    args = parser.parse_args(argv)
    if not args.facts:
        parser.print_usage(sys.stderr)
        print("preflight: nothing to do (pass --facts)", file=sys.stderr)
        return 2
    try:
        print(render_facts(collect_facts()))
    except Exception as exc:  # facts are informational: whatever goes wrong here must not stop a run
        print(ascii_text(f"preflight: facts unavailable: {exc}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
