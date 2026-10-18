"""Cross-module contract for budgets, reservations and the usage ledger (§22, §42).

Owned by ``aeos.cost``. Reservations are atomic; concurrency must not open a way past a limit.
If budget state cannot be trusted, admission is refused (§50 "Budget DB/gateway tidak sehat").
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from aeos.models.cost import BudgetReservation, UsageLedgerEntry


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
    mode: str = "LIVE",
) -> UsageLedgerEntry:
    from aeos.cost.service import record_usage as impl

    return impl(**locals())
