"""The worker tool vocabulary: a planner's synonyms map onto the reviewed names; nothing else changes (LESSONS L29)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aeos.core.tool_names import CANONICAL_WORKER_TOOLS, canonical_tool, canonical_tools
from aeos.routing import service
from aeos.worker_runtime.opencode import BASE_ALLOW

REVIEWED = list(CANONICAL_WORKER_TOOLS)


@pytest.mark.parametrize(("name", "expected"), [
    ("read_file", "fs.read"), ("Read_File ", "fs.read"), ("git_diff", "fs.read"), ("grep", "fs.read"),
    ("list", "fs.read"),
    ("write_file", "fs.write:allowed_paths"), ("edit_file", "fs.write:allowed_paths"),
    ("apply_patch", "fs.write:allowed_paths"), ("fs.write", "fs.write:allowed_paths"),
    ("run_tests", "tests.run"), ("pytest", "tests.run"),
])
def test_a_synonym_maps_to_its_reviewed_name(name, expected):
    assert canonical_tool(name) == expected


@pytest.mark.parametrize("name", ["fs.read", "fs.write:allowed_paths", "tests.run", "bash", "bash:pytest -q", "webfetch",
                                  "shell_exec", "deploy"])
def test_reviewed_names_shell_egress_and_unknown_names_are_kept_as_written(name):
    assert canonical_tool(name) == name


def test_canonical_tools_keeps_order_and_drops_repeats():
    assert canonical_tools(["read_file", "fs.read", "run_tests", "write_file", "tests.run"]) == [
        "fs.read", "tests.run", "fs.write:allowed_paths"]
    assert canonical_tools(None) == [] and canonical_tools([]) == []


def _route(tool_set):
    return SimpleNamespace(tool_set=tool_set)


def test_a_task_written_with_synonyms_fits_the_reviewed_tool_set():
    """Red before L29: 'TaskSpec tools [read_file, write_file, run_tests] outside the route's reviewed tool set'."""
    assert service.tool_set_rejections(_route(REVIEWED), ["read_file", "write_file", "run_tests", "git_diff"]) == []


def test_near_miss_an_unknown_tool_still_refuses_the_route_by_its_own_name():
    problems = service.tool_set_rejections(_route(REVIEWED), ["read_file", "shell_exec"])
    assert problems == ["TaskSpec tools ['shell_exec'] outside the route's reviewed tool set "
                        "['fs.read', 'fs.write:allowed_paths', 'tests.run']"]


def test_a_task_written_with_the_runtime_tool_names_fits_the_reviewed_tool_set():
    """Red before L29 strike 2: the revised plans of 2026-10-29 named the runtime's own tools, and 'list' and
    'todowrite' put every task outside every route's reviewed tool set."""
    assert set(canonical_tools(BASE_ALLOW)) <= set(CANONICAL_WORKER_TOOLS)
    assert service.tool_set_rejections(_route(REVIEWED), list(BASE_ALLOW)) == []


def test_the_agent_task_list_asks_for_no_capability():
    assert canonical_tools(["todowrite", " TodoRead", "list"]) == ["fs.read"]
    assert canonical_tools(["todowrite"]) == []


def test_a_synonym_never_widens_a_capability_beyond_the_reviewed_set():
    """A route reviewed for reading only is not opened by a write synonym."""
    problems = service.tool_set_rejections(_route(["fs.read"]), ["read_file", "write_file"])
    assert problems == ["TaskSpec tools ['fs.write:allowed_paths'] outside the route's reviewed tool set ['fs.read']"]
