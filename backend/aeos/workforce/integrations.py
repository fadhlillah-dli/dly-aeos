"""Fail-closed calls into other modules' facades.

Other modules are developed in parallel; when an implementation module is missing the facade's
lazy import raises ``ImportError``. Every wrapper here turns that into an explicit NOT-READY
answer with a reason — it never pretends success (AGENTS.md invariant 8).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from aeos.core.enums import Capability, Domain
from aeos.core.errors import Blocked
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.models.decisions import Decision
from aeos.models.planning import PlanVersion, TaskSpec
from aeos.models.routing import ModelRoute

log = logging.getLogger("aeos.workforce")


def _unavailable(module: str, exc: BaseException) -> str:
    log.info("module %s unavailable: %s", module, exc)
    return f"{module} module unavailable ({type(exc).__name__}); failing closed"


def approved_version(session: Session, plan_id: str | None) -> tuple[PlanVersion | None, str]:
    if not plan_id:
        return None, "task has no plan"
    from aeos.planning import facade as planning

    try:
        v = planning.get_approved_version(session, plan_id)
    except ImportError as exc:
        return None, _unavailable("planning", exc)
    if v is None:
        return None, "plan has no ACTIVE approval decision"
    return v, ""


def acceptance_valid(session: Session, task: TaskSpec) -> tuple[bool, str]:
    from aeos.verification import facade as verification

    try:
        ok = verification.acceptance_is_valid(session, task.id, plan_version_hash=task.plan_version_hash)
    except ImportError as exc:
        return False, _unavailable("verification", exc)
    return bool(ok), "" if ok else "no ACCEPTED acceptance record still valid for this plan version"


def acceptance_valid_for_run(session: Session, task: TaskSpec, base_commit: str | None) -> tuple[bool, str]:
    from aeos.verification import facade as verification

    try:
        ok = verification.acceptance_is_valid(session, task.id, base_commit=base_commit,
                                              plan_version_hash=task.plan_version_hash)
    except ImportError as exc:
        return False, _unavailable("verification", exc)
    return bool(ok), "" if ok else "acceptance record is not ACCEPTED/valid for this base/plan identity"


def capability_ready(session: Session, capability: Capability, *, domain: str, project_id: str | None) -> tuple[bool, list[str]]:
    from aeos.doctor import facade as doctor

    try:
        doctor.require_capability(session, capability, domain=domain, project_id=project_id)
    except ImportError as exc:
        return False, [_unavailable("doctor", exc)]
    except Blocked as exc:
        reasons = list(exc.details.get("reasons") or []) or [exc.message]
        return False, [f"{capability.value}: {r}" for r in reasons]
    return True, []


def select_route(session: Session, task: TaskSpec) -> tuple[ModelRoute | None, list[str]]:
    from aeos.routing import facade as routing

    try:
        decision = routing.select_route(session, task=task)
    except ImportError as exc:
        return None, [_unavailable("routing", exc)]
    except Blocked as exc:
        return None, [exc.message, *[str(r) for r in exc.details.get("reasons", [])]]
    route = decision.route
    if route is None:
        return None, [decision.reason or "no eligible approved route"]
    problems: list[str] = []
    # Defence in depth (§12, §23): the worker pool never uses the Claude Max supervisor route, and
    # the route must explicitly list the task's domain.
    if route.provider == "claude-native" or route.pool != "WORKER":
        problems.append(f"route {route.route_key} is not a WORKER route")
    if route.status not in {"APPROVED", "TRIAL"}:
        problems.append(f"route {route.route_key} status {route.status} is not APPROVED/TRIAL")
    if task.domain not in (route.eligible_domains or []):
        problems.append(f"route {route.route_key} is not eligible for domain {task.domain}")
    if problems:
        return None, problems
    return route, []


def office_external_allowed(session: Session) -> tuple[bool, str]:
    from aeos.policy import facade as policy

    try:
        pol = policy.office_policy(session)
    except ImportError as exc:
        return False, _unavailable("policy", exc)
    if not pol.get("external_worker_allowed"):
        return False, "OFFICE external_worker_allowed=false (§23 default deny)"
    return True, ""


def reserve(session: Session, *, run_id: str, project_id: str, domain: str, route_key: str | None,
            est_usd: float, est_tokens: int, est_requests: int) -> Any:
    """Raises ``Blocked`` when the reservation is refused or the budget module is unavailable."""
    from aeos.cost import facade as cost

    try:
        return cost.reserve(session, cost.ReservationRequest(
            run_id=run_id, project_id=project_id, domain=domain, route_key=route_key, est_usd=est_usd,
            est_tokens=est_tokens, est_requests=est_requests))
    except ImportError as exc:
        raise Blocked(_unavailable("cost", exc), code="BUDGET_UNAVAILABLE") from exc


def release(session: Session, reservation_id: str | None) -> str:
    if not reservation_id:
        return "NONE"
    from aeos.cost import facade as cost

    try:
        cost.release(session, reservation_id)
    except ImportError as exc:
        _unavailable("cost", exc)
        return "UNKNOWN"
    except Exception as exc:  # recorded, never hidden
        log.warning("reservation release failed: %s", exc)
        return "FAILED"
    return "RELEASED"


def record_decision(session: Session, **kwargs: Any) -> Decision:
    from aeos.policy import facade as policy

    try:
        return policy.record_decision(session, **kwargs)
    except ImportError as exc:
        raise Blocked(_unavailable("policy", exc), code="POLICY_UNAVAILABLE") from exc


def active_exception_decision(session: Session, *, target_type: str, target_id: str) -> Decision | None:
    """ACTIVE, unexpired, human EXCEPTION decision for a target (policy facade first, then a
    read-only lookup of the canonical Decision rows when the policy module is absent)."""
    from aeos.policy import facade as policy

    try:
        d = policy.find_active_decision(session, kind="EXCEPTION", target_type=target_type, target_id=target_id)
    except ImportError:
        from sqlalchemy import select

        rows = session.scalars(
            select(Decision).where(Decision.kind == "EXCEPTION", Decision.target_type == target_type,
                                   Decision.target_id == target_id, Decision.status == "ACTIVE")
            .order_by(Decision.created_at.desc())
        )
        d = next(iter(rows), None)
    if d is None or d.status != "ACTIVE" or d.actor_kind != "HUMAN":
        return None
    exp = ensure_aware(d.expires_at)
    if exp is not None and exp <= utcnow():
        return None
    return d


def is_personal(domain: str) -> bool:
    return domain == Domain.PERSONAL.value
