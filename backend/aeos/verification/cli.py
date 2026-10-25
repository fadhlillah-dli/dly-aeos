"""`aeos verify ...` - independent verifier, checkpoint review, acceptance (ASCII-only output).

Local CLI access to the data directory is the operator authority (same model as `aeos admin`).
Exit codes: 0 = PASS/ACCEPTED, 1 = not passed (FAIL/NOT_PROVEN/QUARANTINED/REJECTED), 2 = BLOCKED/error.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from aeos.core.errors import AeosError

app = typer.Typer(help="Independent verification, checkpoint review and acceptance (master 34).",
                  no_args_is_help=True)
ACTOR = "local-operator"


def _ascii(value: Any) -> str:
    return str(value).encode("ascii", "replace").decode("ascii")


def _echo(line: Any = "") -> None:
    typer.echo(_ascii(line))


def _fail(exc: AeosError) -> None:
    _echo(f"{exc.code}: {exc.message}")
    raise typer.Exit(2) from exc


def _session():
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope

    ensure_database()
    return session_scope()


@app.command("run")
def run_cmd(
    run_id: str = typer.Argument(..., help="WorkerRun id (must be in REVIEW)"),
    candidate: str = typer.Option(None, help="Candidate worktree path (default: the run's workspace_ref)"),
    no_repair: bool = typer.Option(False, "--no-repair", help="Do not request a repair attempt on FAIL"),
    as_json: bool = typer.Option(False, "--json", help="Print the full outcome as JSON"),
) -> None:
    """Run the independent verifier (trusted suite from the controller checkout) for one run."""
    from aeos.verification import verifier

    try:
        with _session() as s:
            out = verifier.verify_run(s, run_id, actor=ACTOR, actor_type="cli", candidate_path=candidate,
                                      request_repair_on_fail=not no_repair)
            data = out.to_dict()
    except AeosError as exc:
        _fail(exc)
        return
    if as_json:
        _echo(json.dumps(data, indent=2, default=str))
    else:
        _echo(f"verifier status: {data['status']}")
        _echo(f"head: {data['head_commit']}  base: {data['base_commit']}")
        _echo(f"suite hash: {data['suite_hash']}")
        for name, chk in data["checks"].items():
            _echo(f"  [{chk['status']:<11}] {name}: {chk['summary']}")
        for r in data["reasons"]:
            _echo(f"  reason: {r}")
        for n in data["not_proven"]:
            _echo(f"  not proven: {n}")
        _echo(f"report evidence: {data['report_evidence_id']}  run update: {data['run_update']}  "
              f"repair: {data['repair']}")
    raise typer.Exit(0 if data["status"] == "PASS" else (2 if data["status"] == "BLOCKED" else 1))


@app.command("pending")
def pending_cmd(limit: int = typer.Option(1, min=1, max=20, help="Maximum runs to verify now")) -> None:
    """Verify runs in REVIEW that have no verification report for their current head."""
    from aeos.cli import ensure_database
    from aeos.verification import loop

    ensure_database()
    results = loop.run_once(limit=limit)
    if not results:
        _echo("No run is waiting for verification.")
    for r in results:
        _echo(f"run {r['run_id']}: {r['status']} {r.get('reason') or r.get('report') or ''}")


@app.command("review")
def review_cmd(
    run_id: str = typer.Argument(..., help="Verified WorkerRun id"),
    security: bool = typer.Option(False, "--security", help="Request the separate Security Reviewer session"),
    kind: str = typer.Option(None, "--kind", help="Specialist review kind: SECURITY | A11Y | PRIVACY | SRE"),
) -> None:
    """Request the Claude checkpoint review (official claude binary; manual package fallback)."""
    from aeos.verification import review

    reviewer = (kind or ("SECURITY" if security else "FEATURE")).upper()
    try:
        with _session() as s:
            outcome = review.review_run(s, run_id, actor=ACTOR, actor_type="cli", reviewer=reviewer)
            view = review.review_view(outcome.review)
            acc = outcome.acceptance
            notes = outcome.notes
    except AeosError as exc:
        _fail(exc)
        return
    _echo(f"review {view['id']}: status={view['status']} verdict={view['verdict']}")
    if view["status_reason"]:
        _echo(f"  {view['status_reason']}")
    for f in view["findings"]:
        _echo(f"  [{f.get('severity')}] {f.get('title')}")
    if acc:
        _echo(f"acceptance: {acc['acceptance_status']} (valid={acc['valid']})")
    for n in notes:
        _echo(f"  note: {n}")
    raise typer.Exit(0 if view["verdict"].startswith("PASS") else 1)


@app.command("collect-review")
def collect_review_cmd(
    review_id: str = typer.Argument(..., help="Review waiting for a manual result (NEEDS_DECISION)"),
    result_file: Path = typer.Option(None, "--file", help="JSON result copied from official Claude Code"),
) -> None:
    """Import the result of a manual review package (master 12 fallback)."""
    from aeos.verification import review

    structured = None
    if result_file is not None:
        try:
            structured = json.loads(result_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _echo(f"cannot read result file: {exc}")
            raise typer.Exit(2) from exc
    try:
        with _session() as s:
            outcome = review.collect_manual_review(s, review_id, actor=ACTOR, actor_type="cli", structured=structured)
            view = review.review_view(outcome.review)
    except AeosError as exc:
        _fail(exc)
        return
    _echo(f"review {view['id']}: status={view['status']} verdict={view['verdict']} (mode {view['mode']})")
    raise typer.Exit(0 if view["verdict"].startswith("PASS") else 1)


@app.command("accept")
def accept_cmd(run_id: str = typer.Argument(..., help="Reviewed WorkerRun id")) -> None:
    """Re-evaluate acceptance (verifier PASS + review PASS-CONTROLLED or better)."""
    from aeos.verification import acceptance

    try:
        with _session() as s:
            rec = acceptance.evaluate_acceptance(s, run_id, actor=f"acceptance-engine (cli {ACTOR})")
            view = acceptance.acceptance_view(s, rec)
    except AeosError as exc:
        _fail(exc)
        return
    _echo(f"acceptance {view['id']}: {view['acceptance_status']} valid={view['valid']}")
    for r in view["reasons"]:
        _echo(f"  reason: {r}")
    raise typer.Exit(0 if view["acceptance_status"] == "ACCEPTED" else 1)


@app.command("suite-register")
def suite_register_cmd(
    project_id: str = typer.Argument(..., help="Project id (not the platform project)"),
    as_user: str = typer.Option(..., "--as", help="Your SUPER_ADMIN username (a human owner decision)"),
    domain: str = typer.Option(..., "--domain", help="The project's domain, PERSONAL or OFFICE (chosen by you)"),
    rationale: str = typer.Option(..., help="Why this grader is trusted (min 5 chars; stored on the decision)"),
    commit: str = typer.Option(None, help="Reviewed commit/branch of the project's repository (default: its "
                                          "default branch head)"),
) -> None:
    """Register the project's trusted suite (tests/acceptance of a reviewed commit) outside every candidate,
    bound to a human POLICY decision on the suite hash (ADR-0060)."""
    from sqlalchemy import select

    from aeos.core.enums import Role
    from aeos.core.errors import NotFound
    from aeos.identity import facade as identity
    from aeos.models.identity import User
    from aeos.models.portfolio import Project
    from aeos.verification import suite

    password = typer.prompt(f"Password for {as_user} (confirms this human decision)", hide_input=True,
                            default="", show_default=False)
    try:
        with _session() as s:
            # Human-only AUTH: an active SUPER_ADMIN proves it by password (never stored or logged)
            user = s.scalars(select(User).where(User.username == as_user)).first()
            if user is None or not user.is_active or user.role != Role.SUPER_ADMIN.value:
                raise typer.BadParameter(f"{as_user!r} is not an active SUPER_ADMIN (decisions are human-only)")
            if not password or not identity.verify_password(user.password_hash, password):
                raise typer.BadParameter("password check failed - no decision recorded")
            uid = str(user.id)
            project = s.get(Project, project_id)
            if project is None or project.domain != domain.strip().upper():
                raise NotFound("project not found in this domain")
            out = suite.register_project_suite(s, project, actor_user_id=uid, rationale=rationale, commit=commit)
    except AeosError as exc:
        _fail(exc)
        return
    _echo(f"project {out['project_id']}: trusted suite {out['suite_hash']} from {out['source_commit']} "
          f"({out['files']} files), decision {out['decision_id']}")


@app.command("suite-status")
def suite_status_cmd(project_id: str = typer.Argument(..., help="Project id"),
                     domain: str = typer.Option(..., "--domain", help="The project's domain, PERSONAL or OFFICE")) -> None:
    """Show the project's trusted suite registration (read-only)."""
    from aeos.core.errors import NotFound
    from aeos.models.portfolio import Project
    from aeos.verification import suite

    try:
        with _session() as s:
            project = s.get(Project, project_id)
            if project is None or project.domain != domain.strip().upper():
                raise NotFound("project not found in this domain")
            st = suite.suite_status(s, project)
    except AeosError as exc:
        _fail(exc)
        return
    _echo(f"project {project_id}: {st['status']} {st.get('code') or ''} {st.get('reason') or ''}".rstrip())
    if st.get("suite_hash"):
        _echo(f"  suite {st['suite_hash']} commit {st.get('source_commit') or '-'} decision {st.get('decision_id')}")


@app.command("gates")
def gates_cmd(task_id: str = typer.Argument(..., help="TaskSpec id")) -> None:
    """Show delivery gates G0-G8 for a task (computed from records; merge stays human)."""
    from aeos.delivery import gates

    try:
        with _session() as s:
            data = gates.compute_gates(s, task_id)
    except AeosError as exc:
        _fail(exc)
        return
    _echo(f"task {data['task_key']} ({data['task_id']}) head {data['head_commit']}")
    for g in data["gates"]:
        _echo(f"  {g['gate']} {g['name']:<17} {g['status']:<15} {g['summary']}")
    sep = data["separate_status"]
    _echo(f"worker-reported={sep['worker_reported']['status']} verifier={sep['verifier']['status']} "
          f"reviewer={sep['reviewer']['verdict']} accepted={sep['accepted']['status']} merged={sep['merged']['status']}")
    _echo(f"ready for human merge: {data['ready_for_human_merge']} (merge authority: HUMAN_ONLY)")
