"""`aeos work ...` - scheduler control from the local terminal (ASCII-only output)."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.markup import escape

app = typer.Typer(help="Workforce scheduler: status, run-once, pause/resume, cancel, demo fixtures.",
                  no_args_is_help=True)
console = Console()


def _out(text: str) -> None:
    """ASCII-only, markup-safe output (AGENTS.md: legacy Windows consoles). Reasons can come from
    other modules and contain non-ASCII characters or square brackets."""
    clean = str(text).replace("\u00a7", "s").replace("\u2014", "-").replace("\u2192", "->")
    console.print(escape(clean.encode("ascii", "replace").decode("ascii")), highlight=False)
ACTOR = "cli:local-operator"


def _db() -> None:
    from aeos.cli import ensure_database

    ensure_database()


@app.command("status")
def status(domain: str = typer.Option("PERSONAL", help="PERSONAL or OFFICE")) -> None:
    """Show scheduler state, task counts, active runs and blockers (no LLM)."""
    from aeos.core.db import session_scope
    from aeos.workforce import loop, views

    _db()
    with session_scope() as s:
        data = views.summary(s, domain=domain.upper(), project_ids=None, loop_running=loop.is_running())
        runs = views.list_runs(s, domain=domain.upper(), project_ids=None, active_only=True)
    sch = data["scheduler"]
    _out(f"scheduler enabled={sch['enabled']} paused={sch['paused']} cap={sch['concurrency_cap']} "
                  f"loop_running_in_this_process={sch['loop_running']}")
    _out("tasks: " + ", ".join(f"{k}={v}" for k, v in data["task_counts"].items()))
    _out(f"active runs (LIVE): {data['active_runs']}   DEMO rows: {data['demo_runs']}")
    _out(f"sandbox: {data['sandbox']['status']} kind={data['sandbox']['kind']}   "
                  f"engine: {data['engine']['status']} {data['engine'].get('version') or ''}")
    cost = data["cost"]
    _out(f"worker API cost: {cost['cost_known_usd'] if cost['cost_known_usd'] is not None else 'UNKNOWN'} "
                  f"(status {cost['cost_status']}, unknown entries {cost['cost_unknown_entries']})")
    for r in runs:
        _out(f"  run {r['run_id']} [{r['mode']}] {r['execution_state']}/{r['ui_state']} "
                      f"task={r['task']['key']} action={r['current_action'][:60]}")
    for b in data["blockers"]:
        _out(f"  blocker {b['task_key']} {b['state']} {b['reason_code']}: {b['reason'][:100]}")


@app.command("run-once")
def run_once(no_execute: bool = typer.Option(False, "--no-execute", help="Only reconcile/refresh/admit")) -> None:
    """One scheduler tick in the foreground (admits even if the background loop is disabled, never when paused)."""
    from aeos.core.config import get_settings
    from aeos.workforce import loop

    _db()
    info = loop.run_once(get_settings(), execute=not no_execute, force_admit=True)
    _out(f"reconcile={info.get('reconcile')} refreshed={info.get('refreshed')} leased={info.get('leased')}")
    for r in info.get("results", []):
        _out(f"  run {r.get('run_id')}: {r.get('status')} {r.get('code', '')} {r.get('reason', '')}")
    if info.get("paused"):
        _out("Scheduling is paused. Resume with: aeos work resume")


@app.command("pause")
def pause(reason: str = typer.Option("", help="Reason")) -> None:
    """Pause admission of new runs (running work is not killed; use cancel for that)."""
    from aeos.core.db import session_scope
    from aeos.workforce import scheduler

    _db()
    with session_scope() as s:
        scheduler.set_scheduling_paused(s, True, actor=ACTOR, reason=reason)
    _out("Scheduling paused.")


@app.command("resume")
def resume(reason: str = typer.Option("", help="Reason")) -> None:
    """Resume admission."""
    from aeos.core.db import session_scope
    from aeos.workforce import scheduler

    _db()
    with session_scope() as s:
        scheduler.set_scheduling_paused(s, False, actor=ACTOR, reason=reason)
    _out("Scheduling resumed.")


@app.command("enable")
def enable() -> None:
    """Allow the background loop to admit READY tasks (scheduler.enabled = true)."""
    from aeos.core.db import session_scope
    from aeos.workforce import scheduler

    _db()
    with session_scope() as s:
        scheduler.set_scheduler_enabled(s, True, actor=ACTOR)
    _out("Scheduler enabled (admission still requires approval, CAN_RUN_WORKER, route, sandbox, budget).")


@app.command("disable")
def disable() -> None:
    """Stop the background loop from admitting new runs."""
    from aeos.core.db import session_scope
    from aeos.workforce import scheduler

    _db()
    with session_scope() as s:
        scheduler.set_scheduler_enabled(s, False, actor=ACTOR)
    _out("Scheduler disabled.")


@app.command("cancel")
def cancel(run: str = typer.Argument(..., help="Run id"), reason: str = typer.Option("cancelled from CLI")) -> None:
    """Cancel a run: revoke capabilities, kill the process tree, release reservations, keep evidence."""
    from aeos.core.db import session_scope
    from aeos.workforce import scheduler

    _db()
    with session_scope() as s:
        r = scheduler.cancel_run(s, run, reason=reason, actor=ACTOR)
        state = r.execution_status
    _out(f"Run {run}: {state}. External side effects that already happened are NOT undone automatically.")


@app.command("demo-fixtures")
def demo_fixtures(remove: bool = typer.Option(False, "--remove", help="Delete DEMO rows instead"),
                  domain: str = typer.Option("PERSONAL", help="Domain for the DEMO rows")) -> None:
    """Insert clearly labelled DEMO runs for UI preview (excluded from counts/cost/acceptance)."""
    from aeos.core.db import session_scope
    from aeos.workforce import demo

    _db()
    with session_scope() as s:
        res = demo.remove(s, actor=ACTOR) if remove else demo.insert(s, domain=domain.upper(), actor=ACTOR)
    _out(f"DEMO fixtures: {res}")
