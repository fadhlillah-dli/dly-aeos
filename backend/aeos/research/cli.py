"""`aeos research ...` - registry listing and the manual upstream check (ASCII output only)."""

from __future__ import annotations

import typer

from aeos.core.enums import Domain

app = typer.Typer(help="Research & Technology Registry (candidates/references; never installs anything).",
                  no_args_is_help=True)


@app.command("list")
def list_cmd(
    domain: Domain = typer.Option(..., help="PERSONAL or OFFICE - chosen explicitly"),
    lifecycle: str = typer.Option(None, help="Filter by lifecycle, e.g. SOURCE_VERIFIED"),
    kind: str = typer.Option(None, help="reference | candidate"),
    q: str = typer.Option(None, help="Text search"),
) -> None:
    """List registry entries with lifecycle, evidence level and freshness."""
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.research import service

    ensure_database()
    with session_scope() as s:
        rows = service.list_entries(s, domain=domain.value, lifecycle=lifecycle, kind=kind, q=q)
    for r in rows:
        url = (r["urls"] or ["(no URL - identity " + r["identity_status"] + ")"])[0]
        typer.echo(f"{r['id'][:8]}  {r['name'][:34]:<34} {r['lifecycle']:<16} {r['evidence_level']} "
                   f"{r['identity_status']:<16} {r['freshness']:<11} {url}".encode("ascii", "replace").decode())
    typer.echo(f"{len(rows)} entries. Research lifecycle is not installation/usage evidence.")


@app.command("check")
def check_cmd(
    entry_id: str = typer.Argument(..., help="Entry id (or unique prefix)"),
    domain: Domain = typer.Option(..., help="PERSONAL or OFFICE - chosen explicitly"),
) -> None:
    """Fetch public GitHub repo + latest release metadata (network). Never installs or activates."""
    from sqlalchemy import select

    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.core.errors import AeosError
    from aeos.models.research import ResearchEntry
    from aeos.research import service

    ensure_database()
    try:
        with session_scope() as s:
            matches = list(s.scalars(select(ResearchEntry).where(ResearchEntry.id.startswith(entry_id))))
            if len(matches) != 1:
                typer.echo(f"{len(matches)} entries match '{entry_id}'; give a longer id.")
                raise typer.Exit(2)
            entry = service.get_entry(s, matches[0].id, domain=domain.value)
            view = service.check_upstream(s, entry, actor_type="cli", actor_id="local-operator", domain=domain.value)
    except AeosError as exc:
        typer.echo(f"{exc.code}: {exc.message}")
        raise typer.Exit(1) from exc
    up = view.get("upstream") or {}
    typer.echo(f"{view['name']}: {view['check_status']} identity={view['identity_status']} lifecycle={view['lifecycle']}")
    typer.echo(f"  observed={up.get('observed_full_name')} latest_tag={up.get('latest_tag')} "
               f"archived={up.get('archived')} redirected={up.get('redirected')}")
    typer.echo("  Nothing was installed, updated or activated. Update available != compatible.")


@app.command("link-components")
def link_cmd() -> None:
    """Link registry entries to inventory component ids whose names/identities match."""
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.research import service

    ensure_database()
    with session_scope() as s:
        n = service.link_components(s)
    typer.echo(f"linked {n} entries")
