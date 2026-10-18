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
