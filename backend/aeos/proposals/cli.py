"""`aeos proposal ...` - read-only views of change proposals (ASCII output only).

Decisions are human actions in the Control Center (bound to baseline/candidate hashes); the CLI
does not approve anything."""

from __future__ import annotations

import json

import typer

from aeos.core.enums import Domain

app = typer.Typer(help="Change proposals (read-only; decisions are made in the Control Center).", no_args_is_help=True)


def _ascii(s: str) -> str:
    return s.encode("ascii", "replace").decode()


@app.command("list")
def list_cmd(
    domain: Domain = typer.Option(..., help="PERSONAL or OFFICE - chosen explicitly"),
    status: str = typer.Option(None, help="Filter by status"),
) -> None:
    """List proposals in a domain with status."""
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.proposals import service

    ensure_database()
    with session_scope() as s:
        rows = service.list_proposals(s, domain=domain.value, allowed_project_ids=None, status=status)
    for r in rows:
        dup = f" duplicate_of={r['duplicate_of'][:8]}" if r["duplicate_of"] else ""
        typer.echo(_ascii(f"{r['id'][:8]}  {r['kind']:<8} {r['status']:<22} {r['title'][:60]}{dup}"))
    typer.echo(f"{len(rows)} proposals.")


@app.command("show")
def show_cmd(
    proposal_id: str = typer.Argument(..., help="Proposal id (or unique prefix)"),
    domain: Domain = typer.Option(..., help="PERSONAL or OFFICE - chosen explicitly"),
    as_json: bool = typer.Option(False, "--json", help="Print the full record as JSON"),
) -> None:
    """Show a proposal: hashes, review package status, decisions and outcome."""
    from sqlalchemy import select

    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.models.research import ChangeProposal
    from aeos.proposals import service

    ensure_database()
    with session_scope() as s:
        matches = list(s.scalars(select(ChangeProposal).where(ChangeProposal.id.startswith(proposal_id),
                                                              ChangeProposal.domain == domain.value)))
        if len(matches) != 1:
            typer.echo(f"{len(matches)} proposals match '{proposal_id}' in {domain.value}.")
            raise typer.Exit(2)
        v = service.proposal_view(s, matches[0], detail=True)
    if as_json:
        typer.echo(json.dumps(v, ensure_ascii=True, indent=2, default=str))
        return
    typer.echo(_ascii(f"{v['title']} [{v['kind']}] status={v['status']}"))
    typer.echo(f"  baseline_hash={v['baseline_hash']}  candidate_hash={v['candidate_hash']}")
    typer.echo(_ascii(f"  sources: {', '.join(v['canonical_identities']) or '-'}"))
    pkg = v.get("review_package") or {}
    typer.echo(f"  review package: {'present' if pkg else 'not reviewed yet'}; "
               f"claude: {(pkg.get('claude_review') or {}).get('status', '-')}")
    for d in v["decisions"]:
        typer.echo(f"  decision {d['id'][:8]} {d['kind']}/{d['action']} {d['status']} by {d['actor_user_id']}")
    typer.echo(f"  outcome: {v['outcome_status'] or 'PENDING'}")
