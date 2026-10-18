"""`aeos secrets ...` - enter/inspect secrets locally (values never echoed; ASCII output only).

V1 enters secret values via this CLI only (there is no web write endpoint). Examples::

    aeos secrets set workers/openrouter            (hidden prompt)
    gh auth token | aeos secrets set personal/github --stdin
"""

from __future__ import annotations

import getpass
import sys
from datetime import datetime

import typer

app = typer.Typer(help="Secrets in OpenBao (or an owner-approved interim OS keyring). Values are never shown.",
                  no_args_is_help=True)


def _err(msg: str) -> None:
    typer.secho(msg, fg="red", err=True)


def _audit(action: str, ref: str, backend: str, result: str = "SUCCESS") -> None:
    from aeos.audit import service as audit
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope

    try:
        ensure_database()
        with session_scope() as s:
            audit.record(s, actor_type="cli", actor_id="local-owner", action=action, result=result,
                         target_type="secret_ref", target_id=ref, details={"backend": backend})
    except Exception as exc:  # pragma: no cover - audit failure is reported, value never involved
        typer.secho(f"warning: audit record failed ({type(exc).__name__})", fg="yellow", err=True)


def _read_value(stdin: bool) -> str:
    if stdin:
        data = sys.stdin.read()
        # strip only the trailing newline(s) a pipe adds; inner whitespace is part of the value
        return data.rstrip("\r\n")
    first = getpass.getpass("Value (hidden): ")
    second = getpass.getpass("Repeat value (hidden): ")
    if first != second:
        raise typer.BadParameter("values do not match")
    return first


@app.command("set")
def set_cmd(
    ref: str = typer.Argument(..., help="e.g. personal/github, workers/openrouter"),
    stdin: bool = typer.Option(False, "--stdin", help="Read the value from standard input (pipe)"),
    expires_at: str = typer.Option(None, help="Optional expiry (ISO 8601) shown as status, e.g. 2027-01-31"),
) -> None:
    """Store a secret value (prompted without echo, or piped with --stdin)."""
    from aeos.core.errors import AeosError
    from aeos.core.redaction import register_secret
    from aeos.secrets.provider import provider_put, strict_ref

    try:
        strict_ref(ref)
    except AeosError as exc:
        _err(f"{exc.code}: {exc.message}")
        raise typer.Exit(2) from exc
    exp = None
    if expires_at:
        try:
            exp = datetime.fromisoformat(expires_at)
        except ValueError as exc:
            raise typer.BadParameter("expires-at must be ISO 8601") from exc
    value = _read_value(stdin)
    if not value:
        _err("empty value - nothing stored")
        raise typer.Exit(2)
    register_secret(value)
    try:
        status = provider_put(ref, value, actor="cli", expires_at=exp)
    except AeosError as exc:
        _err(f"{exc.code}: {exc.message}")
        _audit("secret.put", ref, "none", "FAILED")
        raise typer.Exit(1) from exc
    finally:
        value = ""
    _audit("secret.put", ref, status.backend)
    typer.echo(f"Stored {ref} in {status.backend} (value not shown).")


@app.command("status")
def status_cmd(include_office: bool = typer.Option(False, help="Also list office/* references")) -> None:
    """List secret references with exists/backend/updated/expiry - never values."""
    from aeos.core.config import get_settings
    from aeos.secrets.health import secrets_status, store_health

    settings = get_settings()
    h = store_health(settings)
    typer.echo(f"store: {h['status']} backend={h['backend']}")
    for r in h["reasons"]:
        typer.echo(f"  reason: {r}")
    if h["interim"]["active"]:
        typer.secho(f"  WARNING: {h['interim']['residual_risk']}", fg="yellow")
    for rem in h["remedies"]:
        typer.echo(f"  remedy: {rem['command']}")
    out = secrets_status(settings, include_office=include_office)
    for item in out["items"]:
        typer.echo(f"{item['ref']:<28} {item['state']:<8} backend={item['backend']:<20} "
                   f"updated={item['updated_at'] or '-'} expires={item['expires_at'] or '-'}")


@app.command("delete")
def delete_cmd(ref: str = typer.Argument(...), yes: bool = typer.Option(False, "--yes", help="Confirm")) -> None:
    """Delete a secret (all versions)."""
    from aeos.core.errors import AeosError
    from aeos.secrets.provider import get_provider, provider_delete, strict_ref

    try:
        strict_ref(ref)
    except AeosError as exc:
        _err(f"{exc.code}: {exc.message}")
        raise typer.Exit(2) from exc
    if not yes and not typer.confirm(f"Delete {ref} permanently?"):
        raise typer.Exit(1)
    try:
        backend = get_provider().name
        provider_delete(ref, actor="cli")
    except AeosError as exc:
        _err(f"{exc.code}: {exc.message}")
        raise typer.Exit(1) from exc
    _audit("secret.delete", ref, backend)
    typer.echo(f"Deleted {ref} from {backend}.")


INTERIM_TEXT = """Interim OS-keyring secret store (exception to U-13 / master 26)

DLY AEOS uses OpenBao as the runtime vault. If you cannot run OpenBao yet, the owner may approve a
TEMPORARY exception that stores PERSONAL/worker/platform secrets in the OS credential store
({store}). This command records NOTHING - the exception must be a human decision.

Residual risk (shown on the dashboard while active):
  * no token TTL/lease, no vault audit device, no per-namespace ACL policy;
  * any process running as your OS user can read the values;
  * OFFICE secrets are never accepted in this mode.

To approve, record a Decision through the decisions API/UI (Policy & Decisions page) with:
  kind            = EXCEPTION
  domain          = PERSONAL
  target_type     = secret_store
  target_id       = interim-keyring
  details         = {{"secret_store_interim_keyring": true}}
  expires_at      = a near date (recommended: 30 days)
  rationale       = why OpenBao is not available yet

Then check: aeos secrets status   (backend must show os-keyring-interim)
Leave the exception: install OpenBao (aeos vault install / start / init), move the secrets with
`aeos secrets set ...`, and invalidate the decision.
"""


@app.command("interim-keyring")
def interim_keyring() -> None:
    """Explain the interim OS-keyring mode and the owner decision it needs (records nothing)."""
    from aeos.secrets.provider import platform_label

    typer.echo(INTERIM_TEXT.format(store=platform_label()))
