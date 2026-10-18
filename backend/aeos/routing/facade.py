"""Cross-module contract for worker model routing (§23). Owned by ``aeos.routing``.

Two-stage routing: hard eligibility filter first (domain/data/terms/tools/context/budget/
availability), then selection by evaluated task results. Models never select themselves; fallback
only to already-approved routes; no silent paid fallback or supervisor downgrade.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from aeos.models.planning import TaskSpec
from aeos.models.routing import ModelRoute


@dataclass(frozen=True)
class RouteDecision:
    route: ModelRoute | None
    eligible: list[str]
    rejected: dict[str, list[str]] = field(default_factory=dict)  # route_key -> reasons
    reason: str = ""


def eligible_routes(
    session: Session, *, domain: str, data_classification: str, needs_tools: bool = True,
    min_context: int | None = None, pool: str = "WORKER",
) -> RouteDecision:
    from aeos.routing.service import eligible_routes as impl

    return impl(session, domain=domain, data_classification=data_classification, needs_tools=needs_tools,
                min_context=min_context, pool=pool)


def select_route(session: Session, *, task: TaskSpec) -> RouteDecision:
    """Raise ``Blocked`` when no approved route is eligible for the task."""
    from aeos.routing.service import select_route as impl

    return impl(session, task=task)


# --- additive contracts (existing signatures above are unchanged) ------------------------------


def fallback_route(session: Session, *, task: TaskSpec, failed_route_key: str) -> RouteDecision:
    """Next route after a failure (e.g. 429): only along the failed route's fallback_chain, only to
    routes that are themselves eligible, same pool and free. Raises ``Blocked`` -> back off/queue."""
    from aeos.routing.service import fallback_route as impl

    return impl(session, task=task, failed_route_key=failed_route_key)


def note_rate_limited(
    session: Session, *, provider: str, scope: str = "free-daily", retry_after_seconds: int | None = None,
    source: str = "observed-429",
) -> None:
    """Record an observed rate/quota limit so routing excludes the route until it resets."""
    from aeos.routing.service import note_rate_limited as impl

    impl(session, provider=provider, scope=scope, retry_after_seconds=retry_after_seconds, source=source)
