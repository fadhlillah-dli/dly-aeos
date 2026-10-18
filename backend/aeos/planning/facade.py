"""Cross-module contract for plans (§13, §14). Owned by ``aeos.planning``."""

from __future__ import annotations

from sqlalchemy.orm import Session

from aeos.models.planning import Plan, PlanVersion, TaskSpec


def get_plan(session: Session, plan_id: str) -> Plan | None:
    return session.get(Plan, plan_id)


def get_approved_version(session: Session, plan_id: str) -> PlanVersion | None:
    """The exact version bound by an ACTIVE approval decision — never "latest" (§50)."""
    from aeos.planning.service import get_approved_version as impl

    return impl(session, plan_id)


def tasks_for_version(session: Session, plan_version_id: str) -> list[TaskSpec]:
    from sqlalchemy import select

    return list(session.scalars(select(TaskSpec).where(TaskSpec.plan_version_id == plan_version_id)))


# ---- additive (non-breaking) contract ---------------------------------------------------------------


# Publication states another module (the publisher) may set. Human decisions (APPROVED, REJECTED,
# REVISION_REQUESTED, ...) are recorded only through ``aeos.planning.service.decide`` with a Decision row
# bound to the exact version + hash; the pipeline states belong to the planning service itself.
PUBLICATION_STATES = frozenset({"PUBLISHING", "PUBLISHED", "PUBLICATION_PARTIAL", "PUBLICATION_FAILED"})
_DELEGATED_TARGETS = PUBLICATION_STATES | {"NEEDS_DECISION"}


def transition_plan(session: Session, plan_id: str, to_state: str, *, reason: str, actor: str) -> Plan:
    """Apply a legal §14 *publication* transition for the publisher (APPROVED -> PUBLISHING -> PUBLISHED /
    PUBLICATION_PARTIAL / PUBLICATION_FAILED, a bounded retry back to PUBLISHING, or NEEDS_DECISION from a
    publication state). Raises ``Conflict`` for an illegal or non-delegated transition (a module can never
    set APPROVED/REJECTED/... — those are human decisions) and ``NotFound`` for an unknown plan. Entering
    PUBLISHING requires a version bound by an ACTIVE approval (fail closed: a revoked/expired/superseded
    approval cannot start or resume a publication). Does not commit."""
    from aeos.core.errors import Conflict, NotFound
    from aeos.planning.states import transition

    plan = session.get(Plan, plan_id)
    if plan is None:
        raise NotFound("plan not found")
    if to_state not in _DELEGATED_TARGETS or (to_state == "NEEDS_DECISION" and plan.state not in PUBLICATION_STATES):
        raise Conflict(f"transition to {to_state} is not delegated to other modules (human decision or planning "
                       "pipeline only)", code="TRANSITION_NOT_DELEGATED",
                       details={"from": plan.state, "to": to_state, "delegated": sorted(_DELEGATED_TARGETS)})
    if to_state == "PUBLISHING" and get_approved_version(session, plan_id) is None:
        raise Conflict("publishing needs a version bound by an ACTIVE approval", code="NOT_APPROVED")
    transition(session, plan, to_state, reason=reason, actor=actor)
    return plan
