"""Local operator commands for bootstrap and recovery (§26). These run on the user's machine with
filesystem access to the data directory — that local access is the authority for bootstrap."""

from __future__ import annotations

import getpass

import typer
from rich.console import Console
from sqlalchemy import select

from aeos.core.db import session_scope
from aeos.core.enums import Role
from aeos.core.errors import AeosError
from aeos.identity import service as identity
from aeos.models.identity import User

app = typer.Typer(help="Local accounts: bootstrap the first SUPER_ADMIN, recovery, listing.", no_args_is_help=True)
console = Console()


def _prompt_password(confirm: bool = True) -> str:
    pw = getpass.getpass("Password (min 12 chars): ")
    if confirm and getpass.getpass("Repeat password: ") != pw:
        raise typer.BadParameter("passwords do not match")
    return pw


@app.command("create")
def create(
    username: str = typer.Option(..., prompt=True, help="Login name"),
    display_name: str = typer.Option("", help="Display name"),
    role: Role = typer.Option(Role.SUPER_ADMIN, help="SUPER_ADMIN for the first account; VIEWER is read-only"),
) -> None:
    """Create an account. The first account must be SUPER_ADMIN (bootstrap)."""
    from aeos.cli import ensure_database

    ensure_database()
    password = _prompt_password()
    try:
        with session_scope() as s:
            if not identity.admin_exists(s):
                if role != Role.SUPER_ADMIN:
                    raise typer.BadParameter("bootstrap requires the first account to be SUPER_ADMIN")
                user = identity.bootstrap_admin(s, username=username, password=password, display_name=display_name)
            else:
                user = identity.create_user(s, username=username, password=password, role=role,
                                            display_name=display_name, actor="cli")
            console.print(f"[green]Created {user.role} account '{user.username}'.[/green]")
    except AeosError as exc:
        console.print(f"[red]{exc.code}: {exc.message}[/red]")
        raise typer.Exit(1) from exc


@app.command("recover")
def recover(username: str = typer.Argument(..., help="Account to recover")) -> None:
    """Issue a one-time temporary password and revoke all sessions for the account."""
    from aeos.cli import ensure_database

    ensure_database()
    try:
        with session_scope() as s:
            temp = identity.recover_user(s, username=username)
    except AeosError as exc:
        console.print(f"[red]{exc.code}: {exc.message}[/red]")
        raise typer.Exit(1) from exc
    console.print("Temporary password (shown once; you must change it at next login):")
    console.print(f"[bold]{temp}[/bold]")


@app.command("list")
def list_users() -> None:
    from aeos.cli import ensure_database

    ensure_database()
    with session_scope() as s:
        for u in s.scalars(select(User).order_by(User.created_at)):
            flag = " (must change password)" if u.must_change_password else ""
            console.print(f"{u.username:24} {u.role:12} active={u.is_active}{flag}")
