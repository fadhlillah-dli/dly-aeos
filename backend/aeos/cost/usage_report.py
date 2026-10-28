"""Usage value report (owner request R34, 2026-10-28): are the two $10 plans used well? Read-only, PERSONAL scope.

The owner asked "Lakukan yang terbaik; saya tidak ingin hanya membuang $10 di OpenRouter dan $10 di OpenCode". This
report answers that from records AEOS already keeps; it decides nothing, changes nothing and calls nothing:

* **OpenCode Go** (``pricing.billing`` SUBSCRIPTION routes): per route, the allowance this AEOS instance consumed in the
  current trailing 5h window, the trailing week and the selected calendar month (UTC), against the seeded monthly
  allowance (window limit = allowance x the window's share), in percent. Per call: the recorded
  ``usage.opencode.allowance`` (REPORTED) or the cost module's estimate (``aeos.cost.subscription.entry_allowance``).
  Totals, the plan fee once per plan, and the WORKER routes never used in the month.
* **OpenRouter**: the ``:free`` requests counted today (``aeos.routing.facade.free_tier_status``, account-wide), paid
  spend per route and per role from the usage ledger (actual / estimate / UNKNOWN kept apart), the owner BUDGET rows
  that govern it (GLOBAL and ``openrouter/*`` ROUTE budgets: limit, spent, held, headroom) and the key limit the
  OpenRouter key endpoint last reported.
* **Work**: tasks and runs per role, the verifier pass rate per (route, role), escalation and overflow events (emitted
  by the paid-admission change once it is merged; their absence is reported, not treated as zero use of a feature),
  and the median wall-clock of the finished runs per plan.
* **Claude**: Claude Max token use is measured by ccusage outside AEOS. When ``ccusage_export_path`` names a
  ``ccusage daily --json`` export, its token totals are shown (never a dollar figure, §42); AEOS's own NATIVE_CLAUDE
  ledger rows are counted beside it.

Scope: PERSONAL only. Ledger rows, runs and events of OFFICE never enter it (``service._visible_ledger`` and explicit
domain filters). Figures that span domains (GLOBAL/ROUTE budget spend, the OpenRouter key limit, the ccusage export)
follow the GLOBAL/ROUTE visibility rule: hidden from a principal who cannot see every domain. Only route keys, role
keys, plan ids, counts and amounts are output: no prompt, task text, error text, event payload or secret.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aeos.core.enums import DataMode, Domain
from aeos.core.errors import ValidationFailed
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.cost import subscription as subs
from aeos.cost.amounts import ZERO, money
from aeos.cost.ccusage import parse_daily
from aeos.models.audit import Event
from aeos.models.cost import QuotaState, UsageLedgerEntry
from aeos.models.planning import PlanVersion, TaskSpec
from aeos.models.routing import ModelRoute
from aeos.models.workforce import WorkerRun

SCOPE_DOMAIN = Domain.PERSONAL.value
SOURCE = ("usage_ledger (broker/provider), model_routes pricing, budgets + BUDGET decisions, broker request count, "
          "worker_runs, events, optional ccusage export")
LIVE = DataMode.LIVE.value
MODEL_CALL_CATEGORY = subs.MODEL_CALL_CATEGORY
NATIVE_CATEGORY = "NATIVE_CLAUDE"
OPENROUTER = "openrouter"
MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
TRAILING = {"5h": timedelta(hours=5), "week": timedelta(days=7)}
PASS_STATUSES = frozenset({"PASS", "PASSED"})
# A verifier verdict that is not a pass (NOT_PROVEN and BLOCKED are never rendered as pass, invariant 7).
NOT_PASS_STATUSES = frozenset({"FAIL", "FAILED", "QUARANTINED", "NOT_PROVEN", "BLOCKED", "ERROR"})
# Event kinds of the paid-admission branch (owner R34: escalation after two failed $0-marginal attempts, overflow when
# every $0-marginal route of the role is at capacity). Matched by marker so the report needs no change when it lands.
ESCALATION_MARKER = "escalat"
OVERFLOW_MARKER = "overflow"
CCUSAGE_MAX_BYTES = 5 * 1024 * 1024
HIDDEN = "Angka ini mencakup domain lain yang tidak dapat Anda lihat."
CLAUDE_STATEMENT = ("Claude Max token use is measured by ccusage outside AEOS (local Claude Code logs). AEOS never "
                    "converts it to dollars or to a share of the Max quota (master section 42).")
GO_BASIS = ("Calls made through this AEOS instance in the PERSONAL domain only (the provider's own count can be "
            "higher: other clients of the same workspace are invisible). The month is the calendar month in UTC, not "
            "the provider's billing cycle, whose anchor AEOS does not know. 5h and week are trailing windows ending "
            "now, shown for the current month only.")


def month_bounds(month: str | None, now: datetime) -> tuple[datetime, datetime, str]:
    """[start, end) of a calendar month in UTC (``YYYY-MM``; None = the month of *now*) and its label."""
    if month is None:
        year, mon = now.year, now.month
    else:
        m = MONTH_RE.match(month.strip())
        if not m:
            raise ValidationFailed(f"month must be YYYY-MM (for example 2026-10), got {month[:16]!r}",
                                   code="USAGE_REPORT_BAD_MONTH")
        year, mon = int(m.group(1)), int(m.group(2))
    start = datetime(year, mon, 1, tzinfo=UTC)
    end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=UTC)
    return start, end, f"{year:04d}-{mon:02d}"


def _f(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _pct(used: Decimal, limit: Decimal | None) -> float | None:
    if limit is None or limit <= ZERO:
        return None
    return round(float(used / limit * 100), 1)


def _at(value: datetime | None) -> datetime | None:
    return ensure_aware(value)


def _ledger(session: Session, *, start: datetime, end: datetime,
            allowed_project_ids: list[str] | None) -> list[UsageLedgerEntry]:
    from aeos.cost.service import _visible_ledger

    rows = _visible_ledger(session, domain=SCOPE_DOMAIN, allowed_project_ids=allowed_project_ids, start=start)
    out = []
    for e in rows:
        at = _at(e.at)
        if e.mode == LIVE and at is not None and at < end:
            out.append(e)
    return out


def _runs(session: Session, *, start: datetime, end: datetime,
          allowed_project_ids: list[str] | None) -> list[WorkerRun]:
    q = select(WorkerRun).where(WorkerRun.domain == SCOPE_DOMAIN, WorkerRun.created_at >= start,
                                WorkerRun.created_at < end)
    return [r for r in session.scalars(q) if allowed_project_ids is None or r.project_id in allowed_project_ids]


# ------------------------------------------------------------------------------------------------ OpenCode Go


def _go_section(session: Session, routes: list[ModelRoute], entries: list[UsageLedgerEntry], *, start: datetime,
                end: datetime, now: datetime, current: bool) -> dict[str, Any]:
    by_route: dict[str, list[UsageLedgerEntry]] = defaultdict(list)
    for e in entries:
        if e.category == MODEL_CALL_CATEGORY and e.route_key:
            by_route[e.route_key].append(e)
    items: list[dict[str, Any]] = []
    never_used: list[dict[str, Any]] = []
    plans: dict[str, float | None] = {}
    total_allowance = ZERO
    total_used = ZERO
    for route in routes:
        if not subs.is_subscription_priced(route):
            continue
        calls = by_route.get(route.route_key, [])
        month_calls = [e for e in calls if (_at(e.at) or end) >= start]
        if route.pool != "WORKER" and not month_calls:
            continue  # a catalog/watchlist row nobody uses is not a subscription in use
        terms, problem = subs.subscription_terms(route)
        item: dict[str, Any] = {"route_key": route.route_key, "status": route.status, "pool": route.pool,
                                "allowance_usd_month": None, "windows": [], "state": "OK"}
        if terms is None:
            items.append(item | {"state": "UNTRUSTED", "problem": problem, "requests_month": len(month_calls)})
            continue
        allowance: Decimal = terms["allowance_usd_month"]
        plans[str(terms["plan"])] = _f(terms["monthly_fee_usd"])
        item["allowance_usd_month"] = float(allowance)
        windows: list[tuple[str, datetime, Decimal]] = [("month", start, allowance)]
        if current:
            windows = [(name, now - length, (allowance * terms["shares"][name]).quantize(Decimal("0.000001")))
                       for name, length in TRAILING.items()] + windows
        for name, since, limit in windows:
            inside = [e for e in calls if (_at(e.at) or since) >= since and (_at(e.at) or end) < end]
            used = ZERO
            reported = ZERO
            unknown = 0
            for e in inside:
                amount, basis = subs.entry_allowance(e, terms)
                if amount is None:
                    unknown += 1
                    continue
                used += amount
                if basis == "REPORTED":
                    reported += amount
            item["windows"].append({
                "window": name, "since": since.isoformat(), "limit_usd": float(limit), "used_usd": float(used),
                "reported_usd": float(reported), "estimated_usd": float(used - reported), "requests": len(inside),
                "unknown_requests": unknown, "used_pct": _pct(used, limit),
                **({"used_pct_of_monthly_allowance": _pct(used, allowance)} if name != "month" else {})})
            if name == "month":
                total_used += used
                item["requests_month"] = len(inside)
                if unknown:
                    item["state"] = "LOWER_BOUND"  # some calls have no countable allowance
        total_allowance += allowance
        items.append(item)
        if route.pool == "WORKER" and not month_calls:
            never_used.append({"route_key": route.route_key, "status": route.status,
                               "allowance_usd_month": float(allowance)})
    fees = [v for v in plans.values() if v is not None]
    fee_total = sum(fees) if fees else None
    return {
        "routes": items,
        "never_used_this_month": never_used,
        "totals": {"allowance_usd_month": float(total_allowance), "used_usd_month": float(total_used),
                   "used_pct": _pct(total_used, total_allowance), "routes": len(items),
                   "routes_never_used": len(never_used),
                   "plans": [{"plan": k, "monthly_fee_usd": v} for k, v in sorted(plans.items())],
                   "allowance_used_per_fee_dollar": (round(float(total_used) / fee_total, 2)
                                                     if fee_total else None)},
        "basis": GO_BASIS,
    }


# ------------------------------------------------------------------------------------------------ OpenRouter


def _is_openrouter(e: UsageLedgerEntry, route: ModelRoute | None) -> bool:
    return (route.provider if route is not None else e.provider) == OPENROUTER


def _openrouter_section(session: Session, routes: dict[str, ModelRoute], entries: list[UsageLedgerEntry],
                        runs_by_id: dict[str, WorkerRun], *, allowed_project_ids: list[str] | None,
                        cross_domain_visible: bool, now: datetime) -> dict[str, Any]:
    from aeos.cost.service import list_budgets
    from aeos.routing.facade import free_tier_status

    free = free_tier_status(session, now=now)
    per_route: dict[str, dict[str, Any]] = {}
    per_role: dict[str, dict[str, Any]] = {}

    def bucket() -> dict[str, Any]:
        return {"actual_usd": ZERO, "estimate_usd": ZERO, "unknown_cost_entries": 0, "requests": 0}

    for e in entries:
        route = routes.get(e.route_key or "")
        if e.category != MODEL_CALL_CATEGORY or not _is_openrouter(e, route):
            continue
        cost = None
        try:
            cost = money(e.cost_usd)
        except ValueError:
            cost = None
        paid = (route is not None and route.is_free == "NO") or (cost is not None and cost > ZERO)
        if not paid:
            continue
        run = runs_by_id.get(e.run_id or "")
        role = (run.role_key if run is not None else None) or "(no role)"
        for name, table in ((e.route_key or "(no route)", per_route), (role, per_role)):
            b = table.setdefault(name, bucket())
            b["requests"] += int(e.requests or 0)
            if cost is None:
                b["unknown_cost_entries"] += 1
            elif e.is_estimate:
                b["estimate_usd"] += cost
            else:
                b["actual_usd"] += cost

    def out(table: dict[str, dict[str, Any]], label: str) -> list[dict[str, Any]]:
        return [{label: k, "actual_usd": float(v["actual_usd"]), "estimate_usd": float(v["estimate_usd"]),
                 "unknown_cost_entries": v["unknown_cost_entries"], "requests": v["requests"]}
                for k, v in sorted(table.items())]

    budgets = []
    for b in list_budgets(session, domain=SCOPE_DOMAIN, allowed_project_ids=allowed_project_ids,
                          cross_domain_visible=cross_domain_visible):
        if b["scope_type"] == "GLOBAL" or (b["scope_type"] == "ROUTE"
                                           and str(b["scope_id"]).startswith(f"{OPENROUTER}/")):
            decision = b.get("decision") or {}
            budgets.append({k: b.get(k) for k in ("scope_type", "scope_id", "period", "limit_usd", "spent_usd",
                                                  "held_usd", "headroom_usd", "state", "is_default", "hard_stop")}
                           | {"decision_id": decision.get("id"), **({"spend_hidden": True} if b.get("spend_hidden")
                                                                     else {})})
    global_month = next((b for b in budgets if b["scope_type"] == "GLOBAL" and b["period"] == "MONTH"), None)
    paid_allowed = bool(global_month and (global_month.get("limit_usd") or 0) > 0)
    key: dict[str, Any] | None = None
    if cross_domain_visible:
        row = session.scalar(select(QuotaState).where(QuotaState.provider == OPENROUTER, QuotaState.scope == "credits"))
        if row is not None:
            d = row.details or {}
            key = {k: d.get(k) for k in ("limit", "limit_remaining", "usage", "limit_reset", "credit_state")} | {
                "observed_at": row.observed_at.isoformat() if row.observed_at else None,
                "source": "OpenRouter key endpoint at the last catalog refresh (aeos routes refresh)"}
    return {
        "free": {k: free.get(k) for k in ("requests_today_utc", "daily_limit", "daily_basis", "requests_last_minute",
                                          "rpm_limit", "state", "counted")}
                | {"used_pct_today": (round(100 * int(free["requests_today_utc"]) / int(free["daily_limit"]), 1)
                                      if free.get("daily_limit") else None)},
        "paid_by_route": out(per_route, "route_key"),
        "paid_by_role": out(per_role, "role_key"),
        "budgets": budgets,
        "budgets_scope": ("GLOBAL and ROUTE budgets govern spend in every domain: their spent, held and remaining "
                          "figures include every domain, so they are shown only to a principal who can see every "
                          "domain (GLOBAL/ROUTE visibility rule)."),
        "paid_admission": ("OWNER_BUDGET" if paid_allowed else "NONE (GLOBAL MONTH budget is $0: paid routes are "
                                                               "refused until an owner BUDGET decision)"),
        "key_limit": key if cross_domain_visible else None,
        **({} if cross_domain_visible else {"key_limit_note": HIDDEN}),
        "basis": ("Paid = an OpenRouter route with is_free NO or a call with a cost above $0, PERSONAL ledger rows of "
                  "the month; actual, estimate and UNKNOWN are never summed. The :free count is this AEOS instance's "
                  "account-wide count for the current UTC day."),
    }


# ------------------------------------------------------------------------------------------------ work


def _plan_of_tasks(session: Session, task_ids: set[str]) -> dict[str, str]:
    if not task_ids:
        return {}
    rows = session.execute(select(TaskSpec.id, PlanVersion.plan_id).join(
        PlanVersion, PlanVersion.id == TaskSpec.plan_version_id).where(TaskSpec.id.in_(task_ids)))
    return {tid: pid for tid, pid in rows}


def _work_section(session: Session, runs: list[WorkerRun], *, start: datetime, end: datetime) -> dict[str, Any]:
    roles: dict[str, dict[str, Any]] = {}
    rates: dict[tuple[str, str], dict[str, int]] = {}
    for r in runs:
        role = r.role_key or "(no role)"
        rr = roles.setdefault(role, {"tasks": set(), "runs": 0})
        rr["runs"] += 1
        if r.task_id:
            rr["tasks"].add(r.task_id)
        verdict = (r.verifier_status or "").upper()
        cell = rates.setdefault((r.model_route_key or "(no route)", role), {"passed": 0, "not_passed": 0,
                                                                          "undecided": 0})
        if verdict in PASS_STATUSES:
            cell["passed"] += 1
        elif verdict in NOT_PASS_STATUSES:
            cell["not_passed"] += 1
        else:
            cell["undecided"] += 1
    pass_rates = []
    for (route_key, role), c in sorted(rates.items()):
        decided = c["passed"] + c["not_passed"]
        pass_rates.append({"route_key": route_key, "role_key": role, **c,
                           "pass_rate_pct": round(100 * c["passed"] / decided, 1) if decided else None})
    plan_of = _plan_of_tasks(session, {r.task_id for r in runs if r.task_id})
    per_plan: dict[str, list[WorkerRun]] = defaultdict(list)
    for r in runs:
        pid = plan_of.get(r.task_id or "")
        if pid:
            per_plan[pid].append(r)
    plans = []
    for pid, plan_runs in sorted(per_plan.items()):
        spans = [(a, b) for a, b in ((_at(r.started_at), _at(r.finished_at)) for r in plan_runs)
                 if a is not None and b is not None]
        durations = [max(0.0, (b - a).total_seconds()) for a, b in spans]
        plans.append({"plan_id": pid, "runs": len(plan_runs), "finished_runs": len(durations),
                      "tasks": len({r.task_id for r in plan_runs}),
                      "median_run_wall_clock_s": round(statistics.median(durations), 1) if durations else None,
                      "plan_wall_clock_s": (round(max(0.0, (max(b for _, b in spans) - min(a for a, _ in spans))
                                                  .total_seconds()), 1) if spans else None)})
    run_ids = {r.id for r in runs}
    ev_q = select(Event.kind, Event.domain, Event.run_id).where(
        Event.at >= start, Event.at < end, Event.mode == LIVE,
        or_(Event.kind.like(f"%{ESCALATION_MARKER}%"), Event.kind.like(f"%{OVERFLOW_MARKER}%")))
    by_kind: dict[str, int] = defaultdict(int)
    for kind, domain, run_id in session.execute(ev_q):
        if domain == SCOPE_DOMAIN or (domain is None and run_id in run_ids):
            by_kind[kind] += 1
    escalations = sum(n for k, n in by_kind.items() if ESCALATION_MARKER in k)
    overflows = sum(n for k, n in by_kind.items() if OVERFLOW_MARKER in k)
    return {
        "tasks_per_role": [{"role_key": k, "tasks": len(v["tasks"]), "runs": v["runs"]}
                           for k, v in sorted(roles.items())],
        "pass_rate": pass_rates,
        "paid_admissions": {
            "escalations": escalations, "overflows": overflows, "by_kind": dict(sorted(by_kind.items())),
            "status": "EVENTS_FOUND" if by_kind else "NO_EVENTS",
            "note": ("Escalation (after two failed $0-marginal attempts) and overflow (every $0-marginal route of the "
                     "role at capacity) events come from the paid-admission change; NO_EVENTS means none happened in "
                     "the month or that change is not installed - AEOS cannot tell the two apart here."),
        },
        "plans": plans,
        "basis": ("Worker runs created in the month (PERSONAL). Pass rate = verifier PASS / runs with a verifier "
                  "verdict; NOT_RUN and running runs are undecided, NOT_PROVEN and BLOCKED are not a pass. Wall-clock "
                  "= started_at -> finished_at of finished runs."),
    }


# ------------------------------------------------------------------------------------------------ Claude


def _ccusage_export(path: Path | None, label: str) -> dict[str, Any]:
    if path is None:
        return {"status": "NOT_CONFIGURED",
                "remedy": "Optional: write 'ccusage daily --json' to a file and set ccusage_export_path in aeos.yaml"}
    try:
        p = Path(path)
        size = p.stat().st_size
        if size > CCUSAGE_MAX_BYTES:
            return {"status": "FAILED", "reason": f"export is larger than {CCUSAGE_MAX_BYTES} bytes"}
        observed = datetime.fromtimestamp(p.stat().st_mtime, tz=UTC)
        parsed = parse_daily(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "FAILED", "reason": "the configured ccusage export file does not exist"}
    except OSError as exc:
        return {"status": "FAILED", "reason": f"the configured ccusage export cannot be read ({type(exc).__name__})"}
    except (ValueError, json.JSONDecodeError) as exc:
        return {"status": "FAILED", "reason": f"the ccusage export is not ccusage daily JSON ({type(exc).__name__})"}
    month: dict[str, int] = defaultdict(int)
    days = 0
    for day in parsed["days"]:
        if str(day.get("date", "")).startswith(label):
            days += 1
            for k, v in day.items():
                if k.endswith("_tokens") and isinstance(v, int):
                    month[k] += v
    return {"status": "ESTIMATE", "mode": "ESTIMATE", "observed_at": observed.isoformat(), "days_in_month": days,
            "month_tokens": dict(sorted(month.items())), "export_totals": parsed["totals"],
            "authority": "ccusage estimate from local Claude Code logs; not an invoice and not a quota authority"}


def _claude_section(entries: list[UsageLedgerEntry], *, label: str, cross_domain_visible: bool,
                    ccusage_path: Path | None) -> dict[str, Any]:
    native = [e for e in entries if e.category == NATIVE_CATEGORY]
    out: dict[str, Any] = {
        "statement": CLAUDE_STATEMENT,
        "aeos_recorded": {"requests": sum(int(e.requests or 0) for e in native),
                          "input_tokens": sum(int(e.input_tokens or 0) for e in native),
                          "output_tokens": sum(int(e.output_tokens or 0) for e in native),
                          "basis": "NATIVE_CLAUDE ledger rows AEOS recorded for its own supervisor calls (PERSONAL)"},
    }
    if not cross_domain_visible:
        return out | {"ccusage_export": {"status": "HIDDEN", "note": HIDDEN}}
    return out | {"ccusage_export": _ccusage_export(ccusage_path, label)}


# ------------------------------------------------------------------------------------------------ report


def build_report(session: Session, *, month: str | None = None, allowed_project_ids: list[str] | None = None,
                 cross_domain_visible: bool = True, now: datetime | None = None,
                 ccusage_path: Path | None = None, use_settings: bool = True) -> dict[str, Any]:
    """The usage value report for *month* (``YYYY-MM``, default the current UTC month). Pure reads: no row is added,
    changed or deleted, and nothing leaves this host. ``ccusage_path`` overrides the ``ccusage_export_path`` setting
    (``use_settings=False`` ignores the setting)."""
    current_time = ensure_aware(now or utcnow())
    assert current_time is not None
    start, end, label = month_bounds(month, current_time)
    current = start <= current_time < end
    if ccusage_path is None and use_settings:
        from aeos.core.config import get_settings

        ccusage_path = get_settings().ccusage_export_path
    window_start = min(start, current_time - TRAILING["week"]) if current else start
    entries = _ledger(session, start=window_start, end=end, allowed_project_ids=allowed_project_ids)
    month_entries = [e for e in entries if (_at(e.at) or start) >= start]
    routes = {r.route_key: r for r in session.scalars(select(ModelRoute).order_by(ModelRoute.route_key))}
    runs = _runs(session, start=start, end=end, allowed_project_ids=allowed_project_ids)
    run_ids = {e.run_id for e in month_entries if e.run_id}
    runs_by_id = {r.id: r for r in runs}
    missing = run_ids - set(runs_by_id)
    if missing:  # a ledger row of the month whose run was created earlier
        runs_by_id.update({r.id: r for r in session.scalars(select(WorkerRun).where(
            WorkerRun.id.in_(missing), WorkerRun.domain == SCOPE_DOMAIN))})
    return {
        "source": SOURCE,
        "checked_at": current_time.isoformat(),
        "mode": LIVE,
        "scope": {"domain": SCOPE_DOMAIN, "month": label, "start": start.isoformat(), "end": end.isoformat(),
                  "current_month": current, "read_only": True,
                  "note": "PERSONAL only; OFFICE records never enter this report."},
        "opencode_go": _go_section(session, list(routes.values()), entries, start=start, end=end, now=current_time,
                                   current=current),
        "openrouter": _openrouter_section(session, routes, month_entries, runs_by_id,
                                          allowed_project_ids=allowed_project_ids,
                                          cross_domain_visible=cross_domain_visible, now=current_time),
        "work": _work_section(session, runs, start=start, end=end),
        "claude": _claude_section(month_entries, label=label, cross_domain_visible=cross_domain_visible,
                                  ccusage_path=ccusage_path),
    }


# ------------------------------------------------------------------------------------------------ text


def _usd(v: Any) -> str:
    return "UNKNOWN" if v is None else f"${float(v):.2f}"


def _p(v: Any) -> str:
    return "n/a" if v is None else f"{v:.1f}%"


def render_text(report: dict[str, Any]) -> list[str]:
    """The report as ASCII lines (legacy Windows consoles)."""
    sc = report["scope"]
    lines = [f"Usage report {sc['month']} - scope {sc['domain']} (read-only; OFFICE never included)", ""]
    go = report["opencode_go"]
    t = go["totals"]
    fees = ", ".join(f"{p['plan']} {_usd(p['monthly_fee_usd'])}/month" for p in t["plans"]) or "no plan"
    lines += ["OpenCode Go (subscription allowance, counted by this AEOS instance):",
              f"  total: used {_usd(t['used_usd_month'])} of {_usd(t['allowance_usd_month'])} monthly allowance "
              f"({_p(t['used_pct'])}) over {t['routes']} route(s); plan: {fees}; allowance per fee dollar: "
              f"{t['allowance_used_per_fee_dollar'] if t['allowance_used_per_fee_dollar'] is not None else 'n/a'}"]
    for r in go["routes"]:
        if r.get("state") == "UNTRUSTED":
            lines.append(f"  {r['route_key']} [{r['status']}] UNTRUSTED terms: {r.get('problem')}")
            continue
        parts = [f"{w['window']} {_usd(w['used_usd'])}/{_usd(w['limit_usd'])} {_p(w['used_pct'])}"
                 for w in r["windows"]]
        lines.append(f"  {r['route_key']} [{r['status']}] {r.get('requests_month', 0)} req: " + "; ".join(parts)
                     + (" (lower bound: calls with UNKNOWN allowance)" if r.get("state") == "LOWER_BOUND" else ""))
    never = go["never_used_this_month"]
    lines.append(f"  never used this month: {len(never)}"
                 + (" - " + ", ".join(f"{n['route_key']} [{n['status']}]" for n in never) if never else ""))
    orr = report["openrouter"]
    f = orr["free"]
    lines += ["", "OpenRouter:",
              f"  free requests today (UTC): {f['requests_today_utc']} of {f['daily_limit']} ({_p(f['used_pct_today'])},"
              f" {f['daily_basis']}) state {f['state']}",
              f"  paid admission: {orr['paid_admission']}"]
    for b in orr["budgets"]:
        spend = ("spend hidden" if b.get("spend_hidden") else
                 f"spent {_usd(b['spent_usd'])} held {_usd(b['held_usd'])} remaining {_usd(b['headroom_usd'])}")
        lines.append(f"  budget {b['scope_type']}:{b['scope_id']}:{b['period']} limit {_usd(b['limit_usd'])} {spend} "
                     f"state {b['state']}")
    key = orr.get("key_limit")
    if key:
        lines.append(f"  key limit (last refresh {key.get('observed_at')}): remaining {_usd(key.get('limit_remaining'))}"
                     f" of {_usd(key.get('limit'))}")
    for label, rows, field in (("route", orr["paid_by_route"], "route_key"), ("role", orr["paid_by_role"], "role_key")):
        if not rows:
            lines.append(f"  paid by {label}: none this month")
        for row in rows:
            lines.append(f"  paid by {label} {row[field]}: actual {_usd(row['actual_usd'])} estimate "
                         f"{_usd(row['estimate_usd'])} unknown {row['unknown_cost_entries']} ({row['requests']} req)")
    w = report["work"]
    lines += ["", "Work:"]
    lines += [f"  role {r['role_key']}: {r['tasks']} task(s), {r['runs']} run(s)" for r in w["tasks_per_role"]] or [
        "  no worker runs this month"]
    lines += [f"  pass rate {p['route_key']} x {p['role_key']}: {_p(p['pass_rate_pct'])} ({p['passed']} pass, "
              f"{p['not_passed']} not pass, {p['undecided']} undecided)" for p in w["pass_rate"]]
    pa = w["paid_admissions"]
    lines.append(f"  escalations {pa['escalations']}, overflows {pa['overflows']} ({pa['status']})")
    lines += [f"  plan {p['plan_id']}: {p['tasks']} task(s), median run {p['median_run_wall_clock_s']} s, plan "
              f"wall-clock {p['plan_wall_clock_s']} s" for p in w["plans"]]
    c = report["claude"]
    a = c["aeos_recorded"]
    cc = c["ccusage_export"]
    lines += ["", "Claude:", f"  {c['statement']}",
              f"  AEOS-recorded supervisor calls: {a['requests']} request(s), {a['input_tokens']} in / "
              f"{a['output_tokens']} out tokens",
              f"  ccusage export: {cc['status']}" + (f" - {cc.get('reason')}" if cc.get("reason") else "")]
    if cc.get("status") == "ESTIMATE":
        lines.append(f"  ccusage month tokens ({cc['days_in_month']} day(s)): "
                     + ", ".join(f"{k} {v}" for k, v in cc["month_tokens"].items()))
    return [line.encode("ascii", "replace").decode("ascii") for line in lines]
