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


def supervisor_prompt_runtime() -> dict[str, object]:
    """What a planning call to the native supervisor ran with (additive, ADR-0041 amendment 2026-10-25), so routing can
    bind it to the supervisor ModelRoute identity (§23 "prompt revision", "context strategy"):

    * ``prompt_revision``: the declared ``PROMPT_REVISION`` plus a fingerprint of the prompt module's text (line endings
      normalised), so an edit of the templates/fixed policy without a revision bump is still a new revision;
    * ``context``: how planning context is assembled - fixed control-plane facts, untrusted request/attachments as
      fenced DATA, the challenger/final reviewer see the review package only (§15) - and its size budgets. None when a
      value cannot be determined (it is then never bound)."""
    import hashlib
    from pathlib import Path

    from aeos.planning import prompts

    declared = str(getattr(prompts, "PROMPT_REVISION", "") or "UNKNOWN")
    try:
        text = Path(prompts.__file__).read_bytes().replace(b"\r\n", b"\n")
        revision = f"{declared}@{hashlib.sha256(text).hexdigest()[:12]}"[:64]
    except (OSError, TypeError):
        revision = declared
    context: dict[str, object] | None = None
    try:
        from aeos.planning import service as planning

        context = {"assembly": "fixed-context+fenced-untrusted-data", "review_isolation": "review-package-only",
                   "max_prompt_chars": planning.MAX_PROMPT_CHARS, "max_attachment_chars": planning.MAX_ATTACHMENT_CHARS,
                   "max_attachments": planning.MAX_ATTACHMENTS}
    except (ImportError, AttributeError):  # a planning build without the budgets: the strategy cannot be stated
        context = None
    return {"prompt_revision": revision, "context": context}
