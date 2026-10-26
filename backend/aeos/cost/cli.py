"""`aeos cost ...` - read-only cost/budget views for the local operator (ASCII output only)."""

from __future__ import annotations

import typer

from aeos.core.enums import Domain

app = typer.Typer(help="Costs, budgets and quotas (read-only; budget changes need an owner decision in the UI).",
                  no_args_is_help=True)


def _usd(v: float | None) -> str:
    return "UNKNOWN" if v is None else f"${v:.4f}"


@app.command("summary")
def summary_cmd(
    domain: Domain = typer.Option(..., help="PERSONAL or OFFICE - chosen explicitly, never defaulted"),
    period: str = typer.Option("MONTH", help="DAY|WEEK|MONTH|TOTAL"),
) -> None:
    """Actual vs estimate vs UNKNOWN per category, worker subscriptions (plan + allowance), native subscription."""
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.cost import service

    ensure_database()
    with session_scope() as s:
        data = service.summary(s, domain=domain.value, allowed_project_ids=None, cross_domain_visible=True,
                               period=period.upper())
    t = data["totals"]
    typer.echo(f"Window: {data['window']['period']} since {data['window']['start'] or 'beginning'}")
    typer.echo(f"Actual: {_usd(t['actual_usd'])}  Estimate: {_usd(t['estimate_usd'])}  "
               f"UNKNOWN entries: {t['unknown_count']}  (not summed together)")
    typer.echo("By category:")
    for c in data["by_category"]:
        if c["category"] == "NATIVE_CLAUDE":
            typer.echo(f"  {c['category']:<14} subscription  requests={c['requests']}  (no dollar conversion)")
        else:
            typer.echo(f"  {c['category']:<14} actual={_usd(c['actual_usd'])} estimate={_usd(c['estimate_usd'])} "
                       f"unknown={c['unknown_count']}")
    for line in subscription_lines(data.get("subscriptions") or []):
        typer.echo(line)
    n = data["native_subscription"]
    typer.echo(f"Native Claude (Max): remaining quota {n['remaining_quota']} - source: {n['quota_source']}")
    p = data["per_accepted_outcome"]
    typer.echo(f"Accepted outcomes: {p['accepted_outcomes']}  cost/accepted: {_usd(p.get('cost_per_accepted_usd'))} "
               f"({p['status']})")
    typer.echo(f"Excluded non-LIVE entries: {data['excluded_non_live']['entries']}")


def subscription_lines(subs: list[dict]) -> list[str]:
    """Worker subscription routes (ADR-0080 amendment 2026-10-26): the plan and the allowance this AEOS instance
    consumed per provider window - never per-token spend. The 5h window blocks admission; week and month are advisory.
    ASCII only."""
    if not subs:
        return []
    out = ["Worker subscriptions (plan fee + allowance per window; not per-token spend):",
           "  counted: calls made through this AEOS instance only; the provider's own limit stays authoritative"]
    for s in subs:
        fee = s.get("monthly_fee_usd")
        plan = f"{s.get('plan') or 'subscription'} {_usd(fee) if fee is not None else 'fee UNKNOWN'}/month"
        out.append(f"  {s['route_key']}  plan={plan}  allowance={_usd(s.get('allowance_usd_month'))}/month  "
                   f"state={s['state']}  route={s.get('status')}")
        if s.get("spend_hidden"):
            out.append("    allowance hidden: it spans domains you cannot see")
            continue
        for w in s.get("windows") or []:
            out.append(f"    {w['window']:<5} used {_usd(w['used_usd'])} of {_usd(w['limit_usd'])} "
                       f"(remaining {_usd(w['remaining_usd'])}; reported {_usd(w['reported_usd'])}, "
                       f"estimated {_usd(w['estimated_usd'])}; {w['requests']} request(s)) {w['state']}"
                       + ("" if w.get("blocking", True) else " (advisory)"))
        if s.get("beyond_plan_usd") or s.get("beyond_plan_unknown_requests"):
            unknown = s.get("beyond_plan_unknown_requests") or 0
            out.append(f"    paid beyond the plan: {_usd(s.get('beyond_plan_usd') or 0)}"
                       + (f" + {unknown} call(s) of UNKNOWN amount" if unknown else "")
                       + " (provider-side paid fallback was used)")
        bp = s.get("beyond_plan")
        if bp:
            out.append(f"    REFUSED (SUBSCRIPTION_BEYOND_PLAN) since {bp.get('at')}: {bp.get('remedy')}")
        for p in s.get("problems") or []:
            out.append(f"    blocking: {p}")
        for w_text in s.get("warnings") or []:
            out.append(f"    advisory: {w_text}")
    return [line.encode("ascii", "replace").decode("ascii") for line in out]


@app.command("budgets")
def budgets_cmd(
    domain: Domain = typer.Option(..., help="PERSONAL or OFFICE - chosen explicitly"),
    all_rows: bool = typer.Option(False, "--all", help="Include superseded budgets"),
) -> None:
    """Stop caps with the decision that set them."""
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.cost import service

    ensure_database()
    with session_scope() as s:
        rows = service.list_budgets(s, domain=domain.value, allowed_project_ids=None, cross_domain_visible=True,
                                    include_superseded=all_rows)
    for b in rows:
        dec = b.get("decision") or {}
        who = f"decision {dec.get('id')} by {dec.get('actor_user_id')}" if dec else (
            "default (no decision needed for $0)" if b["is_default"] else "NO DECISION")
        typer.echo(f"{b['scope_type']}:{b['scope_id']}:{b['period']:<6} limit={_usd(b['limit_usd'])} "
                   f"spent={_usd(b.get('spent_usd'))} held={_usd(b.get('held_usd'))} state={b['state']} "
                   f"{'hard' if b['hard_stop'] else 'soft'} - {who}")
        for r in b.get("trust_reasons") or []:
            typer.echo(f"    untrusted: {r}")
