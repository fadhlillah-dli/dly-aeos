from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from aeos.api.deps import DB, CurrentPrincipal
from aeos.audit import service as audit

router = APIRouter(tags=["audit"])


@router.get("/api/audit")
def list_audit(
    principal: CurrentPrincipal,
    db: DB,
    action_prefix: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    run_id: str | None = Query(None, max_length=36),
    before: int | None = Query(None, ge=1),
    limit: int = Query(100, le=500),
) -> dict[str, Any]:
    domains = [principal.active_domain.value] if principal.active_domain else []
    rows = audit.list_events(
        db, project_ids=principal.allowed_project_ids, domains=domains, action_prefix=action_prefix,
        target_type=target_type, target_id=target_id, run_id=run_id, before_seq=before, limit=limit,
    )
    from aeos.core.timeutil import utcnow
    from aeos.operations import facade as operations

    retention = operations.RetentionMarkers(db)  # §35: the policy rule of each record, never a deletion
    return {
        "items": [
            {
                "seq": r.seq, "at": r.at.isoformat(), "actor_type": r.actor_type, "actor_id": r.actor_id,
                "action": r.action, "target_type": r.target_type, "target_id": r.target_id,
                "project_id": r.project_id, "domain": r.domain, "run_id": r.run_id, "result": r.result,
                "reason": r.reason, "details": r.details, "retention": retention.audit(r),
            }
            for r in rows
        ],
        "source": "audit_events (append-only local table; not a WORM guarantee — §34)",
        "checked_at": utcnow().isoformat(),
        "mode": "LIVE",
    }
