"""The worker tool vocabulary and the synonyms a planner may write for it (LESSONS L29).

TaskSpecs name the tools a worker may use; routes record the tool set the owner reviewed at activation, in the same
vocabulary (``fs.read``, ``fs.write:allowed_paths``, ``tests.run``, plus ``bash``/``bash:<pattern>`` and ``webfetch``
when a task truly needs them). The planning model writes free text, and on 2026-10-29 the first live plan named its
tools ``read_file``, ``write_file``, ``run_tests`` and ``git_diff``: every route then refused every task as "outside the
reviewed tool set" and no worker could start.

``canonical_tools`` maps the synonyms that mean exactly one of the three reviewed capabilities onto it. A synonym never
widens a capability: reading a diff is reading files, writing or editing a file is the path-scoped write, running the
tests is the test runner. Anything else (``bash``, ``bash:<pattern>``, ``webfetch``, an unknown name) is kept as
written, so an unknown tool still makes the task ineligible with its own name in the reason (fail closed).
"""

from __future__ import annotations

from collections.abc import Iterable

FS_READ = "fs.read"
FS_WRITE = "fs.write:allowed_paths"
TESTS_RUN = "tests.run"
CANONICAL_WORKER_TOOLS = (FS_READ, FS_WRITE, TESTS_RUN)

SYNONYMS: dict[str, str] = {
    **dict.fromkeys(("read_file", "read", "read_files", "view", "view_file", "open_file", "list_files", "list_dir",
                     "glob", "grep", "search", "search_files", "git_diff", "git_status", "git_log", "git_show",
                     "diff"), FS_READ),
    **dict.fromkeys(("write_file", "write", "edit_file", "edit", "apply_patch", "patch", "create_file", "fs.write"),
                    FS_WRITE),
    **dict.fromkeys(("run_tests", "run_test", "test", "tests", "pytest", "vitest", "npm_test"), TESTS_RUN),
}


def canonical_tool(name: str) -> str:
    """The reviewed name for ``name`` when it is a known synonym (case and surrounding space ignored), else ``name``."""
    key = str(name).strip()
    return SYNONYMS.get(key.lower(), key)


def canonical_tools(names: Iterable[str] | None) -> list[str]:
    """Each name mapped by ``canonical_tool``, in order, without repeats."""
    out: list[str] = []
    for name in names or []:
        tool = canonical_tool(name)
        if tool and tool not in out:
            out.append(tool)
    return out
