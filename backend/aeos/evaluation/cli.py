"""`aeos eval ...` - suites, experiments and the leaderboard (read-only, ASCII output only)."""

from __future__ import annotations

import typer

from aeos.core.enums import Domain


def _ascii(s: str) -> str:
    return s.encode("ascii", "replace").decode()


app = typer.Typer(help="Model/configuration evaluation (definitions + recorded results only).", no_args_is_help=True)


@app.command("suites")
def suites_cmd() -> None:
    """List protected held-out suites (no results are seeded)."""
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.evaluation import service

    ensure_database()
    with session_scope() as s:
        rows = service.list_suites(s)
    for r in rows:
        typer.echo(f"{r['suite_key']:<36} role={r['role_key'] or '-':<28} cases={r['case_count']} "
                   f"kinds={','.join(r['case_kinds'])} integrity={r['integrity']}")
    if not rows:
        typer.echo("No suites. Run: aeos seed --only aeos.evaluation.seed")


@app.command("experiments")
def experiments_cmd() -> None:
    """Topology experiments A-D (identical gates)."""
    from aeos.evaluation import definitions as D

    for e in D.EXPERIMENTS:
        typer.echo(f"{e['key']} {e['name']:<24} {e['status']:<16} gates_hash={e['gates_hash'][7:19]}")


@app.command("leaderboard")
def leaderboard_cmd(
    role: str = typer.Option(None, help="Role key, e.g. feature-implementer"),
    domain: Domain = typer.Option(None, help="Also include runs planned in this domain (PERSONAL or OFFICE); "
                                             "without it only platform runs are ranked"),
) -> None:
    """Per-role ranking from recorded runs only (ties and sample sizes shown)."""
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.evaluation import service

    ensure_database()
    with session_scope() as s:
        # Never mixes domains: runs planned in one domain are only ranked when that domain is chosen.
        board = service.leaderboard(s, role=role, domain=domain.value if domain else None, scoped=True)
    if board["status"] == service.NOT_TESTED:
        typer.echo("All candidates are BELUM DIUJI (not tested) in this platform.")
    for suite in board["suites"]:
        typer.echo(f"[{suite['suite_key']}] {suite['status']}")
        for e in suite["ranking"]:
            tie = " (tie)" if e["tie"] else ""
            typer.echo(_ascii(f"  #{e['rank']}{tie} {e['label'][:60]} n={e['sample_size']} "
                              f"accept={e['final_acceptance_rate']} ci95={e['acceptance_ci95']}"))
        for e in suite["excluded"]:
            typer.echo(_ascii(f"  excluded {e['label'][:60]}: {e['eligibility']['status']}"))
        for e in suite.get("not_comparable", []):
            typer.echo(_ascii(f"  not comparable {e['label'][:60]}: different limits/suite/environment"))
    typer.echo(board["route_change"])


def _owner(session, as_user: str) -> str:
    """Owner actions on a run are human actions: the SUPER_ADMIN proves it by password (never echoed)."""
    from aeos.routing.cli import _admin_user_id

    password = typer.prompt(f"Password for {as_user}", hide_input=True, default="", show_default=False)
    return _admin_user_id(session, as_user, password)


def _run_action(action: str, run_id: str, as_user: str, **kw) -> None:
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.core.errors import AeosError
    from aeos.evaluation import executor

    ensure_database()
    try:
        with session_scope() as s:
            uid = _owner(s, as_user)
            if action == "start":
                run = executor.start_run(s, run_id, actor_user_id=uid)
                typer.echo(f"{run.id} RUNNING, slots: {', '.join(x['slot_id'] for x in executor.slots(s, run))}")
            elif action == "bind":
                run = executor.bind_task(s, run_id, slot_id=kw["slot_id"], task_id=kw["task_id"], actor_user_id=uid)
                typer.echo(f"{run.id}: slot {kw['slot_id']} -> task {kw['task_id']}")
            elif action == "collect":
                out = executor.collect(s, run_id)
                typer.echo(f"collected {out['collected']}/{out['slots']} pending={len(out['pending'])} "
                           f"invalid={len(out['invalid'])} unbound={len(out['unbound'])} recorded={out['recorded']}")
                for row in out["invalid"]:
                    typer.echo(_ascii(f"  invalid {row['slot_id']}: {row['reason']}"))
            else:
                run = executor.finish_run(s, run_id, actor_user_id=uid, reason=kw["reason"])
                typer.echo(f"{run.id} -> {run.status} (unexecuted slots recorded as CANCELLED)")
    except AeosError as exc:
        typer.secho(f"{exc.code}: {exc.message}", fg="red", err=True)
        raise typer.Exit(1) from exc


AS_OPTION = typer.Option(..., "--as", help="Your SUPER_ADMIN username (a human owner action)")


@app.command("start")
def start_cmd(run_id: str = typer.Argument(...), as_user: str = AS_OPTION) -> None:
    """Start a PLANNED run: case x repetition slots are created (nothing runs by itself)."""
    _run_action("start", run_id, as_user)


@app.command("bind")
def bind_cmd(run_id: str = typer.Argument(...), slot_id: str = typer.Argument(...), task_id: str = typer.Argument(...),
             as_user: str = AS_OPTION) -> None:
    """Bind a TaskSpec executed through the workforce to one case slot of a RUNNING run."""
    _run_action("bind", run_id, as_user, slot_id=slot_id, task_id=task_id)


@app.command("collect")
def collect_cmd(run_id: str = typer.Argument(...), as_user: str = AS_OPTION) -> None:
    """Trusted collector: derive samples from task/run/acceptance records; records the run when complete."""
    _run_action("collect", run_id, as_user)


@app.command("finish")
def finish_cmd(run_id: str = typer.Argument(...), reason: str = typer.Option(..., help="Why the run stops early"),
               as_user: str = AS_OPTION) -> None:
    """Stop a RUNNING run early; unexecuted slots are recorded as CANCELLED (never dropped)."""
    _run_action("finish", run_id, as_user, reason=reason)
