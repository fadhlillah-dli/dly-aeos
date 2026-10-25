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
    """Read-only capability check: GitHub branch protection/rulesets (PERSONAL) or Bitbucket Cloud branch
    restrictions and merge-check enforcement (OFFICE, only while the OFFICE adapter is authorised)."""
    from aeos.delivery import bitbucket, provider
    from aeos.models.portfolio import Project, RepositoryReference
    from aeos.portfolio import facade as portfolio

    try:
        with _session() as s:
            if repo:
                if not (provider.REPO_RE.match(repo) or bitbucket.REPO_RE.match(repo)):
                    provider.validate_repo(repo)
                ref = s.scalar(select(RepositoryReference).where(RepositoryReference.full_name == repo))
            else:
                platform = portfolio.platform_project(s)
                ref = next((r for r in portfolio.repositories(s, platform.id) if r.provider.upper() == "GITHUB"),
                           None) if platform else None
            if ref is None:
                _echo("No registered repository found. Register it first (aeos project ...) or pass --repo.")
                raise typer.Exit(2)
            project = s.get(Project, ref.project_id)
            if (ref.provider or "").upper() == "BITBUCKET":
                caps = bitbucket.capabilities_for(s, project, ref.full_name, branch or ref.default_branch)
            else:
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
    _echo(f"merge gate: {caps.merge_gate}  enforcement tested: {caps.enforcement_tested}  "
          f"entitlement: {caps.entitlement}")
    for r in caps.residual_risks:
        _echo(f"  residual risk: {r}")


@app.command("sync-pr")
def sync_pr_cmd(pr_id: str = typer.Argument(..., help="Delivery PR record id")) -> None:
    """Observe a PR on its provider (GitHub, or Bitbucket Cloud for OFFICE): state, head/base, merged_by, CI;
    stale acceptance is invalidated."""
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


@app.command("observe-target")
def observe_target_cmd(
    project_id: str = typer.Argument(..., help="Project id with a registered deployment-target integration"),
    environment: str = typer.Option("staging", help="staging | production"),
    sha: str = typer.Option(..., help="Commit the target is expected to serve"),
) -> None:
    """Read-only GET of a registered deployment target's health endpoint (G6/G8 evidence). Never deploys."""
    from aeos.delivery import deployment

    try:
        with _session() as s:
            result = deployment.observe(s, project_id, environment, sha, actor=ACTOR)
    except AeosError as exc:
        _echo(f"{exc.code}: {exc.message}")
        raise typer.Exit(2) from exc
    _echo(f"{result['environment']} {result['target']}: {result['status']} - {result['reason']}")
    _echo(f"evidence {result['evidence_id']}")


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
