"""Cross-module contract for budgets, reservations and the usage ledger (§22, §42).

Owned by ``aeos.cost``. Reservations are atomic; concurrency must not open a way past a limit.
If budget state cannot be trusted, admission is refused (§50 "Budget DB/gateway tidak sehat").

On ``Blocked`` from ``reserve`` the caller must roll back its transaction (the admission lock is
held until commit/rollback). Native Claude usage (category ``NATIVE_CLAUDE``) is never converted
into dollars; any ``cost_usd`` passed for it is discarded.

Flat-fee SUBSCRIPTION routes (additive, ADR-0080 amendment 2026-10-26): ``subscription_terms`` and
``allowance_status`` state a route's plan and the allowance this AEOS instance consumed per provider window;
``reserve`` refuses ``SUBSCRIPTION_ALLOWANCE_EXHAUSTED`` (the 5h window used up), ``SUBSCRIPTION_ALLOWANCE_UNKNOWN``
(a call in the 5h window cannot be counted; at most 5 hours) and ``SUBSCRIPTION_BEYOND_PLAN`` (a call billed beyond
the plan, until the owner re-activates the route). The week/month windows are advisory (``WARN_EXHAUSTED``).
``subscription_admission_block`` gives the same (code, reason) to the scheduler and the probe,
``subscription_beyond_plan`` the broker's per-request check, and ``subscription_without_figures`` the view of a
principal who cannot see every domain.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from aeos.models.cost import BudgetReservation, QuotaState, UsageLedgerEntry
from aeos.models.routing import ModelRoute


@dataclass(frozen=True)
class ReservationRequest:
    run_id: str
    project_id: str
    domain: str
    route_key: str | None
    est_usd: float
    est_tokens: int
    est_requests: int = 1


def reserve(session: Session, req: ReservationRequest) -> BudgetReservation:
    """Reserve against every applicable budget atomically; raise ``Blocked`` when exceeded."""
    from aeos.cost.service import reserve as impl

    return impl(session, req)


def release(session: Session, reservation_id: str) -> None:
    from aeos.cost.service import release as impl

    impl(session, reservation_id)


def record_usage(
    session: Session, *, provider: str, category: str, source: str, project_id: str | None = None,
    domain: str | None = None, run_id: str | None = None, route_key: str | None = None, model: str | None = None,
    input_tokens: int | None = None, output_tokens: int | None = None, requests: int = 1,
    cost_usd: float | None = None, is_estimate: bool = False, reservation_id: str | None = None,
    mode: str = "LIVE", partial: bool = False, details: dict[str, Any] | None = None,
) -> UsageLedgerEntry:
    """``partial=True`` (additive, integration 2026-10-18): one call of a multi-call run commits into the
    run-level reservation without closing it; ``release`` settles the hold when the run ends. MOCK/DEMO
    usage never commits a reservation — release it instead (``NON_LIVE_RESERVATION_COMMIT``).

    ``details`` (additive, ADR-0080 amendment 2026-10-26): provider billing facts of the call, e.g.
    ``reported_allowance_usd`` / ``billing_source`` of a subscription route, or ``charged_tokens`` (an upper bound of
    the call's tokens when the upstream reported none). Redacted before it is stored."""
    kwargs = dict(locals())  # captured before the lazy import adds 'impl'
    if kwargs.get("details") is None:
        kwargs.pop("details")
    from aeos.cost.service import record_usage as impl

    return impl(**kwargs)


# --- additive (module group H): non-token units and quota state ------------------------------


def record_unit_usage(
    session: Session, *, provider: str, category: str, source: str, quantity: float, unit: str,
    project_id: str | None = None, domain: str | None = None, run_id: str | None = None, task_id: str | None = None,
    cost_usd: float | None = None, is_estimate: bool = False, mode: str = "LIVE",
) -> UsageLedgerEntry:
    """§42 units kept separate: CI minutes, compute seconds, storage bytes, human minutes (category
    CI/COMPUTE/STORAGE/HUMAN_TIME). ``cost_usd`` stays UNKNOWN unless a real price/bill exists."""
    from aeos.cost.service import record_usage as impl

    return impl(session, provider=provider, category=category, source=source, quantity=quantity, unit=unit,
                project_id=project_id, domain=domain, run_id=run_id, task_id=task_id, cost_usd=cost_usd,
                is_estimate=is_estimate, mode=mode)


def subscription_terms(route: ModelRoute | None) -> tuple[dict[str, Any] | None, str | None]:
    """(terms, problem) of a flat-fee SUBSCRIPTION route (``pricing.billing``); (None, None) for any other route.
    A problem means incomplete terms: the route is an unpriced paid route (fail closed), never a free one."""
    from aeos.cost.subscription import subscription_terms as impl

    return impl(route)


def allowance_status(session: Session, route: ModelRoute, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Plan + allowance this AEOS instance consumed per provider window (state OK / WARN_EXHAUSTED (advisory) /
    EXHAUSTED / UNKNOWN / BEYOND_PLAN / UNTRUSTED); None when *route* is not a SUBSCRIPTION route. Read-only."""
    from aeos.cost.subscription import allowance_status as impl

    return impl(session, route, now=now)


def subscription_admission_block(status: dict[str, Any] | None) -> tuple[str, str] | None:
    """(code, ASCII reason without allowance figures) when an ``allowance_status`` refuses a new admission or probe:
    ``SUBSCRIPTION_BEYOND_PLAN``, ``SUBSCRIPTION_ALLOWANCE_EXHAUSTED`` or ``SUBSCRIPTION_ALLOWANCE_UNKNOWN``."""
    from aeos.cost.subscription import admission_block as impl

    return impl(status)


def subscription_beyond_plan(session: Session, route: ModelRoute) -> dict[str, Any] | None:
    """The newest call of a SUBSCRIPTION route billed beyond the plan that no owner (re-)activation postdates (entry
    id, time, amount or UNKNOWN, billing source, remedy); None when the route may be used. Read-only."""
    from aeos.cost.subscription import beyond_plan_block as impl

    return impl(session, route)


def subscription_without_figures(status: dict[str, Any]) -> dict[str, Any]:
    """An ``allowance_status`` without the figures that span domains (windows, beyond-plan amounts, problems)."""
    from aeos.cost.subscription import without_figures as impl

    return impl(status)


def set_quota_state(
    session: Session, *, provider: str, state: str, source: str, scope: str = "*",
    details: dict[str, Any] | None = None, observed_at: datetime | None = None, resets_at: datetime | None = None,
) -> QuotaState:
    """Record OK/LOW/EXHAUSTED/UNKNOWN with its source. For ``claude-native`` only an official
    source may claim OK/LOW; an observed limit message may set EXHAUSTED (§42)."""
    from aeos.cost.service import set_quota_state as impl

    return impl(session, provider=provider, state=state, source=source, scope=scope, details=details,
                observed_at=observed_at, resets_at=resets_at)
