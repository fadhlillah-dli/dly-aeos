"""`aeos usage ...` - the read-only usage value report (owner request R34; ASCII output only)."""

from __future__ import annotations

import json

import typer

app = typer.Typer(help="Usage value of the worker plans (read-only, PERSONAL scope).", no_args_is_help=True)


@app.command("report")
def report_cmd(
    month: str | None = typer.Option(None, "--month", help="Calendar month YYYY-MM in UTC (default: the current one)"),
    as_json: bool = typer.Option(False, "--json", help="Print the report as JSON"),
) -> None:
    """OpenCode Go allowance used per route, OpenRouter free requests and paid spend with the owner BUDGET, work
    per role (pass rate, escalations, wall-clock) and Claude usage. PERSONAL only; writes nothing, calls no network."""
    from aeos.cli import ensure_database
    from aeos.core.db import session_factory
    from aeos.core.errors import ValidationFailed
    from aeos.cost.usage_report import build_report, render_text

    ensure_database()
    session = session_factory()()
    try:
        data = build_report(session, month=month)
    except ValidationFailed as exc:
        typer.echo(f"error: {exc.message}", err=True)
        raise typer.Exit(2) from exc
    finally:
        session.rollback()  # read-only: nothing is ever committed
        session.close()
    if as_json:
        typer.echo(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True))
        return
    for line in render_text(data):
        typer.echo(line)
