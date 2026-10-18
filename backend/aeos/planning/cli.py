"""`aeos plan ...` - create, run (synchronously), inspect and import manual Claude results.

The CLI is a local operator surface: the named ``--actor`` must be an active SUPER_ADMIN account and
the domain must be given explicitly (it is never inferred, U-05). Output is ASCII only.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from sqlalchemy import select

from aeos.core.db import session_scope
from aeos.core.enums import Domain, Role
from aeos.core.errors import AeosError

app = typer.Typer(help="Planning: create plans, run the 4-stage Opus/Fable pipeline, import manual results.",
                  no_args_is_help=True)
console = Console(highlight=False, markup=False)  # plan titles/errors are DATA, never markup


def _ascii(text: object) -> str:
    return str(text).encode("ascii", "replace").decode("ascii")


def _ensure_db() -> None:
    from aeos.cli import ensure_database

    ensure_database()


def _actor_id(session, username: str) -> str:
    from aeos.models.identity import User

    user = session.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None or not user.is_active or user.role != Role.SUPER_ADMIN.value:
        raise typer.BadParameter("--actor must be an active SUPER_ADMIN username")
    return user.id


def _fail(exc: AeosError) -> None:
    console.print(_ascii(f"{exc.code}: {exc.message}"))
    for r in (exc.details or {}).get("reasons", []) or []:
        console.print(_ascii(f"  - {r}"))
    for e in (exc.details or {}).get("errors", []) or []:
        console.print(_ascii(f"  - {e}"))
    raise typer.Exit(1)


@app.command("create")
def create(
    project: str = typer.Option(..., help="Project id or project_key"),
    domain: Domain = typer.Option(..., help="Explicit domain (must equal the project's domain)"),
    title: str = typer.Option(..., help="Plan title"),
    prompt: str = typer.Option(None, help="Idea / prompt text"),
    prompt_file: Path = typer.Option(None, help="Read the prompt from a UTF-8 file (.md/.txt)"),
    attachment: list[str] = typer.Option(None, help="Attachment id (repeatable)"),
    actor: str = typer.Option(..., help="SUPER_ADMIN username recorded as creator"),
) -> None:
    """Create a plan (DRAFT -> CONTEXT_READY) for an existing project."""
    from aeos.models.portfolio import Project
    from aeos.planning import service

    if not prompt and not prompt_file:
        raise typer.BadParameter("give --prompt or --prompt-file")
    text = prompt_file.read_text(encoding="utf-8-sig") if prompt_file else prompt  # BOM-safe (Windows editors)
    _ensure_db()
    try:
        with session_scope() as s:
            actor_id = _actor_id(s, actor)
            proj = s.get(Project, project) or s.scalar(select(Project).where(Project.project_key == project))
            if proj is None:
                raise typer.BadParameter("project not found")
            plan = service.create_plan(s, project=proj, domain=domain, title=title, prompt=text or "",
                                       attachment_ids=list(attachment or []), actor_user_id=actor_id)
            console.print(f"Plan {plan.id} created [{plan.state}]")
            console.print(_ascii(f"  {plan.state_reason}"))
    except AeosError as exc:
        _fail(exc)


@app.command("run")
def run(plan_id: str = typer.Argument(...), actor: str = typer.Option(..., help="SUPER_ADMIN username")) -> None:
    """Run (or resume) the 4-stage pipeline synchronously."""
    from aeos.core.db import session_factory
    from aeos.planning import service

    _ensure_db()
    with session_scope() as s:
        actor_id = _actor_id(s, actor)
    session = session_factory()()
    try:
        plan = service.run_pipeline(session, plan_id, actor=actor_id)
        console.print(_ascii(f"Plan {plan.id}: {plan.state} - {plan.state_reason}"))
        manual = service.manual_instructions(plan)
        if manual:
            for step in manual["steps"]:
                console.print(_ascii(f"  * {step}"))
    except AeosError as exc:
        session.rollback()
        _fail(exc)
    finally:
        session.close()


@app.command("status")
def status(plan_id: str = typer.Argument(...)) -> None:
    """Show plan state, versions and per-stage runs."""
    from aeos.planning import service

    _ensure_db()
    with session_scope() as s:
        try:
            plan = service._get_plan(s, plan_id)
        except AeosError as exc:
            _fail(exc)
            return
        d = service.plan_detail(s, plan)
        console.print(_ascii(f"Plan {plan.id} '{plan.title}' domain={plan.domain} state={plan.state}"))
        console.print(_ascii(f"  reason: {plan.state_reason}"))
        for v in d["versions"]:
            mark = " (current)" if v["id"] == plan.current_version_id else ""
            mark += " (approved)" if v["id"] == plan.approved_version_id else ""
            console.print(_ascii(f"  v{v['version']} {v['manifest_hash']} {v['source']}{mark}"))
        for st in d["stages"]:
            r = st["latest"]
            if r is None:
                console.print(f"  {st['stage']:17} {st['role']:24} not run")
                continue
            console.print(_ascii(f"  {st['stage']:17} {st['role']:24} {r['status']:16} requested={r['model_requested']} "
                                 f"resolved={r['model_resolved'] or 'UNKNOWN'} mode={r['mode']}"))
        if d["manual"]:
            for step in d["manual"]["steps"]:
                console.print(_ascii(f"  * {step}"))


@app.command("import-manual")
def import_manual(plan_id: str = typer.Argument(...), actor: str = typer.Option(..., help="SUPER_ADMIN username")) -> None:
    """Import the manual Claude Code result for the waiting stage and continue the pipeline."""
    from aeos.core.db import session_factory
    from aeos.planning import service

    _ensure_db()
    with session_scope() as s:
        actor_id = _actor_id(s, actor)
    session = session_factory()()
    try:
        plan = service.import_manual_result(session, plan_id, actor=actor_id)
        console.print(_ascii(f"Plan {plan.id}: {plan.state} - {plan.state_reason}"))
    except AeosError as exc:
        session.rollback()
        _fail(exc)
    finally:
        session.close()


@app.command("schemas")
def schemas(write: bool = typer.Option(False, "--write", help="Write contracts/planning/*.schema.json")) -> None:
    """Print or (re)write the planning JSON Schemas used with `claude -p --json-schema`."""
    from aeos.planning import contracts

    if write:
        for p in contracts.write_contract_files():
            console.print(_ascii(f"wrote {p}"))
        return
    for name, filename in contracts.SCHEMA_FILES.items():
        console.print(f"{name}: contracts/planning/{filename}")
