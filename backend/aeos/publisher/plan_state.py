"""Planning-state transitions requested by the publisher (master §14, ADR-0031 consequences).

The planning module owns ``Plan.state``. The publisher moves a plan only through the frozen
``aeos.planning.facade.transition_plan`` contract, which is limited to the publication states
(APPROVED -> PUBLISHING -> PUBLISHED / PUBLICATION_PARTIAL / PUBLICATION_FAILED, a bounded retry back to
PUBLISHING) and requires a version bound by an ACTIVE approval to enter PUBLISHING. There is no private
fallback table: when planning is unavailable or refuses, nothing is changed and the refusal is reported.

Publication status itself lives on ``Publication``; the plan state is a summary for the plan
lifecycle. If the plan moved on (e.g. a newer revision is in review) the publisher does not
overwrite that state (§50 "Revisi baru muncul selama publish").
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from aeos.core.errors import AeosError

log = logging.getLogger("aeos.publisher")


def set_plan_state(session: Session, plan_id: str | None, to_state: str, *, reason: str,
                   actor: str = "publisher") -> tuple[bool, str]:
    """Return (applied, note). Never raises for an illegal transition — it is reported instead."""
    if not plan_id:
        return False, "publication has no plan reference"
    try:
        from aeos.planning import facade as planning
    except (ImportError, ModuleNotFoundError):  # fail closed: no planning, no plan-state change
        return False, "planning module unavailable; plan state not changed"
    plan = planning.get_plan(session, plan_id)
    if plan is None:
        return False, "plan not found"
    if plan.state == str(to_state):
        return True, "already in state"
    try:
        planning.transition_plan(session, plan_id, str(to_state), reason=f"[publisher] {reason}", actor=actor)
    except AeosError as exc:
        log.info("planning refused %s -> %s for plan %s: %s", plan.state, to_state, plan_id, exc.code)
        return False, f"planning refused transition {plan.state} -> {to_state} [{exc.code}]: {exc.message}"
    return True, "planning.facade.transition_plan"
