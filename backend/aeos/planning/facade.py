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


def transition_plan(session: Session, plan_id: str, to_state: str, *, reason: str, actor: str) -> Plan:
    """Apply a legal §14 planning transition (e.g. the publisher: APPROVED -> PUBLISHING -> PUBLISHED /
    PUBLICATION_PARTIAL / PUBLICATION_FAILED). Raises ``Conflict`` for an illegal transition and
    ``NotFound`` for an unknown plan. Does not commit."""
    from aeos.core.errors import NotFound
    from aeos.planning.states import transition

    plan = session.get(Plan, plan_id)
    if plan is None:
        raise NotFound("plan not found")
    transition(session, plan, to_state, reason=reason, actor=actor)
    return plan
