"""`aeos doctor` - Readiness Doctor bootstrap layer (§08 layer 1).

Runs without the server, without an LLM, and even when the database is not initialised (results are
then only printed). Exit code 1 when a critical check for CAN_OPEN_DASHBOARD fails. Remedies are
printed for the operator to copy; nothing is installed or changed.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import typer

from aeos.inventory.asciitext import ascii_safe, table

app = typer.Typer(help="Readiness Doctor: bootstrap checks without server or LLM (prints remedies, never installs).",
                  invoke_without_command=True, no_args_is_help=False)

GROUP_OPTION = typer.Option(None, "--group", "-g", help="Only this check group (repeatable), e.g. HOST, RUNTIME")


def _open_session(settings: Any) -> tuple[Any, str]:
    db = settings.layout.db_path
    if not db.is_file():
        return None, "missing"
    try:
        from sqlalchemy import inspect

        from aeos.core.db import get_engine, init_engine, session_factory

        init_engine(settings)
        if "check_results" not in inspect(get_engine()).get_table_names():
            return None, "schema-incomplete"
        return session_factory()(), "present"
    except Exception:
        return None, "unreadable"


def dashboard_critical_failures(results: list[Any]) -> list[str]:
    from aeos.core.enums import Capability
    from aeos.doctor.capabilities import RULES
    from aeos.doctor.checks import REGISTRY

    critical = set(RULES[Capability.CAN_OPEN_DASHBOARD].critical)
    return [r.check_id for r in results if r.check_id in critical and r.status not in REGISTRY[r.check_id].ok_statuses]


@app.callback(invoke_without_command=True)
def doctor(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
    group: list[str] = GROUP_OPTION,
    capability: str = typer.Option(None, "--capability", "-c", help="Only checks for one capability, e.g. CAN_RUN_WORKER"),
    functional: bool = typer.Option(False, "--functional", help="Opt-in heavier checks (Claude capability test uses Max "
                                    "quota; GitHub read-only probe uses the network)"),
) -> None:
    """Run readiness checks and print status, capability gates and remedies."""
    if ctx.invoked_subcommand is not None:
        return
    from aeos.core.config import get_settings
    from aeos.core.enums import Capability
    from aeos.core.errors import AeosError
    from aeos.doctor.capabilities import capability_matrix
    from aeos.doctor.runner import row_view, run_doctor

    if capability:
        try:
            capability = Capability(capability.upper()).value
        except ValueError as exc:
            raise typer.BadParameter(f"unknown capability {capability}") from exc
    settings = get_settings()
    session, db_state = _open_session(settings)
    try:
        report = run_doctor(settings, session, groups=group or None, capability=capability, functional=functional,
                            trigger="cli")
        persisted = False
        if session is not None:
            try:
                session.commit()
                persisted = True
            except Exception:
                session.rollback()
        rows = {r.check_id: row_view(r) for r in report.results}
        matrix = capability_matrix(session, domain=None) if persisted else capability_matrix(None, rows=rows)
    except AeosError as exc:
        typer.echo(ascii_safe(f"{exc.code}: {exc.message}"), err=True)
        raise typer.Exit(2) from exc
    finally:
        if session is not None:
            session.close()
    failures = dashboard_critical_failures(report.results)
    exit_code = 1 if failures else 0
    if as_json:
        payload = {"data_dir": str(settings.data_dir), "database": db_state, "persisted": persisted,
                   "run_id": report.run_id, "functional": functional, "results": [r.as_dict() for r in report.results],
                   "skipped": report.skipped, "capabilities": matrix, "dashboard_critical_failures": failures,
                   "exit_code": exit_code}
        typer.echo(json.dumps(payload, indent=2, ensure_ascii=True, default=str))
        raise typer.Exit(exit_code)
    _print_report(settings, db_state, persisted, report, matrix, failures)
    raise typer.Exit(exit_code)


def _print_report(settings: Any, db_state: str, persisted: bool, report: Any, matrix: list[dict[str, Any]],
                  failures: list[str]) -> None:
    echo = typer.echo
    echo("DLY AEOS Readiness Doctor - bootstrap layer (no server, no LLM)")
    echo(ascii_safe(f"platform: {sys.platform}   data dir: {settings.data_dir}   database: {db_state}"))
    rows = [[r.group, r.check_id, r.status, r.severity, r.summary] for r in report.results]
    echo(table(["GROUP", "CHECK", "STATUS", "SEV", "SUMMARY"], rows, max_width=90))
    if report.skipped:
        echo(ascii_safe("Skipped (opt-in): " + ", ".join(s["check_id"] for s in report.skipped)
                        + "  -> run with --functional"))
    echo("")
    echo("Capabilities (READY only when every critical check is OK):")
    cap_rows = []
    for c in matrix:
        reason = (c["reasons"][0] if c["reasons"] else "")
        cap_rows.append([c["capability"], c["status"], reason])
    echo(table(["CAPABILITY", "STATUS", "FIRST REASON"], cap_rows, max_width=100))
    grouped: dict[tuple[str, str], tuple[list[str], dict[str, Any]]] = {}
    for r in report.results:
        if r.remediation and r.status in ("MISSING", "FAILED", "EXPIRED", "UNKNOWN", "STALE"):
            key = (str(r.remediation.get("operation")), str(r.remediation.get("command")))
            grouped.setdefault(key, ([], r.remediation))[0].append(r.check_id)
    if grouped:
        echo("")
        echo("Remedies (copy and run yourself; DLY AEOS never runs them):")
        for ids, rem in grouped.values():
            echo(ascii_safe(f"  [{', '.join(ids)}] {rem.get('operation')}"))
            if rem.get("command"):
                echo(ascii_safe(f"      command : {rem['command']}"))
            for key, label in (("source", "source  "), ("target", "target  "), ("privilege", "privilege"),
                               ("cost", "cost    "), ("rollback", "rollback"), ("evidence_to_collect", "evidence")):
                if rem.get(key):
                    echo(ascii_safe(f"      {label}: {rem[key]}"))
    echo("")
    if not persisted:
        echo("Results were NOT stored (database not initialised). Create it with: aeos db upgrade")
    if failures:
        echo(ascii_safe("EXIT 1: critical check(s) for CAN_OPEN_DASHBOARD not OK: " + ", ".join(failures)))
    else:
        echo("EXIT 0: no critical CAN_OPEN_DASHBOARD check failed in this run.")


@app.command("list")
def list_checks() -> None:
    """List registered checks (id, group, component, opt-in flags)."""
    from aeos.doctor.checks import ordered_specs

    rows = [[s.group, s.check_id, s.component_id or "-", "yes" if s.functional else "", "yes" if s.needs_db else "",
             s.title] for s in ordered_specs()]
    typer.echo(table(["GROUP", "CHECK", "COMPONENT", "FUNCTIONAL", "NEEDS DB", "TITLE"], rows, max_width=60))
