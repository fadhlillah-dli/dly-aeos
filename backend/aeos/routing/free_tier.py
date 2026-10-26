"""Free OpenRouter WORKER routes for PUBLIC data (ADR-0041 amendment 2026-10-26, owner decision C).

Decision C: an OpenRouter ``:free`` route may serve worker tasks (pool WORKER) for PUBLIC data, PERSONAL only, while it
is free. INTERNAL data stays on the OpenCode Go routes; CONFIDENTIAL never reaches a worker route. Nothing here moves or
activates a route: the route reaches WORKER only through the owner's pool move (``service.set_route_pool``, a
configuration change that voids every earlier decision) and serves only after a new hash-bound human activation.

* ``decision_c_problems`` / ``worker_problems``: the eligibility rules, checked at the pool move, at activation, in the
  routing hard filter and on every broker request (fail closed). A route that fails them is never selected or proxied.
* Account limits (E1, OpenRouter limits page read 2026-10-26): ``:free`` models are limited per account and the limits
  are shared by every ``:free`` model: 20 requests/minute, and 50 requests per UTC day (less than 10 credits purchased,
  all time) or 1000 (at least 10). The caps are owner settings (``openrouter_free_rpm_limit``,
  ``openrouter_free_daily_limit``); a lower daily limit the key endpoint reports (``free_model_daily_requests.limit``)
  wins. This AEOS instance counts its own upstream requests to ``:free`` models: forwarded broker requests (worker and
  evaluation calls) and feasibility probes in the usage ledger. A 429 OpenRouter answers is recorded as an account-wide
  quota state. Over a limit the request is refused with 429 + Retry-After and the task waits - it never moves to
  another route, and never to a paid one.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.timeutil import ensure_aware, utcnow
from aeos.models.broker_ext import BrokerRequest
from aeos.models.cost import QuotaState, UsageLedgerEntry
from aeos.models.decisions import Decision
from aeos.models.routing import ModelRoute

PROVIDER = "openrouter"
FREE_SUFFIX = ":free"
# The pools decision C governs: the broker-served worker pool (``service.WORKER_EXECUTED_POOLS``).
WORKER_POOLS = ("WORKER",)
# Price components the OpenRouter catalog refresh keeps (``catalog._apply_openrouter``); prompt and completion must be
# observed and exactly zero, any other listed component must be zero too.
REQUIRED_PRICES = ("prompt", "completion")
OTHER_PRICES = ("request", "image", "input_cache_read")
CODE_INELIGIBLE = "FREE_WORKER_ROUTE_INELIGIBLE"
CODE_LIMIT = "FREE_TIER_LIMIT_REACHED"
# Account-wide quota states of the free tier (``QuotaState`` provider ``openrouter``): ``free-daily`` is the existing
# daily scope (catalog key info, ``service.note_rate_limited``); ``free-rpm`` holds a per-minute 429.
SCOPE_DAILY = "free-daily"
SCOPE_RPM = "free-rpm"
QUOTA_SCOPES = (SCOPE_DAILY, SCOPE_RPM)
MINUTE = timedelta(seconds=60)
# Broker request outcomes that never reached the upstream (refused by the broker itself)
NOT_FORWARDED = ("DENIED", "CAP_EXCEEDED", "UNKNOWN_MODEL")
PROBE_SOURCE = "broker-feasibility"
ACTIVATION_KINDS = ("TRIAL", "ROUTE_ACTIVATION")


# ------------------------------------------------------------------------------ eligibility


def is_free_model_route(route: ModelRoute) -> bool:
    """An OpenRouter ``:free`` model (any pool): its calls count against the account's free-model limits."""
    return route.provider == PROVIDER and str(route.model_id or "").endswith(FREE_SUFFIX)


def is_decision_c_route(route: ModelRoute) -> bool:
    """An OpenRouter route in the worker pool: decision C's rules apply to it, whatever its model id says."""
    return route.provider == PROVIDER and route.pool in WORKER_POOLS


def _zero(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def price_problems(pricing: Any) -> list[str]:
    """Why the catalog pricing is not exactly zero (empty = free per the catalog)."""
    p: dict[str, Any] = pricing if isinstance(pricing, dict) else {}
    out: list[str] = []
    for key in REQUIRED_PRICES:
        if key not in p or p[key] is None:
            out.append(f"catalog price '{key}' not observed (run `aeos routes refresh`)")
        elif not _zero(p[key]):
            out.append(f"catalog price '{key}' is {str(p[key])[:24]!r}, not exactly zero")
    for key in OTHER_PRICES:
        if p.get(key) is not None and not _zero(p[key]):
            out.append(f"catalog price '{key}' is {str(p[key])[:24]!r}, not zero")
    return out


def decision_c_problems(route: ModelRoute, *, classes: list[str] | None = None, now: datetime | None = None) -> list[str]:
    """Decision C's rules for *route* (empty = it may be a free WORKER route). ``classes`` checks the data classes an
    activation is about to record instead of the stored ones."""
    current = ensure_aware(now or utcnow())
    out: list[str] = []
    if route.provider != PROVIDER:
        out.append(f"provider {route.provider}: decision C covers OpenRouter routes only")
    if not str(route.model_id or "").endswith(FREE_SUFFIX):
        out.append(f"model id {route.model_id!r} does not end with '{FREE_SUFFIX}'")
    if route.is_free != "YES":
        out.append(f"is_free={route.is_free}: the catalog does not list the model as free")
    out += price_problems(route.pricing)
    until = ensure_aware(route.free_until)
    if until is not None and current is not None and until <= current:
        out.append(f"free period ended ({until.isoformat()}): the route is treated as paid; decision C covers free "
                   "routes only (a paid OpenRouter WORKER route needs a BUDGET decision and its own owner decision)")
    chosen = list(classes if classes is not None else (route.allowed_data_classes or []))
    if chosen != ["PUBLIC"]:
        out.append(f"allowed data classes {chosen}: decision C allows exactly ['PUBLIC'] (inputs may be used for "
                   "training; INTERNAL stays on OpenCode Go, CONFIDENTIAL never goes to a worker)")
    domains = list(route.eligible_domains or [])
    if domains != ["PERSONAL"]:
        out.append(f"eligible domains {domains}: decision C allows exactly ['PERSONAL'] (OFFICE default deny)")
    return out


def _activated_at(session: Session, route_key: str) -> datetime | None:
    """When the owner last (re-)activated *route_key*: the newest ACTIVE human TRIAL / ROUTE_ACTIVATION decision."""
    for d in session.scalars(select(Decision).where(
            Decision.target_type == "model_route", Decision.target_id == route_key,
            Decision.kind.in_(ACTIVATION_KINDS), Decision.status == "ACTIVE").order_by(Decision.created_at.desc())):
        if (d.actor_kind or "HUMAN") == "HUMAN":
            return ensure_aware(d.created_at)
    return None


def charged_problem(session: Session, route: ModelRoute) -> str | None:
    """A LIVE call of this free route that the provider reported with a cost above $0, newer than the owner's latest
    activation: the model was repriced between catalog refreshes. Refused until the owner re-checks and re-activates
    (fail closed: a repriced model stops being served instead of being billed again)."""
    q = select(UsageLedgerEntry).where(UsageLedgerEntry.route_key == route.route_key,
                                       UsageLedgerEntry.mode == "LIVE", UsageLedgerEntry.cost_usd > 0)
    since = _activated_at(session, route.route_key)
    if since is not None:
        q = q.where(UsageLedgerEntry.at >= since)
    e = session.scalars(q.order_by(UsageLedgerEntry.at.desc()).limit(1)).first()
    if e is None:
        return None
    at = ensure_aware(e.at)
    return (f"a call at {at.isoformat() if at else 'UNKNOWN'} was reported with a cost of ${e.cost_usd}: the free model "
            "was repriced; run `aeos routes refresh` and review the route before any re-activation")


def worker_problems(session: Session, route: ModelRoute, *, now: datetime | None = None) -> list[str]:
    """Decision C's rules plus the repricing observed in the ledger (empty = the free WORKER route may serve)."""
    out = decision_c_problems(route, now=now)
    charged = charged_problem(session, route)
    if charged:
        out.append(charged)
    return out


# ------------------------------------------------------------------------------ account limits


def _observed_daily_limit(session: Session) -> int | None:
    """The free-model daily limit the OpenRouter key endpoint reported at the last catalog refresh (None = never)."""
    row = session.scalar(select(QuotaState).where(QuotaState.provider == PROVIDER, QuotaState.scope == "credits"))
    info = (row.details or {}).get("free_model_daily_requests") if row is not None else None
    limit = info.get("limit") if isinstance(info, dict) else None
    return limit if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 else None


def limits(session: Session) -> dict[str, Any]:
    """The caps in force: the owner settings, the daily one lowered to the limit the key endpoint reported."""
    from aeos.core.config import get_settings

    settings = get_settings()
    owner_daily = int(settings.openrouter_free_daily_limit)
    observed = _observed_daily_limit(session)
    daily, basis = owner_daily, "OWNER_SETTING"
    if observed is not None and observed < owner_daily:
        daily, basis = observed, "KEY_ENDPOINT"
    return {"rpm": int(settings.openrouter_free_rpm_limit), "daily": daily, "daily_basis": basis,
            "owner_daily_setting": owner_daily, "observed_daily_limit": observed}


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def next_utc_midnight(now: datetime) -> datetime:
    return _day_start(now) + timedelta(days=1)


def _free_route_keys(session: Session) -> list[str]:
    return list(session.scalars(select(ModelRoute.route_key).where(
        ModelRoute.provider == PROVIDER, ModelRoute.model_id.like(f"%{FREE_SUFFIX}"))))


def usage(session: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """Requests this AEOS instance sent to OpenRouter ``:free`` models in the last minute and in the current UTC day:
    forwarded broker requests (any outcome the upstream saw, a 429 included) and feasibility probes recorded in the
    ledger. Requests the broker refused itself never reached OpenRouter and are not counted."""
    current = ensure_aware(now or utcnow())
    assert current is not None
    minute_start = current - MINUTE
    since = min(_day_start(current), minute_start)
    broker = [ensure_aware(t) for t in session.scalars(select(BrokerRequest.created_at).where(
        BrokerRequest.provider == PROVIDER, BrokerRequest.upstream_model.like(f"%{FREE_SUFFIX}"),
        BrokerRequest.outcome.not_in(NOT_FORWARDED), BrokerRequest.created_at >= since))]
    keys = _free_route_keys(session)
    probes = [ensure_aware(t) for t in session.scalars(select(UsageLedgerEntry.at).where(
        UsageLedgerEntry.source == PROBE_SOURCE, UsageLedgerEntry.provider == PROVIDER,
        UsageLedgerEntry.route_key.in_(keys), UsageLedgerEntry.at >= since))] if keys else []
    stamps = [t for t in (*broker, *probes) if t is not None]
    in_minute = sorted(t for t in stamps if t >= minute_start)
    day = sum(1 for t in stamps if t >= _day_start(current))
    return {"minute": len(in_minute), "day": day, "minute_oldest": in_minute[0] if in_minute else None,
            "checked_at": current}


def _observed_block(session: Session, now: datetime) -> dict[str, Any] | None:
    for q in session.scalars(select(QuotaState).where(QuotaState.provider == PROVIDER,
                                                      QuotaState.scope.in_(QUOTA_SCOPES),
                                                      QuotaState.state == "EXHAUSTED")):
        resets = ensure_aware(q.resets_at)
        if resets is None or resets > now:
            wait = None if resets is None else max(1, math.ceil((resets - now).total_seconds()))
            until = resets.isoformat() if resets else "UNKNOWN (no reset reported)"
            return {"code": CODE_LIMIT, "kind": "OBSERVED", "scope": q.scope, "retry_after_seconds": wait,
                    "resets_at": resets,
                    "reason": f"OpenRouter free-model limit reached ({q.scope}, source {q.source}, account-wide) until "
                              f"{until}; the task waits - no fallback to another route"}
    return None


def limit_problem(session: Session, *, now: datetime | None = None, observed: bool = True) -> dict[str, Any] | None:
    """The account-wide free-model limit that refuses a request now, or None. ``observed=False`` leaves out the 429s
    recorded as quota states (the routing hard filter reports those itself)."""
    current = ensure_aware(now or utcnow())
    assert current is not None
    if observed:
        block = _observed_block(session, current)
        if block is not None:
            return block
    lim = limits(session)
    used = usage(session, now=current)
    if used["day"] >= lim["daily"]:
        resets = next_utc_midnight(current)
        return {"code": CODE_LIMIT, "kind": "DAILY", "scope": SCOPE_DAILY, "resets_at": resets,
                "retry_after_seconds": max(1, math.ceil((resets - current).total_seconds())),
                "reason": f"OpenRouter free-model daily cap reached: {used['day']} of {lim['daily']} requests this UTC "
                          f"day ({lim['daily_basis']}, counted by this AEOS instance); the task waits until "
                          f"{resets.isoformat()} - no fallback to another route"}
    if used["minute"] >= lim["rpm"]:
        oldest = used["minute_oldest"] or current
        resets = oldest + MINUTE
        return {"code": CODE_LIMIT, "kind": "RPM", "scope": SCOPE_RPM, "resets_at": resets,
                "retry_after_seconds": max(1, math.ceil((resets - current).total_seconds())),
                "reason": f"OpenRouter free-model rate limit reached: {used['minute']} of {lim['rpm']} requests in the "
                          "last minute (counted by this AEOS instance); the task waits - no fallback to another route"}
    return None


def route_limit_problem(session: Session, route: ModelRoute, *, now: datetime | None = None,
                        observed: bool = True) -> dict[str, Any] | None:
    """``limit_problem`` for a route whose calls count against the free-model limits; None for any other route."""
    return limit_problem(session, now=now, observed=observed) if is_free_model_route(route) else None


def note_rate_limited(session: Session, *, route_key: str, retry_after_seconds: int | None,
                      headers: dict[str, str] | None = None, run_id: str | None = None,
                      source: str = "observed-429", now: datetime | None = None) -> dict[str, Any]:
    """Record a 429 OpenRouter answered for a ``:free`` model as an account-wide quota state, so every free route
    waits (the limits are shared by every ``:free`` model). The reset: the provider's Retry-After when it sent one;
    otherwise the next UTC midnight when this instance's daily count has reached the cap, else one minute (the
    per-minute window). Which limit a 429 without Retry-After hit is not documented (NOT-PROVEN)."""
    current = ensure_aware(now or utcnow())
    assert current is not None
    if retry_after_seconds is not None:
        wait = max(1, int(retry_after_seconds))
        resets, basis = current + timedelta(seconds=wait), "retry-after"
        scope = SCOPE_RPM if wait <= int(MINUTE.total_seconds()) else SCOPE_DAILY
    elif usage(session, now=current)["day"] >= limits(session)["daily"]:
        resets, basis, scope = next_utc_midnight(current), "daily-cap-counted", SCOPE_DAILY
    else:
        resets, basis, scope = current + MINUTE, "per-minute-window", SCOPE_RPM
    details = {"status": 429, "route_key": route_key, "run_id": run_id, "reset_basis": basis,
               "rate_limit_headers": dict(headers or {}),
               "note": "account-wide: every OpenRouter :free route waits; no fallback to another route"}
    try:
        from aeos.cost import facade as cost

        cost.set_quota_state(session, provider=PROVIDER, state="EXHAUSTED", source=source, scope=scope,
                             details=details, observed_at=current, resets_at=resets)
    except ImportError:  # the cost module owns quota state; without it the 429 stays on the route scope only
        return {"recorded": False, "scope": scope, "resets_at": resets.isoformat()}
    return {"recorded": True, "scope": scope, "resets_at": resets.isoformat(), "basis": basis}


def status(session: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """The free-model limits and this instance's count (route view, CLI)."""
    current = ensure_aware(now or utcnow())
    assert current is not None
    lim = limits(session)
    used = usage(session, now=current)
    block = limit_problem(session, now=current)
    return {"provider": PROVIDER, "scope": "account (shared by every :free model)", "rpm_limit": lim["rpm"],
            "daily_limit": lim["daily"], "daily_basis": lim["daily_basis"],
            "owner_daily_setting": lim["owner_daily_setting"], "observed_daily_limit": lim["observed_daily_limit"],
            "requests_last_minute": used["minute"], "requests_today_utc": used["day"],
            "state": "LIMIT_REACHED" if block else "OK", "reason": block["reason"] if block else None,
            "counted": "requests this AEOS instance sent (broker and probes); other clients of the same OpenRouter "
                       "account are not visible, so OpenRouter's own 429 stays authoritative",
            "checked_at": current.isoformat()}
