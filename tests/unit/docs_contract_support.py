"""Shared helpers for the documentation contract tests (docs vs code).

One resolver for ``aeos <command> [<subcommand>]``: the CLI registry is read from ``aeos.cli.app`` and a word
sequence is resolved against it. ``test_docs_links.py`` (VS Code tasks) and ``test_docs_commands.py`` (command
drift in the docs) both use it; do not write a second one.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Collection
from pathlib import Path


def registered_commands() -> set[str]:
    """Every registered ``aeos`` command path, groups included, for example ``admin`` and ``admin create``."""
    from typer.main import get_command

    from aeos.cli import app

    root = get_command(app)
    names: set[str] = set()

    def walk(cmd, prefix: str) -> None:
        for name, sub in getattr(cmd, "commands", {}).items():
            full = f"{prefix} {name}".strip()
            names.add(full)
            walk(sub, full)

    walk(root, "")
    return names


WORD = re.compile(r"^[a-z][a-z0-9_-]*$")


def command_problem(words: list[str], commands: Collection[str]) -> str | None:
    """Resolve the command words that follow ``aeos`` and name the first unknown one, or return None.

    The first word must be a registered command. While the command resolved so far is a group (a registered path
    with registered children), the next word must be one of its subcommands; at a leaf command the remaining
    words are arguments and are not checked. No words (a bare ``aeos``) resolves.
    """
    if not words:
        return None
    if words[0] not in commands:
        return f"aeos {words[0]}"
    cur = words[0]
    for word in words[1:]:
        if not any(c.startswith(cur + " ") for c in commands):
            return None  # a leaf: the rest are arguments
        nxt = f"{cur} {word}"
        if nxt not in commands:
            return f"aeos {nxt}"
        cur = nxt
    return None


ALWAYS = {"--help", "-h"}  # argparse adds them to every parser
_flag_cache: dict[tuple[Path, int, int], set[str]] = {}


def script_flags(path: Path) -> set[str]:
    """Flags a script defines, read with ast (never imported or run): ``add_argument("--x")`` calls and a literal
    ``"--x"`` in a comparison (the ``"--x" in argv`` style)."""
    st = path.stat()
    key = (path.resolve(), st.st_mtime_ns, st.st_size)
    if key not in _flag_cache:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        flags: set[str] = set(ALWAYS)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument":
                flags.update(a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)
                             and a.value.startswith("-"))
            elif isinstance(node, ast.Compare):
                for part in [node.left, *node.comparators]:
                    if isinstance(part, ast.Constant) and isinstance(part.value, str) and part.value.startswith("--"):
                        flags.add(part.value)
        _flag_cache[key] = flags
    return _flag_cache[key]
