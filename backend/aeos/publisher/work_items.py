"""OperationalWorkProjection sync (master §13, §14, §30).

GitHub Issues (PERSONAL) / Jira (OFFICE) are the source of truth for *work status after
publication*. AEOS mirrors that status as a projection with ``last_synced_at``; it is never a second
work authority and it never feeds runtime readiness: a closed/Done issue does not create acceptance,
does not move a TaskExecution and does not unlock dependencies (§14, §50).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.errors import Blocked
from aeos.core.timeutil import utcnow
from aeos.events import service as events
from aeos.models.portfolio import Project
from aeos.models.publishing import OperationalWorkProjection
from aeos.publisher.providers import build_provider
from aeos.publisher.providers.base import ProviderBlocked, ProviderError
from aeos.publisher.targets import PROFILE, resolve_target

AUTHORITY_NOTE = ("Proyeksi status dari issue tracker (bukan authority kerja kedua). Status closed/Done tidak "
                  "membuka dependency runtime tanpa bukti penerimaan AEOS.")


def projection_view(row: OperationalWorkProjection) -> dict[str, Any]:
    return {
        "id": row.id, "project_id": row.project_id, "task_id": row.task_id, "provider": row.provider,
        "issue_id": row.issue_id, "issue_url": row.issue_url, "external_status": row.external_status,
        "last_synced_at": row.last_synced_at.isoformat() if row.last_synced_at else None,
        "sync_status": row.sync_status, "sync_error": row.sync_error, "logical_id": row.logical_id,
        "feature_key": row.feature_key, "title": row.title, "authority": "PROJECTION_ONLY",
        "unlocks_runtime_dependencies": False,
    }


def list_work_items(session: Session, project: Project) -> dict[str, Any]:
    rows = list(session.scalars(select(OperationalWorkProjection).where(
        OperationalWorkProjection.project_id == project.id).order_by(OperationalWorkProjection.created_at)))
    checked = session.scalar(select(func.max(OperationalWorkProjection.last_synced_at)).where(
        OperationalWorkProjection.project_id == project.id))
    family = PROFILE[project.domain]["issues"]
    return {"items": [projection_view(r) for r in rows], "source": family, "checked_at": checked.isoformat()
            if checked else None, "mode": "LIVE", "authority": "PROJECTION_ONLY", "note": AUTHORITY_NOTE,
            "capabilities": {"github_projects": "NOT_IMPLEMENTED"} if family == "github-issues" else {}}


def sync_work_items(session: Session, project: Project, *, actor_user_id: str) -> dict[str, Any]:
    """Read issue states from the tracker. Read-only towards the provider; no interlock needed."""
    target = resolve_target(session, project, "issues")
    if target.status != "READY":
        raise Blocked(f"{target.label} not ready", code="TARGET_NOT_READY",
                      details={"reasons": target.reasons, "remedies": target.remedies})
    try:
        provider = build_provider(target.family, target.config)
    except ProviderBlocked as exc:
        raise Blocked(exc.message, code=exc.code, details={"remedies": [exc.remedy]}) from exc
    now = utcnow()
    synced = failed = 0
    try:
        get_state = getattr(provider, "issue_state", None)
        if get_state is None:
            raise Blocked(f"{target.label} adapter cannot read issue state", code="CAPABILITY_MISSING")
        rows = list(session.scalars(select(OperationalWorkProjection).where(
            OperationalWorkProjection.project_id == project.id,
            OperationalWorkProjection.provider == target.family)))
        for row in rows:
            try:
                st = get_state(row.issue_id)
            except ProviderError as exc:
                row.sync_status = "FAILED"  # last known status stays visible with its old timestamp
                row.sync_error = exc.message[:2000]
                failed += 1
                continue
            row.external_status = str(st.get("external_status") or "UNKNOWN")[:64]
            row.issue_url = st.get("url") or row.issue_url
            if st.get("title"):
                row.title = str(st["title"])[:256]
            row.last_synced_at = now
            row.sync_status = "SYNCED"
            row.sync_error = ""
            synced += 1
    finally:
        provider.close()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="work_items.sync",
                 target_type="project", target_id=project.id, project_id=project.id, domain=project.domain,
                 result="SUCCESS" if not failed else "FAILED", details={"synced": synced, "failed": failed})
    events.emit(session, source="publisher", kind="work_items.synced", entity_type="project", entity_id=project.id,
                project_id=project.id, domain=project.domain, payload={"synced": synced, "failed": failed})
    session.flush()
    return {"synced": synced, "failed": failed, "checked_at": now.isoformat(), "source": target.family,
            "authority": "PROJECTION_ONLY"}
