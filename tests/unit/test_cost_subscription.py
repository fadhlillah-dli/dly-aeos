"""Flat-fee SUBSCRIPTION routes in the cost and admission path (ADR-0080 amendment 2026-10-26; ADR-0041 decision A,
OpenCode Go). The plan fee is the owner's BUDGET decision; a model call costs the provider's marginal charge (0 within
the plan) and consumes plan ALLOWANCE, counted per provider window (5 hours 20%, week 50%, month 100% of the monthly
allowance; E1 Go docs read 2026-10-26) over the calls of THIS AEOS instance only. The 5h window blocks admission; the
week and month windows are advisory (WARN_EXHAUSTED). A call billed beyond the plan (a charge, a non-plan billing
source) refuses the route until the owner re-activates it. Never per-token spend, never the reservation charged as
made-up dollars. Real cost module, real ledger; upstreams are respx mocks."""

from __future__ import annotations

import json
import math
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from typer.testing import CliRunner

from aeos.broker import facade as broker
from aeos.cli import app
from aeos.core.errors import Blocked, PolicyDenied, ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.cost import facade, service, subscription
from aeos.models.audit import Event
from aeos.models.cost import UsageLedgerEntry
from aeos.models.decisions import Decision
from aeos.models.routing import ModelRoute
from aeos.routing.seed import GO_OPENAI_BASE, _go_pricing
from tests.unit.test_cost_fakes import install_fake_policy

GO = "opencode-go/glm-5.3-flash"
GO_CHAT = f"{GO_OPENAI_BASE}/chat/completions"
GO_KEY = "go-test-" + "s" * 40  # visibly fake
RATES = {"input": 0.15, "output": 0.50, "cached_read": 0.03}  # glm-5.3-flash allowance rates per 1M tokens
runner = CliRunner()


def _go_route(db, key: str = GO, *, status: str = "TRIAL", **pricing: Any) -> ModelRoute:
    """A seeded-shape Go route: $10/month plan, $60 monthly allowance (5h $12, week $30, month $60)."""
    r = ModelRoute(route_key=key, pool="WORKER", provider="opencode-go", model_id=key, status=status, is_free="NO",
                   endpoint=GO_OPENAI_BASE, eligible_domains=["PERSONAL"], allowed_data_classes=["PUBLIC", "INTERNAL"],
                   pricing={**_go_pricing(60, RATES), **pricing}, pricing_checked_at=utcnow(),
                   mode_params={"credential_ref": "workers/opencode-go", "upstream_model": key.split("/", 1)[1],
                                "upstream_api": "openai.chat_completions", "reasoning": "PROVIDER_DEFAULT"})
    db.add(r)
    db.commit()
    return r


def _use(db, *, hours_ago: float, allowance: str | None, key: str = GO, cost: float | None = 0.0,
         basis: str = "REPORTED", **kw: Any) -> UsageLedgerEntry:
    """A recorded model call on the route, *hours_ago* (LIVE, WORKER_API)."""
    details = {"billing": "SUBSCRIPTION", "allowance_usd": allowance, "allowance_basis": basis} if allowance else {}
    e = UsageLedgerEntry(at=utcnow() - timedelta(hours=hours_ago), route_key=key, provider="opencode-go",
                         category="WORKER_API", source="broker", requests=1, cost_usd=cost, charged_usd=cost,
                         details=details, mode="LIVE", **kw)
    db.add(e)
    db.commit()
    return e


def _req(run: str = "r1", usd: float = 0.0, tokens: int = 1000, key: str = GO) -> facade.ReservationRequest:
    return facade.ReservationRequest(run_id=run, project_id="p1", domain="PERSONAL", route_key=key, est_usd=usd,
                                     est_tokens=tokens, est_requests=1)


# ---------------------------------------------------------------------------------------------- terms and allowance


def test_seeded_go_pricing_is_trusted_subscription_terms_and_incomplete_terms_are_not(db):
    terms, problem = facade.subscription_terms(_go_route(db))
    assert problem is None and terms["plan"] == "OpenCode Go" and float(terms["monthly_fee_usd"]) == 10
    assert float(terms["allowance_usd_month"]) == 60 and {k: float(v) for k, v in terms["shares"].items()} == {
        "5h": 0.2, "week": 0.5, "month": 1.0}
    paid = ModelRoute(route_key="paid", pool="WORKER", provider="openrouter", model_id="p", is_free="NO",
                      pricing={"input_per_mtok": 1.0, "output_per_mtok": 2.0}, pricing_checked_at=utcnow())
    assert facade.subscription_terms(paid) == (None, None)  # a per-token route is not a subscription
    for key, broken in (("no-allowance", {"allowance_usd_month": None}), ("no-windows", {"allowance_windows": {}}),
                        ("bad-share", {"allowance_windows": {"5h": 0.2, "week": 0.5, "month": 1.5}}),
                        ("no-rates", {"allowance_rate_per_mtok": {}}), ("nan", {"allowance_usd_month": "NaN"})):
        terms, problem = facade.subscription_terms(_go_route(db, f"opencode-go/{key}", **broken))
        assert terms is None and problem, key
    free = _go_route(db, "opencode-go/free-flag")
    free.is_free = "YES"  # a "free" subscription is contradictory: refused, never treated as free
    assert facade.subscription_terms(free)[0] is None
    unchecked = _go_route(db, "opencode-go/unchecked")
    unchecked.pricing_checked_at = None
    assert "never checked" in facade.subscription_terms(unchecked)[1]


def test_allowance_is_reported_else_estimated_from_tokens_and_rounded_up(db):
    terms, _ = facade.subscription_terms(_go_route(db))
    amount, basis, how = subscription.allowance_of(terms, reported=0.0123, input_tokens=10, output_tokens=10)
    assert (float(amount), basis, how) == (0.0123, "REPORTED", None)
    amount, basis, how = subscription.allowance_of(terms, input_tokens=1_000_000, output_tokens=200_000)
    assert float(amount) == pytest.approx(0.15 + 0.10) and basis == "ESTIMATED" and how == "TOKENS_X_RATES"
    amount, _, _ = subscription.allowance_of(terms, input_tokens=1, output_tokens=0)  # 0.00000015 -> rounded UP
    assert float(amount) == 0.000001
    amount, basis, how = subscription.allowance_of(terms, charged_tokens=1000)  # no token counts: the hold's bound
    assert float(amount) == pytest.approx(0.0005) and how == "TOKEN_BOUND_X_HIGHEST_RATE"
    assert subscription.allowance_of(terms) == (None, "UNKNOWN", None)
    assert subscription.allowance_of(terms, reported=float("nan"), output_tokens=2)[1] == "ESTIMATED"


# ---------------------------------------------------------------------------------------------- ledger


def test_usage_records_marginal_cost_and_allowance_and_never_charges_the_reservation(db):
    _go_route(db)
    res = facade.reserve(db, _req())  # the default $0 GLOBAL hard stop admits a $0 marginal estimate
    db.commit()
    assert res.pricing_basis == "SUBSCRIPTION" and float(res.reserved_usd) == 0
    reported = facade.record_usage(db, provider="opencode-go", category="WORKER_API", source="broker", run_id="r1",
                                   route_key=GO, input_tokens=100, output_tokens=20, cost_usd=0.0,
                                   reservation_id=res.id, partial=True,
                                   details={"reported_allowance_usd": 0.00004, "billing_source": "go"})
    unreported = facade.record_usage(db, provider="opencode-go", category="WORKER_API", source="broker",
                                     run_id="r1", route_key=GO, input_tokens=1000, output_tokens=None,
                                     cost_usd=None, reservation_id=res.id, partial=True)
    db.commit()
    assert reported.details["allowance_usd"] == "0.000040" and reported.details["allowance_basis"] == "REPORTED"
    assert reported.details["billing_source"] == "go" and reported.details["billing"] == "SUBSCRIPTION"
    assert "reported_allowance_usd" not in reported.details and float(reported.charged_usd) == 0
    # no usage.cost: the marginal cost stays UNKNOWN and is charged $0 - never the reservation as made-up dollars
    assert unreported.cost_usd is None and float(unreported.charged_usd) == 0
    assert unreported.details["marginal_cost_unreported"] is True
    assert "cost_unknown_charged_reservation" not in unreported.details
    assert unreported.details["allowance_basis"] == "ESTIMATED" and unreported.details["allowance_usd"] == "0.000150"
    facade.release(db, res.id)
    db.commit()
    assert float(db.get(type(res), res.id).committed_usd) == 0
    view = service.ledger_view(reported)
    assert view["cost_kind"] == "SUBSCRIPTION" and view["allowance_usd"] == pytest.approx(0.00004)
    assert service.ledger_view(unreported)["cost_kind"] == "UNKNOWN"


def _reactivate(db, key: str = GO) -> Decision:
    """The owner's (re-)activation of the route: a human TRIAL decision recorded now."""
    d = Decision(kind="TRIAL", actor_user_id="owner", actor_kind="HUMAN", target_type="model_route", target_id=key,
                 target_hash="sha256:" + "0" * 64, domain="PERSONAL", rationale="owner re-activation after review",
                 allowed_operations=["route.use"])
    db.add(d)
    db.commit()
    return d


def test_a_marginal_charge_is_paid_usage_beyond_the_plan_and_counts_against_budgets(db):
    _go_route(db)
    e = facade.record_usage(db, provider="opencode-go", category="WORKER_API", source="broker", route_key=GO,
                            input_tokens=10, output_tokens=10, cost_usd=0.02,
                            details={"reported_allowance_usd": 0.0, "billing_source": "balance"})
    db.commit()
    assert e.details["beyond_plan_usd"] == "0.020000" and float(e.charged_usd) == pytest.approx(0.02)
    assert service.ledger_view(e)["cost_kind"] == "ACTUAL"
    ev = db.scalar(select(Event).where(Event.kind == "cost.subscription.beyond_plan"))
    assert ev is not None and ev.payload["route_key"] == GO and ev.payload["billing_source"] == "balance"
    with pytest.raises(Blocked) as ei:  # fail closed: the route is refused until the owner re-activates it
        facade.reserve(db, _req(run="r2"))
    db.rollback()
    assert ei.value.code == "SUBSCRIPTION_BEYOND_PLAN" and "$" not in " ".join(ei.value.details["reasons"])
    _reactivate(db)
    with pytest.raises(Blocked) as ei:  # $0.02 real spend under the $0 default hard stop: the next run still waits
        facade.reserve(db, _req(run="r3"))
    assert ei.value.code == "BUDGET_EXCEEDED"
    db.rollback()


@pytest.mark.parametrize("cost,source,amount", [
    (None, "balance", "UNKNOWN"),   # the pay-as-you-go balance paid it, no usage.cost reported
    (0.0, "Balance", "UNKNOWN"),    # case-insensitive; a $0 cost does not override a non-plan billing source
    (0.5, "go", "0.500000"),        # a marginal charge, whatever the source says
])
def test_provider_side_paid_fallback_refuses_the_route_until_the_owner_reactivates_it(db, cost, source, amount):
    """Invariant 8 (no silent paid fallback): a call billed beyond the plan on the provider side - a billing source
    outside the plan or a marginal charge - is flagged, emitted, and refuses the route for new admissions (fail closed)
    until a human TRIAL/APPROVED decision for the route is newer than the call."""
    route = _go_route(db)
    first = _reactivate(db)  # the activation in force when the call happens: older than the call
    e = facade.record_usage(db, provider="opencode-go", category="WORKER_API", source="broker", route_key=GO,
                            input_tokens=10, output_tokens=10, cost_usd=cost,
                            details={"reported_allowance_usd": 0.00001, "billing_source": source})
    db.commit()
    assert e.details["beyond_plan_usd"] == amount
    ev = db.scalar(select(Event).where(Event.kind == "cost.subscription.beyond_plan"))
    assert ev is not None and ev.payload["charged_usd"] == amount and ev.entity_id == e.id
    status = facade.allowance_status(db, route)
    assert status["state"] == "BEYOND_PLAN" and status["beyond_plan"]["entry_id"] == e.id
    assert status["beyond_plan"]["activated_at"] == subscription.ensure_aware(first.created_at).isoformat()
    code, reason = facade.subscription_admission_block(status)
    assert code == "SUBSCRIPTION_BEYOND_PLAN" and "re-activates" in reason and reason.isascii()
    assert facade.subscription_beyond_plan(db, route)["entry_id"] == e.id
    with pytest.raises(Blocked) as ei:
        facade.reserve(db, _req(usd=0.0))
    db.rollback()
    assert ei.value.code == "SUBSCRIPTION_BEYOND_PLAN"
    _reactivate(db)  # the owner checked Extra Usage and re-activated the route: newer than the flagged call
    assert facade.subscription_beyond_plan(db, route) is None
    assert facade.allowance_status(db, route)["state"] != "BEYOND_PLAN"


def test_calls_within_the_plan_are_never_flagged(db):
    route = _go_route(db)
    for source in ("go", "go-plus", None):
        facade.record_usage(db, provider="opencode-go", category="WORKER_API", source="broker", route_key=GO,
                            input_tokens=10, output_tokens=10, cost_usd=0.0,
                            details={"billing_source": source} if source else None)
    db.commit()
    assert db.scalar(select(Event).where(Event.kind == "cost.subscription.beyond_plan")) is None
    assert facade.allowance_status(db, route)["state"] == "OK"
    assert facade.reserve(db, _req()).pricing_basis == "SUBSCRIPTION"
    db.rollback()


def test_an_absurd_provider_figure_never_crashes_the_ledger(db):
    """money() turns every decimal arithmetic error into ValueError (1e25 does not fit the 6-dp context); the broker
    parser treats absurd figures as unreported, and a cost the provider stated but AEOS cannot read is paid usage
    beyond the plan on a subscription route."""
    from aeos.broker import model_proxy
    from aeos.cost.amounts import money

    for bad in (1e25, "1e25", Decimal("1e25"), 10 ** 30):
        with pytest.raises(ValueError):
            money(bad)
    assert subscription.allowance_of({"rates": {"input": Decimal("0.15"), "output": Decimal("0.5")}},
                                     reported=1e25, input_tokens=1_000_000)[1] == "ESTIMATED"
    data = {"usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 1e25,
                      "opencode": {"allowance": 1e25, "billing_source": "go"}}}
    assert model_proxy._usage_of(data)[2] is None
    assert model_proxy._billing_of(data) == {"cost_reported_unusable": True, "billing_source": "go"}
    assert model_proxy._billing_of({"usage": {"opencode": {"allowance": 2e9}}}) == {}  # above the guard: unreported
    assert model_proxy._billing_of({"usage": {"opencode": {"allowance": 0.25}}}) == {"reported_allowance_usd": 0.25}
    route = _go_route(db)
    e = facade.record_usage(db, provider="opencode-go", category="WORKER_API", source="broker", route_key=GO,
                            input_tokens=10, output_tokens=2, cost_usd=model_proxy._usage_of(data)[2],
                            details=model_proxy._billing_of(data))
    db.commit()
    assert e.cost_usd is None and e.details["allowance_basis"] == "ESTIMATED"
    assert e.details["beyond_plan_usd"] == "UNKNOWN" and facade.allowance_status(db, route)["state"] == "BEYOND_PLAN"
    # the cost module itself refuses an out-of-range amount as a validation error, never an arithmetic crash
    with pytest.raises(ValidationFailed):
        facade.record_usage(db, provider="opencode-go", category="WORKER_API", source="broker", route_key=GO,
                            cost_usd=1e25)
    db.rollback()


def test_per_token_paid_route_keeps_charging_the_reservation_when_cost_is_unknown(db, monkeypatch):
    install_fake_policy(monkeypatch)
    db.add(ModelRoute(route_key="paid-pro", pool="WORKER", provider="openrouter", model_id="pro", is_free="NO",
                      pricing={"input_per_mtok": 1.0}, pricing_checked_at=utcnow()))
    db.commit()
    service.set_budget(db, actor_user_id="owner-1", scope_type="GLOBAL", scope_id="*", period="MONTH", limit_usd=1.0,
                       rationale="owner budget for the test")
    db.commit()
    with pytest.raises(Blocked) as ei:  # a $0 estimate on a per-token route is still untrusted (COST_UNKNOWN gap)
        facade.reserve(db, _req(key="paid-pro"))
    assert ei.value.code == "BUDGET_STATE_UNTRUSTED"
    db.rollback()
    res = facade.reserve(db, _req(key="paid-pro", usd=0.5))
    e = facade.record_usage(db, provider="openrouter", category="WORKER_API", source="broker", run_id="r1",
                            route_key="paid-pro", input_tokens=1, output_tokens=1, reservation_id=res.id,
                            details={"reported_allowance_usd": 0.1})
    db.commit()
    assert float(e.charged_usd) == 0.5 and e.details["cost_unknown_charged_reservation"] is True
    assert "allowance_usd" not in e.details and e.details["reported_allowance_usd"] == 0.1


# ---------------------------------------------------------------------------------------------- admission


def test_admission_waits_when_the_5h_window_is_used_up(db):
    _go_route(db)
    _use(db, hours_ago=1, allowance="12")  # 20% of $60 within the trailing 5 hours
    status = facade.allowance_status(db, db.scalar(select(ModelRoute).where(ModelRoute.route_key == GO)))
    states = {w["window"]: (w["state"], w["blocking"]) for w in status["windows"]}
    assert status["state"] == "EXHAUSTED" and states == {
        "5h": ("EXHAUSTED", True), "week": ("OK", False), "month": ("OK", False)}
    assert status["counted_scope"] == "THIS_AEOS_INSTANCE" and "this AEOS instance" in status["basis"]
    with pytest.raises(Blocked) as ei:
        facade.reserve(db, _req())
    db.rollback()
    assert ei.value.code == "SUBSCRIPTION_ALLOWANCE_EXHAUSTED"
    (reason,) = ei.value.details["reasons"]
    assert "5h window" in reason and "$" not in reason  # no allowance figures in a reason (they span domains)


@pytest.mark.parametrize("hours_ago,allowance,window", [
    (6, "30", "week"),        # outside 5h, 50% within the trailing week
    (24 * 8, "60", "month"),  # outside the week, 100% within the trailing 31 days
])
def test_week_and_month_windows_are_advisory_and_never_block(db, hours_ago, allowance, window):
    """AEOS cannot know where the provider anchors its week/month windows, and with Extra Usage OFF exceeding them
    only yields provider refusals (no money): shown as WARN_EXHAUSTED, never a lock of up to 31 days."""
    _go_route(db)
    _use(db, hours_ago=hours_ago, allowance=allowance)
    status = facade.allowance_status(db, db.scalar(select(ModelRoute).where(ModelRoute.route_key == GO)))
    states = {w["window"]: w["state"] for w in status["windows"]}
    assert status["state"] == "WARN_EXHAUSTED" and states[window] == "WARN_EXHAUSTED" and states["5h"] == "OK"
    assert any(f"{window} window" in w and "advisory" in w for w in status["warnings"])
    assert facade.subscription_admission_block(status) is None
    assert facade.reserve(db, _req()).pricing_basis == "SUBSCRIPTION"  # admitted: the provider's limit decides
    db.rollback()


def test_window_lengths_are_trailing_5_hours_7_days_and_31_days(db):
    _go_route(db)
    sec = 1 / 3600
    for hours_ago, allowance in ((5 + sec, "1"), (24 * 7 + sec, "2"), (24 * 31 + sec, "4"), (24 * 31 - 0.01, "8")):
        _use(db, hours_ago=hours_ago, allowance=allowance)
    status = facade.allowance_status(db, db.scalar(select(ModelRoute).where(ModelRoute.route_key == GO)))
    used = {w["window"]: w["used_usd"] for w in status["windows"]}
    assert used == {"5h": 0.0, "week": 1.0, "month": 11.0}  # 31 days + 1 s is outside the month window


def test_admission_counts_reported_and_estimated_allowance_and_admits_below_the_limit(db):
    _go_route(db)
    _use(db, hours_ago=1, allowance="11.5")
    _use(db, hours_ago=2, allowance="0.4", basis="ESTIMATED")
    _use(db, hours_ago=3, allowance=None, cost=None, input_tokens=100_000, output_tokens=100_000)  # estimated now
    status = facade.allowance_status(db, db.scalar(select(ModelRoute).where(ModelRoute.route_key == GO)))
    five = status["windows"][0]
    assert five["window"] == "5h" and five["limit_usd"] == 12.0 and five["requests"] == 3
    assert five["used_usd"] == pytest.approx(11.5 + 0.4 + 0.065) and five["reported_usd"] == pytest.approx(11.5)
    assert five["state"] == "OK" and status["state"] == "OK" and status["beyond_plan_usd"] == 0
    assert facade.reserve(db, _req()).pricing_basis == "SUBSCRIPTION"  # $11.965 of $12: still admitted
    db.rollback()
    _use(db, hours_ago=0.5, allowance="0.035")  # exactly $12.000: used up
    with pytest.raises(Blocked) as ei:
        facade.reserve(db, _req(run="r2"))
    db.rollback()
    assert ei.value.code == "SUBSCRIPTION_ALLOWANCE_EXHAUSTED"


def test_an_uncountable_call_blocks_only_the_5h_window_and_at_most_5_hours(db):
    """Truly nothing known (no allowance, no tokens, no bound): UNKNOWN blocks the 5h window until the call leaves it
    (at most 5 hours, fail closed); in the advisory week/month windows it is a warning - never a 31-day lock."""
    route = _go_route(db)
    e = _use(db, hours_ago=1, allowance=None, cost=None)
    status = facade.allowance_status(db, route)
    five = status["windows"][0]
    assert status["state"] == "UNKNOWN" and five["state"] == "UNKNOWN" and five["unknown_requests"] == 1
    assert five["blocked_until"] == (subscription.ensure_aware(e.at) + timedelta(hours=5)).isoformat()
    with pytest.raises(Blocked) as ei:
        facade.reserve(db, _req())
    db.rollback()
    assert ei.value.code == "SUBSCRIPTION_ALLOWANCE_UNKNOWN" and "at most 5 hours" in ei.value.details["reasons"][0]
    e.at = utcnow() - timedelta(hours=6)  # the call left the 5h window: advisory only
    db.commit()
    status = facade.allowance_status(db, route)
    assert status["state"] == "OK" and status["windows"][1]["unknown_requests"] == 1
    assert any("UNKNOWN allowance" in w and "advisory" in w for w in status["warnings"])
    assert facade.reserve(db, _req(run="r2")).pricing_basis == "SUBSCRIPTION"
    db.rollback()


def test_a_call_without_usage_on_the_settled_path_records_an_upper_bound_estimate(db):
    """No reservation (the hold was already settled) and no usage reported: the broker's bound (clamped max_tokens +
    prompt size) makes the allowance ESTIMATED, never UNKNOWN - no lock-out from an ordinary late answer."""
    from aeos.broker import model_proxy

    route = _go_route(db)
    body = {"model": "glm-5.3-flash", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 256}
    bound = model_proxy.request_token_bound(body, token_cap=10_000)
    assert bound == 256 + len(json.dumps(body).encode("utf-8")) + model_proxy.TEMPLATE_TOKEN_MARGIN
    assert model_proxy.request_token_bound({"messages": []}, token_cap=10_000) == 10_000  # no max_tokens: the cap
    assert model_proxy.request_token_bound({"messages": []}) is None  # truly nothing bounds it
    e = facade.record_usage(db, provider="opencode-go", category="WORKER_API", source="broker", route_key=GO,
                            cost_usd=None, details={"charged_tokens": bound})
    db.commit()
    assert e.details["allowance_basis"] == "ESTIMATED"
    assert e.details["allowance_estimate_from"] == "TOKEN_BOUND_X_HIGHEST_RATE"
    assert facade.allowance_status(db, route)["state"] == "OK"
    # on the hold path the caller's per-call bound wins over the hold's remainder (a tighter upper bound)
    res = facade.reserve(db, _req(run="r2", tokens=500_000))
    db.commit()
    p = facade.record_usage(db, provider="opencode-go", category="WORKER_API", source="broker", run_id="r2",
                            route_key=GO, reservation_id=res.id, partial=True, details={"charged_tokens": bound})
    db.commit()
    assert p.details["charged_tokens"] == 500_000 and p.details["allowance_token_bound"] == bound
    assert float(p.details["allowance_usd"]) == pytest.approx(math.ceil(bound * 0.5) / 1_000_000)


def test_incomplete_terms_refuse_admission(db):
    _go_route(db, "opencode-go/broken", allowance_rate_per_mtok={})
    with pytest.raises(Blocked) as ei:
        facade.reserve(db, _req(key="opencode-go/broken"))
    db.rollback()
    assert ei.value.code == "BUDGET_STATE_UNTRUSTED" and any("allowance rates" in r for r in ei.value.details["reasons"])


# ---------------------------------------------------------------------------------------------- views and CLI


def test_cost_summary_shows_plan_and_allowance_per_window_never_per_token_spend(db, settings):
    _go_route(db)
    _go_route(db, "opencode-go/kimi-k2.7-code", status="CANDIDATE")  # a catalog row without usage: not listed
    _use(db, hours_ago=1, allowance="1.5", domain="PERSONAL", input_tokens=10, output_tokens=5)
    data = service.summary(db, domain="PERSONAL", allowed_project_ids=None, cross_domain_visible=True)
    (sub,) = data["subscriptions"]
    assert sub["route_key"] == GO and sub["plan"] == "OpenCode Go" and sub["monthly_fee_usd"] == 10
    assert [(w["window"], w["limit_usd"], w["used_usd"]) for w in sub["windows"]] == [
        ("5h", 12.0, 1.5), ("week", 30.0, 1.5), ("month", 60.0, 1.5)]
    route = next(r for r in data["by_route"] if r["route_key"] == GO)
    assert route["billing"] == "SUBSCRIPTION" and route["allowance_usd"] == 1.5 and route["actual_usd"] == 0
    hidden = service.summary(db, domain="PERSONAL", allowed_project_ids=None, cross_domain_visible=False)
    assert hidden["subscriptions"][0]["windows"] is None and hidden["subscriptions"][0]["spend_hidden"] is True
    out = runner.invoke(app, ["cost", "summary", "--domain", "PERSONAL"])
    assert out.exit_code == 0, out.output
    assert "Worker subscriptions (plan fee + allowance per window; not per-token spend):" in out.output
    assert f"{GO}  plan=OpenCode Go $10.0000/month  allowance=$60.0000/month  state=OK" in out.output
    assert "    5h    used $1.5000 of $12.0000 (remaining $10.5000; reported $1.5000, estimated $0.0000; " \
           "1 request(s)) OK" in out.output
    assert "    week  used $1.5000 of $30.0000 (remaining $28.5000; reported $1.5000, estimated $0.0000; " \
           "1 request(s)) OK (advisory)" in out.output
    assert "counted: calls made through this AEOS instance only" in out.output
    assert "kimi-k2.7-code" not in out.output and out.output.isascii()


def test_doctor_route_registry_reports_a_subscription_neither_as_free_nor_as_unpriced(settings, db):
    from aeos.doctor.checks import REGISTRY, load_all
    from aeos.doctor.runner import make_context, run_check
    from tests.unit.test_inventory_fakes import base_runner

    _go_route(db, status="CANDIDATE")
    _go_route(db, "opencode-go/broken", status="CANDIDATE", allowance_usd_month=None)
    db.add(ModelRoute(route_key="cheap", pool="WORKER", provider="openrouter", model_id="a:free", is_free="YES"))
    db.commit()
    load_all()
    res = run_check(REGISTRY["route.registry"], make_context(settings, db, runner=base_runner()))
    assert res.status == "MISSING" and res.requires_paid_write is True  # the plan fee is a BUDGET decision
    assert res.observed_evidence["subscription_candidates"] == [GO]
    assert res.observed_evidence["paid_or_unknown_price_candidates"] == ["opencode-go/broken"]  # incomplete terms: unpriced


# ---------------------------------------------------------------------------------------------- feasibility probe


def _activate(db, route: ModelRoute) -> None:
    """The owner's TRIAL decision bound to the route's hash, with a BUDGET decision covering it."""
    from aeos.routing.service import compute_config_hash

    budget = Decision(kind="BUDGET", actor_user_id="owner", actor_kind="HUMAN", target_type="model_budget",
                      target_id=route.route_key, domain="PERSONAL", rationale="plan fee for the trial",
                      auth_scope={"routes": [route.route_key]}, allowed_operations=["budget.set"])
    db.add(budget)
    db.flush()
    route.config_hash = compute_config_hash(route)
    db.add(Decision(kind="TRIAL", actor_user_id="owner", actor_kind="HUMAN", target_type="model_route",
                    target_id=route.route_key, target_hash=route.config_hash, domain="PERSONAL",
                    rationale="owner trial after reading terms", allowed_operations=["route.use"],
                    details={"budget_decision_id": budget.id}))
    db.commit()


@pytest.fixture
def keys(secrets_memory):
    secrets_memory.put("workers/opencode-go", GO_KEY, actor="test")
    secrets_memory.put("workers/openrouter", "sk-or-v1-" + "q" * 48, actor="test")


def test_probe_runs_on_a_subscription_route_and_is_refused_on_a_per_token_paid_route(db, keys):
    _activate(db, _go_route(db))
    usage = {"prompt_tokens": 12, "completion_tokens": 1, "cost": 0, "opencode": {"allowance": 0.000006,
                                                                                  "billing_source": "go"}}
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(GO_CHAT).mock(return_value=httpx.Response(200, json={
            "model": "glm-5.3-flash", "choices": [{"message": {"content": "OK"}}], "usage": usage}))
        out = broker.probe_route(db, route_key=GO, actor="user:owner")
        db.commit()
        assert len(mock.calls) == 1
    assert out["outcome"] == "REACHABLE" and out["billing"]["allowance_basis"] == "REPORTED"
    assert GO_KEY not in json.dumps(out)
    paid = ModelRoute(route_key="worker.paid-pro", pool="WORKER", provider="openrouter", model_id="vendor/pro",
                      status="TRIAL", is_free="NO", eligible_domains=["PERSONAL"], allowed_data_classes=["PUBLIC"],
                      pricing={"input_per_mtok": 1.0, "output_per_mtok": 2.0}, pricing_checked_at=utcnow(),
                      mode_params={"credential_ref": "workers/openrouter"})
    db.add(paid)
    db.commit()
    _activate(db, paid)
    with respx.mock(assert_all_mocked=True) as mock:
        with pytest.raises(PolicyDenied) as ei:
            broker.probe_route(db, route_key="worker.paid-pro", actor="user:owner")
        assert not mock.calls  # a per-token paid route: no budget path for a probe
    assert ei.value.code == "ROUTE_NOT_FREE"


def test_probe_on_a_used_up_subscription_window_is_refused_before_any_call(db, keys):
    _activate(db, _go_route(db))
    _use(db, hours_ago=1, allowance="12")
    with respx.mock(assert_all_mocked=True) as mock:
        with pytest.raises(PolicyDenied) as ei:
            broker.probe_route(db, route_key=GO, actor="user:owner")
        assert not mock.calls
    assert ei.value.code == "SUBSCRIPTION_ALLOWANCE_EXHAUSTED" and "5h" in ei.value.message


def test_probe_is_refused_after_a_call_billed_beyond_the_plan_and_flags_its_own(db, keys):
    """The probe shares the beyond-plan refusal (fail closed) and flags a probe the provider billed beyond the plan."""
    route = _go_route(db)
    _activate(db, route)
    usage = {"prompt_tokens": 12, "completion_tokens": 1, "opencode": {"allowance": 0.000006,
                                                                       "billing_source": "balance"}}
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(GO_CHAT).mock(return_value=httpx.Response(200, json={
            "model": "glm-5.3-flash", "choices": [{"message": {"content": "OK"}}], "usage": usage}))
        out = broker.probe_route(db, route_key=GO, actor="user:owner")
        db.commit()
    assert out["billing"]["beyond_plan_usd"] == "UNKNOWN" and "SUBSCRIPTION_BEYOND_PLAN" in out["note"]
    with respx.mock(assert_all_mocked=True) as mock:
        with pytest.raises(PolicyDenied) as ei:
            broker.probe_route(db, route_key=GO, actor="user:owner")
        assert not mock.calls
    assert ei.value.code == "SUBSCRIPTION_BEYOND_PLAN"
    out = runner.invoke(app, ["cost", "summary", "--domain", "PERSONAL"])
    assert out.exit_code == 0 and "REFUSED (SUBSCRIPTION_BEYOND_PLAN)" in out.output and out.output.isascii()
