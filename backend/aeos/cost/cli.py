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
    """Actual vs estimate vs UNKNOWN per category, native subscription, stop caps."""
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
    n = data["native_subscription"]
    typer.echo(f"Native Claude (Max): remaining quota {n['remaining_quota']} - source: {n['quota_source']}")
    p = data["per_accepted_outcome"]
    typer.echo(f"Accepted outcomes: {p['accepted_outcomes']}  cost/accepted: {_usd(p.get('cost_per_accepted_usd'))} "
               f"({p['status']})")
    typer.echo(f"Excluded non-LIVE entries: {data['excluded_non_live']['entries']}")


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
