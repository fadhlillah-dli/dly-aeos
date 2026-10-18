"""Completeness-review fixes for the worker runtime (§35 observed actions, §50 'File path
traversal/symlink keluar sandbox -> Akses diblokir')."""

from __future__ import annotations

import json

from aeos.worker_runtime import opencode


def _tool(path: str, tool: str = "read") -> str:
    return json.dumps({"type": "tool_use", "part": {"tool": tool, "state": {"status": "completed",
                                                                           "input": {"filePath": path}}}})


def test_relative_traversal_is_an_outside_attempt(tmp_path):
    wt = tmp_path / "repo"
    (wt / "src").mkdir(parents=True)
    p = opencode.EventParser(worktree=wt)
    inside = p.parse_line(_tool("src/../src/a.py", "edit"))
    assert inside.path == "src/a.py" and p.outside_attempts == []
    assert p.touched == ["src/a.py"]
    out = p.parse_line(_tool("../../outside/secret.txt"))
    assert out.kind == "read_file" and p.outside_attempts == ["../../outside/secret.txt"]
    p.parse_line(_tool(r"..\..\x.txt", "write"))
    assert len(p.outside_attempts) == 2 and r"..\..\x.txt" not in p.touched  # never counted as progress


def test_absolute_paths_inside_and_outside(tmp_path):
    wt = tmp_path / "repo"
    wt.mkdir()
    p = opencode.EventParser(worktree=wt)
    assert p.parse_line(_tool(str(wt / "a.py"))).path == "a.py"
    p.parse_line(_tool(str(tmp_path / "elsewhere.py")))
    assert p.outside_attempts and p.outside_attempts[0].endswith("elsewhere.py")
