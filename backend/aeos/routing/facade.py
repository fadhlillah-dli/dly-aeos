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


def terms_eligibility(route: ModelRoute, *, data_classification: str) -> list[str]:
    """Terms/data/provider eligibility reasons of *route* for a workload of *data_classification* (empty =
    eligible). The evaluation gate of §40/§43; a subset of the routing hard filter."""
    from aeos.routing.service import terms_eligibility as impl

    return impl(route, data_classification=data_classification)


def route_use_problem(session: Session, route: ModelRoute) -> str | None:
    """Why *route* may not serve a request right now (None = allowed): its APPROVED/TRIAL status must
    rest on an ACTIVE human decision bound to the CURRENT configuration hash, and a route that is not
    free (any more) needs a valid budget decision. The broker re-checks this per request (§23)."""
    from aeos.routing.service import route_use_problem as impl

    return impl(session, route)


def client_restriction_problem(route: ModelRoute) -> str | None:
    """Why the broker may not call *route* as its own HTTP client (None = no provider client restriction, or the owner
    recorded a decision for it at activation; additive, ADR-0041 amendment 2026-10-25). Part of
    ``route_use_problem``; the broker also checks it first to report ``CLIENT_TERMS_RESTRICTED``."""
    from aeos.routing.service import client_restriction_problem as impl

    return impl(route)


def note_rate_limited(
    session: Session, *, provider: str, scope: str = "free-daily", retry_after_seconds: int | None = None,
    source: str = "observed-429",
) -> None:
    """Record an observed rate/quota limit so routing excludes the route until it resets."""
    from aeos.routing.service import note_rate_limited as impl

    impl(session, provider=provider, scope=scope, retry_after_seconds=retry_after_seconds, source=source)


def observe_supervisor_call(session: Session, *, model: str, engine_version: str | None,
                            effort: str | None = None) -> dict[str, object]:
    """§23: bind the Claude Code version, the effort/thinking setting, the planning prompt revision, the policy version
    and the planning context strategy of a planning/review call to the supervisor route's configuration identity (a
    later change is a new configuration, never silent)."""
    from aeos.routing.service import observe_supervisor_call as impl

    return impl(session, model=model, engine_version=engine_version, effort=effort)


def supervisor_configuration(session: Session, model: str) -> dict[str, object] | None:
    """§02: the supervisor route configuration a call ran on and its evaluation state (NEEDS_EVALUATION etc.)."""
    from aeos.routing.service import supervisor_configuration as impl

    return impl(session, model)
