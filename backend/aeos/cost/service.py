"""Budgets, atomic reservations, usage ledger and quota state (§14, §22, §42, §50).

Rules implemented here (see ADR-0080):

* Default GLOBAL worker cash budget is **$0 with a hard stop**: only free routes are admitted until
  an owner ``BUDGET`` decision raises it. Budgets are never edited in place — a change creates a
  new row bound to the decision's ``target_hash`` and supersedes the old one.
* ``reserve`` serialises admissions with a write lock taken *before* budget state is read, then
  re-checks ``committed + reserved + request <= limit`` (usd/tokens/requests) for every applicable
  GLOBAL/PROJECT/ROUTE/RUN budget inside the caller's transaction.
* If budget state cannot be trusted (missing/invalid decision, tampered terms, duplicate active
  budgets, negative limits, unpriced paid usage, unknown pricing for a paid route) admission is
  refused (§50 "Budget DB/gateway tidak sehat").
* Native Claude (Max subscription) usage is never converted into dollars or a % of quota (§42).
* Flat-fee SUBSCRIPTION worker routes (``pricing.billing``, ADR-0080 amendment 2026-10-26; ``aeos.cost.subscription``):
  the plan fee is the owner's BUDGET decision at activation, the marginal cost is the provider's ``usage.cost`` (0
  within the plan, never the reservation charged as made-up dollars) and the allowance each call consumes is recorded
  apart from money. Only this AEOS instance's calls are counted. Admission waits while the 5h window's allowance is
  used up (``SUBSCRIPTION_ALLOWANCE_EXHAUSTED``) or a call in it cannot be counted (``SUBSCRIPTION_ALLOWANCE_UNKNOWN``,
  at most 5 hours); the week/month windows are advisory. A call billed beyond the plan refuses the route until the
  owner re-activates it (``SUBSCRIPTION_BEYOND_PLAN``, fail closed).
* Categories WORKER_API / NATIVE_CLAUDE / CI / COMPUTE / STORAGE / HUMAN_TIME stay separate.
* MOCK/DEMO entries never count as actual usage or against budgets (§50).

There is no paid fallback, no auto-topup and no key rotation anywhere in this module.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.enums import DataMode, Domain
from aeos.core.errors import Blocked, Conflict, NotFound, ValidationFailed
from aeos.core.hashing import content_hash
from aeos.core.redaction import redact, redact_text
from aeos.core.timeutil import ensure_aware, is_expired, utcnow
from aeos.cost import subscription as subs
from aeos.cost.amounts import USD_Q, ZERO, money, money_out
from aeos.events import service as events
from aeos.models.cost import (
    USAGE_CATEGORIES,
    Budget,
    BudgetReservation,
    CostAdmissionLock,
    QuotaState,
    UsageLedgerEntry,
)
from aeos.models.decisions import Decision
from aeos.models.evidence import AcceptanceRecord
from aeos.models.portfolio import Project
from aeos.models.routing import ModelRoute
from aeos.models.workforce import WorkerRun

SCOPE_TYPES = ("GLOBAL", "PROJECT", "ROUTE", "RUN")
PERIODS = ("DAY", "WEEK", "MONTH", "TOTAL")
QUOTA_STATES = ("OK", "LOW", "EXHAUSTED", "UNKNOWN")
# Only these sources may claim a remaining-quota state for the native Max subscription (§42).
OFFICIAL_QUOTA_SOURCES = frozenset({"official"})
NATIVE_PROVIDER = "claude-native"
BUDGETED_CATEGORIES = ("WORKER_API",)
ECONOMICS_CATEGORIES = ("WORKER_API", "CI", "COMPUTE")  # §43 Economics: worker API/compute/CI per accepted outcome
LIVE = DataMode.LIVE.value
NATIVE_NOTE = "Batas langganan berlaku; sisa kuota UNKNOWN"
DEFAULT_NOTES = (
    "Default: anggaran tunai worker $0 dengan hard stop - hanya rute gratis yang dapat berjalan "
    "sampai keputusan BUDGET pemilik menaikkannya (§42)."
)

CATEGORY_LABELS = {
    "WORKER_API": "API worker",
    "NATIVE_CLAUDE": "Claude native (langganan Max)",
    "CI": "CI",
    "COMPUTE": "Compute",
    "STORAGE": "Storage",
    "HUMAN_TIME": "Waktu manusia",
}


# --------------------------------------------------------------------------- primitives


def period_start(period: str, now: datetime | None = None) -> datetime | None:
    now = ensure_aware(now or utcnow())
    assert now is not None
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "DAY":
        return day
    if period == "WEEK":
        return day - timedelta(days=day.weekday())
    if period == "MONTH":
        return day.replace(day=1)
    if period == "TOTAL":
        return None
    raise ValidationFailed(f"unknown budget period {period!r}")


def budget_terms(b: Budget) -> dict[str, Any]:
    lim = money(b.limit_usd)
    return {
        "scope_type": b.scope_type,
        "scope_id": b.scope_id,
        "period": b.period,
        "limit_usd": None if lim is None else str(lim),
        "limit_tokens": b.limit_tokens,
        "limit_requests": b.limit_requests,
        "hard_stop": bool(b.hard_stop),
    }


def terms_hash(terms: dict[str, Any]) -> str:
    return content_hash(terms)


def _label(b: Budget) -> str:
    return f"{b.scope_type}:{b.scope_id}:{b.period}"


def _acquire_admission_lock(session: Session) -> None:
    """Take the SQLite write lock before reading budget state (see CostAdmissionLock)."""
    stmt = sqlite_insert(CostAdmissionLock).values(name="budget", seq=1, updated_at=utcnow())
    stmt = stmt.on_conflict_do_update(
        index_elements=["name"], set_={"seq": CostAdmissionLock.seq + 1, "updated_at": utcnow()}
    )
    try:
        session.execute(stmt)
    except OperationalError as exc:
        raise Blocked(
            "budget state is locked by another admission; refused (fail closed)",
            code="BUDGET_LOCK_UNAVAILABLE",
            details={"reasons": ["cost admission lock unavailable"]},
        ) from exc


# --------------------------------------------------------------------------- default budget


def _default_terms() -> dict[str, Any]:
    return {
        "scope_type": "GLOBAL",
        "scope_id": "*",
        "period": "MONTH",
        "limit_usd": str(money(0)),
        "limit_tokens": None,
        "limit_requests": None,
        "hard_stop": True,
    }


def _active_global_month(session: Session) -> Budget | None:
    return session.scalar(
        select(Budget)
        .where(Budget.scope_type == "GLOBAL", Budget.scope_id == "*", Budget.period == "MONTH",
               Budget.status == "ACTIVE")
        .order_by(Budget.created_at.desc())
    )


def ensure_default_budget(session: Session) -> Budget:
    """Materialise the $0 hard-stop GLOBAL default when no active GLOBAL monthly budget exists.
    A missing/broken chain therefore falls back to *more* restrictive, never to unlimited."""
    existing = _active_global_month(session)
    if existing is not None:
        return existing
    b = Budget(
        scope_type="GLOBAL", scope_id="*", period="MONTH", limit_usd=money(0), limit_tokens=None,
        limit_requests=None, hard_stop=True, decision_id=None, notes=DEFAULT_NOTES, status="ACTIVE",
        terms_hash=terms_hash(_default_terms()), created_by="system", is_default=True,
    )
    session.add(b)
    session.flush()
    events.emit(session, source="cost", kind="cost.budget.default_created", entity_type="budget",
                entity_id=b.id, payload={"limit_usd": "0", "hard_stop": True})
    return b


# --------------------------------------------------------------------------- integrity


def verify_budget_authority(session: Session, b: Budget) -> list[str]:
    """Reasons this budget row cannot be trusted (empty list = trusted)."""
    reasons: list[str] = []
    try:
        lim = money(b.limit_usd)
    except ValueError:
        return ["limit_usd is not a finite number"]
    if lim is not None and lim < ZERO:
        reasons.append("negative limit_usd")
    for name in ("limit_tokens", "limit_requests"):
        v = getattr(b, name)
        if v is not None and v < 0:
            reasons.append(f"negative {name}")
    if b.scope_type not in SCOPE_TYPES or b.period not in PERIODS:
        reasons.append("unknown scope_type/period")
        return reasons
    expected = terms_hash(budget_terms(b))
    if b.terms_hash != expected:
        reasons.append("budget terms changed outside the decision flow (terms hash mismatch)")
    if b.is_default:
        if budget_terms(b) != _default_terms():
            reasons.append("default budget row differs from the $0 hard-stop default")
        return reasons
    if not b.decision_id:
        reasons.append("no owner BUDGET decision recorded for this budget")
        return reasons
    d = session.get(Decision, b.decision_id)
    if d is None:
        reasons.append("bound BUDGET decision is missing")
    elif d.kind != "BUDGET":
        reasons.append(f"bound decision has kind {d.kind}, not BUDGET")
    elif d.actor_kind != "HUMAN":
        reasons.append("bound decision was not made by a human")
    elif d.status != "ACTIVE":
        reasons.append(f"bound BUDGET decision is {d.status}")
    elif d.target_hash != expected:
        reasons.append("BUDGET decision is bound to different budget terms")
    elif is_expired(d.expires_at):
        reasons.append("BUDGET decision expired")
    return reasons


@dataclass
class _Usage:
    usd: Decimal = ZERO
    tokens: int = 0
    requests: int = 0
    untrusted: list[str] = field(default_factory=list)


def _route_cache(session: Session) -> dict[str, ModelRoute]:
    return {r.route_key: r for r in session.scalars(select(ModelRoute))}


def _scope_filter_ledger(b: Budget) -> Any:
    if b.scope_type == "PROJECT":
        return UsageLedgerEntry.project_id == b.scope_id
    if b.scope_type == "ROUTE":
        return UsageLedgerEntry.route_key == b.scope_id
    if b.scope_type == "RUN":
        return UsageLedgerEntry.run_id == b.scope_id
    return None


def _scope_filter_res(b: Budget) -> Any:
    if b.scope_type == "PROJECT":
        return BudgetReservation.project_id == b.scope_id
    if b.scope_type == "ROUTE":
        return BudgetReservation.route_key == b.scope_id
    if b.scope_type == "RUN":
        return BudgetReservation.run_id == b.scope_id
    return None


def committed_usage(session: Session, b: Budget, *, routes: dict[str, ModelRoute] | None = None,
                    now: datetime | None = None) -> _Usage:
    """Spend already charged in the budget period (LIVE worker API ledger)."""
    out = _Usage()
    start = period_start(b.period, now)
    q = select(UsageLedgerEntry).where(UsageLedgerEntry.category.in_(BUDGETED_CATEGORIES),
                                       UsageLedgerEntry.mode == LIVE)
    if start is not None:
        q = q.where(UsageLedgerEntry.at >= start)
    flt = _scope_filter_ledger(b)
    if flt is not None:
        q = q.where(flt)
    routes = routes if routes is not None else _route_cache(session)
    for e in session.scalars(q):
        charged = e.charged_usd if e.charged_usd is not None else e.cost_usd
        if charged is None:
            route = routes.get(e.route_key or "")
            if route is not None and (route.is_free == "YES" or subs.subscription_terms(route)[0] is not None):
                # free, or within a subscription's plan fee (an unreported marginal cost is not made-up dollars)
                charged = ZERO
            elif b.limit_usd is not None:
                out.untrusted.append(f"ledger entry {e.id} has UNKNOWN cost on a non-free route")
                charged = ZERO
            else:
                charged = ZERO
        try:
            out.usd += money(charged) or ZERO
        except ValueError:
            out.untrusted.append(f"ledger entry {e.id} has a non-finite cost")
        if e.input_tokens is not None or e.output_tokens is not None:
            out.tokens += int(e.input_tokens or 0) + int(e.output_tokens or 0)
        elif isinstance((e.details or {}).get("charged_tokens"), int):
            out.tokens += int(e.details["charged_tokens"])
        elif b.limit_tokens is not None:
            out.untrusted.append(f"ledger entry {e.id} has UNKNOWN token usage")
        out.requests += int(e.requests or 0)
    return out


def held_usage(session: Session, b: Budget) -> _Usage:
    """In-flight holds (RESERVED). Holds never expire silently: a crashed run keeps its hold until
    released/committed by recovery (§14 crash recovery reads reservations before retry)."""
    out = _Usage()
    q = select(BudgetReservation).where(BudgetReservation.status == "RESERVED")
    flt = _scope_filter_res(b)
    if flt is not None:
        q = q.where(flt)
    for r in session.scalars(q):
        try:
            reserved = money(r.reserved_usd) or ZERO
            # A partially committed run-level hold (``record_usage(partial=True)``) keeps only what is
            # still unused: the committed part is already in the ledger (no double count).
            out.usd += max(ZERO, reserved - (money(r.committed_usd) or ZERO))
        except ValueError:
            out.untrusted.append(f"reservation {r.id} has a non-finite amount")
        out.tokens += max(0, int(r.reserved_tokens or 0) - int(r.committed_tokens or 0))
        out.requests += max(0, int(r.reserved_requests or 0) - int(r.committed_requests or 0))
        if (money(r.reserved_usd) or ZERO) < ZERO or (r.reserved_tokens or 0) < 0 or (r.reserved_requests or 0) < 0:
            out.untrusted.append(f"reservation {r.id} has a negative amount")
    return out


def applicable_budgets(session: Session, *, project_id: str | None, route_key: str | None,
                       run_id: str | None) -> list[Budget]:
    conds = [and_(Budget.scope_type == "GLOBAL", Budget.scope_id == "*")]
    if project_id:
        conds.append(and_(Budget.scope_type == "PROJECT", Budget.scope_id == project_id))
    if route_key:
        conds.append(and_(Budget.scope_type == "ROUTE", Budget.scope_id == route_key))
    if run_id:
        conds.append(and_(Budget.scope_type == "RUN", Budget.scope_id == run_id))
    q = select(Budget).where(Budget.status == "ACTIVE", or_(*conds)).order_by(Budget.scope_type, Budget.period)
    return list(session.scalars(q))


def _pricing_check(session: Session, route_key: str | None, est_usd: Decimal, est_tokens: int,
                   est_requests: int) -> tuple[str, list[str]]:
    if not route_key:
        if est_usd <= ZERO and (est_tokens > 0 or est_requests > 0):
            # "$0" without a registered route cannot be proven to be a free route; under the $0
            # hard-stop default that would be a way around the budget (fail closed, §42/§50).
            return "UNKNOWN", ["request has no route_key: a $0 estimate cannot be verified as a free route"]
        return "CALLER_ESTIMATE", []
    route = session.scalar(select(ModelRoute).where(ModelRoute.route_key == route_key))
    if route is None:
        return "UNKNOWN", [f"route {route_key!r} is not registered; pricing UNKNOWN"]
    if subs.is_subscription_priced(route):
        # ADR-0080 amendment 2026-10-26: a flat plan fee (the activation's BUDGET decision) - the marginal estimate is
        # $0 within the plan; the allowance windows are checked separately. Incomplete or contradictory terms (e.g. a
        # "free" subscription): an unpriced paid route, refused.
        terms, problem = subs.subscription_terms(route)
        if terms is None:
            return "UNKNOWN", [problem or f"subscription route {route_key!r} terms cannot be trusted"]
        return ("SUBSCRIPTION" if est_usd == ZERO else "CALLER_ESTIMATE"), []
    if route.is_free == "YES":
        return ("FREE_ROUTE" if est_usd == ZERO else "CALLER_ESTIMATE"), []
    if route.is_free != "NO":
        return "UNKNOWN", [f"route {route_key!r} free/paid status is {route.is_free}; pricing cannot be trusted"]
    if not route.pricing:
        return "UNKNOWN", [f"paid route {route_key!r} has no pricing record; cost UNKNOWN"]
    if route.pricing_checked_at is None:
        return "UNKNOWN", [f"paid route {route_key!r} pricing was never checked"]
    if est_usd <= ZERO and (est_tokens > 0 or est_requests > 0):
        return "UNKNOWN", [f"paid route {route_key!r} request estimated at $0; estimate not trustworthy"]
    return "ROUTE_PRICING", []


def _quota_block(session: Session, route_key: str | None) -> list[str]:
    """§50 "Free quota habis / 429": an EXHAUSTED provider/route quota stops admission (the caller
    backs off/queues or uses an already-approved free fallback) — never a paid fallback."""
    if not route_key:
        return []
    route = session.scalar(select(ModelRoute).where(ModelRoute.route_key == route_key))
    if route is None:
        return []
    now = utcnow()
    out = []
    for q in session.scalars(select(QuotaState).where(QuotaState.provider == route.provider,
                                                      QuotaState.scope.in_(["*", route_key]),
                                                      QuotaState.state == "EXHAUSTED")):
        resets = ensure_aware(q.resets_at)
        if resets is None or resets > now:
            out.append(f"quota {q.provider}:{q.scope} EXHAUSTED (source {q.source}"
                       f"{', resets ' + resets.isoformat() if resets else ''})")
    return out


def _validate_amounts(est_usd: Any, est_tokens: Any, est_requests: Any) -> tuple[Decimal, int, int]:
    try:
        usd = money(est_usd)
    except ValueError as exc:
        raise ValidationFailed(f"invalid est_usd: {exc}") from exc
    if usd is None or usd < ZERO:
        raise ValidationFailed("est_usd must be a finite number >= 0")
    for name, v in (("est_tokens", est_tokens), ("est_requests", est_requests)):
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ValidationFailed(f"{name} must be an integer >= 0")
    return usd, int(est_tokens), int(est_requests)


_DOMAINS = frozenset(d.value for d in Domain)


def _check_domain(session: Session, *, domain: str | None, project_id: str | None, run_id: str | None,
                  required: bool) -> str | None:
    """Domain-first (U-05): the domain is the caller's explicit choice and must match the project's
    (immutable, human-chosen) domain and the run's domain when those records exist. It is never
    taken from model output or rewritten.

    Returns the effective domain: the given one, or — only when ``required`` is False and none was
    given — the recorded domain of the project/run the usage belongs to, so an Office entry or its
    dashboard event is never domain-less (domain-less events reach every session, §35/§50)."""
    project = session.get(Project, project_id) if project_id else None
    run = session.get(WorkerRun, run_id) if run_id else None
    recorded = {d for d in ((project.domain if project else None), (run.domain if run else None)) if d}
    if len(recorded) > 1:
        raise ValidationFailed("the project and the run belong to different domains", code="DOMAIN_MISMATCH")
    if domain is None:
        if required:
            raise ValidationFailed("domain (PERSONAL/OFFICE) is required", code="DOMAIN_REQUIRED")
        return next(iter(recorded), None)
    if domain not in _DOMAINS:
        raise ValidationFailed(f"domain must be one of {sorted(_DOMAINS)}")
    if project is not None and project.domain != domain:
        raise ValidationFailed("domain does not match the project's domain", code="DOMAIN_MISMATCH")
    if run is not None and run.domain and run.domain != domain:
        raise ValidationFailed("domain does not match the run's domain", code="DOMAIN_MISMATCH")
    return domain


# --------------------------------------------------------------------------- reserve / release


def reserve(session: Session, req: Any) -> BudgetReservation:
    """Atomically admit one request against every applicable budget or raise ``Blocked``.

    On ``Blocked`` the caller must roll back its transaction (the admission lock is held until
    commit/rollback). Holds stay until ``release`` or ``record_usage`` commits them."""
    if not getattr(req, "run_id", None):
        raise ValidationFailed("run_id is required for a reservation")
    est_usd, est_tokens, est_requests = _validate_amounts(req.est_usd, req.est_tokens, req.est_requests)
    _check_domain(session, domain=getattr(req, "domain", None), project_id=req.project_id, run_id=req.run_id,
                  required=True)

    _acquire_admission_lock(session)
    default = ensure_default_budget(session)
    budgets = applicable_budgets(session, project_id=req.project_id, route_key=req.route_key, run_id=req.run_id)
    if not budgets:  # pragma: no cover - default guarantees one; keep fail closed anyway
        raise Blocked("no budget applies; refused", code="BUDGET_STATE_UNTRUSTED",
                      details={"reasons": ["no applicable budget"]})

    untrusted: list[str] = []
    exceeded: list[str] = []
    warnings: list[str] = []
    basis, pricing_reasons = _pricing_check(session, req.route_key, est_usd, est_tokens, est_requests)
    untrusted += pricing_reasons

    seen: dict[tuple[str, str, str], int] = defaultdict(int)
    for b in budgets:
        seen[(b.scope_type, b.scope_id, b.period)] += 1
    for key, n in seen.items():
        if n > 1:
            untrusted.append(f"{n} ACTIVE budgets for {':'.join(key)} (ambiguous)")

    routes = _route_cache(session)
    now = utcnow()
    checked: list[dict[str, Any]] = []
    for b in budgets:
        untrusted += [f"{_label(b)}: {r}" for r in verify_budget_authority(session, b)]
        spent = committed_usage(session, b, routes=routes, now=now)
        held = held_usage(session, b)
        untrusted += [f"{_label(b)}: {r}" for r in spent.untrusted + held.untrusted]
        sink = exceeded if b.hard_stop else warnings
        lim_usd = money(b.limit_usd)
        if lim_usd is not None and spent.usd + held.usd + est_usd > lim_usd:
            sink.append(f"{_label(b)}: usd {spent.usd}+{held.usd}+{est_usd} > {lim_usd}")
        if b.limit_tokens is not None and spent.tokens + held.tokens + est_tokens > b.limit_tokens:
            sink.append(f"{_label(b)}: tokens {spent.tokens}+{held.tokens}+{est_tokens} > {b.limit_tokens}")
        if b.limit_requests is not None and spent.requests + held.requests + est_requests > b.limit_requests:
            sink.append(f"{_label(b)}: requests {spent.requests}+{held.requests}+{est_requests} > {b.limit_requests}")
        checked.append({"budget_id": b.id, "scope": _label(b), "limit_usd": None if lim_usd is None else str(lim_usd)})

    # ADR-0080 amendment 2026-10-26: a subscription route's state (provider limits as counted by this AEOS instance,
    # not a budget). Blocking: a call billed beyond the plan (until the owner re-activates the route), the 5h window
    # used up, or a call in it whose allowance cannot be counted (at most 5 hours). The week/month windows are advisory.
    # Incomplete terms are already refused by the pricing check above.
    route = routes.get(req.route_key or "")
    allowance = subs.allowance_status(session, route, now=now) if route is not None else None
    block = subs.admission_block(allowance)
    if untrusted:
        raise Blocked("budget state cannot be trusted; admission refused (fail closed)",
                      code="BUDGET_STATE_UNTRUSTED", details={"reasons": untrusted, "budgets": checked})
    quota = _quota_block(session, req.route_key)
    if quota:
        raise Blocked("provider quota exhausted; wait/backoff or use an approved free fallback (no paid fallback)",
                      code="QUOTA_EXHAUSTED", details={"reasons": quota})
    if block and allowance is not None:
        # never another route, never paid usage beyond the plan; the reason carries no allowance figures
        raise Blocked(subs.BLOCK_MESSAGES[block[0]], code=block[0],
                      details={"reasons": [block[1]], "state": allowance["state"]})
    if exceeded:
        raise Blocked("budget hard stop reached; admission refused (no paid fallback, no auto-topup)",
                      code="BUDGET_EXCEEDED", details={"reasons": exceeded, "budgets": checked})

    global_b = next((b for b in budgets if b.scope_type == "GLOBAL"), default)
    res = BudgetReservation(
        budget_id=global_b.id, run_id=req.run_id, project_id=req.project_id, domain=req.domain,
        route_key=req.route_key, budget_ids=[b.id for b in budgets], reserved_usd=est_usd,
        reserved_tokens=est_tokens, reserved_requests=est_requests, status="RESERVED",
        warnings=warnings, pricing_basis=basis,
    )
    session.add(res)
    session.flush()
    events.emit(
        session, source="cost", kind="cost.reservation.created", entity_type="budget_reservation",
        entity_id=res.id, project_id=req.project_id, domain=req.domain, run_id=req.run_id,
        payload={"reserved_usd": str(est_usd), "reserved_tokens": est_tokens, "reserved_requests": est_requests,
                 "route_key": req.route_key, "pricing_basis": basis, "warnings": warnings},
    )
    return res


def _partially_committed(res: BudgetReservation) -> bool:
    return any(v is not None for v in (res.committed_usd, res.committed_tokens, res.committed_requests))


def release(session: Session, reservation_id: str) -> None:
    """Release an unused hold. Idempotent: COMMITTED/RELEASED reservations are left unchanged.

    A run-level hold that already received partial commits (``record_usage(partial=True)``, one per
    broker call) is *settled*: it becomes COMMITTED with the committed amounts and the unused rest is
    freed (integration amendment, ADR-0080)."""
    res = session.get(BudgetReservation, reservation_id)
    if res is None:
        raise NotFound("reservation not found")
    if res.status != "RESERVED":
        return
    now = utcnow()
    if _partially_committed(res):
        res.status = "COMMITTED"
        res.committed_at = res.committed_at or now
        res.released_at = now
        kind = "cost.reservation.settled"
    else:
        res.status = "RELEASED"
        res.released_at = now
        kind = "cost.reservation.released"
    session.flush()
    events.emit(session, source="cost", kind=kind, entity_type="budget_reservation",
                entity_id=res.id, project_id=res.project_id, domain=res.domain, run_id=res.run_id,
                payload={"committed_usd": str(res.committed_usd) if res.committed_usd is not None else None,
                         "committed_tokens": res.committed_tokens, "committed_requests": res.committed_requests})


# --------------------------------------------------------------------------- usage ledger


def record_usage(
    session: Session,
    *,
    provider: str,
    category: str,
    source: str,
    project_id: str | None = None,
    domain: str | None = None,
    run_id: str | None = None,
    route_key: str | None = None,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    requests: int = 1,
    cost_usd: float | None = None,
    is_estimate: bool = False,
    reservation_id: str | None = None,
    mode: str = "LIVE",
    task_id: str | None = None,
    quantity: float | None = None,
    unit: str | None = None,
    details: dict[str, Any] | None = None,
    partial: bool = False,
    **extra: Any,
) -> UsageLedgerEntry:
    """Write one ledger entry (actual vs estimate vs UNKNOWN) and commit its reservation, if any.

    ``partial=True`` (one call of a multi-call run, e.g. each broker request) adds the entry to the
    run-level hold instead of closing it: the reservation stays RESERVED, its committed amounts grow,
    the remaining hold keeps counting against the budgets, and ``release`` settles it when the run
    ends (integration amendment, ADR-0080). An UNKNOWN cost charges the remaining reserved estimate.

    A SUBSCRIPTION route (ADR-0080 amendment 2026-10-26) is never charged its reservation: ``cost_usd`` is the
    provider's marginal charge (0 within the plan; UNKNOWN stays UNKNOWN and is charged $0), and the allowance the call
    consumed goes to ``details.allowance_usd`` with its basis: ``details.reported_allowance_usd`` from the provider
    (REPORTED), else estimated from the tokens x the route's allowance rates, else from an upper bound of the call's
    tokens (``details.charged_tokens`` from the caller - the broker's clamped ``max_tokens`` plus the prompt size - else
    the tokens charged against the hold) x the highest rate (ESTIMATED); UNKNOWN only when nothing bounds the call.
    Paid usage beyond the plan - a marginal charge above $0, a ``details.billing_source`` that is not one of the plan's
    sources (e.g. ``balance``), or a cost the provider stated but AEOS could not read (``details.cost_reported_unusable``)
    - is charged to the budgets when reported, flagged ``beyond_plan_usd`` (the amount, else UNKNOWN) and emitted as
    ``cost.subscription.beyond_plan``; the route is then refused until the owner re-activates it."""
    extra.pop("impl", None)  # the facade forwards **locals(), which includes its import alias
    if extra:
        raise TypeError(f"unexpected arguments: {sorted(extra)}")
    category = (category or "").upper()
    if category not in USAGE_CATEGORIES:
        raise ValidationFailed(f"category must be one of {USAGE_CATEGORIES}")
    if mode not in {m.value for m in DataMode}:
        raise ValidationFailed("unknown data mode")
    for name, v in (("input_tokens", input_tokens), ("output_tokens", output_tokens)):
        if v is not None and (not isinstance(v, int) or v < 0):
            raise ValidationFailed(f"{name} must be an integer >= 0")
    if not isinstance(requests, int) or requests < 0:
        raise ValidationFailed("requests must be an integer >= 0")
    try:
        cost = money(cost_usd)
        qty = money(quantity) if quantity is not None else None
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    if cost is not None and cost < ZERO:
        raise ValidationFailed("cost_usd must be >= 0")
    if qty is not None:
        if qty < ZERO:
            raise ValidationFailed("quantity must be >= 0")
        if not unit or not str(unit).strip() or len(str(unit)) > 32:
            raise ValidationFailed("a quantity needs its unit (e.g. minutes, seconds, bytes), max 32 chars")
        unit = str(unit).strip()
    if reservation_id and mode != LIVE:
        # A MOCK/DEMO entry never counts against budgets, so letting it commit a LIVE hold would
        # make real spend vanish from the budget (§50 "Mock ... tidak menambah usage actual").
        raise ValidationFailed("only LIVE usage can commit a budget reservation; release it instead",
                               code="NON_LIVE_RESERVATION_COMMIT")
    info: dict[str, Any] = dict(redact(details or {}))

    if category == "NATIVE_CLAUDE":
        # §42: subscription usage is never converted into dollars or a percentage of Max quota.
        if cost is not None:
            info["dollar_value_discarded"] = True
        cost = None
        is_estimate = False
        info["billing"] = "SUBSCRIPTION"
        info["note"] = NATIVE_NOTE

    charged: Decimal | None = cost
    res: BudgetReservation | None = None
    if reservation_id:
        if partial:
            # Several broker calls of one run commit into the same hold: serialise the read-modify-write
            # with the admission lock (SQLite write lock until the caller's commit) so no update is lost.
            _acquire_admission_lock(session)
        res = session.get(BudgetReservation, reservation_id, populate_existing=True)
        if res is None:
            raise NotFound("reservation not found")
        if res.status != "RESERVED":
            raise Conflict(f"reservation is already {res.status}")
        if run_id and res.run_id != run_id:
            raise Conflict("reservation belongs to a different run")
        for name, given, held in (("project", project_id, res.project_id), ("domain", domain, res.domain),
                                  ("route", route_key, res.route_key)):
            if given and held and given != held:
                raise Conflict(f"reservation belongs to a different {name}")
        if category not in BUDGETED_CATEGORIES:
            raise ValidationFailed("only WORKER_API usage commits a budget reservation")
        project_id = project_id or res.project_id
        domain = domain or res.domain
        route_key = route_key or res.route_key
        run_id = run_id or res.run_id
    domain = _check_domain(session, domain=domain, project_id=project_id, run_id=run_id, required=False)
    subscription_priced, sub_terms, reported_allowance = _subscription_usage(session, category, route_key, info)
    # the caller's upper bound of this call's tokens when the upstream reported none (facade contract)
    caller_bound = subs.token_count(info.get("charged_tokens"))
    if caller_bound is None:
        info.pop("charged_tokens", None)
    if res is not None and partial:
        reserved_usd = money(res.reserved_usd) or ZERO
        already_usd = money(res.committed_usd) or ZERO
        if cost is None and sub_terms is not None:
            charged = ZERO  # within the plan fee: never the reservation as made-up dollars
            info["marginal_cost_unreported"] = True
        elif cost is None:
            charged = max(ZERO, reserved_usd - already_usd)
            info["cost_unknown_charged_reservation"] = True
        elif already_usd + cost > reserved_usd:
            info["overrun_usd"] = str(already_usd + cost - reserved_usd)
        tokens_known = input_tokens is not None or output_tokens is not None
        if tokens_known:
            call_tokens = int(input_tokens or 0) + int(output_tokens or 0)
        else:
            call_tokens = max(0, int(res.reserved_tokens or 0) - int(res.committed_tokens or 0))
            info["charged_tokens"] = call_tokens
        res.committed_usd = already_usd + (charged or ZERO)
        res.committed_tokens = int(res.committed_tokens or 0) + call_tokens
        res.committed_requests = int(res.committed_requests or 0) + requests
        res.committed_at = utcnow()
        info["partial_commit"] = True
    elif res is not None:
        reserved_usd = money(res.reserved_usd) or ZERO
        if cost is None and sub_terms is not None:
            charged = ZERO
            info["marginal_cost_unreported"] = True
        elif cost is None:
            charged = reserved_usd
            info["cost_unknown_charged_reservation"] = True
        elif cost > reserved_usd:
            info["overrun_usd"] = str(cost - reserved_usd)
        tokens_known = input_tokens is not None or output_tokens is not None
        committed_tokens = (int(input_tokens or 0) + int(output_tokens or 0)) if tokens_known else int(res.reserved_tokens or 0)
        if not tokens_known:
            info["charged_tokens"] = int(res.reserved_tokens or 0)
        res.committed_usd = charged
        res.committed_tokens = committed_tokens
        res.committed_requests = requests
        res.status = "COMMITTED"
        res.committed_at = utcnow()
    elif cost is None and sub_terms is not None:
        charged = ZERO
        info["marginal_cost_unreported"] = True
    if sub_terms is not None:
        # the caller's per-call bound (clamped max_tokens + prompt size) is tighter than the hold's remainder
        bound = caller_bound if caller_bound is not None else subs.token_count(info.get("charged_tokens"))
        if caller_bound is not None and info.get("charged_tokens") != caller_bound:
            info["allowance_token_bound"] = caller_bound
        allowance, basis, how = subs.allowance_of(sub_terms, reported=reported_allowance, input_tokens=input_tokens,
                                                  output_tokens=output_tokens, charged_tokens=bound)
        info.update({"plan": sub_terms["plan"], "allowance_usd": None if allowance is None else str(allowance),
                     "allowance_basis": basis})
        if how:
            info["allowance_estimate_from"] = how
    if subscription_priced:
        # provider-side paid fallback (e.g. Extra Usage ON): a charge, a non-plan billing source or an unreadable cost
        beyond = subs.beyond_plan_amount(cost=cost, billing_source=info.get("billing_source"),
                                         cost_unreadable=bool(info.get("cost_reported_unusable")
                                                              or info.get("billing_source_unreadable")))
        if beyond is not None:
            info["beyond_plan_usd"] = beyond

    entry = UsageLedgerEntry(
        project_id=project_id, domain=domain, run_id=run_id, task_id=task_id, route_key=route_key,
        provider=redact_text(provider)[:64], model=redact_text(model)[:160] if model else None, category=category,
        input_tokens=input_tokens, output_tokens=output_tokens, requests=requests, cost_usd=cost,
        is_estimate=bool(is_estimate) and cost is not None, charged_usd=charged, reservation_id=reservation_id,
        quantity=qty, unit=unit, source=redact_text(source)[:64], details=info, mode=mode,
    )
    session.add(entry)
    session.flush()
    events.emit(
        session, source="cost", kind="cost.usage.recorded", entity_type="usage_ledger", entity_id=entry.id,
        project_id=project_id, domain=domain, run_id=run_id, mode=mode,
        payload={"category": category, "cost": "UNKNOWN" if cost is None else str(cost),
                 "is_estimate": entry.is_estimate, "reservation_id": reservation_id,
                 **({"billing": subs.SUBSCRIPTION, "allowance_usd": info.get("allowance_usd"),
                     "allowance_basis": info.get("allowance_basis")} if sub_terms is not None else {})},
    )
    if subscription_priced and "beyond_plan_usd" in info:
        events.emit(session, source="cost", kind="cost.subscription.beyond_plan", entity_type="usage_ledger",
                    entity_id=entry.id, project_id=project_id, domain=domain, run_id=run_id, mode=mode,
                    payload={"route_key": route_key, "charged_usd": info["beyond_plan_usd"],
                             "billing_source": info.get("billing_source"),
                             "note": "paid usage beyond the plan on a subscription route (provider-side paid "
                                     "fallback); the route is refused for new admissions, probes and broker requests "
                                     "until the owner re-activates it"})
    if res is not None and "overrun_usd" in info:
        events.emit(session, source="cost", kind="cost.budget.overrun", entity_type="budget_reservation",
                    entity_id=res.id, project_id=project_id, domain=domain, run_id=run_id,
                    payload={"overrun_usd": info["overrun_usd"]})
    return entry


def _subscription_usage(session: Session, category: str, route_key: str | None,
                        info: dict[str, Any]) -> tuple[bool, dict[str, Any] | None, Any]:
    """(the route is subscription-priced, its trusted terms or None, the provider-reported allowance) of a model call;
    marks the entry's billing.

    The provider figure arrives as ``details.reported_allowance_usd``; it is kept as such on any other route."""
    reported = info.pop("reported_allowance_usd", None)
    route = (session.scalar(select(ModelRoute).where(ModelRoute.route_key == route_key))
             if category in BUDGETED_CATEGORIES and route_key else None)
    if not subs.is_subscription_priced(route):
        if reported is not None:
            info["reported_allowance_usd"] = reported
        return False, None, None
    terms, problem = subs.subscription_terms(route)
    info["billing"] = subs.SUBSCRIPTION
    if problem:
        info["subscription_terms_problem"] = problem  # an unpriced paid route: the paid-route rules apply
        if reported is not None:
            info["reported_allowance_usd"] = reported
    return True, terms, reported


# --------------------------------------------------------------------------- budgets (owner decisions)


def _validate_limits(limit_usd: Any, limit_tokens: Any, limit_requests: Any) -> tuple[Decimal | None, int | None, int | None]:
    try:
        usd = money(limit_usd)
    except ValueError as exc:
        raise ValidationFailed(f"invalid limit_usd: {exc}") from exc
    if usd is not None and usd < ZERO:
        raise ValidationFailed("limit_usd must be >= 0")
    for name, v in (("limit_tokens", limit_tokens), ("limit_requests", limit_requests)):
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
            raise ValidationFailed(f"{name} must be an integer >= 0")
    return usd, limit_tokens, limit_requests


def set_budget(
    session: Session,
    *,
    actor_user_id: str,
    scope_type: str,
    scope_id: str,
    period: str,
    limit_usd: Any,
    limit_tokens: int | None = None,
    limit_requests: int | None = None,
    hard_stop: bool = True,
    rationale: str,
    project_id: str | None = None,
    domain: str | None = None,
) -> Budget:
    """Create/raise/lower a budget — only together with a recorded owner ``BUDGET`` decision.
    Fails closed with ``Blocked`` when the decision registry is unavailable."""
    scope_type = (scope_type or "").upper()
    period = (period or "").upper()
    if scope_type not in SCOPE_TYPES:
        raise ValidationFailed(f"scope_type must be one of {SCOPE_TYPES}")
    if period not in PERIODS:
        raise ValidationFailed(f"period must be one of {PERIODS}")
    scope_id = "*" if scope_type == "GLOBAL" else (scope_id or "").strip()
    if not scope_id or (scope_type != "GLOBAL" and scope_id == "*"):
        raise ValidationFailed("scope_id is required for PROJECT/ROUTE/RUN budgets")
    usd, tokens, reqs = _validate_limits(limit_usd, limit_tokens, limit_requests)
    if usd is None and tokens is None and reqs is None:
        raise ValidationFailed("a budget needs at least one limit (unlimited budgets are not allowed)")
    if scope_type == "GLOBAL" and usd is None:
        raise ValidationFailed("a GLOBAL budget must state limit_usd")
    if not (rationale or "").strip():
        raise ValidationFailed("rationale is required for a budget decision")

    _acquire_admission_lock(session)
    ensure_default_budget(session)
    previous = session.scalar(
        select(Budget).where(Budget.scope_type == scope_type, Budget.scope_id == scope_id,
                             Budget.period == period, Budget.status == "ACTIVE")
        .order_by(Budget.created_at.desc())
    )
    candidate = Budget(scope_type=scope_type, scope_id=scope_id, period=period, limit_usd=usd,
                       limit_tokens=tokens, limit_requests=reqs, hard_stop=bool(hard_stop))
    terms = budget_terms(candidate)
    th = terms_hash(terms)
    try:
        from aeos.policy import facade as policy

        decision = policy.record_decision(
            session, kind="BUDGET", actor_user_id=actor_user_id, target_type="budget",
            target_id=_label(candidate), target_hash=th, allowed_operations=["budget.set"],
            auth_scope={"scope_type": scope_type, "scope_id": scope_id, "period": period},
            project_id=project_id, domain=domain, rationale=redact_text(rationale),
            details={"terms": terms, "previous_budget_id": previous.id if previous else None},
        )
    except (ImportError, ModuleNotFoundError) as exc:
        raise Blocked("decision registry (aeos.policy) unavailable; budget unchanged (fail closed)",
                      code="DECISION_REGISTRY_UNAVAILABLE",
                      details={"reasons": ["policy decision registry not available"]}) from exc
    if decision is None or decision.kind != "BUDGET" or decision.target_hash != th:
        raise Blocked("BUDGET decision was not recorded with the exact budget terms; budget unchanged",
                      code="DECISION_NOT_BOUND", details={"reasons": ["decision/terms binding mismatch"]})

    candidate.decision_id = decision.id
    candidate.terms_hash = th
    candidate.created_by = actor_user_id
    candidate.status = "ACTIVE"
    candidate.domain = domain
    candidate.notes = redact_text(rationale)[:2000]
    session.add(candidate)
    session.flush()
    if previous is not None:
        previous.status = "SUPERSEDED"
        previous.superseded_by = candidate.id
    audit.record(
        session, actor_type="user", actor_id=actor_user_id, action="cost.budget.set", target_type="budget",
        target_id=candidate.id, project_id=project_id, domain=domain, reason=rationale,
        details={"terms": terms, "decision_id": decision.id, "previous_budget_id": previous.id if previous else None},
    )
    events.emit(session, source="cost", kind="cost.budget.set", entity_type="budget", entity_id=candidate.id,
                project_id=project_id, domain=domain,
                payload={"scope": _label(candidate), "limit_usd": terms["limit_usd"], "decision_id": decision.id})
    session.flush()
    return candidate


def _decision_view(session: Session, decision_id: str | None) -> dict[str, Any] | None:
    if not decision_id:
        return None
    d = session.get(Decision, decision_id)
    if d is None:
        return {"id": decision_id, "status": "MISSING"}
    return {"id": d.id, "kind": d.kind, "actor_user_id": d.actor_user_id, "status": d.status,
            "created_at": d.created_at.isoformat() if d.created_at else None, "rationale": d.rationale,
            "target_hash": d.target_hash}


def budget_view(session: Session, b: Budget, *, spend_visible: bool = True,
                routes: dict[str, ModelRoute] | None = None) -> dict[str, Any]:
    reasons = verify_budget_authority(session, b)
    spent = committed_usage(session, b, routes=routes)
    held = held_usage(session, b)
    reasons += spent.untrusted + held.untrusted
    lim = money(b.limit_usd)
    headroom = None if lim is None else lim - spent.usd - held.usd
    state = "UNTRUSTED" if reasons else ("EXHAUSTED" if headroom is not None and headroom <= ZERO else "OK")
    if lim is not None and lim == ZERO and not reasons:
        state = "HARD_STOP_ZERO"
    view: dict[str, Any] = {
        "id": b.id, "scope_type": b.scope_type, "scope_id": b.scope_id, "period": b.period,
        "period_start": (lambda s: s.isoformat() if s else None)(period_start(b.period)),
        "limit_usd": money_out(b.limit_usd), "limit_tokens": b.limit_tokens, "limit_requests": b.limit_requests,
        "hard_stop": b.hard_stop, "status": b.status, "is_default": b.is_default, "implicit": False,
        "state": state, "trust_reasons": reasons, "decision": _decision_view(session, b.decision_id),
        "notes": b.notes, "created_at": b.created_at.isoformat() if b.created_at else None,
        "superseded_by": b.superseded_by,
    }
    if spend_visible:
        view.update({
            "spent_usd": float(spent.usd), "held_usd": float(held.usd),
            "headroom_usd": None if headroom is None else float(headroom),
            "spent_tokens": spent.tokens, "held_tokens": held.tokens,
            "spent_requests": spent.requests, "held_requests": held.requests,
        })
    else:
        view.update({"spent_usd": None, "held_usd": None, "headroom_usd": None, "spend_hidden": True,
                     "spend_note": "Angka GLOBAL mencakup domain lain yang tidak dapat Anda lihat."})
    return view


def implicit_default_view(session: Session, *, spend_visible: bool) -> dict[str, Any]:
    """The $0 default before its row is materialised by the first admission (GET never writes)."""
    transient = Budget(scope_type="GLOBAL", scope_id="*", period="MONTH", limit_usd=money(0), hard_stop=True,
                       status="ACTIVE", is_default=True)
    spent = committed_usage(session, transient)
    held = held_usage(session, transient)
    start = period_start("MONTH")
    view: dict[str, Any] = {
        "id": None, "scope_type": "GLOBAL", "scope_id": "*", "period": "MONTH",
        "period_start": start.isoformat() if start else None, "limit_usd": 0.0, "limit_tokens": None,
        "limit_requests": None, "hard_stop": True, "status": "ACTIVE", "is_default": True, "implicit": True,
        "state": "HARD_STOP_ZERO", "trust_reasons": spent.untrusted + held.untrusted, "decision": None,
        "notes": DEFAULT_NOTES, "created_at": None, "superseded_by": None,
    }
    if spend_visible:
        view.update({"spent_usd": float(spent.usd), "held_usd": float(held.usd),
                     "headroom_usd": float(ZERO - spent.usd - held.usd)})
    else:
        view.update({"spent_usd": None, "held_usd": None, "headroom_usd": None, "spend_hidden": True,
                     "spend_note": "Angka GLOBAL mencakup domain lain yang tidak dapat Anda lihat."})
    return view


def list_budgets(session: Session, *, domain: str | None, allowed_project_ids: list[str] | None,
                 cross_domain_visible: bool, include_superseded: bool = False) -> list[dict[str, Any]]:
    q = select(Budget).order_by(Budget.scope_type, Budget.created_at.desc())
    if not include_superseded:
        q = q.where(Budget.status == "ACTIVE")
    routes = _route_cache(session)
    project_domains = {p.id: p.domain for p in session.scalars(select(Project))}
    run_domains = {r.id: r.domain for r in session.scalars(select(WorkerRun))}
    out: list[dict[str, Any]] = []
    for b in session.scalars(q):
        if b.scope_type == "PROJECT":
            pd = project_domains.get(b.scope_id)
            if pd is None or pd != domain:
                continue
            if allowed_project_ids is not None and b.scope_id not in allowed_project_ids:
                continue
            out.append(budget_view(session, b, routes=routes))
        elif b.scope_type == "RUN":
            if run_domains.get(b.scope_id) != domain:
                continue
            out.append(budget_view(session, b, routes=routes))
        else:  # GLOBAL / ROUTE span domains
            out.append(budget_view(session, b, spend_visible=cross_domain_visible, routes=routes))
    if _active_global_month(session) is None:
        out.insert(0, implicit_default_view(session, spend_visible=cross_domain_visible))
    return out


# --------------------------------------------------------------------------- quota state


def set_quota_state(
    session: Session, *, provider: str, state: str, source: str, scope: str = "*",
    details: dict[str, Any] | None = None, observed_at: datetime | None = None, resets_at: datetime | None = None,
) -> QuotaState:
    state = (state or "").upper()
    if state not in QUOTA_STATES:
        raise ValidationFailed(f"quota state must be one of {QUOTA_STATES}")
    if provider == NATIVE_PROVIDER and state in ("OK", "LOW") and source not in OFFICIAL_QUOTA_SOURCES:
        raise ValidationFailed("native Max remaining quota can only be OK/LOW from an official source; use UNKNOWN")
    row = session.scalar(select(QuotaState).where(QuotaState.provider == provider, QuotaState.scope == scope))
    if row is None:
        row = QuotaState(provider=provider, scope=scope)
        session.add(row)
    row.state = state
    row.source = source[:64]
    row.details = redact(details or {})
    row.observed_at = observed_at or utcnow()
    row.resets_at = resets_at
    session.flush()
    events.emit(session, source="cost", kind="cost.quota.updated", entity_type="quota_state", entity_id=row.id,
                payload={"provider": provider, "scope": scope, "state": state, "source": source})
    return row


def _quota_view(q: QuotaState) -> dict[str, Any]:
    return {
        "id": q.id, "provider": q.provider, "scope": q.scope, "state": q.state, "source": q.source,
        "observed_at": q.observed_at.isoformat() if q.observed_at else None,
        "resets_at": q.resets_at.isoformat() if q.resets_at else None, "details": q.details or {},
        "official": q.source in OFFICIAL_QUOTA_SOURCES, "implicit": False,
    }


def native_quota_default() -> dict[str, Any]:
    return {
        "id": None, "provider": NATIVE_PROVIDER, "scope": "*", "state": "UNKNOWN",
        "source": "no official remaining-quota source", "observed_at": None, "resets_at": None,
        "details": {"note": NATIVE_NOTE}, "official": False, "implicit": True,
    }


def list_quotas(session: Session) -> list[dict[str, Any]]:
    rows = [_quota_view(q) for q in session.scalars(select(QuotaState).order_by(QuotaState.provider, QuotaState.scope))]
    if not any(r["provider"] == NATIVE_PROVIDER for r in rows):
        rows.insert(0, native_quota_default())
    return rows


# --------------------------------------------------------------------------- summary


def _visible_ledger(session: Session, *, domain: str | None, allowed_project_ids: list[str] | None,
                    start: datetime | None) -> list[UsageLedgerEntry]:
    q = select(UsageLedgerEntry).order_by(UsageLedgerEntry.at.desc())
    if start is not None:
        q = q.where(UsageLedgerEntry.at >= start)
    q = q.where(or_(UsageLedgerEntry.domain == domain, UsageLedgerEntry.domain.is_(None)))
    rows = list(session.scalars(q))
    if allowed_project_ids is not None:
        rows = [e for e in rows if e.project_id is None or e.project_id in allowed_project_ids]
    # Defence in depth: a project-/run-scoped entry must belong to a project/run of the active domain,
    # even when the entry itself carries no domain (Office usage never reaches a Personal view).
    project_domains = {p.id: p.domain for p in session.scalars(select(Project))}
    run_ids = {e.run_id for e in rows if e.run_id}
    run_domains = ({r.id: r.domain for r in session.scalars(select(WorkerRun).where(WorkerRun.id.in_(run_ids)))}
                   if run_ids else {})
    out = []
    for e in rows:
        if e.project_id is not None and project_domains.get(e.project_id, e.domain) != domain:
            continue
        rd = run_domains.get(e.run_id or "")
        if rd and rd != domain:
            continue
        out.append(e)
    return out


def _bucket() -> dict[str, Any]:
    return {"actual_usd": ZERO, "estimate_usd": ZERO, "unknown_count": 0, "entries": 0, "input_tokens": 0,
            "output_tokens": 0, "requests": 0, "quantity": defaultdict(lambda: ZERO)}


def _add(b: dict[str, Any], e: UsageLedgerEntry) -> None:
    b["entries"] += 1
    b["input_tokens"] += int(e.input_tokens or 0)
    b["output_tokens"] += int(e.output_tokens or 0)
    b["requests"] += int(e.requests or 0)
    if e.quantity is not None and e.unit:
        b["quantity"][e.unit] += money(e.quantity) or ZERO
    if e.category == "NATIVE_CLAUDE":
        return
    if e.cost_usd is None:
        b["unknown_count"] += 1
    elif e.is_estimate:
        b["estimate_usd"] += money(e.cost_usd) or ZERO
    else:
        b["actual_usd"] += money(e.cost_usd) or ZERO


def _bucket_out(b: dict[str, Any]) -> dict[str, Any]:
    return {
        "actual_usd": float(b["actual_usd"]), "estimate_usd": float(b["estimate_usd"]),
        "unknown_count": b["unknown_count"], "entries": b["entries"], "input_tokens": b["input_tokens"],
        "output_tokens": b["output_tokens"], "requests": b["requests"],
        "quantity": {k: float(v) for k, v in b["quantity"].items()},
    }


def per_accepted_outcome(session: Session, *, domain: str | None, allowed_project_ids: list[str] | None,
                         start: datetime | None, entries: list[UsageLedgerEntry]) -> dict[str, Any]:
    """§43 Economics: worker API/compute/CI cost per accepted outcome (failed attempts included in
    the numerator — they are real cost), separate from the subscription quota."""
    q = select(AcceptanceRecord, Project).join(Project, Project.id == AcceptanceRecord.project_id).where(
        AcceptanceRecord.acceptance_status == "ACCEPTED", Project.domain == domain)
    if start is not None:
        q = q.where(AcceptanceRecord.updated_at >= start)
    accepted = [a for a, p in session.execute(q).all()
                if allowed_project_ids is None or p.id in allowed_project_ids]
    accepted_tasks = sorted({a.task_id for a in accepted})
    econ = [e for e in entries if e.mode == LIVE and e.category in ECONOMICS_CATEGORIES]
    known = sum((money(e.cost_usd) or ZERO for e in econ if e.cost_usd is not None), ZERO)
    unknown = sum(1 for e in econ if e.cost_usd is None)
    base: dict[str, Any] = {
        "accepted_outcomes": len(accepted_tasks), "categories": list(ECONOMICS_CATEGORIES),
        "total_known_usd": float(known), "unknown_entries": unknown,
        "basis": "Total biaya API worker/compute/CI (termasuk attempt gagal) dibagi jumlah accepted outcome",
    }
    if not accepted_tasks:
        return base | {"cost_per_accepted_usd": None, "status": "NO_ACCEPTED_OUTCOMES", "per_task": [],
                       "note": "Belum ada accepted outcome; biaya per hasil diterima belum dapat dihitung."}
    run_task = {r.id: r.task_id for r in session.scalars(select(WorkerRun).where(WorkerRun.task_id.in_(accepted_tasks)))}
    per_task: list[dict[str, Any]] = []
    for task_id in accepted_tasks[:100]:
        rows = [e for e in econ if (e.task_id or run_task.get(e.run_id or "")) == task_id]
        t_known = sum((money(e.cost_usd) or ZERO for e in rows if e.cost_usd is not None), ZERO)
        t_unknown = sum(1 for e in rows if e.cost_usd is None)
        per_task.append({"task_id": task_id, "known_usd": float(t_known), "unknown_entries": t_unknown,
                         "cost_usd": None if t_unknown else float(t_known), "entries": len(rows)})
    if unknown:
        return base | {"cost_per_accepted_usd": None, "status": "UNKNOWN", "per_task": per_task,
                       "known_partial_per_accepted_usd": float(known / len(accepted_tasks)),
                       "note": f"{unknown} entri biaya UNKNOWN; angka per hasil diterima tidak dinyatakan."}
    return base | {"cost_per_accepted_usd": float((known / len(accepted_tasks)).quantize(USD_Q)),
                   "status": "COMPUTED", "per_task": per_task}


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(float(s[max(0, min(len(s) - 1, math.ceil(q * len(s)) - 1))]), 3)


def operations_usage(session: Session, *, domain: str | None, allowed_project_ids: list[str] | None,
                     start: datetime | None) -> dict[str, Any]:
    """§42 "latency, jumlah repair" monitored separately from money and tokens: worker-run latency
    (started -> finished, p50/p95 with sample size) and repair attempts (attempt > 1) in the window, scoped to
    the active domain/projects like every other figure on this view."""
    from aeos.models.workforce import TaskExecution

    q = select(WorkerRun).where(WorkerRun.domain == domain)
    if start is not None:
        q = q.where(WorkerRun.created_at >= start)
    runs = [r for r in session.scalars(q) if allowed_project_ids is None or r.project_id in allowed_project_ids]
    durations = []
    for r in runs:
        a, b = ensure_aware(r.started_at), ensure_aware(r.finished_at)
        if a is not None and b is not None:
            durations.append(max(0.0, (b - a).total_seconds()))  # type: ignore[operator]
    tq = select(TaskExecution).where(TaskExecution.domain == domain)
    if start is not None:
        tq = tq.where(TaskExecution.updated_at >= start)
    tasks = [t for t in session.scalars(tq) if allowed_project_ids is None or t.project_id in allowed_project_ids]
    repairs = sum(max(0, int(t.attempts_used or 0) - 1) for t in tasks)
    return {
        "latency": {"p50_s": _percentile(durations, 0.5), "p95_s": _percentile(durations, 0.95),
                    "samples": len(durations), "runs": len(runs),
                    "basis": "WorkerRun started_at -> finished_at (run selesai saja)",
                    "status": "MEASURED" if durations else "NO_FINISHED_RUNS"},
        "repairs": {"repair_attempts": repairs, "tasks": len(tasks),
                    "tasks_repaired": sum(1 for t in tasks if int(t.attempts_used or 0) > 1),
                    "basis": "attempts_used - 1 per task (attempt pertama bukan repair)"},
        "note": "Latency dan repair dipantau terpisah dari biaya tunai/token; bukan konversi ke dolar.",
    }


def summary(session: Session, *, domain: str | None, allowed_project_ids: list[str] | None,
            cross_domain_visible: bool, period: str = "MONTH") -> dict[str, Any]:
    period = (period or "MONTH").upper()
    start = period_start(period)
    entries = _visible_ledger(session, domain=domain, allowed_project_ids=allowed_project_ids, start=start)
    live = [e for e in entries if e.mode == LIVE]
    mock = [e for e in entries if e.mode != LIVE]

    totals = _bucket()
    by_cat: dict[str, dict[str, Any]] = {c: _bucket() for c in USAGE_CATEGORIES}
    by_project: dict[str | None, dict[str, Any]] = defaultdict(_bucket)
    by_route: dict[str | None, dict[str, Any]] = defaultdict(_bucket)
    native = _bucket()
    for e in live:
        by_cat.setdefault(e.category, _bucket())
        _add(by_cat[e.category], e)
        if e.category == "NATIVE_CLAUDE":
            _add(native, e)
            continue
        _add(totals, e)
        _add(by_project[e.project_id], e)
        _add(by_route[e.route_key], e)

    names = {p.id: p.name for p in session.scalars(select(Project))}
    routes = _route_cache(session)
    allowance_in_window: dict[str | None, Decimal] = defaultdict(lambda: ZERO)
    for e in live:
        if (e.details or {}).get("billing") == subs.SUBSCRIPTION and e.category in BUDGETED_CATEGORIES:
            try:
                allowance_in_window[e.route_key] += money((e.details or {}).get("allowance_usd")) or ZERO
            except ValueError:
                pass
    quotas = {(q["provider"], q["scope"]): q for q in list_quotas(session)}
    nq = quotas.get((NATIVE_PROVIDER, "*")) or native_quota_default()
    remaining = nq["state"] if nq.get("official") else ("EXHAUSTED" if nq["state"] == "EXHAUSTED" else "UNKNOWN")
    out_totals = _bucket_out(totals)
    return {
        "source": "usage_ledger + budgets + quota_states (data kode/provider, bukan tebakan model)",
        "checked_at": utcnow().isoformat(),
        "mode": LIVE,
        "window": {"period": period, "start": start.isoformat() if start else None},
        "totals": {
            "actual_usd": out_totals["actual_usd"], "estimate_usd": out_totals["estimate_usd"],
            "unknown_count": out_totals["unknown_count"], "entries": out_totals["entries"],
            "note": "Actual, estimasi dan UNKNOWN tidak dijumlahkan menjadi satu angka.",
        },
        "by_category": [
            {"category": c, "label": CATEGORY_LABELS.get(c, c),
             "billing": "SUBSCRIPTION" if c == "NATIVE_CLAUDE" else "METERED", **_bucket_out(b)}
            | ({"actual_usd": None, "estimate_usd": None} if c == "NATIVE_CLAUDE" else {})
            for c, b in by_cat.items()
        ],
        "by_project": sorted(
            ({"project_id": k, "project_name": names.get(k or "", "Platform / tanpa proyek") if k else "Platform / tanpa proyek",
              **_bucket_out(v)} for k, v in by_project.items()),
            key=lambda r: -(r["actual_usd"] + r["estimate_usd"]),
        ),
        "by_route": sorted(
            ({"route_key": k or "(tanpa rute)", **_bucket_out(v), **_route_billing(routes.get(k or ""),
                                                                                  allowance_in_window.get(k))}
             for k, v in by_route.items()),
            key=lambda r: -(r["actual_usd"] + r["estimate_usd"]),
        ),
        # ADR-0080 amendment 2026-10-26: flat-fee routes as plan + allowance consumed per provider window (never as
        # per-token spend). The allowance is per route over every domain, so its figures follow the GLOBAL/ROUTE rule.
        "subscriptions": subscription_views(session, routes=routes, spend_visible=cross_domain_visible),
        "native_subscription": {
            "provider": NATIVE_PROVIDER, "billing": "SUBSCRIPTION", "dollar_cost": None,
            "dollar_note": "Penggunaan Claude native tidak dikonversi ke dolar atau persentase kuota Max (§42).",
            "remaining_quota": remaining, "quota_state": nq["state"], "quota_source": nq["source"],
            "quota_observed_at": nq["observed_at"], "requests": native["requests"],
            "input_tokens": native["input_tokens"], "output_tokens": native["output_tokens"],
            "entries": native["entries"], "note": NATIVE_NOTE,
        },
        "stop_caps": list_budgets(session, domain=domain, allowed_project_ids=allowed_project_ids,
                                  cross_domain_visible=cross_domain_visible),
        "per_accepted_outcome": per_accepted_outcome(session, domain=domain, allowed_project_ids=allowed_project_ids,
                                                     start=start, entries=entries),
        "operations": operations_usage(session, domain=domain, allowed_project_ids=allowed_project_ids, start=start),
        "excluded_non_live": {"entries": len(mock), "modes": sorted({e.mode for e in mock}),
                              "note": "Entri MOCK/DEMO/ESTIMATE-fixture tidak dihitung sebagai usage actual."},
    }


def _route_billing(route: ModelRoute | None, allowance: Decimal | None) -> dict[str, Any]:
    """How a route of the summary is paid: SUBSCRIPTION (plan + allowance), FREE, METERED (per token) or UNKNOWN."""
    if route is None or not subs.is_subscription_priced(route):
        is_free = route.is_free if route is not None else None
        return {"billing": "FREE" if is_free == "YES" else "METERED" if is_free == "NO" else "UNKNOWN"}
    return {"billing": subs.SUBSCRIPTION, "plan": (route.pricing or {}).get("plan"),
            "allowance_usd": float(allowance or ZERO),
            "billing_note": "Langganan: biaya paket (keputusan BUDGET) + allowance per jendela; Aktual = tagihan "
                            "marginal provider (0 dalam paket), bukan biaya per token."}


def subscription_views(session: Session, *, routes: dict[str, ModelRoute] | None = None,
                       spend_visible: bool = True) -> list[dict[str, Any]]:
    """Plan + allowance per window of every SUBSCRIPTION route that is activated (TRIAL/APPROVED) or has usage in the
    longest window; CANDIDATE rows without usage are catalog entries, not subscriptions in use."""
    now = utcnow()
    out: list[dict[str, Any]] = []
    for route in sorted((routes or _route_cache(session)).values(), key=lambda r: r.route_key):
        status = subs.allowance_status(session, route, now=now)
        if status is None:
            continue
        if route.status not in ("TRIAL", "APPROVED") and not any(w["requests"] for w in status["windows"]):
            continue
        out.append(status if spend_visible else subs.without_figures(status))
    return out


def ledger_view(e: UsageLedgerEntry) -> dict[str, Any]:
    info = e.details or {}
    subscription = e.category != "NATIVE_CLAUDE" and info.get("billing") == subs.SUBSCRIPTION
    if e.mode != LIVE:
        cost_kind = e.mode  # MOCK/DEMO/DESIGN/ESTIMATE fixture: never labelled as actual usage (§50)
    elif e.category == "NATIVE_CLAUDE":
        cost_kind = "SUBSCRIPTION"
    elif e.cost_usd is None:
        cost_kind = "UNKNOWN"
    elif subscription and (money(e.cost_usd) or ZERO) == ZERO:
        cost_kind = "SUBSCRIPTION"  # within the plan fee: no marginal charge (the allowance is in details)
    else:
        cost_kind = "ESTIMATE" if e.is_estimate else "ACTUAL"
    return {
        "id": e.id, "at": e.at.isoformat() if e.at else None, "project_id": e.project_id, "domain": e.domain,
        "run_id": e.run_id, "task_id": e.task_id, "route_key": e.route_key, "provider": e.provider, "model": e.model,
        "category": e.category, "input_tokens": e.input_tokens, "output_tokens": e.output_tokens,
        "requests": e.requests, "cost_usd": money_out(e.cost_usd), "cost_kind": cost_kind,
        "is_estimate": e.is_estimate, "charged_usd": money_out(e.charged_usd), "reservation_id": e.reservation_id,
        "quantity": money_out(e.quantity), "unit": e.unit, "source": e.source, "mode": e.mode,
        "details": info,
        **({"billing": subs.SUBSCRIPTION, "allowance_usd": _allowance_out(info.get("allowance_usd")),
            "allowance_basis": info.get("allowance_basis") or "UNKNOWN"} if subscription else {}),
    }


def _allowance_out(value: Any) -> float | None:
    try:
        return money_out(value)
    except ValueError:
        return None


def list_ledger(session: Session, *, domain: str | None, allowed_project_ids: list[str] | None,
                category: str | None = None, project_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    rows = _visible_ledger(session, domain=domain, allowed_project_ids=allowed_project_ids, start=None)
    if category:
        rows = [e for e in rows if e.category == category.upper()]
    if project_id:
        rows = [e for e in rows if e.project_id == project_id]
    return [ledger_view(e) for e in rows[: max(1, min(limit, 1000))]]
