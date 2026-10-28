"""The POLICY decision behind lowering a project's data classification (ADR-0133, §04, §20, §23).

``aeos.portfolio.reclassify`` owns the change itself and calls these through ``aeos.policy.facade``:

* ``record_lowering``: one POLICY decision on ``project_classification/<project id>`` bound to the old and new class
  (``target_version`` = new class, ``target_hash`` over project/from/to), operation ``lower_data_classification``.
  The registry re-checks the actor (active SUPER_ADMIN, scope), audits it and emits ``decision.recorded``. A newer
  lowering supersedes the project's previous ACTIVE lowering decision.
* ``end_lowerings``: a raise ends (INVALIDATED, with actor and reason) the project's ACTIVE lowering decisions, which
  no longer describe the class. Call it after the class changed: the registry's lifecycle hook restores a class
  only while the decision still backs it.

Only the dedicated command records this target: the generic decision path refuses it (``catalog.DEDICATED_TARGETS``).
Never commits.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.hashing import content_hash
from aeos.models.decisions import Decision
from aeos.policy import catalog
from aeos.policy import decisions as registry

TARGET = catalog.TARGET_PROJECT_CLASSIFICATION


def active_lowerings(session: Session, project_id: str) -> list[Decision]:
    return list(session.scalars(select(Decision).where(
        Decision.kind == "POLICY", Decision.target_type == TARGET, Decision.target_id == project_id,
        Decision.status == "ACTIVE").order_by(Decision.created_at)))


def record_lowering(session: Session, *, project_id: str, project_key: str, domain: str, from_class: str,
                    to_class: str, actor_user_id: str, reason: str,
                    details: dict[str, Any] | None = None) -> tuple[Decision, list[str]]:
    """Record the lowering decision; return it and the ids of the earlier lowering decisions it superseded."""
    previous = active_lowerings(session, project_id)
    decision = registry.record_decision(
        session, kind="POLICY", actor_user_id=actor_user_id, target_type=TARGET, target_id=project_id,
        target_version=to_class, target_hash=content_hash({"project_id": project_id, "from": from_class,
                                                           "to": to_class}),
        allowed_operations=list(catalog.PROJECT_CLASSIFICATION_OPERATIONS),
        auth_scope={"project_id": project_id, "project_key": project_key, "from": from_class, "to": to_class,
                    "operation": "lower_data_classification", "recorded_via": "aeos project reclassify"},
        project_id=project_id, domain=domain, rationale=reason,
        details={**(details or {}), "from_classification": from_class, "to_classification": to_class,
                 "direction": "LOWER",
                 "system_change": {"class": "NOT_A_SYSTEM_CHANGE", "evaluation": "NOT_APPLICABLE",
                                   "note": "a project's data classification, not a policy/model/tool/prompt "
                                           "target"}},
    )
    for old in previous:
        registry.supersede_decision(session, old, new_decision_id=decision.id,
                                    reason=f"classification lowered again to {to_class}", actor_type="user",
                                    actor_id=actor_user_id)
    return decision, [d.id for d in previous]


def end_lowerings(session: Session, *, project_id: str, actor_user_id: str, reason: str) -> list[str]:
    """Invalidate the project's ACTIVE lowering decisions (after a raise); return their ids."""
    ended = active_lowerings(session, project_id)
    for d in ended:
        registry.invalidate_decision(session, d, reason=reason, actor_type="user", actor_id=actor_user_id)
    return [d.id for d in ended]
