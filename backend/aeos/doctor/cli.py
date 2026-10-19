"""`aeos doctor` - Readiness Doctor bootstrap layer (§08 layer 1).

Runs without the server, without an LLM, and even when the database is not initialised (results are
then only printed). Only the opt-in ``--functional`` pass makes one small Claude Code call through the
native bridge (Max quota) and runs the containment/keyring/GitHub read-only probes. Exit code 1 when a
critical check for CAN_OPEN_DASHBOARD fails. Remedies are printed for the operator to copy; nothing is
installed or changed.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import typer
from sqlalchemy.exc import SQLAlchemyError

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


def data_dir_label(settings: Any) -> str:
    """Logical name of the data directory for printed/JSON output: no user name or absolute path (§37)."""
    from aeos.core.paths import default_data_dir, repo_root
    from aeos.inventory.probes import logical_location

    try:
        if settings.data_dir.resolve() == default_data_dir().resolve():
            return {"win32": "%LOCALAPPDATA%\\DLY-AEOS", "darwin": "~/Library/Application Support/DLY-AEOS"}.get(
                sys.platform, "$XDG_DATA_HOME/DLY-AEOS") + " (default OS)"
    except OSError:
        pass
    return f"{logical_location(settings.data_dir, repo=repo_root())} (AEOS_DATA_DIR)"


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
        try:
            report = run_doctor(settings, session, groups=group or None, capability=capability,
                                functional=functional, trigger="cli")
        except SQLAlchemyError:
            # An existing database whose schema predates this version (e.g. a column added without a
            # migration): still report everything, print-only, instead of crashing before the UI (§50).
            if session is None:
                raise
            session.rollback()
            session.close()
            session, db_state = None, "schema-outdated"
            report = run_doctor(settings, None, groups=group or None, capability=capability,
                                functional=functional, trigger="cli")
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
        payload = {"data_dir": data_dir_label(settings), "database": db_state, "persisted": persisted,
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
    if report.functional:
        echo("DLY AEOS Readiness Doctor - bootstrap layer (no server; --functional: opt-in checks incl. ONE small "
             "Claude Code call via the native bridge, using Max quota)")
    else:
        echo("DLY AEOS Readiness Doctor - bootstrap layer (no server, no LLM)")
    echo(ascii_safe(f"platform: {sys.platform}   data dir: {data_dir_label(settings)}   database: {db_state}"))
    rows = [[r.group, r.check_id + (f" @project {str((r.scope or {}).get('project_id'))[:8]}"
                                    if (r.scope or {}).get("project_id") else ""), r.status, r.severity, r.summary]
            for r in report.results]
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
    risks = [(c["capability"], r) for c in matrix for r in c.get("residual_risks") or []]
    if risks:
        echo("")
        echo("Residual risks (true even when a gate is READY; never counted as a pass):")
        for cap, r in risks:
            echo(ascii_safe(f"  [{cap}] {r}"))
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
    from aeos.doctor.model import aeos_cmd

    if not persisted and db_state == "schema-outdated":
        echo("Results were NOT stored: the database schema is older than this version. Back up and migrate it "
             f"with: {aeos_cmd('db upgrade')}")
    elif not persisted:
        echo(f"Results were NOT stored (database not initialised). Create it with: {aeos_cmd('db upgrade')}")
    if failures:
        echo(ascii_safe("EXIT 1: critical check(s) for CAN_OPEN_DASHBOARD not OK: " + ", ".join(failures)))
    else:
        echo("EXIT 0: no critical CAN_OPEN_DASHBOARD check failed in this run.")


@app.command("review-with-claude")
def review_with_claude_cmd(model: str = typer.Option("opus", help="Supervisor model alias for the review")) -> None:
    """Optional: ask official Claude Code (Max quota) to explain the latest platform-level results. Stored as
    E0 advisory findings; never changes a status, grants AUTH or runs a remedy."""
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.doctor.checks import load_all
    from aeos.doctor.review import review_with_claude
    from aeos.doctor.runner import latest_results, row_view

    ensure_database()
    load_all()
    with session_scope() as s:
        results = [row_view(r) for r in latest_results(s).values()]
        if not results:
            typer.echo("no stored Doctor results; run: aeos doctor", err=True)
            raise typer.Exit(1)
        rec = review_with_claude(s, results, model=model)
    if not rec["ok"]:
        typer.echo(ascii_safe(f"review not available ({rec.get('error_code') or 'BRIDGE'}); use the manual package path"),
                   err=True)
        raise typer.Exit(1)
    typer.echo(ascii_safe(f"Claude review (E0 advisory, {rec['checks_sent']} checks): {rec['summary']}"))
    for f in rec["findings"]:
        typer.echo(ascii_safe(f"  [{f.get('check_id')}] {f.get('observation')} -> {f.get('suggestion')}"))


@app.command("budget")
def budget(
    cpu: int = typer.Option(None, "--cpu", help="CPU cores this machine may dedicate to DLY AEOS"),
    ram: float = typer.Option(None, "--ram", help="RAM budget in GiB"),
    disk: float = typer.Option(None, "--disk", help="Free disk floor in GiB at the data dir"),
    max_workers: int = typer.Option(None, "--max-workers", help="Maximum parallel worker runs"),
    reason: str = typer.Option(None, "--reason", help="Why these values (recorded in the audit log)"),
) -> None:
    """Show or set the owner's hardware/OS resource budget (master 51 P2). Setting it is audited."""
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.doctor.checks.host import BUDGET_FIELDS, resource_budget, set_resource_budget

    ensure_database()
    with session_scope() as s:
        if all(v is None for v in (cpu, ram, disk, max_workers)):
            b = resource_budget(s)
            typer.echo("resource budget: " + (", ".join(f"{k}={b.get(k)}" for k in BUDGET_FIELDS) if b else
                                              "UNKNOWN (not set; host checks use a provisional 1 GiB disk floor)"))
            return
        if not reason:
            raise typer.BadParameter("--reason is required when setting the budget")
        value = set_resource_budget(s, actor="local-operator", reason=reason, cpu_cores=cpu, ram_gib=ram,
                                    disk_gib=disk, max_parallel_workers=max_workers)
    typer.echo(ascii_safe("resource budget set: " + ", ".join(f"{k}={value.get(k)}" for k in BUDGET_FIELDS)
                          + " (audited; host checks are rechecked on the next Doctor run)"))


@app.command("list")
def list_checks() -> None:
    """List registered checks (id, group, component, opt-in flags)."""
    from aeos.doctor.checks import ordered_specs

    rows = [[s.group, s.check_id, s.component_id or "-", "yes" if s.functional else "", "yes" if s.needs_db else "",
             s.title] for s in ordered_specs()]
    typer.echo(table(["GROUP", "CHECK", "COMPONENT", "FUNCTIONAL", "NEEDS DB", "TITLE"], rows, max_width=60))
