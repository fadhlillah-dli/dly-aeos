"""Append-only audit trail (§13 AuditEvent, §26 audit, §35 History/Audit).

Every privileged or externally visible action records actor, target, result and reason. Details
are redacted before persistence so secrets never reach the audit table.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.redaction import redact
from aeos.models.audit import AuditEvent


def record(
    session: Session,
    *,
    actor_type: str,
    actor_id: str,
    action: str,
    result: str = "SUCCESS",
    target_type: str = "",
    target_id: str = "",
    project_id: str | None = None,
    domain: str | None = None,
    reason: str = "",
    details: dict[str, Any] | None = None,
    request_id: str = "",
) -> AuditEvent:
    ev = AuditEvent(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        result=result,
        target_type=target_type,
        target_id=str(target_id),
        project_id=project_id,
        domain=domain,
        reason=redact(reason) if reason else "",
        details=redact(details or {}),
        request_id=request_id,
    )
    session.add(ev)
    session.flush()
    return ev


def list_events(
    session: Session,
    *,
    project_ids: list[str] | None = None,
    domains: list[str] | None = None,
    action_prefix: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    before_seq: int | None = None,
    limit: int = 100,
) -> list[AuditEvent]:
    q = select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(min(limit, 500))
    if project_ids is not None:
        q = q.where((AuditEvent.project_id.in_(project_ids)) | (AuditEvent.project_id.is_(None)))
    if domains is not None:
        q = q.where((AuditEvent.domain.in_(domains)) | (AuditEvent.domain.is_(None)))
    if action_prefix:
        q = q.where(AuditEvent.action.startswith(action_prefix))
    if target_type:
        q = q.where(AuditEvent.target_type == target_type)
    if target_id:
        q = q.where(AuditEvent.target_id == target_id)
    if before_seq:
        q = q.where(AuditEvent.seq < before_seq)
    return list(session.scalars(q))
