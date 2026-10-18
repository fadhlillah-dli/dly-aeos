"""`aeos policy ...` - policy snapshot, decision register and ADR index (ASCII output only)."""

from __future__ import annotations

import typer
from rich.console import Console
from sqlalchemy import select

from aeos.core.db import session_scope

app = typer.Typer(help="Policy & Decision Registry: snapshot, decisions, ADRs.", no_args_is_help=True)
console = Console(highlight=False)


def _ensure_db() -> None:
    from aeos.cli import ensure_database

    ensure_database()


@app.command("snapshot")
def snapshot_cmd(
    refresh: bool = typer.Option(False, "--refresh", help="Activate a new snapshot if policy files/decisions changed"),
) -> None:
    """Show the active policy snapshot hash and MCI reference status."""
    from aeos.core.hashing import content_hash
    from aeos.policy import snapshot

    _ensure_db()
    with session_scope() as s:
        snap = snapshot.ensure_active_snapshot(s, source="cli refresh") if refresh else snapshot.active_snapshot(s)
        current = content_hash(snapshot.compute_content(s))
        mci = snapshot.mci_reference()
        if snap is None:
            console.print("No active policy snapshot. Run: aeos seed --only aeos.policy.seed")
        else:
            console.print(f"Active snapshot v{snap.version}: {snap.content_hash}")
            console.print(f"  created {snap.created_at.isoformat()} source={snap.source}")
            for name, f in (snap.content.get("files") or {}).items():
                console.print(f"  file {name}: {f.get('sha256')} [{f.get('status')}]")
            console.print(f"  decisions bound: {len(snap.content.get('decisions') or [])}")
            if snap.content_hash != current:
                console.print("  DRIFT: policy files or decisions changed; run with --refresh")
        console.print(f"MCI reference: {mci['name']} status={mci['status']} compliance={mci['compliance']}")


@app.command("decisions")
def decisions_cmd(
    kind: str = typer.Option(None, help="Filter by kind (APPROVAL, EXCEPTION, POLICY, ...)"),
    status: str = typer.Option(None, help="Filter by effective status (ACTIVE, EXPIRED, INVALIDATED, SUPERSEDED)"),
    limit: int = typer.Option(50, help="Maximum rows"),
) -> None:
    """List recorded human decisions (newest first)."""
    from aeos.models.decisions import Decision
    from aeos.policy.decisions import effective_status

    _ensure_db()
    with session_scope() as s:
        q = select(Decision).order_by(Decision.created_at.desc()).limit(max(1, min(limit, 500)))
        if kind:
            q = q.where(Decision.kind == kind)
        rows = [d for d in s.scalars(q) if not status or effective_status(d) == status]
        if not rows:
            console.print("No decisions recorded.")
        for d in rows:
            exp = d.expires_at.isoformat() if d.expires_at else "-"
            console.print(f"{d.created_at.isoformat()[:19]} {d.kind:16} {effective_status(d):11} "
                          f"{d.target_type}:{d.target_id} actor={d.actor_user_id} ({d.actor_kind}) "
                          f"ops={','.join(d.allowed_operations or []) or '-'} expires={exp}")


@app.command("adrs")
def adrs_cmd(load: bool = typer.Option(False, "--load", help="(Re)load docs/decisions/ADR-*.md first")) -> None:
    """List the ADR register."""
    from aeos.models.decisions import ArchitectureDecisionRecord
    from aeos.policy import adrs

    _ensure_db()
    with session_scope() as s:
        if load:
            res = adrs.load_adrs(s)
            console.print(f"Loaded ADRs: created={res['created']} updated={res['updated']} unchanged={res['unchanged']}")
        rows = list(s.scalars(select(ArchitectureDecisionRecord).order_by(ArchitectureDecisionRecord.adr_key)))
        if not rows:
            console.print("No ADRs loaded. Run: aeos policy adrs --load")
        for r in rows:
            title = r.title.encode("ascii", "replace").decode("ascii")
            console.print(f"{r.adr_key} [{r.status}] {title}  ({r.source_path}, {r.content_hash})")
