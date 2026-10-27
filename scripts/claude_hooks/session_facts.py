"""SessionStart hook launcher: print the environment facts of the project interpreter, not of whatever python3 is on PATH.

    python3 scripts/claude_hooks/session_facts.py

Claude Code runs the hook with the system ``python3``; ``scripts/preflight.py --facts`` would then describe that
interpreter (no project venv, no pytest, no xdist) while ``scripts/run_tests.sh`` and the gates use the project
``.venv``. This launcher picks the interpreter the same way ``scripts/run_tests.sh`` does and runs
``scripts/preflight.py --facts`` with it (LESSONS L10/L17: the facts stay owned by preflight.py; this file only chooses
the interpreter):

1. ``$AEOS_GATES_PYTHON`` when it names an existing executable file (when it is set but does not, the first fact
   line says so, because ``scripts/run_tests.sh`` and the gates stop on it);
2. the ``.venv`` of this checkout, then the ``.venv`` of the main checkout (a linked worktree has none of its own;
   never link one into a worktree);
3. the interpreter that runs this launcher (the facts then say plainly that it is not the project venv).

Fail-open like the other hook: whatever goes wrong (no interpreter, a timeout, an exception, a non-zero exit) prints
nothing and exits 0, so a fault here can never obstruct a session. Stdlib only, Python 3.11+."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

sys.dont_write_bytecode = True  # a hook must not drop __pycache__ into the checkout

ROOT = Path(__file__).resolve().parents[2]
TIMEOUT_SECONDS = 15  # below the hook timeout registered in .claude/settings.json


def interpreter(root: Path = ROOT) -> str:
    """The interpreter whose facts are worth reporting (see the module docstring for the order)."""
    override = os.environ.get("AEOS_GATES_PYTHON")
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return override
    sys.path.insert(0, str(ROOT / "scripts"))  # the tools of this checkout, whatever ``root`` is
    import preflight  # the owner of the facts; also knows how to find the main checkout
    import worktree  # the owner of the venv layout (bin/python or Scripts/python.exe)

    for base in (root, preflight.main_checkout(root)):
        if base is not None and worktree.venv_python(base).is_file():
            return str(worktree.venv_python(base))
    return sys.executable


def override_problem() -> str | None:
    """A fact line when ``$AEOS_GATES_PYTHON`` is set but is no executable file, else None."""
    override = os.environ.get("AEOS_GATES_PYTHON")
    if override and not (os.path.isfile(override) and os.access(override, os.X_OK)):
        text = f"gates_python_override: INVALID ({override}): scripts/run_tests.sh and the gates stop on it"
        return text.encode("ascii", "replace").decode("ascii")
    return None


def main(run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run, root: Path = ROOT) -> int:
    """Always 0."""
    with contextlib.suppress(Exception):  # fail-open on anything, including a timeout
        problem = override_problem()
        if problem:
            sys.stdout.write(problem + "\n")
            sys.stdout.flush()
        proc = run([interpreter(root), str(root / "scripts" / "preflight.py"), "--facts"], capture_output=True,
                   timeout=TIMEOUT_SECONDS, check=False, cwd=str(root), stdin=subprocess.DEVNULL)
        if proc.returncode == 0 and proc.stdout:
            sys.stdout.buffer.write(proc.stdout)
            sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
