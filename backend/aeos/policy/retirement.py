"""Owner's final retirement verification (§33) as a hash-bound Decision.

§33: "Retirement memetakan consumer/dependency, data retention/disposal, credential/IAM/repo/integration/
job/infra closure, legal hold, backup closure, communication, dan verifikasi final oleh pemilik yang sah."

* The checklist itself is computed by ``aeos.operations.retirement`` (read-only projection).
* The owner records ``RETIREMENT`` on ``project/<id>`` bound to ``content_hash`` of the checklist items he
  verified (the final-verification row excluded, it depends on this decision). The decision is refused
  while any item is BLOCKING or the project is not RETIRING.
* ``RETIRED`` is reachable only while an ACTIVE RETIREMENT decision matches the *current* checklist hash:
  a new task, grant or publication after the verification changes the hash and the owner verifies again.
* Nothing is deleted, revoked or archived here (no agent removes data or backups, §33).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.config import Settings, get_settings
from aeos.core.errors import Conflict, ValidationFailed
from aeos.core.hashing import content_hash
from aeos.models.decisions import Decision
from aeos.models.portfolio import Project
from aeos.policy import catalog
from aeos.policy import decisions as registry

FINAL_ITEM = "final_verification"


def checklist_hash(checklist: dict[str, Any]) -> str:
    """Hash of what the owner verifies: every item's key/status/detail except the final verification row."""
    items = [{"key": i.get("key"), "status": i.get("status"), "detail": i.get("detail") or {}}
             for i in checklist.get("items") or [] if i.get("key") != FINAL_ITEM]
    return content_hash({"project": (checklist.get("project") or {}).get("id"), "items": items})


def _checklist(session: Session, settings: Settings, project: Project) -> dict[str, Any]:
    from aeos.operations import retirement

    return retirement.checklist(session, settings, project)


def active_retirement(session: Session, project_id: str) -> Decision | None:
    return registry.find_active_decision(session, kind="RETIREMENT", target_type=catalog.TARGET_PROJECT_RETIREMENT,
                                         target_id=project_id)


def binding_status(session: Session, project: Project, *, current_hash: str) -> dict[str, Any]:
    """Whether an ACTIVE RETIREMENT decision is bound to *current_hash* (used by the checklist view)."""
    d = active_retirement(session, project.id)
    if d is None:
        return {"status": "PENDING_OWNER", "decision_id": None, "bound_hash": None}
    if d.target_hash != current_hash:
        return {"status": "STALE", "decision_id": d.id, "bound_hash": d.target_hash,
                "reason": "checklist berubah setelah verifikasi pemilik; verifikasi ulang diperlukan"}
    return {"status": "VERIFIED", "decision_id": d.id, "bound_hash": d.target_hash}


def record_retirement(session: Session, *, project: Project, actor_user_id: str, rationale: str,
                      expected_hash: str | None = None, settings: Settings | None = None) -> Decision:
    """Record the owner's final verification. Never commits."""
    settings = settings or get_settings()
    if (project.lifecycle or "ACTIVE").upper() != "RETIRING":
        raise Conflict(f"project lifecycle is {project.lifecycle}; the final verification is recorded while "
                       "RETIRING", code="RETIREMENT_REQUIRES_RETIRING")
    if project.is_platform:
        raise Conflict("retiring the DLY AEOS platform project needs a migration decision",
                       code="PLATFORM_RETIREMENT_DENIED")
    rationale = (rationale or "").strip()
    if len(rationale) < 10:
        raise ValidationFailed("the final verification needs a rationale (min 10 characters)")
    checklist = _checklist(session, settings, project)
    if checklist.get("blocking"):
        raise Conflict("retirement checklist still has BLOCKING items", code="RETIREMENT_BLOCKING_ITEMS",
                       details={"blocking": list(checklist["blocking"])})
    current = checklist_hash(checklist)
    if expected_hash is not None and expected_hash != current:
        raise Conflict("the checklist changed since it was shown; review it again", code="RETIREMENT_CHECKLIST_STALE",
                       details={"current_hash": current, "requested_hash": expected_hash})
    previous = [d for d in session.scalars(select(Decision).where(
        Decision.kind == "RETIREMENT", Decision.target_type == catalog.TARGET_PROJECT_RETIREMENT,
        Decision.target_id == project.id, Decision.status == "ACTIVE"))]
    decision = registry.record_decision(
        session, kind="RETIREMENT", actor_user_id=actor_user_id, target_type=catalog.TARGET_PROJECT_RETIREMENT,
        target_id=project.id, target_hash=current, allowed_operations=list(catalog.RETIREMENT_OPERATIONS),
        auth_scope={"project_id": project.id, "scope": "final retirement verification (§33)"},
        project_id=project.id, domain=project.domain, rationale=rationale,
        details={"checklist_items": [i.get("key") for i in checklist.get("items") or []],
                 "owner_actions_open": [i.get("key") for i in checklist.get("items") or []
                                        if i.get("status") in ("OWNER_ACTION", "OWNER_DECISION_REQUIRED",
                                                               "REVIEW_REQUIRED", "UNKNOWN")],
                 "note": "verifikasi pemilik; tidak ada data/backup yang dihapus otomatis"},
    )
    for old in previous:
        registry.supersede_decision(session, old, new_decision_id=decision.id, reason="new final verification")
    return decision


def require_retirement_verified(session: Session, project: Project, *, settings: Settings | None = None) -> Decision:
    """Raise ``Conflict(RETIREMENT_NOT_VERIFIED)`` unless an ACTIVE RETIREMENT decision is bound to the
    current checklist hash and nothing is BLOCKING (fail closed)."""
    settings = settings or get_settings()
    checklist = _checklist(session, settings, project)
    if checklist.get("blocking"):
        raise Conflict("retirement checklist still has BLOCKING items", code="RETIREMENT_NOT_VERIFIED",
                       details={"blocking": list(checklist["blocking"])})
    current = checklist_hash(checklist)
    d = active_retirement(session, project.id)
    if d is None or d.target_hash != current:
        raise Conflict("RETIRED needs the owner's final verification (RETIREMENT decision) bound to the current "
                       "checklist", code="RETIREMENT_NOT_VERIFIED",
                       details={"current_hash": current, "decision_id": d.id if d else None,
                                "bound_hash": d.target_hash if d else None})
    return d
