"""Owner request R34 (2026-10-28): the read-only usage value report (``aeos usage report``, GET /api/usage/report).

A seeded temporary database with fakes only: Go subscription routes, a paid and a free OpenRouter route, worker runs,
ledger rows, events and a ccusage export file. Checks the figures, PERSONAL scope (OFFICE rows change nothing, an
OFFICE session is refused), that the report writes nothing and calls no network, and that no task text, error text,
event payload or ledger detail reaches its output.
"""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import event, select
from typer.testing import CliRunner

from aeos.core.enums import Domain
from aeos.core.errors import ValidationFailed
from aeos.cost import usage_report
from aeos.models.audit import Event
from aeos.models.broker_ext import BrokerRequest
from aeos.models.cost import UsageLedgerEntry
from aeos.models.routing import ModelRoute
from aeos.models.workforce import WorkerRun
from tests.unit.test_workforce_support import make_plan, make_project

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
MARKER = "PRIVATE-TEXT-MARKER-R34"  # stands for task text, an error or a payload; must never be output
GO_A, GO_B = "opencode-go/test-a", "opencode-go/test-b"
PAID, FREE = "openrouter/vendor/paid-model", "openrouter/vendor/free-model:free"


def _go(key: str, allowance: float) -> ModelRoute:
    return ModelRoute(
        route_key=key, pool="WORKER", provider="opencode-go", model_id=key, is_free="NO", status="TRIAL",
        pricing_checked_at=NOW - timedelta(days=30),
        pricing={"billing": "SUBSCRIPTION", "plan": "OpenCode Go", "monthly_fee_usd": 10, "currency": "USD",
                 "allowance_usd_month": allowance, "allowance_windows": {"5h": 0.2, "week": 0.5, "month": 1.0},
                 "allowance_rate_per_mtok": {"input": 1.0, "output": 2.0}})


def _ledger(db, *, route: str, at: datetime, domain: str | None = "PERSONAL", allowance: str | None = None,
            basis: str = "REPORTED", cost: str | None = "0", run_id: str | None = None, project_id: str | None = None,
            provider: str = "opencode-go", category: str = "WORKER_API", requests: int = 1) -> None:
    details: dict[str, Any] = {"note": MARKER}
    if allowance is not None:
        details |= {"billing": "SUBSCRIPTION", "allowance_usd": allowance, "allowance_basis": basis}
    db.add(UsageLedgerEntry(at=at, route_key=route, provider=provider, category=category, domain=domain,
                            run_id=run_id, project_id=project_id, requests=requests, input_tokens=100,
                            output_tokens=50, cost_usd=None if cost is None else Decimal(cost), source="broker",
                            details=details, mode="LIVE"))


@pytest.fixture
def seeded(db, tmp_path):
    db.add_all([_go(GO_A, 60), _go(GO_B, 30),
                ModelRoute(route_key=PAID, pool="WORKER", provider="openrouter", model_id="vendor/paid-model",
                           is_free="NO", status="TRIAL"),
                ModelRoute(route_key=FREE, pool="WORKER", provider="openrouter", model_id="vendor/free-model:free",
                           is_free="YES", status="TRIAL")])
    personal, office = make_project(db), make_project(db, domain="OFFICE")
    plan, _, specs = make_plan(db, personal, [{"task_key": "A", "goal": MARKER}, {"task_key": "B", "goal": MARKER}])
    _, _, ospecs = make_plan(db, office, [{"task_key": "O"}])

    def run(spec_id, project, domain, role, route, verdict, minutes):
        r = WorkerRun(task_id=spec_id, project_id=project.id, domain=domain, role_key=role, model_route_key=route,
                      verifier_status=verdict, started_at=NOW - timedelta(hours=2),
                      finished_at=None if minutes is None else NOW - timedelta(hours=2) + timedelta(minutes=minutes),
                      error=MARKER, block_reason=MARKER, current_action=MARKER)
        db.add(r)
        db.flush()
        r.created_at = NOW - timedelta(hours=3)
        return r

    r1 = run(specs[0].id, personal, "PERSONAL", "coder", GO_A, "PASS", 10)
    run(specs[0].id, personal, "PERSONAL", "coder", GO_A, "FAIL", 30)
    r3 = run(specs[1].id, personal, "PERSONAL", "writer", PAID, "NOT_RUN", None)
    run(ospecs[0].id, office, "OFFICE", "coder", GO_A, "PASS", 5)
    _ledger(db, route=GO_A, at=NOW - timedelta(hours=1), allowance="3", run_id=r1.id, project_id=personal.id)
    _ledger(db, route=GO_A, at=NOW - timedelta(days=3), allowance="6", basis="ESTIMATED")
    _ledger(db, route=GO_A, at=datetime(2026, 10, 2, tzinfo=UTC), allowance="9")
    _ledger(db, route=GO_A, at=datetime(2026, 9, 29, tzinfo=UTC), allowance="40")  # last month: never counted
    _ledger(db, route=PAID, at=NOW - timedelta(hours=1), cost="0.12", run_id=r3.id, project_id=personal.id,
            provider="openrouter")
    _ledger(db, route=PAID, at=NOW - timedelta(hours=1), cost=None, run_id=r3.id, project_id=personal.id,
            provider="openrouter")
    _ledger(db, route="claude-native/opus", at=NOW - timedelta(hours=1), provider="claude-native",
            category="NATIVE_CLAUDE", cost=None, requests=2)
    db.add(BrokerRequest(grant_id="g", run_id=r3.id, project_id=personal.id, domain="PERSONAL", route_key=FREE,
                         provider="openrouter", upstream_model="vendor/free-model:free", outcome="OK",
                         error_summary=MARKER, created_at=NOW - timedelta(minutes=30)))
    db.add(Event(at=NOW - timedelta(hours=1), source="scheduler", kind="routing.paid.escalation", domain="PERSONAL",
                 run_id=r3.id, payload={"text": MARKER}))
    db.add(Event(at=NOW - timedelta(hours=1), source="scheduler", kind="routing.paid.escalation", domain="PERSONAL",
                 payload={"text": MARKER}))
    db.commit()
    export = tmp_path / "ccusage.json"
    export.write_text(json.dumps({
        "daily": [{"date": "2026-10-01", "inputTokens": 1000, "outputTokens": 200, "totalTokens": 1200,
                   "totalCost": 9.99, "modelsUsed": [MARKER]},
                  {"date": "2026-09-30", "inputTokens": 5, "outputTokens": 5, "totalTokens": 10}],
        "totals": {"inputTokens": 1005, "outputTokens": 205, "totalTokens": 1210, "totalCost": 9.99}}),
        encoding="utf-8")
    return {"plan": plan, "personal": personal, "office": office, "ospecs": ospecs, "export": export}


def _report(db, seeded, **kw: Any) -> dict[str, Any]:
    kw.setdefault("ccusage_path", seeded["export"])
    return usage_report.build_report(db, month="2026-10", now=NOW, **kw)


def _route(rep: dict[str, Any], key: str) -> dict[str, Any]:
    return next(r for r in rep["opencode_go"]["routes"] if r["route_key"] == key)


def test_go_allowance_per_window_and_never_used(db, seeded):
    rep = _report(db, seeded)
    windows = {w["window"]: w for w in _route(rep, GO_A)["windows"]}
    assert (windows["5h"]["used_usd"], windows["5h"]["limit_usd"], windows["5h"]["used_pct"]) == (3.0, 12.0, 25.0)
    assert (windows["week"]["used_usd"], windows["week"]["limit_usd"], windows["week"]["used_pct"]) == (9.0, 30.0, 30.0)
    assert (windows["month"]["used_usd"], windows["month"]["used_pct"], windows["month"]["requests"]) == (18.0, 30.0, 3)
    assert windows["week"]["reported_usd"] == 3.0 and windows["week"]["estimated_usd"] == 6.0
    totals = rep["opencode_go"]["totals"]
    assert (totals["allowance_usd_month"], totals["used_usd_month"], totals["used_pct"]) == (90.0, 18.0, 20.0)
    assert totals["plans"] == [{"plan": "OpenCode Go", "monthly_fee_usd": 10.0}]
    assert totals["allowance_used_per_fee_dollar"] == 1.8
    assert [n["route_key"] for n in rep["opencode_go"]["never_used_this_month"]] == [GO_B]


def test_past_month_has_no_trailing_windows(db, seeded):
    rep = usage_report.build_report(db, month="2026-09", now=NOW, use_settings=False)
    windows = _route(rep, GO_A)["windows"]
    assert [w["window"] for w in windows] == ["month"] and windows[0]["used_usd"] == 40.0
    assert rep["scope"]["current_month"] is False


def test_openrouter_free_count_paid_spend_and_budget(db, seeded):
    o = _report(db, seeded)["openrouter"]
    assert o["free"]["requests_today_utc"] == 1 and o["free"]["daily_limit"] >= 1
    assert o["paid_by_route"] == [{"route_key": PAID, "actual_usd": 0.12, "estimate_usd": 0.0,
                                   "unknown_cost_entries": 1, "requests": 2}]
    assert o["paid_by_role"][0]["role_key"] == "writer" and o["paid_by_role"][0]["unknown_cost_entries"] == 1
    glob = next(b for b in o["budgets"] if b["scope_type"] == "GLOBAL")
    assert glob["limit_usd"] == 0.0 and glob["is_default"] is True
    assert o["paid_admission"].startswith("NONE")


def test_work_pass_rate_escalations_and_plan_wall_clock(db, seeded):
    w = _report(db, seeded)["work"]
    assert {r["role_key"]: (r["tasks"], r["runs"]) for r in w["tasks_per_role"]} == {"coder": (1, 2),
                                                                                     "writer": (1, 1)}
    coder = next(p for p in w["pass_rate"] if p["role_key"] == "coder")
    assert (coder["route_key"], coder["passed"], coder["not_passed"], coder["pass_rate_pct"]) == (GO_A, 1, 1, 50.0)
    writer = next(p for p in w["pass_rate"] if p["role_key"] == "writer")
    assert writer["pass_rate_pct"] is None and writer["undecided"] == 1  # NOT_RUN is never a pass or a fail
    assert w["paid_admissions"]["escalations"] == 2 and w["paid_admissions"]["status"] == "EVENTS_FOUND"
    (plan,) = w["plans"]
    assert plan["plan_id"] == seeded["plan"].id and plan["median_run_wall_clock_s"] == 1200.0
    assert plan["plan_wall_clock_s"] == 1800.0 and plan["finished_runs"] == 2


def test_absent_paid_admission_events_are_reported_not_failed(db, seeded):
    w = usage_report.build_report(db, month="2026-08", now=NOW, use_settings=False)["work"]
    assert w["paid_admissions"]["status"] == "NO_EVENTS" and w["paid_admissions"]["escalations"] == 0


def test_claude_statement_and_ccusage_export(db, seeded):
    c = _report(db, seeded)["claude"]
    assert "measured by ccusage outside AEOS" in c["statement"]
    assert c["aeos_recorded"]["requests"] == 2
    cc = c["ccusage_export"]
    assert cc["status"] == "ESTIMATE" and cc["days_in_month"] == 1
    assert cc["month_tokens"] == {"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200}
    assert "9.99" not in json.dumps(c)  # ccusage's API-equivalent dollars are never shown (§42)
    unset = usage_report.build_report(db, month="2026-10", now=NOW, use_settings=False)["claude"]["ccusage_export"]
    assert unset["status"] == "NOT_CONFIGURED"
    bad = seeded["export"].with_name("bad.json")
    bad.write_text("not json", encoding="utf-8")
    failed = usage_report.build_report(db, month="2026-10", now=NOW, ccusage_path=bad)["claude"]["ccusage_export"]
    assert failed["status"] == "FAILED" and "not ccusage daily JSON" in failed["reason"]


def test_office_records_never_change_the_personal_report(db, seeded):
    """LESSONS L12: OFFICE ledger rows, runs and events on the same routes leave the PERSONAL report unchanged."""
    before = _report(db, seeded)
    hidden_before = _report(db, seeded, cross_domain_visible=False)
    office = seeded["office"]
    orun = db.scalars(select(WorkerRun).where(WorkerRun.domain == "OFFICE")).one()
    _ledger(db, route=GO_A, at=NOW - timedelta(hours=1), domain="OFFICE", allowance="11", run_id=orun.id,
            project_id=office.id)
    _ledger(db, route=PAID, at=NOW - timedelta(hours=1), domain="OFFICE", cost="5", run_id=orun.id,
            project_id=office.id, provider="openrouter")
    # an entry without a domain but tied to an OFFICE run is still OFFICE's
    _ledger(db, route=PAID, at=NOW - timedelta(hours=1), domain=None, cost="7", run_id=orun.id,
            provider="openrouter")
    db.add(Event(at=NOW - timedelta(hours=1), source="scheduler", kind="routing.paid.overflow", domain="OFFICE"))
    db.add(WorkerRun(task_id=seeded["ospecs"][0].id, project_id=office.id, domain="OFFICE", role_key="writer",
                     model_route_key=PAID, verifier_status="FAIL", created_at=NOW - timedelta(hours=1)))
    db.commit()
    after = _report(db, seeded)
    hidden_after = _report(db, seeded, cross_domain_visible=False)
    for rep in (before, after, hidden_before, hidden_after):
        rep.pop("checked_at")
    assert hidden_after == hidden_before  # a principal limited to PERSONAL sees nothing of OFFICE
    # The GLOBAL budget governs every domain: only its spend figures move, and they are labelled as spanning domains.
    glob_before, glob_after = (next(b for b in r["openrouter"]["budgets"] if b["scope_type"] == "GLOBAL")
                               for r in (before, after))
    assert (glob_before["spent_usd"], glob_after["spent_usd"]) == (0.12, 12.12)
    assert "every domain" in after["openrouter"]["budgets_scope"]
    for r in (before, after):
        for b in r["openrouter"]["budgets"]:
            for k in ("spent_usd", "held_usd", "headroom_usd", "state"):
                b.pop(k)
    assert after == before


def test_report_writes_nothing_and_calls_no_network(db, seeded, monkeypatch):
    statements: list[str] = []

    def capture(_conn, _cursor, statement, *_args):
        statements.append(statement.lstrip().split(" ", 1)[0].upper())

    def no_network(*_a, **_k):
        raise AssertionError("the usage report must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        _report(db, seeded)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert statements and set(statements) <= {"SELECT", "WITH", "PRAGMA"}, sorted(set(statements))


def test_no_task_text_errors_payloads_or_details_in_the_output(db, seeded):
    rep = _report(db, seeded)
    text = "\n".join(usage_report.render_text(rep))
    assert MARKER not in json.dumps(rep) and MARKER not in text
    assert text.isascii()
    assert "opencode-go/test-a [TRIAL] 3 req: 5h $3.00/$12.00 25.0%" in text


def test_bad_month_is_refused_with_its_message_and_a_valid_one_passes(db, seeded):
    with pytest.raises(ValidationFailed, match="month must be YYYY-MM") as exc:
        usage_report.build_report(db, month="2026-13", now=NOW, use_settings=False)
    assert exc.value.code == "USAGE_REPORT_BAD_MONTH"
    assert usage_report.build_report(db, month="2026-12", now=NOW, use_settings=False)["scope"]["month"] == "2026-12"


def test_api_refuses_an_office_session_and_serves_personal(personal_client, db, seeded):
    ok = personal_client.get("/api/usage/report", params={"month": "2026-10"})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["source"] and body["checked_at"] and body["scope"]["domain"] == "PERSONAL"
    assert MARKER not in ok.text
    personal_client.select_domain(Domain.OFFICE)
    refused = personal_client.get("/api/usage/report")
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "USAGE_REPORT_PERSONAL_ONLY"
    assert "PERSONAL domain only" in refused.json()["error"]["message"]


def test_cli_report_json_and_text(db, seeded, monkeypatch):
    from aeos.cli import app
    from aeos.core.config import reset_settings_cache

    monkeypatch.setenv("AEOS_CCUSAGE_EXPORT_PATH", str(seeded["export"]))
    reset_settings_cache()
    runner = CliRunner()
    res = runner.invoke(app, ["usage", "report", "--month", "2026-10", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["scope"]["month"] == "2026-10" and data["claude"]["ccusage_export"]["status"] == "ESTIMATE"
    assert MARKER not in res.output
    text = runner.invoke(app, ["usage", "report", "--month", "2026-10"])
    assert text.exit_code == 0 and "OpenCode Go" in text.output and text.output.isascii()
    bad = runner.invoke(app, ["usage", "report", "--month", "26-10"])
    assert bad.exit_code == 2 and "month must be YYYY-MM" in bad.output


def test_seeded_go_worker_routes_show_as_never_used(db):
    from aeos.routing import seed

    seed.run(db)
    db.commit()
    rep = usage_report.build_report(db, now=NOW, use_settings=False)
    never = {n["route_key"] for n in rep["opencode_go"]["never_used_this_month"]}
    seeded_go = {r.route_key for r in db.scalars(select(ModelRoute).where(ModelRoute.provider == "opencode-go",
                                                                          ModelRoute.pool == "WORKER"))}
    assert seeded_go and never == seeded_go
    assert rep["opencode_go"]["totals"]["used_usd_month"] == 0.0
