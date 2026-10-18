"""`aeos project` - portfolio commands (ASCII-only output for legacy Windows consoles).

Read-only except `seed-self`, which idempotently registers the DLY AEOS self-build project (§09).
Creating projects needs an explicit human domain choice and is done in the Control Center. Every
project-scoped command names the domain explicitly (U-05): a project of another domain is reported
as "not found", exactly like the API's cross-domain 404.
"""

from __future__ import annotations

import json
from typing import Any

import typer
from rich.console import Console
from sqlalchemy import select

app = typer.Typer(help="Projects, repositories and integrations (Gateway & Portfolio).", no_args_is_help=True)
console = Console()

DOMAIN_HELP = "PERSONAL or OFFICE (required; never inferred)"


def _session():  # type: ignore[no-untyped-def]
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope

    ensure_database()
    return session_scope()


def ascii_safe(value: Any) -> str:
    """Legacy Windows consoles cannot print every code point; names/descriptions are user data."""
    return str(value).encode("ascii", errors="replace").decode("ascii")


def _say(text: str) -> None:
    # soft_wrap: never insert line breaks (JSON output must stay machine-readable when piped)
    console.print(ascii_safe(text), markup=False, highlight=False, soft_wrap=True)


def _require_domain(domain: str | None) -> str:
    from aeos.core.enums import Domain

    value = (domain or "").strip().upper()
    if value not in {d.value for d in Domain}:
        _say("Choose the domain explicitly: --domain PERSONAL or --domain OFFICE (never inferred).")
        raise typer.Exit(2)
    return value


def _find(s: Any, project_key: str, domain: str) -> Any:
    from aeos.portfolio import service

    p = service.find_by_key(s, project_key)
    if p is None or p.domain != domain:  # other-domain projects are not revealed (same as API 404)
        _say(f"Project {project_key} not found in {domain}.")
        raise typer.Exit(1)
    return p


@app.command("list")
def list_projects(domain: str = typer.Option(None, help=DOMAIN_HELP)) -> None:
    """List projects of one explicitly chosen domain."""
    from aeos.models.portfolio import Project

    chosen = _require_domain(domain)
    with _session() as s:
        rows = list(s.scalars(select(Project).where(Project.domain == chosen)
                              .order_by(Project.is_platform.desc(), Project.name)))
        if not rows:
            _say(f"No {chosen} projects. Run `aeos seed` (or `aeos project seed-self`) for the DLY AEOS self project.")
            return
        for p in rows:
            flag = " (platform)" if p.is_platform else ""
            _say(f"{p.project_key:<32} {p.lifecycle:<9} {p.data_classification:<12} {p.name}{flag}")


@app.command("show")
def show(project_key: str, domain: str = typer.Option(None, help=DOMAIN_HELP)) -> None:
    """Show one project with repositories and integrations (no secret values)."""
    from aeos.portfolio import service

    chosen = _require_domain(domain)
    with _session() as s:
        p = _find(s, project_key, chosen)
        view = service.project_view(s, p, detail=True)
        _say(json.dumps(view, indent=2, ensure_ascii=True, default=str))


def _print_inventory(result: dict[str, Any]) -> None:
    langs = ", ".join(f"{x['language']}({x['files']})" for x in result["languages"][:8])
    _say(f"commit   {result['commit']}  branch {result['branch']}  head {result.get('head_commit') or '-'}")
    _say(f"files    {result['files']}  languages: {langs or '-'}")
    _say(f"manifests {len(result['dependency_manifests'])}  CI: {', '.join(result['ci']) or '-'}")
    _say(f"tests    frameworks: {', '.join(result['tests']['frameworks']) or '-'} (not executed)")
    for gap in result["coverage_gaps"]:
        _say(f"gap      {gap}")


@app.command("inventory")
def inventory(project_key: str, domain: str = typer.Option(None, help=DOMAIN_HELP),
              commit: str = typer.Option(None, help="Exact commit (default HEAD)")) -> None:
    """Read-only inventory of the project's local checkout at a known commit (no scaffolding)."""
    from aeos.core.errors import AeosError
    from aeos.portfolio import repositories

    chosen = _require_domain(domain)
    with _session() as s:
        p = _find(s, project_key, chosen)
        repo = repositories.primary_repository(s, p.id)
        if repo is None:
            _say("No repository registered. Add one in the Control Center (Proyek > Detail).")
            raise typer.Exit(1)
        try:
            result = repositories.inventory_for_repository(s, None, p, repo, commit=commit)
        except AeosError as exc:
            _say(f"Inventory not possible: {exc.message} [{exc.code}]")
            raise typer.Exit(1) from exc
        _print_inventory(result)


@app.command("seed-self")
def seed_self(inventory_repo: bool = typer.Option(
        True, "--inventory/--no-inventory",
        help="Also take a read-only inventory of the platform checkout (git ls-tree; nothing is changed)")) -> None:
    """Register the DLY AEOS self-build project (PERSONAL, platform repo UNVERIFIED until verified)."""
    from aeos.core.errors import AeosError
    from aeos.portfolio import repositories
    from aeos.portfolio.seed import run

    with _session() as s:
        result = run(s)
        if inventory_repo and result.get("repository_id") and "warning" not in result:
            from aeos.models.portfolio import Project, RepositoryReference

            repo = s.get(RepositoryReference, result["repository_id"])
            project = s.get(Project, result["project_id"])
            try:
                inv = repositories.inventory_for_repository(s, None, project, repo)
                result["inventory"] = {"commit": inv["commit"], "files": inv["files"], "hash": inv["inventory_hash"]}
            except AeosError as exc:  # e.g. not a git checkout (installed package) or git missing
                result["inventory"] = {"status": "SKIPPED", "reason": exc.message, "code": exc.code}
    _say(json.dumps(result, indent=2, ensure_ascii=True, default=str))
