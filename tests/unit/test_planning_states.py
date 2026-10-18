"""§14 planning state machine: every state exists, only legal transitions apply, planning state is
kept separate from runtime/publication/acceptance state (§13)."""

from __future__ import annotations

import itertools

import pytest

from aeos.core.enums import ExecutionState, PlanningState
from aeos.core.errors import Conflict
from aeos.models.planning import Plan
from aeos.planning import states
from tests.unit.test_planning_support import make_project

ALL = list(PlanningState)


def test_every_section14_state_has_a_transition_row():
    expected = {
        "DRAFT", "CONTEXT_READY", "PRIMARY_PLANNING", "CHALLENGE_REVIEW", "SYNTHESIS", "FINAL_REVIEW",
        "READY_FOR_HUMAN_REVIEW", "APPROVED", "PUBLISHING", "PUBLISHED", "REVISION_REQUESTED", "REJECTED",
        "PLANNING_FAILED", "REVIEW_FAILED", "NEEDS_DECISION", "PUBLICATION_PARTIAL", "PUBLICATION_FAILED",
        "AWAITING_MANUAL_CLAUDE",
    }
    assert {s.value for s in ALL} == expected
    assert set(states.TRANSITIONS) == set(ALL)


@pytest.mark.parametrize(("src", "dst"), list(itertools.product(ALL, ALL)))
def test_can_transition_matches_table(src, dst):
    assert states.can_transition(src, dst) is (dst in states.TRANSITIONS[src])


def test_pipeline_order_and_no_stage_skipping():
    s = PlanningState
    assert states.can_transition(s.CONTEXT_READY, s.PRIMARY_PLANNING)
    assert states.can_transition(s.PRIMARY_PLANNING, s.CHALLENGE_REVIEW)
    assert states.can_transition(s.CHALLENGE_REVIEW, s.SYNTHESIS)
    assert states.can_transition(s.SYNTHESIS, s.FINAL_REVIEW)
    assert states.can_transition(s.FINAL_REVIEW, s.READY_FOR_HUMAN_REVIEW)
    # skipping review stages is never a legal transition
    for bad in [(s.CONTEXT_READY, s.READY_FOR_HUMAN_REVIEW), (s.PRIMARY_PLANNING, s.SYNTHESIS),
                (s.PRIMARY_PLANNING, s.READY_FOR_HUMAN_REVIEW), (s.CHALLENGE_REVIEW, s.FINAL_REVIEW),
                (s.SYNTHESIS, s.READY_FOR_HUMAN_REVIEW), (s.CONTEXT_READY, s.APPROVED),
                (s.READY_FOR_HUMAN_REVIEW, s.PUBLISHING), (s.DRAFT, s.APPROVED)]:
        assert not states.can_transition(*bad), bad


def test_rejected_is_terminal_and_approval_only_from_review():
    s = PlanningState
    assert states.TRANSITIONS[s.REJECTED] == frozenset()
    sources = [src for src in ALL if s.APPROVED in states.TRANSITIONS[src]]
    assert set(sources) == {s.READY_FOR_HUMAN_REVIEW, s.APPROVED}


def test_transition_applies_legal_and_rejects_illegal(db):
    project = make_project(db)
    plan = Plan(project_id=project.id, domain=project.domain, title="x", state="DRAFT")
    db.add(plan)
    db.flush()
    states.transition(db, plan, PlanningState.CONTEXT_READY, reason="ok")
    assert plan.state == "CONTEXT_READY" and plan.state_reason == "ok"
    with pytest.raises(Conflict) as exc:
        states.transition(db, plan, PlanningState.APPROVED)
    assert exc.value.code == "ILLEGAL_PLANNING_TRANSITION"
    assert exc.value.details["from"] == "CONTEXT_READY"
    assert plan.state == "CONTEXT_READY"


def test_transition_emits_event(db):
    from aeos.models.audit import Event

    project = make_project(db)
    plan = Plan(project_id=project.id, domain=project.domain, title="x", state="DRAFT")
    db.add(plan)
    db.flush()
    states.transition(db, plan, PlanningState.CONTEXT_READY, reason="r")
    ev = db.query(Event).filter(Event.kind == "plan.state_changed", Event.entity_id == plan.id).one()
    assert ev.payload["from"] == "DRAFT" and ev.payload["to"] == "CONTEXT_READY"


def test_planning_states_are_not_runtime_states():
    """§13: planning state is not merged with runtime execution state."""
    assert not ({s.value for s in PlanningState} & {e.value for e in ExecutionState if e.value != "REVIEW"})
    assert "REVIEW" not in {s.value for s in PlanningState}
