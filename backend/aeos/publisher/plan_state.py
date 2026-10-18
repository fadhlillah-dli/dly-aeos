"""Planning-state transitions requested by the publisher (master §14).

The planning module owns ``Plan.state``. When ``aeos.planning.states`` provides
``transition(session, plan, to_state, *, reason, actor)`` the publisher uses it. Until then the
publisher applies only the publication branch of §14 with an explicit legal-transition table and a
clear ``state_reason``; anything else is refused (and reported) rather than forced.

Publication status itself lives on ``Publication``; the plan state is a summary for the plan
lifecycle. If the plan moved on (e.g. a newer revision is in review) the publisher does not
overwrite that state (§50 "Revisi baru muncul selama publish").
"""

from __future__ import annotations

import inspect
import logging

from sqlalchemy.orm import Session

from aeos.core.enums import PlanningState as PS
from aeos.core.errors import AeosError
from aeos.models.planning import Plan

log = logging.getLogger("aeos.publisher")

LEGAL_PUBLICATION_TRANSITIONS: dict[str, set[str]] = {
    PS.APPROVED: {PS.PUBLISHING},
    PS.PUBLISHING: {PS.PUBLISHED, PS.PUBLICATION_PARTIAL, PS.PUBLICATION_FAILED},
    PS.PUBLICATION_PARTIAL: {PS.PUBLISHING, PS.PUBLISHED},
    PS.PUBLICATION_FAILED: {PS.PUBLISHING},
    PS.PUBLISHED: {PS.PUBLISHING},
}


def _planning_transition():
    try:
        from aeos.planning import states  # type: ignore[attr-defined]
    except ImportError:
        return None
    fn = getattr(states, "transition", None)
    if fn is None:
        return None
    params = inspect.signature(fn).parameters
    if not {"reason", "actor"} <= set(params):
        log.warning("aeos.planning.states.transition has an unexpected signature; using publisher fallback")
        return None
    return fn


def set_plan_state(session: Session, plan_id: str | None, to_state: str, *, reason: str,
                   actor: str = "publisher") -> tuple[bool, str]:
    """Return (applied, note). Never raises for an illegal transition — it is reported instead."""
    if not plan_id:
        return False, "publication has no plan reference"
    plan = session.get(Plan, plan_id)
    if plan is None:
        return False, "plan not found"
    fn = _planning_transition()
    if fn is not None:
        try:
            fn(session, plan, to_state, reason=reason, actor=actor)
            return True, "planning.states.transition"
        except AeosError as exc:
            return False, f"planning refused transition {plan.state} -> {to_state}: {exc.message}"
    current = plan.state
    if current == to_state:
        return True, "already in state"
    if to_state not in LEGAL_PUBLICATION_TRANSITIONS.get(current, set()):
        return False, f"plan state {current} -> {to_state} not applied (not a legal publication transition)"
    plan.state = to_state
    plan.state_reason = f"[publisher] {reason}"
    return True, "publisher fallback (legal publication transition)"
