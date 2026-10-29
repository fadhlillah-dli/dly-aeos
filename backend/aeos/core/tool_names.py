"""The worker tool and verifier suite vocabularies, and what a planner's free text maps to (LESSONS L29).

TaskSpecs name the tools a worker may use; routes record the tool set the owner reviewed at activation, in the same
vocabulary (``fs.read``, ``fs.write:allowed_paths``, ``tests.run``, plus ``bash``/``bash:<pattern>`` and ``webfetch``
when a task truly needs them). The planning model writes free text, and on 2026-10-29 the first live plan named its
tools ``read_file``, ``write_file``, ``run_tests`` and ``git_diff``: every route then refused every task as "outside the
reviewed tool set" and no worker could start. The same day the revised plans named the runtime's own tools (``read``,
``glob``, ``grep``, ``list``, ``edit``, ``todowrite``: opencode ``BASE_ALLOW``), and ``list`` and ``todowrite`` were
unknown, so every task was refused again (L29 strike 2): every runtime tool name now has its meaning here.

``canonical_tools`` maps the synonyms that mean exactly one of the three reviewed capabilities onto it. A synonym never
widens a capability: reading a diff or listing a directory is reading files, writing or editing a file is the
path-scoped write, running the tests is the test runner. The agent's own task list (``todowrite``/``todoread``) touches
no repository file and asks for no capability, so it is dropped. Anything else (``bash``, ``bash:<pattern>``,
``webfetch``, an unknown name) is kept as written, so an unknown tool still makes the task ineligible with its own name
in the reason (fail closed).

``canonical_suites`` does the same for ``verifier_policy.suites`` (L29 strike 3). The verifier selects the candidate's
test directories by the names ``unit`` and ``integration`` only (``aeos.verification.verifier.SUITE_DIRS``); the revised
plans of 2026-10-29 wrote free text ("trusted suite base vs head", "pytest routing"), which selects no directory, so
none of the worker's tests would have run. A name the verifier does not know is dropped, and a list with no known
name leaves the verifier's default selection (all candidate test directories).
"""

from __future__ import annotations

from collections.abc import Iterable

FS_READ = "fs.read"
FS_WRITE = "fs.write:allowed_paths"
TESTS_RUN = "tests.run"
CANONICAL_WORKER_TOOLS = (FS_READ, FS_WRITE, TESTS_RUN)

SYNONYMS: dict[str, str] = {
    **dict.fromkeys(("read_file", "read", "read_files", "view", "view_file", "open_file", "list", "list_files",
                     "list_dir", "glob", "grep", "search", "search_files", "git_diff", "git_status", "git_log",
                     "git_show", "diff"), FS_READ),
    **dict.fromkeys(("write_file", "write", "edit_file", "edit", "apply_patch", "patch", "create_file", "fs.write"),
                    FS_WRITE),
    **dict.fromkeys(("run_tests", "run_test", "test", "tests", "pytest", "vitest", "npm_test"), TESTS_RUN),
}
AGENT_TASK_LIST_TOOLS = frozenset({"todowrite", "todoread"})

VERIFIER_SUITES = ("unit", "integration")
SUITE_SYNONYMS: dict[str, str] = {
    **dict.fromkeys(("unit", "unit tests", "tests/unit"), "unit"),
    **dict.fromkeys(("integration", "integration tests", "tests/integration"), "integration"),
}


def canonical_tool(name: str) -> str:
    """The reviewed name for ``name`` when it is a known synonym (case and surrounding space ignored), else ``name``."""
    key = str(name).strip()
    return SYNONYMS.get(key.lower(), key)


def canonical_tools(names: Iterable[str] | None) -> list[str]:
    """Each name mapped by ``canonical_tool``, in order, without repeats and without the agent's own task list."""
    out: list[str] = []
    for name in names or []:
        tool = canonical_tool(name)
        if tool and tool.lower() not in AGENT_TASK_LIST_TOOLS and tool not in out:
            out.append(tool)
    return out


def canonical_suites(names: Iterable[str] | None) -> list[str]:
    """The verifier suites ``names`` mean (case and surrounding space ignored), in order, without repeats; names the
    verifier does not select by are dropped."""
    out: list[str] = []
    for name in names or []:
        suite = SUITE_SYNONYMS.get(str(name).strip().lower())
        if suite and suite not in out:
            out.append(suite)
    return out
