"""`aeos broker ...` - inspect and revoke capability grants (ASCII output, no tokens)."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.markup import escape
from sqlalchemy import select

app = typer.Typer(help="Trusted capability broker (grants, revocation).", no_args_is_help=True)
console = Console()


def _out(text: str) -> None:
    """ASCII-only, markup-safe output (AGENTS.md: legacy Windows consoles). Reasons can come from
    other modules and contain non-ASCII characters or square brackets."""
    clean = str(text).replace("\u00a7", "s").replace("\u2014", "-").replace("\u2192", "->")
    console.print(escape(clean.encode("ascii", "replace").decode("ascii")), highlight=False)


@app.command("grants")
def grants_cmd(active: bool = typer.Option(False, help="Only active grants"),
               run: str = typer.Option(None, help="Filter by run id")) -> None:
    """List capability grants (token hashes are never printed)."""
    from aeos.broker.grants import grant_view
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.models.routing import CapabilityGrant

    ensure_database()
    with session_scope() as s:
        q = select(CapabilityGrant).order_by(CapabilityGrant.created_at.desc()).limit(100)
        if active:
            q = q.where(CapabilityGrant.active.is_(True))
        if run:
            q = q.where(CapabilityGrant.run_id == run)
        rows = [grant_view(g) for g in s.scalars(q)]
    if not rows:
        _out("No capability grants.")
    for g in rows:
        _out(f"{g['id']}  run={g['run_id']}  kind={g['kind']}  active={g['active']}  "
                      f"expires={g['expires_at']}  used={g['used']}")


@app.command("probe-route")
def probe_route_cmd(route_key: str = typer.Argument(..., help="Route key (ARENA/COMPARATOR/LOCAL/WORKER pool)")) -> None:
    """One synthetic PUBLIC feasibility request to an owner-activated (TRIAL) route that spends no money: free, local
    loopback, or a flat-fee subscription (it consumes plan allowance); evidence only."""
    from aeos.broker.feasibility import probe_route
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.core.errors import AeosError

    ensure_database()
    try:
        with session_scope() as s:
            out = probe_route(s, route_key, actor="cli:local-operator")
    except AeosError as exc:
        _out(f"{exc.code}: {exc.message}")
        raise typer.Exit(2) from exc
    _out(f"{out['route_key']} ({out['pool']}): {out['outcome']} http={out['http_status']} "
         f"latency={out['latency_ms']}ms model={out['model_resolved'] or 'UNKNOWN'} cost={out['cost_usd']}")
    if out.get("billing"):
        b = out["billing"]
        _out(f"subscription allowance used: {b['allowance_usd']} USD ({b['allowance_basis']}, "
             f"billing source {b['billing_source']})")
        if b.get("beyond_plan_usd") is not None:
            _out(f"BEYOND THE PLAN: {b['beyond_plan_usd']} USD - route refused (SUBSCRIPTION_BEYOND_PLAN) until you "
                 "check Extra Usage is OFF and re-activate it")
    _out(f"evidence {out['evidence_id']}; {out['note']}")


@app.command("revoke")
def revoke_cmd(run: str = typer.Argument(..., help="Run id"),
               reason: str = typer.Option("revoked by local operator", help="Reason")) -> None:
    """Revoke every active grant of a run."""
    from aeos.broker.grants import revoke_grants
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope

    ensure_database()
    with session_scope() as s:
        n = revoke_grants(s, run_id=run, reason=reason, actor="cli:local-operator")
    _out(f"Revoked {n} grant(s) for run {run}.")
