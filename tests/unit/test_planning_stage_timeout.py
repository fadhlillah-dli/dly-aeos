"""The planning stage timeout keeps headroom over measured stage durations (LESSONS L31).

On 2026-10-29 the generation stages grew with every revision cycle (343-885 s on Opus 5.5) and the fifth Primary
Planner run of a 14-task plan was killed at the former 900 s limit, so the plan went to PLANNING_FAILED."""

from __future__ import annotations

from aeos.planning import service

# The longest successful generation stage measured on 2026-10-29 (Primary Planner, third revision cycle).
LONGEST_MEASURED_STAGE_S = 885


def test_stage_timeout_keeps_twice_the_longest_measured_stage() -> None:
    assert service.STAGE_TIMEOUT_SECONDS >= 2 * LONGEST_MEASURED_STAGE_S, (
        "a planning stage needs twice the longest measured stage; record the new measurement before lowering it")


def test_a_running_stage_is_not_treated_as_interrupted_before_its_own_timeout() -> None:
    # A RUNNING run younger than the grace blocks a duplicate run in another process (ADR-0030).
    assert service.STALE_RUN_GRACE.total_seconds() > service.STAGE_TIMEOUT_SECONDS
