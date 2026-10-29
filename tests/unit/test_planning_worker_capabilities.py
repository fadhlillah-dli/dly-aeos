"""The planning prompt states what a worker can really do (LESSONS L30).

On 2026-10-29 the first live plans contained tasks whose deliverable was the output of running a command (a full-suite
baseline, an export dry-run); a worker has no shell, so its run correctly refused to invent the result and every
dependent task waited. The prompt line must name the runtime's own tool list and the reviewed tool vocabulary."""

from __future__ import annotations

import re

from aeos.core.tool_names import CANONICAL_WORKER_TOOLS
from aeos.planning import prompts
from aeos.worker_runtime.opencode import BASE_ALLOW


def _capability_line() -> str:
    lines = [p for p in prompts.FIXED_POLICY if p.startswith("Worker capabilities")]
    assert len(lines) == 1, "the planning prompt must state the worker capabilities exactly once"
    return lines[0]


def test_the_prompt_names_exactly_the_runtime_worker_tools():
    """Red when the runtime's tool list changes without the prompt (or the reverse)."""
    m = re.search(r"with the tools (.+?); it has no shell", _capability_line())
    assert m, "the capability line must list the worker tools"
    named = [t.strip() for t in re.split(r",| and ", m.group(1)) if t.strip()]
    assert named == list(BASE_ALLOW)


def test_the_prompt_forbids_command_output_tasks_and_names_the_tool_vocabulary():
    line = _capability_line()
    assert "no shell" in line and "Never plan a task whose deliverable is the output of running a command" in line
    for tool in CANONICAL_WORKER_TOOLS:
        assert tool in line


def test_the_prompt_revision_moved_with_the_new_rule():
    assert prompts.PROMPT_REVISION == "planning-prompts/3"
