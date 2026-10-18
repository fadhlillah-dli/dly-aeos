"""`aeos broker ...` - inspect and revoke capability grants (ASCII output, no tokens)."""

from __future__ import annotations

import typer
from rich.console import Console
from sqlalchemy import select

app = typer.Typer(help="Trusted capability broker (grants, revocation).", no_args_is_help=True)
console = Console()


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
        console.print("No capability grants.")
    for g in rows:
        console.print(f"{g['id']}  run={g['run_id']}  kind={g['kind']}  active={g['active']}  "
                      f"expires={g['expires_at']}  used={g['used']}")


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
    console.print(f"Revoked {n} grant(s) for run {run}.")
