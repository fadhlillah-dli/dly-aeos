"""Cross-module contract for the trusted capability broker (§25, §26, §31). Owned by ``aeos.broker``.

Workers never receive upstream credentials. They receive a short-lived capability token bound to
one run, scope and caps. Only the broker may push ``aeos/*`` task branches and open PRs; there is
no merge operation anywhere in the broker (human-only merge, U-09).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from aeos.models.routing import CapabilityGrant


def issue_grant(
    session: Session, *, run_id: str, project_id: str, domain: str, kind: str, scope: dict[str, Any],
    ttl_seconds: int, max_requests: int | None = None, max_tokens: int | None = None,
    max_cost_usd: float | None = None,
) -> tuple[str, CapabilityGrant]:
    """Return (raw_token, grant). The raw token is handed to the sandbox only; DB stores its hash."""
    from aeos.broker.grants import issue_grant as impl

    return impl(session, run_id=run_id, project_id=project_id, domain=domain, kind=kind, scope=scope,
                ttl_seconds=ttl_seconds, max_requests=max_requests, max_tokens=max_tokens, max_cost_usd=max_cost_usd)


def revoke_grants(session: Session, *, run_id: str | None = None, all_active: bool = False, reason: str) -> int:
    from aeos.broker.grants import revoke_grants as impl

    return impl(session, run_id=run_id, all_active=all_active, reason=reason)


def push_task_branch(session: Session, *, run_id: str, worktree: str, branch: str, repo_full_name: str) -> dict[str, Any]:
    """Push only ``refs/heads/aeos/*`` without force/delete; never the default branch."""
    from aeos.broker.git_broker import push_task_branch as impl

    return impl(session, run_id=run_id, worktree=worktree, branch=branch, repo_full_name=repo_full_name)


def create_pull_request(
    session: Session, *, run_id: str, repo_full_name: str, head: str, base: str, title: str, body: str
) -> dict[str, Any]:
    from aeos.broker.git_broker import create_pull_request as impl

    return impl(session, run_id=run_id, repo_full_name=repo_full_name, head=head, base=base, title=title, body=body)


def reconcile_side_effect(session: Session, *, side_effect_id: str, actor: str) -> dict[str, Any]:
    """Read the external state of a PENDING/UNKNOWN push/PR and record it before any retry (§14).
    Read-only towards the provider. Added by the completeness review; existing signatures unchanged."""
    from aeos.broker.git_broker import reconcile_side_effect as impl

    return impl(session, side_effect_id, actor=actor)
