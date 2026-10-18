"""Cross-module contract for decisions and the external-write interlock (§14, §20, §26).

Owned by ``aeos.policy``. Signatures are frozen for other modules; the policy agent implements
the bodies in ``aeos.policy.decisions`` / ``aeos.policy.interlock``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from aeos.models.decisions import Decision


@dataclass(frozen=True)
class WriteIntent:
    """Everything the interlock needs to decide whether an external write may run (§26)."""

    actor_user_id: str
    project_id: str
    domain: str
    provider: str  # google-drive|google-docs|github-issues|github-repo|confluence|jira|bitbucket
    operation: str  # create_folder|create_doc|update_doc|create_issue|push_branch|create_pr|...
    target: dict[str, Any]
    approved_plan_version_id: str | None
    approved_hash: str | None
    decision_id: str | None
    capability_expires_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InterlockResult:
    allowed: bool
    reasons: list[str]
    decision: Decision | None = None


def record_decision(
    session: Session,
    *,
    kind: str,
    actor_user_id: str,
    target_type: str,
    target_id: str,
    target_version: str | None = None,
    target_hash: str | None = None,
    allowed_operations: list[str] | None = None,
    auth_scope: dict[str, Any] | None = None,
    project_id: str | None = None,
    domain: str | None = None,
    rationale: str = "",
    expires_at: datetime | None = None,
    details: dict[str, Any] | None = None,
) -> Decision:
    from aeos.policy.decisions import record_decision as impl

    return impl(**locals())


def find_active_decision(
    session: Session,
    *,
    kind: str,
    target_type: str,
    target_id: str,
    target_hash: str | None = None,
    operation: str | None = None,
) -> Decision | None:
    from aeos.policy.decisions import find_active_decision as impl

    return impl(**locals())


def invalidate_decisions(session: Session, *, target_type: str, target_id: str, reason: str) -> int:
    from aeos.policy.decisions import invalidate_decisions as impl

    return impl(**locals())


def check_external_write(session: Session, intent: WriteIntent) -> InterlockResult:
    """Fail-closed interlock: bootstrap complete, actor AUTH, domain/provider/target valid,
    write enablement, approved immutable version/hash, unexpired capability (§26)."""
    from aeos.policy.interlock import check_external_write as impl

    return impl(session, intent)


def office_policy(session: Session) -> dict[str, Any]:
    """§23: OFFICE default external_worker_allowed=false and approved_worker_routes=[]."""
    from aeos.policy.office import office_policy as impl

    return impl(session)
