"""`aeos project` - portfolio commands (ASCII-only output for legacy Windows consoles).

Read-only except `seed-self`, which idempotently registers the DLY AEOS self-build project (§09).
Creating projects needs an explicit human domain choice and is done in the Control Center.
"""

from __future__ import annotations

import json

import typer
from rich.console import Console
from sqlalchemy import select

app = typer.Typer(help="Projects, repositories and integrations (Gateway & Portfolio).", no_args_is_help=True)
console = Console()


def _session():  # type: ignore[no-untyped-def]
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope

    ensure_database()
    return session_scope()


@app.command("list")
def list_projects(domain: str = typer.Option(None, help="PERSONAL or OFFICE (required to show projects)")) -> None:
    """List projects of one explicitly chosen domain."""
    from aeos.core.enums import Domain
    from aeos.models.portfolio import Project

    if not domain or domain.upper() not in {d.value for d in Domain}:
        console.print("Choose the domain explicitly: --domain PERSONAL or --domain OFFICE (never inferred).")
        raise typer.Exit(2)
    with _session() as s:
        rows = list(s.scalars(select(Project).where(Project.domain == domain.upper()).order_by(Project.name)))
        if not rows:
            console.print(f"No {domain.upper()} projects. Run `aeos seed` for the DLY AEOS self project.")
            return
        for p in rows:
            flag = " (platform)" if p.is_platform else ""
            console.print(f"{p.project_key:<32} {p.lifecycle:<9} {p.data_classification:<12} {p.name}{flag}",
                          markup=False)


@app.command("show")
def show(project_key: str) -> None:
    """Show one project with repositories and integrations (no secret values)."""
    from aeos.portfolio import service

    with _session() as s:
        p = service.find_by_key(s, project_key)
        if p is None:
            console.print(f"Project {project_key} not found.")
            raise typer.Exit(1)
        view = service.project_view(s, p, detail=True)
        console.print(json.dumps(view, indent=2, ensure_ascii=True, default=str))


@app.command("seed-self")
def seed_self() -> None:
    """Register the DLY AEOS self-build project (PERSONAL, platform repo UNVERIFIED until verified)."""
    from aeos.portfolio.seed import run

    with _session() as s:
        result = run(s)
    console.print(json.dumps(result, indent=2, ensure_ascii=True, default=str))


@app.command("inventory")
def inventory(project_key: str, commit: str = typer.Option(None, help="Exact commit (default HEAD)")) -> None:
    """Read-only inventory of the project's local checkout at a known commit (no scaffolding)."""
    from aeos.portfolio import repositories, service

    with _session() as s:
        p = service.find_by_key(s, project_key)
        if p is None:
            console.print(f"Project {project_key} not found.")
            raise typer.Exit(1)
        repo = repositories.primary_repository(s, p.id)
        if repo is None:
            console.print("No repository registered. Add one in the Control Center (Proyek > Detail).")
            raise typer.Exit(1)
        result = repositories.inventory_for_repository(s, None, p, repo, commit=commit)
        langs = ", ".join(f"{x['language']}({x['files']})" for x in result["languages"][:8])
        console.print(f"commit   {result['commit']}  branch {result['branch']}")
        console.print(f"files    {result['files']}  languages: {langs or '-'}")
        console.print(f"manifests {len(result['dependency_manifests'])}  CI: {', '.join(result['ci']) or '-'}")
        console.print(f"tests    frameworks: {', '.join(result['tests']['frameworks']) or '-'} (not executed)")
        for gap in result["coverage_gaps"]:
            console.print(f"gap      {gap}")
