"""`aeos` command line — works on Windows and macOS (and Linux).

Layer 1 of the Readiness Doctor (§08) lives here: `aeos doctor` reports even when the server/UI
cannot start, and needs no LLM.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys

import typer
from rich.console import Console

from aeos import __version__

app = typer.Typer(
    name="aeos",
    help="DLY AEOS - local-first AI engineering control plane.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

# (module providing `app: typer.Typer`, command group name)
CLI_MODULES: list[tuple[str, str]] = [
    ("aeos.identity.cli", "admin"),
    ("aeos.doctor.cli", "doctor"),
    ("aeos.inventory.cli", "inventory"),
    ("aeos.portfolio.cli", "project"),
    ("aeos.planning.cli", "plan"),
    ("aeos.policy.cli", "policy"),
    ("aeos.secrets.cli", "secrets"),
    ("aeos.secrets.vault_cli", "vault"),
    ("aeos.claude_bridge.cli", "bridge"),
    ("aeos.routing.cli", "routes"),
    ("aeos.broker.cli", "broker"),
    ("aeos.workforce.cli", "work"),
    ("aeos.worker_runtime.cli", "worker"),
    ("aeos.verification.cli", "verify"),
    ("aeos.publisher.cli", "publish"),
    ("aeos.docs_engine.cli", "docs"),
    ("aeos.delivery.cli", "delivery"),
    ("aeos.cost.cli", "cost"),
    ("aeos.evaluation.cli", "eval"),
    ("aeos.proposals.cli", "proposal"),
    ("aeos.research.cli", "research"),
    ("aeos.operations.cli", "ops"),
]

# Modules exposing `run(session) -> dict` used by `aeos seed` (design catalog, labelled as such).
SEEDERS: list[str] = [
    "aeos.policy.seed",
    "aeos.inventory.seed",
    "aeos.workforce.seed",
    "aeos.routing.seed",
    "aeos.research.seed",
    "aeos.evaluation.seed",
    "aeos.portfolio.seed",
]


def _module_exists(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


for _mod, _name in CLI_MODULES:
    if _module_exists(_mod):
        app.add_typer(importlib.import_module(_mod).app, name=_name)


def ensure_database() -> None:
    """Create/upgrade the local metadata DB (migrations with pre-upgrade backup)."""
    from aeos.core.db import init_engine
    from aeos.core.migrate import upgrade_database

    init_engine()
    upgrade_database()


@app.command()
def version() -> None:
    """Print the DLY AEOS version."""
    console.print(f"DLY AEOS {__version__} (python {sys.version.split()[0]}, {sys.platform})")


@app.command()
def serve(
    port: int = typer.Option(None, help="Port (default 8787 or AEOS_PORT)"),
    dev: bool = typer.Option(False, help="Dev mode: allow Vite origin on the dev port, enable API docs"),
    reload: bool = typer.Option(False, help="Auto-reload backend code (dev only)"),
) -> None:
    """Start the control plane on 127.0.0.1 (loopback only)."""
    import os
    import socket

    import uvicorn

    from aeos.core.config import get_settings, reset_settings_cache

    if port:
        os.environ["AEOS_PORT"] = str(port)
    if dev:
        os.environ["AEOS_DEV_MODE"] = "true"
    reset_settings_cache()
    settings = get_settings()
    # §06/§50: detect port conflicts and explain instead of binding elsewhere/publicly.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if sock.connect_ex((settings.host, settings.port)) == 0:
            console.print(
                f"[red]Port {settings.port} on {settings.host} is already in use.[/red] "
                "Stop the other process or choose another port with --port / AEOS_PORT."
            )
            raise typer.Exit(2)
    ensure_database()
    console.print(f"DLY AEOS listening on [bold]{settings.base_url}[/bold] (login required)")
    extra: dict[str, object] = {}
    if reload:
        from aeos.core.paths import repo_root

        # §09: hot reload watches only the trusted controller's own backend code. Candidate workspaces, the
        # data dir and policy/** are never watched, so nothing from a candidate is (re)loaded into the controller.
        extra["reload_dirs"] = [str(repo_root() / "backend")]
    uvicorn.run(
        "aeos.api.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=reload,
        log_level="info",
        **extra,
    )


db_app = typer.Typer(help="Local metadata database.", no_args_is_help=True)
app.add_typer(db_app, name="db")


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Apply migrations (a backup is taken first when the database already exists)."""
    ensure_database()
    console.print("[green]Database is at the latest schema.[/green]")


@db_app.command("status")
def db_status() -> None:
    from aeos.core.migrate import migration_status

    for k, v in migration_status().items():
        console.print(f"{k}: {v}")


@app.command()
def seed(only: list[str] = typer.Option(None, help="Run only these seeders (module names)")) -> None:
    """Load the design catalog (install manifest, roles, model registry, research, self project).

    Seeded rows are DESIGN/CANDIDATE labelled and never count as installation or usage evidence.
    """
    from aeos.core.db import session_scope

    ensure_database()
    for name in SEEDERS:
        if only and name not in only and name.rsplit(".", 2)[-2] not in only:
            continue
        if not _module_exists(name):
            console.print(f"[yellow]skip {name} (not implemented)[/yellow]")
            continue
        with session_scope() as s:
            result = importlib.import_module(name).run(s)
        console.print(f"[green]{name}[/green]: {result}")


def main() -> None:
    # Windows consoles may use a legacy code page; never crash on a non-ASCII character.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    app()


if __name__ == "__main__":
    main()
