"""`aeos intake ...` - upload a file (e.g. a .md idea) into a project's intake from the terminal (§01 U-11).

Same pipeline as the dashboard upload (allow-list, size limit, archive inspection, isolated parser,
untrusted-instruction scan); the file is DATA and never executed. The local operator names an active
SUPER_ADMIN (``--actor``) and the project's domain explicitly (never inferred, U-05). ASCII output only.
"""

from __future__ import annotations

from pathlib import Path

import typer
from sqlalchemy import select

from aeos.core.enums import Domain, Role
from aeos.core.errors import AeosError

app = typer.Typer(help="Intake: upload files into a project (DATA only, never executed).", no_args_is_help=True)


def _echo(text: str) -> None:
    typer.echo(str(text).encode("ascii", "replace").decode("ascii"))


@app.command("upload")
def upload(
    project: str = typer.Argument(..., help="Project id or project_key"),
    file: Path = typer.Argument(..., help="File to upload (.md, .pdf, .docx, image, .zip, ...)"),
    domain: Domain = typer.Option(..., help="Explicit domain (must equal the project's domain)"),
    actor: str = typer.Option(..., help="Active SUPER_ADMIN username recorded as uploader"),
    classification: str = typer.Option(None, help="Data classification (default: the project's)"),
) -> None:
    """Upload one file into the project's intake and print the attachment id for `aeos plan create --attachment`."""
    from aeos.cli import ensure_database
    from aeos.core.config import get_settings
    from aeos.core.db import session_scope
    from aeos.core.timeutil import utcnow
    from aeos.identity.facade import Principal
    from aeos.intake import service
    from aeos.models.identity import AuthSession, User
    from aeos.models.portfolio import Project

    if not file.is_file():
        raise typer.BadParameter(f"{file} is not a file")
    settings = get_settings()
    ensure_database()
    with file.open("rb") as fh:
        data = fh.read(settings.max_upload_bytes + 1)  # the service refuses anything over the limit
    try:
        with session_scope() as s:
            user = s.scalar(select(User).where(User.username == actor.strip().lower()))
            if user is None or not user.is_active or user.role != Role.SUPER_ADMIN.value:
                raise typer.BadParameter("--actor must be an active SUPER_ADMIN username")
            proj = s.get(Project, project) or s.scalar(select(Project).where(Project.project_key == project))
            if proj is None:
                raise typer.BadParameter("project not found")
            if proj.domain != domain.value:
                raise typer.BadParameter(f"project {proj.project_key} is {proj.domain}, not {domain.value} "
                                         "(the domain is never changed or inferred)")
            # A local, non-persisted session object: the CLI is the local operator surface (like aeos admin).
            cli_session = AuthSession(token_hash="cli-local", user_id=user.id, csrf_token="", created_at=utcnow(),
                                      last_seen_at=utcnow(), expires_at=utcnow(), selected_domain=domain.value)
            principal = Principal(user=user, session=cli_session, role=Role(user.role),
                                  allowed_domains=[Domain(d) for d in (user.domain_scope or [d.value for d in Domain])],
                                  allowed_project_ids=user.project_scope)
            if not principal.can_access_project(proj.id, proj.domain):
                raise typer.BadParameter("the actor's scope does not include this project")
            att = service.ingest_upload(s, project=proj, principal=principal, filename=file.name, data=data,
                                        classification=classification, source="cli", settings=settings)
            view = service.attachment_view(att)
    except AeosError as exc:
        _echo(f"ERROR {exc.code}: {exc.message}")
        raise typer.Exit(1) from exc
    _echo(f"Attachment {view['id']} ({view['filename']}): scan {view.get('scan_status')}, "
          f"parse {view.get('parse_status')}")
    for f in view.get("scan_findings") or []:
        _echo(f"  finding: {f.get('kind')} {f.get('message', '')}")
    _echo(f"Use it in a plan: aeos plan create --project {project} --domain {domain.value} --attachment {view['id']} ...")
