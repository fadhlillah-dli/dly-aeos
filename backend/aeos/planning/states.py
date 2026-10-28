"""Explicit planning state machine (§14).

Planning state is separate from work status, runtime execution (TaskExecution), publication status
(Publication) and review acceptance (AcceptanceRecord) (§13) — this module only ever touches
``Plan.state``. Every transition is checked against ``TRANSITIONS``; anything else is a Conflict.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from aeos.core.enums import PlanningState as S
from aeos.core.errors import Conflict
from aeos.core.redaction import redact_text
from aeos.events import service as events
from aeos.models.planning import Plan

PIPELINE_STAGES: tuple[S, ...] = (S.PRIMARY_PLANNING, S.CHALLENGE_REVIEW, S.SYNTHESIS, S.FINAL_REVIEW)
_RESUMABLE_TARGETS = frozenset(PIPELINE_STAGES)

TRANSITIONS: dict[S, frozenset[S]] = {
    S.DRAFT: frozenset({S.CONTEXT_READY, S.PLANNING_FAILED, S.REJECTED}),
    S.CONTEXT_READY: frozenset({S.PRIMARY_PLANNING, S.NEEDS_DECISION, S.REJECTED}),
    S.PRIMARY_PLANNING: frozenset({S.CHALLENGE_REVIEW, S.PLANNING_FAILED, S.AWAITING_MANUAL_CLAUDE, S.NEEDS_DECISION}),
    S.CHALLENGE_REVIEW: frozenset({S.SYNTHESIS, S.REVIEW_FAILED, S.AWAITING_MANUAL_CLAUDE, S.NEEDS_DECISION}),
    S.SYNTHESIS: frozenset({S.FINAL_REVIEW, S.PLANNING_FAILED, S.AWAITING_MANUAL_CLAUDE, S.NEEDS_DECISION}),
    S.FINAL_REVIEW: frozenset({S.READY_FOR_HUMAN_REVIEW, S.REVIEW_FAILED, S.AWAITING_MANUAL_CLAUDE, S.NEEDS_DECISION}),
    S.READY_FOR_HUMAN_REVIEW: frozenset({S.APPROVED, S.REVISION_REQUESTED, S.REJECTED, S.READY_FOR_HUMAN_REVIEW}),
    # APPROVED -> READY_FOR_HUMAN_REVIEW: approval revoked/expired or superseded by a human edit.
    # APPROVED -> APPROVED: a new approval of the same current version supersedes the old one.
    S.APPROVED: frozenset({S.APPROVED, S.PUBLISHING, S.REVISION_REQUESTED, S.READY_FOR_HUMAN_REVIEW, S.REJECTED}),
    S.PUBLISHING: frozenset({S.PUBLISHED, S.PUBLICATION_PARTIAL, S.PUBLICATION_FAILED}),
    # PUBLISHED -> PUBLISHING: another run of the SAME approved version (e.g. a human retry/reconcile of a
    # partial target); entering PUBLISHING still needs an ACTIVE approval (facade.transition_plan).
    S.PUBLISHED: frozenset({S.REVISION_REQUESTED, S.READY_FOR_HUMAN_REVIEW, S.PUBLISHING}),
    S.PUBLICATION_PARTIAL: frozenset({S.PUBLISHING, S.PUBLISHED, S.NEEDS_DECISION, S.REVISION_REQUESTED,
                                      S.READY_FOR_HUMAN_REVIEW}),
    S.PUBLICATION_FAILED: frozenset({S.PUBLISHING, S.NEEDS_DECISION, S.REVISION_REQUESTED, S.READY_FOR_HUMAN_REVIEW}),
    S.REVISION_REQUESTED: frozenset({S.PRIMARY_PLANNING, S.READY_FOR_HUMAN_REVIEW, S.REJECTED, S.NEEDS_DECISION}),
    S.REJECTED: frozenset(),  # terminal: a rejected plan is history; start a new plan instead
    S.PLANNING_FAILED: frozenset({S.PRIMARY_PLANNING, S.SYNTHESIS, S.REVISION_REQUESTED, S.REJECTED,
                                  S.NEEDS_DECISION}),
    S.REVIEW_FAILED: frozenset({S.CHALLENGE_REVIEW, S.FINAL_REVIEW, S.REVISION_REQUESTED, S.REJECTED,
                                S.READY_FOR_HUMAN_REVIEW, S.NEEDS_DECISION}),
    S.NEEDS_DECISION: frozenset(set(_RESUMABLE_TARGETS) | {S.CONTEXT_READY, S.REVISION_REQUESTED, S.REJECTED,
                                                           S.PUBLISHING, S.READY_FOR_HUMAN_REVIEW}),
    S.AWAITING_MANUAL_CLAUDE: frozenset(set(_RESUMABLE_TARGETS) | {S.NEEDS_DECISION, S.REJECTED,
                                                                   S.PLANNING_FAILED, S.REVIEW_FAILED}),
}

# States where a pipeline stage is executing (a model call may be in flight).
RUNNING_STATES = frozenset(PIPELINE_STAGES)
# States that wait for a human (feed the Human Decision Queue).
HUMAN_WAITING_STATES = frozenset({S.READY_FOR_HUMAN_REVIEW, S.NEEDS_DECISION, S.AWAITING_MANUAL_CLAUDE,
                                  S.REVIEW_FAILED, S.PLANNING_FAILED, S.PUBLICATION_PARTIAL, S.PUBLICATION_FAILED})
# States from which the pipeline may be (re)started.
RUNNABLE_STATES = frozenset({S.CONTEXT_READY, S.REVISION_REQUESTED, S.PLANNING_FAILED, S.REVIEW_FAILED,
                             S.NEEDS_DECISION})
# States from which a human may edit the plan into a new version.
EDITABLE_STATES = frozenset({S.READY_FOR_HUMAN_REVIEW, S.APPROVED, S.PUBLISHED, S.REVISION_REQUESTED,
                             S.REVIEW_FAILED, S.NEEDS_DECISION, S.PUBLICATION_PARTIAL, S.PUBLICATION_FAILED})
TERMINAL_STATES = frozenset({S.REJECTED})


@dataclass(frozen=True)
class StageSpec:
    state: S
    role: str
    model: str  # alias requested from the official client (policy baseline)
    family: str  # the resolved model must belong to this family (§12)
    purpose: str
    schema: str
    artifact_kind: str
    failure_state: S


# ADR-0131 (owner decision 2026-10-29, amends master §02/§15/§17): every stage requests Opus; the Claude Fable family is
# never requested (the bridge refuses it). All four stages still run; the reviewers' independence comes from a fresh
# context per stage (a separate bridge invocation, no session reuse; REVIEWER_NOT_INDEPENDENT in the service).
STAGES: tuple[StageSpec, ...] = (
    StageSpec(S.PRIMARY_PLANNING, "Primary Planner", "opus", "opus", "planning.primary", "plan_content",
              "proposal", S.PLANNING_FAILED),
    StageSpec(S.CHALLENGE_REVIEW, "Planning Challenger", "opus", "opus", "planning.challenge",
              "challenge_findings", "findings", S.REVIEW_FAILED),
    StageSpec(S.SYNTHESIS, "Planning Synthesizer", "opus", "opus", "planning.synthesis", "synthesis",
              "synthesis", S.PLANNING_FAILED),
    StageSpec(S.FINAL_REVIEW, "Final Planning Reviewer", "opus", "opus", "planning.final", "final_review",
              "verdict", S.REVIEW_FAILED),
)
STAGE_BY_STATE: dict[S, StageSpec] = {s.state: s for s in STAGES}


def can_transition(current: str | S, target: str | S) -> bool:
    try:
        return S(target) in TRANSITIONS[S(current)]
    except (KeyError, ValueError):
        return False


def transition(session: Session, plan: Plan, target: S | str, *, reason: str = "", actor: str = "planning") -> None:
    """Apply a legal transition, record the reason and emit ``plan.state_changed``."""
    target = S(target)
    current = S(plan.state)
    if target not in TRANSITIONS[current]:
        raise Conflict(
            f"illegal planning transition {current.value} -> {target.value}",
            code="ILLEGAL_PLANNING_TRANSITION",
            details={"from": current.value, "to": target.value,
                     "allowed": sorted(s.value for s in TRANSITIONS[current])},
        )
    plan.state = target.value
    plan.state_reason = redact_text(reason or "")[:4000]
    session.flush()
    events.emit(
        session, source="planning", kind="plan.state_changed", entity_type="plan", entity_id=plan.id,
        entity_version=plan.current_version_id, project_id=plan.project_id, domain=plan.domain,
        payload={"from": current.value, "to": target.value, "reason": plan.state_reason, "actor": actor},
    )
