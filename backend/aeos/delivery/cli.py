"""`aeos delivery ...` - provider capabilities, PR sync, gates and bundle preview (ASCII-only output).

All provider calls are read-only (GET). There is no merge or deploy command.
"""

from __future__ import annotations

from typing import Any

import typer
from sqlalchemy import select

from aeos.core.errors import AeosError

app = typer.Typer(help="Delivery & CI: provider capabilities, PR observation, gates (master 31).",
                  no_args_is_help=True)
ACTOR = "local-operator"


def _echo(line: Any = "") -> None:
    typer.echo(str(line).encode("ascii", "replace").decode("ascii"))


def _session():
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope

    ensure_database()
    return session_scope()


@app.command("capabilities")
def capabilities_cmd(
    repo: str = typer.Option(None, help="owner/name (default: the platform project's GitHub repository)"),
    branch: str = typer.Option(None, help="Branch to check (default: repository default branch)"),
    record: bool = typer.Option(False, "--record", help="Store the observation as PROVIDER_CAPABILITY evidence"),
) -> None:
    """Read-only GitHub capability check: branch protection, rulesets, required checks, plan."""
    from aeos.delivery import provider
    from aeos.models.portfolio import Project, RepositoryReference
    from aeos.portfolio import facade as portfolio

    try:
        with _session() as s:
            if repo:
                provider.validate_repo(repo)
                ref = s.scalar(select(RepositoryReference).where(RepositoryReference.full_name == repo))
            else:
                platform = portfolio.platform_project(s)
                ref = next((r for r in portfolio.repositories(s, platform.id) if r.provider.upper() == "GITHUB"),
                           None) if platform else None
            if ref is None:
                _echo("No registered GitHub repository found. Register it first (aeos project ...) or pass --repo.")
                raise typer.Exit(2)
            project = s.get(Project, ref.project_id)
            token, credential = provider.github_token(project.domain)
            caps = provider.check_capabilities(ref.full_name, branch or ref.default_branch, token=token,
                                               credential=credential)
            if record:
                ev = provider.record_capability_evidence(s, project, caps)
                _echo(f"recorded evidence {ev.id}")
    except AeosError as exc:
        _echo(f"{exc.code}: {exc.message}")
        raise typer.Exit(2) from exc
    _echo(f"repo {caps.repo} branch {caps.branch} visibility {caps.visibility} plan {caps.plan or 'UNKNOWN'}")
    _echo(f"credential: {caps.credential}")
    for c in caps.checks:
        _echo(f"  [{c.status:<15}] {c.key}: {c.summary}")
    _echo(f"merge gate: {caps.merge_gate}  enforcement tested: {caps.enforcement_tested}")
    for r in caps.residual_risks:
        _echo(f"  residual risk: {r}")


@app.command("sync-pr")
def sync_pr_cmd(pr_id: str = typer.Argument(..., help="Delivery PR record id")) -> None:
    """Observe a PR on GitHub (state, head/base, merged_by, CI) and invalidate stale acceptance."""
    from aeos.delivery import sync

    try:
        with _session() as s:
            result = sync.sync_pull_request(s, pr_id, actor=ACTOR)
    except AeosError as exc:
        _echo(f"{exc.code}: {exc.message}")
        raise typer.Exit(2) from exc
    pr = result["pull_request"]
    _echo(f"PR #{pr['number']} {pr['state']} merged_by={pr['merged_by_login']} head={pr['observed_head_sha']}")
    for n in result.get("notes", []):
        _echo(f"  note: {n}")


@app.command("gates")
def gates_cmd(task_id: str = typer.Argument(..., help="TaskSpec id")) -> None:
    """Show delivery gates G0-G8 for a task."""
    from aeos.verification.cli import gates_cmd as verify_gates

    verify_gates(task_id)


@app.command("bundle")
def bundle_cmd(task_id: str = typer.Argument(..., help="Accepted TaskSpec id")) -> None:
    """Print the PR evidence bundle (markdown) for an accepted task."""
    from aeos.delivery import pr_bundle

    try:
        with _session() as s:
            bundle = pr_bundle.build_pr_bundle(s, task_id)
    except AeosError as exc:
        _echo(f"{exc.code}: {exc.message}")
        raise typer.Exit(2) from exc
    _echo(bundle.markdown)
