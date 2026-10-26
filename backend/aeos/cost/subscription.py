"""Flat-fee SUBSCRIPTION routes: plan fee + allowance windows (ADR-0080 amendment 2026-10-26; ADR-0041 decision A).

A route whose ``pricing.billing`` is ``SUBSCRIPTION`` (the seeded ``opencode-go/*`` routes) is paid by a monthly plan fee
(the owner's BUDGET decision at activation), not per token. Within the plan the provider charges no marginal money
(``usage.cost`` 0); every request consumes a per-model dollar ALLOWANCE instead, limited per provider window (OpenCode
Go docs, E1 2026-10-26: 5 hours 20%, week 50%, month 100% of the monthly allowance). At a limit the docs say requests
are blocked while the provider's "Extra Usage" stays OFF; that the block is HTTP 429 comes from OpenCode's own client
test fixture, not from the docs (NOT-PROVEN).

* **Only this AEOS instance is counted.** The windows sum the calls made through THIS AEOS instance (its own usage
  ledger). Usage of the same provider workspace from other clients or machines (the owner's own OpenCode, a second
  AEOS host with the same key) is invisible here, so the provider's own limit stays authoritative and Extra Usage OFF
  stays the only guard against provider-side billing. AEOS's count can therefore be lower than the provider's.
* **Allowance of one request:** the provider's figure (``usage.opencode.allowance``, basis REPORTED), else an estimate
  from its tokens x the route's allowance rates, or - when the upstream reported no tokens - from an upper bound of the
  call's tokens x the highest rate (basis ESTIMATED, rounded up). The bound is the caller's (the broker: the request's
  clamped ``max_tokens`` plus its prompt size, else the grant's token cap), else the tokens charged against the hold.
  UNKNOWN only when nothing bounds the call. Nothing else is ever made up.
* **Windows.** The trailing 5-hour window is a BLOCKING admission check: a used-up window (EXHAUSTED) or a call in it
  whose allowance cannot be counted (UNKNOWN, fail closed) refuses new admissions and probes - for at most 5 hours,
  since the call then leaves the window. The week (7 days) and month (31 days) windows are ADVISORY (WARN_EXHAUSTED,
  never blocking): AEOS cannot know where the provider anchors or resets them, a trailing count could lock a route for
  weeks after the provider reset it, and with Extra Usage OFF exceeding them only yields provider refusals (no money).
* **Paid usage beyond the plan** (provider-side paid fallback, invariant 8): a call whose ``billing_source`` is present
  and not one of the plan's sources (``go`` / ``go-plus``, e.g. ``balance``), or that reports ``usage.cost`` > 0 (or a
  cost AEOS cannot read), is flagged ``beyond_plan_usd`` (the amount, else UNKNOWN). The route is then refused
  (``SUBSCRIPTION_BEYOND_PLAN``) for new admissions, probes and broker requests until the owner re-activates it: a
  human TRIAL / ROUTE_ACTIVATION decision for the route newer than the flagged call (fail closed).
* Terms that are incomplete (no allowance, no window shares, no rates, pricing never checked) make the route an
  unpriced paid route: refused, never treated as free (fail closed).

Pure reads of the route, its activation decisions and the usage ledger; no money is converted and nothing is written.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.enums import DataMode
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.cost.amounts import USD_Q, ZERO, money
from aeos.models.cost import UsageLedgerEntry
from aeos.models.decisions import Decision
from aeos.models.routing import ModelRoute

SUBSCRIPTION = "SUBSCRIPTION"
# (window name in pricing.allowance_windows, trailing length AEOS counts it over)
ALLOWANCE_WINDOWS: tuple[tuple[str, timedelta], ...] = (
    ("5h", timedelta(hours=5)), ("week", timedelta(days=7)), ("month", timedelta(days=31)))
# Only the 5-hour window blocks admission; week and month are advisory (see the module docstring).
BLOCKING_WINDOWS = frozenset({"5h"})
ALLOWANCE_BASES = ("REPORTED", "ESTIMATED", "UNKNOWN")
MODEL_CALL_CATEGORY = "WORKER_API"
MTOK = Decimal(1_000_000)
# usage.opencode.billing_source values that mean "within the plan" (OpenCode Go API docs, E1 2026-10-26); any other
# stated source (e.g. "balance", the pay-as-you-go fallback of Extra Usage) is paid usage beyond the plan.
PLAN_BILLING_SOURCES = frozenset({"go", "go-plus"})
# The owner's (re-)activation of a route: the decisions that clear a beyond-plan refusal when newer than the call.
ACTIVATION_DECISION_KINDS = ("TRIAL", "ROUTE_ACTIVATION")
ROUTE_TARGET_TYPE = "model_route"
CODE_EXHAUSTED = "SUBSCRIPTION_ALLOWANCE_EXHAUSTED"
CODE_UNKNOWN = "SUBSCRIPTION_ALLOWANCE_UNKNOWN"
CODE_BEYOND_PLAN = "SUBSCRIPTION_BEYOND_PLAN"
BLOCK_MESSAGES = {
    CODE_EXHAUSTED: ("subscription allowance used up in the provider's 5h window (as counted by this AEOS instance); "
                     "the run waits for the window (no fallback route, no paid usage beyond the plan)"),
    CODE_UNKNOWN: ("the subscription allowance of a call in the 5h window cannot be counted; admission is refused "
                   "until that call leaves the window (fail closed, at most 5 hours)"),
    CODE_BEYOND_PLAN: ("the subscription route was billed beyond its plan (provider-side paid fallback); it is refused "
                       "until the owner re-activates the route (fail closed)"),
}
BEYOND_PLAN_REMEDY = ("Check on the provider console that Extra Usage is OFF, then re-activate the route "
                      "(aeos routes activate <route> --mode TRIAL ... --budget-decision <id>): a TRIAL or APPROVED "
                      "decision newer than the flagged call clears the refusal.")
BASIS_NOTE = ("AEOS counts only the calls made through this AEOS instance; usage of the same provider workspace from "
              "other clients or machines is invisible to it, so the provider's own limit (blocked requests; HTTP 429 "
              "per OpenCode's client test fixture, NOT-PROVEN) stays authoritative. Allowance per request: provider "
              "usage.opencode.allowance (REPORTED), else tokens x the route's allowance rates or a token upper bound x "
              "the highest rate (ESTIMATED, rounded up). The trailing 5h window blocks admission; the week (7 days) "
              "and month (31 days) windows are advisory (WARN_EXHAUSTED): the provider's reset anchors are unknown and "
              "with Extra Usage OFF exceeding them only yields provider refusals.")
# Figures of an allowance status that span domains (GLOBAL/ROUTE visibility rule): dropped by ``without_figures``.
FIGURE_KEYS = ("windows", "beyond_plan_usd", "beyond_plan_unknown_requests", "beyond_plan", "problems", "warnings")
HIDDEN_NOTE = "Allowance rute mencakup domain lain yang tidak dapat Anda lihat."


def is_subscription_priced(route: ModelRoute | None) -> bool:
    """The route's pricing record declares a flat-fee subscription (complete or not - see ``subscription_terms``)."""
    pricing = route.pricing if route is not None else None
    return isinstance(pricing, dict) and pricing.get("billing") == SUBSCRIPTION


def subscription_terms(route: ModelRoute | None) -> tuple[dict[str, Any] | None, str | None]:
    """(terms, problem) of a SUBSCRIPTION route; (None, None) for any other route.

    Trusted only when complete: is_free NO (a plan fee is paid), pricing checked, a positive monthly allowance, a share
    in (0, 1] for every provider window and non-negative input/output allowance rates. Otherwise the problem says why
    the route is an unpriced paid route (fail closed)."""
    if route is None or not is_subscription_priced(route):
        return None, None
    pricing: dict[str, Any] = route.pricing or {}
    key = route.route_key
    if route.is_free != "NO":
        return None, f"subscription route {key!r} has is_free={route.is_free}; a plan fee is paid (is_free NO)"
    if route.pricing_checked_at is None:
        return None, f"subscription route {key!r} pricing was never checked"
    shares_raw = pricing.get("allowance_windows")
    rates_raw = pricing.get("allowance_rate_per_mtok")
    try:
        allowance = money(pricing.get("allowance_usd_month"))
        fee = money(pricing.get("monthly_fee_usd"))
        shares = ({name: money(shares_raw.get(name)) for name, _ in ALLOWANCE_WINDOWS}
                  if isinstance(shares_raw, dict) else {})
        rates = {k: money(rates_raw.get(k)) for k in ("input", "output")} if isinstance(rates_raw, dict) else {}
    except ValueError as exc:
        return None, f"subscription route {key!r} pricing has an invalid amount ({exc})"
    if allowance is None or allowance <= ZERO:
        return None, f"subscription route {key!r} states no monthly allowance; its usage cannot be counted"
    if not shares or any(s is None or s <= ZERO or s > 1 for s in shares.values()):
        return None, (f"subscription route {key!r} needs a share in (0, 1] for every provider window "
                      f"({', '.join(n for n, _ in ALLOWANCE_WINDOWS)})")
    if not rates or any(r is None or r < ZERO for r in rates.values()):
        return None, f"subscription route {key!r} needs input/output allowance rates per 1M tokens"
    return {"plan": str(pricing.get("plan") or SUBSCRIPTION), "monthly_fee_usd": fee,
            "currency": str(pricing.get("currency") or "USD"), "allowance_usd_month": allowance,
            "shares": shares, "rates": rates}, None


def _ceil(value: Decimal) -> Decimal:
    return value.quantize(USD_Q, rounding=ROUND_CEILING)


def token_count(value: Any) -> int | None:
    """A token count/bound: a non-negative int (never a bool); None otherwise."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def allowance_of(terms: dict[str, Any], *, reported: Any = None, input_tokens: int | None = None,
                 output_tokens: int | None = None, charged_tokens: Any = None) -> tuple[Decimal | None, str, str | None]:
    """(allowance USD, basis, estimate source) of one request: REPORTED by the provider, else ESTIMATED from its tokens
    x the allowance rates, else from an upper bound of its tokens (``charged_tokens``) x the highest rate; UNKNOWN
    otherwise."""
    try:
        rep = money(reported)
    except ValueError:
        rep = None
    if rep is not None and rep >= ZERO:
        return rep, "REPORTED", None
    rates = terms["rates"]
    if input_tokens is not None or output_tokens is not None:
        est = (Decimal(int(input_tokens or 0)) * rates["input"] + Decimal(int(output_tokens or 0)) * rates["output"])
        return _ceil(est / MTOK), "ESTIMATED", "TOKENS_X_RATES"
    bound = token_count(charged_tokens)
    if bound is not None:
        return _ceil(Decimal(bound) * max(rates.values()) / MTOK), "ESTIMATED", "TOKEN_BOUND_X_HIGHEST_RATE"
    return None, "UNKNOWN", None


def entry_allowance(e: UsageLedgerEntry, terms: dict[str, Any]) -> tuple[Decimal | None, str]:
    """The allowance a ledger entry recorded (``details.allowance_usd``), else estimated from its tokens now."""
    info = e.details or {}
    if info.get("allowance_basis") in ("REPORTED", "ESTIMATED"):
        try:
            recorded = money(info.get("allowance_usd"))
        except ValueError:
            recorded = None
        if recorded is not None and recorded >= ZERO:
            return recorded, str(info["allowance_basis"])
    amount, basis, _how = allowance_of(terms, input_tokens=e.input_tokens, output_tokens=e.output_tokens,
                                       charged_tokens=info.get("allowance_token_bound", info.get("charged_tokens")))
    return amount, basis


def beyond_plan_amount(*, cost: Decimal | None, billing_source: Any, cost_unreadable: bool = False) -> str | None:
    """The paid usage beyond the plan one call shows (a USD amount, or ``UNKNOWN``); None when it stayed within the plan.

    Beyond the plan: a marginal charge above $0, a stated billing source that is not one of the plan's (e.g. the
    pay-as-you-go ``balance``), or a marginal cost the provider stated but AEOS could not read."""
    if cost is not None and cost > ZERO:
        return str(cost)
    source = billing_source.strip().lower() if isinstance(billing_source, str) else ""
    if (source and source not in PLAN_BILLING_SOURCES) or cost_unreadable:
        return "UNKNOWN"
    return None


def _activated_at(session: Session, route_key: str) -> datetime | None:
    """When the owner last (re-)activated *route_key*: the newest ACTIVE human TRIAL / ROUTE_ACTIVATION decision."""
    rows = session.scalars(select(Decision).where(
        Decision.target_type == ROUTE_TARGET_TYPE, Decision.target_id == route_key,
        Decision.kind.in_(ACTIVATION_DECISION_KINDS), Decision.status == "ACTIVE").order_by(Decision.created_at.desc()))
    for d in rows:
        if (d.actor_kind or "HUMAN") == "HUMAN":
            return ensure_aware(d.created_at)
    return None


def beyond_plan_block(session: Session, route: ModelRoute) -> dict[str, Any] | None:
    """The newest LIVE model call of *route* flagged as paid usage beyond the plan that no owner (re-)activation of
    the route postdates; None when there is none. While it exists, the route is refused (``SUBSCRIPTION_BEYOND_PLAN``)
    for new admissions, probes and broker requests (fail closed)."""
    if not is_subscription_priced(route):
        return None
    since = _activated_at(session, route.route_key)
    q = select(UsageLedgerEntry).where(
        UsageLedgerEntry.route_key == route.route_key, UsageLedgerEntry.category == MODEL_CALL_CATEGORY,
        UsageLedgerEntry.mode == DataMode.LIVE.value,
        UsageLedgerEntry.details["beyond_plan_usd"].as_string().is_not(None))
    if since is not None:
        q = q.where(UsageLedgerEntry.at >= since)
    e = session.scalars(q.order_by(UsageLedgerEntry.at.desc()).limit(1)).first()
    if e is None:
        return None
    info = e.details or {}
    at = ensure_aware(e.at)
    return {"entry_id": e.id, "at": at.isoformat() if at else None,
            "amount_usd": str(info.get("beyond_plan_usd") or "UNKNOWN"),
            "billing_source": info.get("billing_source"), "activated_at": since.isoformat() if since else None,
            "remedy": BEYOND_PLAN_REMEDY}


def _out(d: Decimal | None) -> float | None:
    return None if d is None else float(d)


def allowance_status(session: Session, route: ModelRoute, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Plan + allowance consumed per provider window for a SUBSCRIPTION route (None for any other route).

    ``state`` (blocking first): UNTRUSTED (incomplete terms), BEYOND_PLAN (a call was billed beyond the plan and the
    owner has not re-activated the route since), EXHAUSTED (the 5h window is used up), UNKNOWN (a call in the 5h window
    has no countable allowance; it blocks for at most 5 hours), WARN_EXHAUSTED (an ADVISORY week/month window is used
    up: shown, never blocking) or OK. The allowance is per route over every LIVE model call this AEOS instance made,
    whatever its domain; usage of the same provider account elsewhere is not visible here."""
    terms, problem = subscription_terms(route)
    if terms is None and problem is None:
        return None
    current = ensure_aware(now or utcnow())
    assert current is not None
    pricing = route.pricing or {}
    out: dict[str, Any] = {
        "route_key": route.route_key, "provider": route.provider, "status": route.status, "billing": SUBSCRIPTION,
        "plan": (terms or {}).get("plan") or pricing.get("plan"), "currency": (terms or {}).get("currency", "USD"),
        "monthly_fee_usd": _out((terms or {}).get("monthly_fee_usd")),
        "allowance_usd_month": _out((terms or {}).get("allowance_usd_month")),
        "basis": BASIS_NOTE, "counted_scope": "THIS_AEOS_INSTANCE", "checked_at": current.isoformat(),
        "windows": [], "problems": [], "warnings": [], "beyond_plan": None, "beyond_plan_usd": 0.0,
        "beyond_plan_unknown_requests": 0,
    }
    if terms is None:
        return out | {"state": "UNTRUSTED", "problems": [problem]}
    longest = max(length for _, length in ALLOWANCE_WINDOWS)
    rows = session.scalars(select(UsageLedgerEntry).where(
        UsageLedgerEntry.route_key == route.route_key, UsageLedgerEntry.category == MODEL_CALL_CATEGORY,
        UsageLedgerEntry.mode == DataMode.LIVE.value, UsageLedgerEntry.at >= current - longest))
    calls: list[tuple[datetime, Decimal | None, str, str]] = []
    beyond_plan = ZERO
    beyond_unknown = 0
    for e in rows:
        at = ensure_aware(e.at)
        if at is None:
            continue
        amount, basis = entry_allowance(e, terms)
        calls.append((at, amount, basis, e.id))
        try:
            cost = money(e.cost_usd)
        except ValueError:
            cost = None
        if cost is not None and cost > ZERO:
            beyond_plan += cost  # a marginal charge on a subscription route: paid usage beyond the plan
        elif (e.details or {}).get("beyond_plan_usd") == "UNKNOWN":
            beyond_unknown += 1
    states: dict[str, str] = {}
    for name, length in ALLOWANCE_WINDOWS:
        since = current - length
        blocking = name in BLOCKING_WINDOWS
        limit = (terms["allowance_usd_month"] * terms["shares"][name]).quantize(USD_Q)
        inside = [c for c in calls if c[0] >= since]
        known = [(amount, basis) for _, amount, basis, _ in inside if amount is not None]
        unknown = [(at, eid) for at, amount, _, eid in inside if amount is None]
        used = sum((a for a, _ in known), ZERO)
        reported = sum((a for a, b in known if b == "REPORTED"), ZERO)
        if used >= limit:
            state = "EXHAUSTED" if blocking else "WARN_EXHAUSTED"
        elif unknown and blocking:
            state = "UNKNOWN"
        else:
            state = "OK"
        states[name] = state
        window: dict[str, Any] = {
            "window": name, "share": float(terms["shares"][name]), "trailing_hours": int(length.total_seconds() // 3600),
            "since": since.isoformat(), "blocking": blocking, "limit_usd": float(limit), "used_usd": float(used),
            "remaining_usd": float(max(ZERO, limit - used)), "reported_usd": float(reported),
            "estimated_usd": float(used - reported), "requests": len(inside), "unknown_requests": len(unknown),
            "state": state}
        if unknown and blocking:
            until = max(at for at, _ in unknown) + length
            window["blocked_until"] = until.isoformat()
            out["problems"] += [f"ledger entry {eid} on {route.route_key} at {at.isoformat()} has an UNKNOWN allowance: "
                                f"the {name} window blocks admission until {(at + length).isoformat()}"
                                for at, eid in unknown]
        elif unknown:
            out["warnings"].append(f"{name} window: {len(unknown)} call(s) with an UNKNOWN allowance, so its total is "
                                   "a lower bound (advisory window, never blocking)")
        if state == "WARN_EXHAUSTED":
            out["warnings"].append(f"{name} window: the allowance counted by this AEOS instance is used up (advisory: "
                                   "the provider refuses requests at its own limit; AEOS does not block)")
        out["windows"].append(window)
    out["beyond_plan_usd"] = float(beyond_plan)
    out["beyond_plan_unknown_requests"] = beyond_unknown
    out["beyond_plan"] = beyond_plan_block(session, route)
    if out["beyond_plan"] is not None:
        bp = out["beyond_plan"]
        out["problems"].insert(0, f"a call at {bp['at']} on {route.route_key} was billed beyond the plan (amount "
                                  f"{bp['amount_usd']}, billing source {bp['billing_source'] or 'not reported'}); "
                                  "refused until the owner re-activates the route")
        out["state"] = "BEYOND_PLAN"
    elif "EXHAUSTED" in states.values():
        out["state"] = "EXHAUSTED"
    elif "UNKNOWN" in states.values():
        out["state"] = "UNKNOWN"
    elif "WARN_EXHAUSTED" in states.values():
        out["state"] = "WARN_EXHAUSTED"
    else:
        out["state"] = "OK"
    return out


def admission_block(status: dict[str, Any] | None) -> tuple[str, str] | None:
    """(code, reason) when a subscription route's state refuses a new admission or probe; None otherwise (also for
    UNTRUSTED terms, which the pricing checks refuse on their own). The reason is ASCII and carries no allowance
    figures (they span domains), so it may be shown to anyone who can see the task."""
    if not status:
        return None
    key = status.get("route_key") or "subscription route"
    state = status.get("state")
    windows: list[dict[str, Any]] = list(status.get("windows") or [])
    if state == "BEYOND_PLAN":
        bp = status.get("beyond_plan") or {}
        return CODE_BEYOND_PLAN, (f"{key}: a call at {bp.get('at') or 'UNKNOWN'} was billed beyond the plan (billing "
                                  f"source {bp.get('billing_source') or 'not reported'}); refused until the owner "
                                  "checks that Extra Usage is OFF and re-activates the route (aeos routes activate)")
    if state == "EXHAUSTED":
        w: dict[str, Any] = next((x for x in windows if x.get("blocking") and x.get("state") == "EXHAUSTED"), {})
        return CODE_EXHAUSTED, (f"{key}: the {w.get('window', '5h')} window allowance counted by this AEOS instance is "
                                f"used up (trailing since {w.get('since', 'UNKNOWN')}); waiting for the window (no "
                                "fallback route, no paid usage beyond the plan)")
    if state == "UNKNOWN":
        u: dict[str, Any] = next((x for x in windows if x.get("blocking") and x.get("state") == "UNKNOWN"), {})
        until = u.get("blocked_until")
        return CODE_UNKNOWN, (f"{key}: the allowance of a call in the trailing 5h window cannot be counted; "
                              + (f"waiting until {until} (at most 5 hours)" if until else
                                 "the allowance state cannot be read (fail closed)"))
    return None


def without_figures(status: dict[str, Any]) -> dict[str, Any]:
    """An allowance status without the figures that span domains (windows, beyond-plan amounts, ledger-entry problems):
    for a principal who cannot see every domain (GLOBAL/ROUTE budget visibility rule). State and plan stay."""
    return {k: v for k, v in status.items() if k not in FIGURE_KEYS} | {
        "windows": None, "spend_hidden": True, "spend_note": HIDDEN_NOTE}
