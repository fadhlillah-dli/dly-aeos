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
    typer.echo(board["route_change"])
