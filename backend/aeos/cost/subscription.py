"""Flat-fee SUBSCRIPTION routes: plan fee + allowance windows (ADR-0080 amendment 2026-10-26; ADR-0041 decision A).

A route whose ``pricing.billing`` is ``SUBSCRIPTION`` (the seeded ``opencode-go/*`` routes) is paid by a monthly plan fee
(the owner's BUDGET decision at activation), not per token. Within the plan the provider charges no marginal money
(``usage.cost`` 0); every request consumes a per-model dollar ALLOWANCE instead, limited per provider window (OpenCode
Go docs, E1 2026-10-26: 5 hours 20%, week 50%, month 100% of the monthly allowance; HTTP 429 at a limit while the
provider's "Extra Usage" stays OFF).

* The allowance of one request is the provider's figure (``usage.opencode.allowance``, basis REPORTED), else an
  estimate from its tokens x the route's allowance rates, or - when the upstream reported no tokens - from the tokens
  charged against the hold x the highest rate (basis ESTIMATED, rounded up). Nothing else is ever made up.
* The provider anchors its windows itself. AEOS counts each one as a TRAILING window of the longest length it can have
  (a billing month is at most 31 days), so the allowance counted is never less than the provider's own window holds.
* Terms that are incomplete (no allowance, no window shares, no rates, pricing never checked) make the route an
  unpriced paid route: refused, never treated as free (fail closed).

Pure reads of the route and the usage ledger; no money is converted and nothing is written here.
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
from aeos.models.routing import ModelRoute

SUBSCRIPTION = "SUBSCRIPTION"
# (window name in pricing.allowance_windows, trailing length AEOS counts it over)
ALLOWANCE_WINDOWS: tuple[tuple[str, timedelta], ...] = (
    ("5h", timedelta(hours=5)), ("week", timedelta(days=7)), ("month", timedelta(days=31)))
ALLOWANCE_BASES = ("REPORTED", "ESTIMATED", "UNKNOWN")
MODEL_CALL_CATEGORY = "WORKER_API"
MTOK = Decimal(1_000_000)
BASIS_NOTE = ("Allowance per request: provider usage.opencode.allowance (REPORTED), else tokens x the route's allowance "
              "rates (ESTIMATED, rounded up). Trailing windows 5h / 7 days / 31 days: the provider's window anchors are "
              "unknown, so AEOS never counts less than the provider's own window holds.")


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


def allowance_of(terms: dict[str, Any], *, reported: Any = None, input_tokens: int | None = None,
                 output_tokens: int | None = None, charged_tokens: Any = None) -> tuple[Decimal | None, str, str | None]:
    """(allowance USD, basis, estimate source) of one request: REPORTED by the provider, else ESTIMATED from its tokens
    x the allowance rates, else from the tokens charged against the hold x the highest rate; UNKNOWN otherwise."""
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
    if isinstance(charged_tokens, int) and not isinstance(charged_tokens, bool) and charged_tokens >= 0:
        return _ceil(Decimal(charged_tokens) * max(rates.values()) / MTOK), "ESTIMATED", "TOKEN_BOUND_X_HIGHEST_RATE"
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
                                       charged_tokens=info.get("charged_tokens"))
    return amount, basis


def _out(d: Decimal | None) -> float | None:
    return None if d is None else float(d)


def allowance_status(session: Session, route: ModelRoute, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Plan + allowance consumed per provider window for a SUBSCRIPTION route (None for any other route).

    ``state``: OK, EXHAUSTED (a window is used up: admission waits, before the provider's 429), UNKNOWN (a ledger entry
    of the window has no countable allowance: admission refused, fail closed) or UNTRUSTED (incomplete terms). The
    allowance is per route (the provider's limit is per model) over every LIVE model call, whatever its domain."""
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
        "basis": BASIS_NOTE, "checked_at": current.isoformat(), "windows": [], "problems": [],
    }
    if terms is None:
        return out | {"state": "UNTRUSTED", "problems": [problem]}
    longest = max(length for _, length in ALLOWANCE_WINDOWS)
    rows = session.scalars(select(UsageLedgerEntry).where(
        UsageLedgerEntry.route_key == route.route_key, UsageLedgerEntry.category == MODEL_CALL_CATEGORY,
        UsageLedgerEntry.mode == DataMode.LIVE.value, UsageLedgerEntry.at >= current - longest))
    counted: list[tuple[datetime, Decimal, str]] = []
    beyond_plan = ZERO
    for e in rows:
        at = ensure_aware(e.at)
        if at is None:
            continue
        amount, basis = entry_allowance(e, terms)
        if amount is None:
            out["problems"].append(f"ledger entry {e.id} on {route.route_key} has an UNKNOWN allowance")
        else:
            counted.append((at, amount, basis))
        try:
            cost = money(e.cost_usd)
        except ValueError:
            cost = None
        if cost is not None and cost > ZERO:
            beyond_plan += cost  # a marginal charge on a subscription route: paid usage beyond the plan
    exhausted = False
    for name, length in ALLOWANCE_WINDOWS:
        since = current - length
        limit = (terms["allowance_usd_month"] * terms["shares"][name]).quantize(USD_Q)
        inside = [(amount, basis) for at, amount, basis in counted if at >= since]
        used = sum((a for a, _ in inside), ZERO)
        reported = sum((a for a, b in inside if b == "REPORTED"), ZERO)
        state = "EXHAUSTED" if used >= limit else "OK"
        exhausted = exhausted or state == "EXHAUSTED"
        out["windows"].append({
            "window": name, "share": float(terms["shares"][name]), "trailing_hours": int(length.total_seconds() // 3600),
            "since": since.isoformat(), "limit_usd": float(limit), "used_usd": float(used),
            "remaining_usd": float(max(ZERO, limit - used)), "reported_usd": float(reported),
            "estimated_usd": float(used - reported), "requests": len(inside), "state": state})
    out["beyond_plan_usd"] = float(beyond_plan)
    out["state"] = "UNKNOWN" if out["problems"] else ("EXHAUSTED" if exhausted else "OK")
    return out


def exhausted_reasons(status: dict[str, Any]) -> list[str]:
    """One line per used-up window of an ``allowance_status`` (ASCII; empty when nothing is used up)."""
    return [f"{status['route_key']}: {w['window']} window allowance used ${w['used_usd']:.6f} of "
            f"${w['limit_usd']:.6f} (trailing since {w['since']})"
            for w in status.get("windows") or [] if w.get("state") == "EXHAUSTED"]
