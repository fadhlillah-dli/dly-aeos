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
                caps = provider.apply_enforcement_evidence(s, caps)
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
    if caps.enforcement_test:
        _echo(f"  enforcement evidence {caps.enforcement_test.get('evidence_id')}: rulesets "
              f"{[r.get('id') for r in caps.enforcement_test.get('rulesets') or []]} observed "
              f"{caps.enforcement_test.get('observed_at') or '-'} {caps.enforcement_test.get('reason') or ''}")
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
    """Read-only GETs of a registered deployment target: health endpoint plus the owner's smoke/critical-flow and
    SLI checks (G6/G8 evidence). Staging runs only while CAN_VALIDATE_STAGING is READY. Never deploys."""
    from aeos.delivery import deployment

    try:
        with _session() as s:
            result = deployment.observe(s, project_id, environment, sha, actor=ACTOR)
    except AeosError as exc:
        with _session() as s:  # the refused attempt is audited (the observation's own work was rolled back)
            deployment.audit_refusal(s, project_id, environment, exc, actor=ACTOR)
        _echo(f"{exc.code}: {exc.message}")
        for reason in (exc.details or {}).get("reasons") or []:
            _echo(f"  - {reason}")
        raise typer.Exit(2) from exc
    _echo(f"{result['environment']} {result['target']}: {result['status']} - {result['reason']}")
    for c in result.get("checks") or []:
        _echo(f"  [{c['status']:<10}] {c['kind']} {c['name']} {c['path']}: {c['reason']}")
    for c in result.get("slis") or []:
        _echo(f"  [{c['status']:<10}] sli {c['name']} {c['path']}: {c['reason']}")
    _echo(f"artifact digest: {result['artifact']['status']}")
    _echo(f"evidence {result['evidence_id']}")
    post = result.get("post_release")
    if post and post.get("incident_id"):
        _echo(f"post-release failure: alert {post['alert_id']} incident {post['incident_id']} decision request "
              f"{post.get('decision_request_id')} - recommendation {post['recommendation']['status']} "
              "(rollback is a HUMAN action; see " + str(post.get("runbook")) + ")")


@app.command("target-checks")
def target_checks_cmd(
    project_id: str = typer.Argument(..., help="Project id with a registered deployment-target integration"),
    environment: str = typer.Option("staging", help="staging | production"),
    file: str = typer.Option(None, "--file", help="JSON file with smoke_checks / sli_checks / rollback_on / "
                                                 "require_artifact_digest to set (omit to show)"),
) -> None:
    """Show or set the owner's checks of a deployment target: smoke/critical-flow GET checks, product SLI checks
    with owner thresholds, rollback criteria, artifact-digest requirement. AEOS still only observes."""
    import json
    from pathlib import Path

    from aeos.delivery import deployment
    from aeos.models.portfolio import Project

    try:
        with _session() as s:
            project = s.get(Project, project_id)
            if project is None:
                _echo("project not found")
                raise typer.Exit(2)
            target = deployment.target_for(s, project, environment)
            if target is None:
                _echo(f"no {environment} deployment-target registered for this project")
                raise typer.Exit(2)
            if file:
                try:
                    body = json.loads(Path(file).read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    _echo(f"cannot read {file}: {type(exc).__name__}")
                    raise typer.Exit(2) from exc
                if not isinstance(body, dict):
                    _echo("the file must contain a JSON object")
                    raise typer.Exit(2)
                view = deployment.set_target_checks(s, target, body, actor=ACTOR, actor_type="cli")
            else:
                view = deployment.target_view(s, target)
    except AeosError as exc:
        _echo(f"{exc.code}: {exc.message}")
        raise typer.Exit(2) from exc
    st = view["settings"]
    _echo(f"{view['environment']} {st.get('base_url')}{st.get('health_path', '')} (integration {view['integration_id']})")
    for c in st.get("smoke_checks") or []:
        _echo(f"  {c['kind']:<14} {c['name']}: GET {c['path']} -> {c['expect_status']} keys={c['json_keys']}")
    for c in st.get("sli_checks") or []:
        _echo(f"  sli            {c['name']}: GET {c['path']} {c['json_key']} min={c['min']} max={c['max']}")
    _echo(f"  rollback criteria: {st.get('rollback_on') or 'UNDECIDED (owner)'}  require digest: "
          f"{st.get('require_artifact_digest')}")
    _echo(f"  latest observation: {view['latest_observation'].get('status')} "
          f"{view['latest_observation'].get('evidence_id') or ''}")


@app.command("artifacts")
def artifacts_cmd(
    project_id: str = typer.Argument(..., help="Project id (PERSONAL, GitHub repository registered)"),
    sha: str = typer.Option(..., help="Full commit sha (normally the merge commit)"),
    repo: str = typer.Option(None, help="owner/name (default: the project's GitHub repository)"),
) -> None:
    """Read-only: provider CI artifact digests (+ attestation presence) of one commit, recorded as provider-ci
    ARTIFACT_DIGEST evidence (build once, master 31). Promotion stays a human action."""
    from aeos.delivery import provider
    from aeos.models.portfolio import Project, RepositoryReference

    try:
        with _session() as s:
            project = s.get(Project, project_id)
            if project is None or project.domain != "PERSONAL":
                _echo("project not found (PERSONAL/GitHub projects only)")
                raise typer.Exit(2)
            refs = [r for r in s.scalars(select(RepositoryReference).where(RepositoryReference.project_id == project.id))
                    if (r.provider or "").upper() == "GITHUB" and (not repo or r.full_name == repo)]
            if not refs:
                _echo("no registered GitHub repository for this project")
                raise typer.Exit(2)
            token, _credential = provider.github_token(project.domain)
            result = provider.fetch_artifact_digests(refs[0].full_name, sha, token=token)
            ev = provider.record_artifact_evidence(s, project, result)
    except AeosError as exc:
        _echo(f"{exc.code}: {exc.message}")
        raise typer.Exit(2) from exc
    _echo(f"{result.repo}@{result.sha[:12]}: {result.status}" + (f" ({result.error})" if result.error else ""))
    for a in result.artifacts:
        _echo(f"  {a['name']}: {a['digest'] or 'no digest'} eligible={a['eligible']} "
              f"attestation={a['attestation']['status']}")
    _echo(f"evidence {ev.id}" if ev else "nothing recorded (a failed read is never evidence)")


@app.command("enforcement")
def enforcement_cmd(
    repo: str = typer.Option(None, help="owner/name (default: the platform project's GitHub repository)"),
    branch: str = typer.Option(None, help="Branch (default: repository default branch)"),
    period: str = typer.Option("week", help="hour | day | week | month (rule history window)"),
    attest: str = typer.Option(None, "--attest", help="Owner fallback: describe the refused bypass attempt (stored as "
                                                     "an untrusted claim, never VERIFIED)"),
) -> None:
    """Read the provider's ruleset evaluation history (GET only): a change refused by an ACTIVE ruleset is recorded
    as PROVIDER_ENFORCEMENT_TEST evidence; then 'delivery capabilities' shows enforcement VERIFIED for exactly this
    repository, branch and ruleset until the ruleset changes. Run it after your own bypass attempt."""
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
            if ref is None or (ref.provider or "").upper() != "GITHUB":
                _echo("No registered GitHub repository found. Pass --repo.")
                raise typer.Exit(2)
            project = s.get(Project, ref.project_id)
            target_branch = branch or ref.default_branch or "main"
            if attest:
                claim = provider.record_owner_attestation(s, project, repo=ref.full_name, branch=target_branch,
                                                          statement=attest, actor_user_id=ACTOR)
                _echo(f"recorded owner attestation {claim.id} (untrusted claim; enforcement shows CLAIMED, not "
                      "VERIFIED)")
                return
            token, _credential = provider.github_token(project.domain)
            obs = provider.fetch_enforcement_observation(ref.full_name, target_branch, token=token, time_period=period)
            ev = provider.record_enforcement_evidence(s, project, obs)
    except AeosError as exc:
        _echo(f"{exc.code}: {exc.message}")
        raise typer.Exit(2) from exc
    _echo(f"{obs.repo}@{obs.branch} ({obs.time_period}): {obs.status}" + (f" - {obs.error}" if obs.error else ""))
    for x in obs.refusals:
        _echo(f"  refused {x['pushed_at']} by {x['actor_name']}: rulesets {[h['ruleset_id'] for h in x['rulesets']]}")
    _echo(f"evidence {ev.id}" if ev else "nothing recorded (no refusal observed is not proof of enforcement)")


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
