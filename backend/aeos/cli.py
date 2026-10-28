"""`aeos` command line — works on Windows and macOS (and Linux).

Layer 1 of the Readiness Doctor (§08) lives here: `aeos doctor` reports even when the server/UI
cannot start, and needs no LLM.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from typing import Any

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
    ("aeos.intake.cli", "intake"),
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


def port_in_use(host: str, port: int) -> bool:
    """True when something already accepts connections on host:port (IPv4 or IPv6 loopback)."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


DEV_REAL_CREDENTIALS_RISK = (
    "Development mode (--dev/--reload) runs code that changes while it runs. With the default data directory "
    "and keyring service it would hold the owner's real PERSONAL credentials, OpenBao AppRole and data (§09). "
    "Use a separate profile: set AEOS_DATA_DIR to a dev data directory and AEOS_KEYRING_SERVICE to e.g. "
    "'dly-aeos-dev', or pass --dev-with-real-credentials to accept this risk explicitly."
)


def dev_profile_problems(settings: Any) -> list[str]:
    """Why a hot-reloading/dev server would run with the owner's real credentials (empty = separate profile)."""
    from aeos.core.paths import default_data_dir

    problems: list[str] = []
    data_dir = settings.data_dir
    if data_dir.expanduser().resolve() == default_data_dir().expanduser().resolve():
        problems.append("AEOS_DATA_DIR is the default (real) data directory")
    if settings.keyring_service == "dly-aeos":
        problems.append("AEOS_KEYRING_SERVICE is the default (real) keyring service 'dly-aeos'")
    return problems


@app.command()
def serve(
    port: int = typer.Option(None, help="Port (default 8787 or AEOS_PORT)"),
    dev: bool = typer.Option(False, help="Dev mode: allow Vite origin on the dev port, enable API docs"),
    reload: bool = typer.Option(False, help="Auto-reload backend code (dev only)"),
    release_mode: str = typer.Option(
        "auto", "--release",
        help="Which code runs the controller: 'active' = the verified promoted release, 'checkout' = this "
             "source tree, 'auto' = active when a release was promoted (checkout for --dev/--reload)"),
    dev_with_real_credentials: bool = typer.Option(
        False, "--dev-with-real-credentials",
        help="Allow --dev/--reload with the default data dir and keyring service (real credentials)"),
    no_ui_build: bool = typer.Option(
        False, "--no-ui-build",
        help="Checkout mode: do not rebuild a stale Control Center bundle at start (the old bundle is served and "
             "/api/health reports ui_build STALE)"),
) -> None:
    """Start the control plane on 127.0.0.1 (loopback only)."""
    import os
    import subprocess

    from aeos.core.config import get_settings, reset_settings_cache

    if release_mode not in ("auto", "active", "checkout"):
        console.print("--release must be one of: auto, active, checkout")
        raise typer.Exit(2)
    if port:
        os.environ["AEOS_PORT"] = str(port)
    if dev:
        os.environ["AEOS_DEV_MODE"] = "true"
    reset_settings_cache()
    try:
        settings = get_settings()
    except ValueError as exc:
        console.print(f"[red]Invalid configuration:[/red] {exc}")
        raise typer.Exit(2) from exc
    developing = dev or reload
    if developing:
        problems = dev_profile_problems(settings)
        if problems and not dev_with_real_credentials:
            console.print("[red]Refusing to start a development server with real credentials.[/red]")
            for p in problems:
                console.print(f"  - {p}")
            console.print(DEV_REAL_CREDENTIALS_RISK)
            raise typer.Exit(2)
        if problems:
            console.print(f"[yellow]WARNING:[/yellow] {DEV_REAL_CREDENTIALS_RISK}")
    # §06/§50: detect port conflicts and explain instead of binding elsewhere/publicly.
    if port_in_use(settings.host, settings.port):
        console.print(
            f"[red]Port {settings.port} on {settings.host} is already in use.[/red] "
            "Stop the other process or choose another port with --port / AEOS_PORT."
        )
        raise typer.Exit(2)
    # ADR-0123: a sealed OpenBao is unsealed from the OS keyring when the key is stored there (never printed);
    # without a stored key the controller starts as before and secret-dependent work fails closed.
    from aeos.secrets.unseal import unseal_at_start

    unseal_note = unseal_at_start(settings)
    if unseal_note:
        console.print(unseal_note, markup=False, highlight=False)

    from aeos.operations import release

    active = (release.read_pointer(settings).get("active") or {}).get("release_id")
    mode = release_mode
    if mode == "auto":
        mode = "active" if active and not developing else "checkout"
    if mode == "active":
        if developing:
            console.print("--dev/--reload run the editable checkout; they cannot be combined with --release active")
            raise typer.Exit(2)
        from aeos.core.errors import AeosError

        try:
            prepared = release.prepare_active_release(settings)
        except AeosError as exc:
            console.print(f"[red]Refusing to start the controller:[/red] {exc.code}: {exc.message}")
            raise typer.Exit(2) from exc
        if not prepared.get("ui"):
            console.print("[yellow]This release has no pinned UI bundle (frontend/dist was not built when it was "
                          "built); the API runs, the Control Center pages return FRONTEND_NOT_BUILT.[/yellow]")
        argv, env = release.release_serve_command(settings, prepared)
        console.print(f"DLY AEOS release {prepared['release_id']} (commit {str(prepared['commit'])[:12]}) listening "
                      f"on [bold]{settings.base_url}[/bold] (login required)")
        raise typer.Exit(subprocess.run(argv, env=env, check=False).returncode)

    if active:
        console.print(f"[yellow]WARNING: running the editable checkout, not the promoted release {active}. "
                      "A local edit or git pull changes this controller without promotion; use "
                      "'aeos serve --release active' for the trusted controller (§09).[/yellow]")
    import uvicorn

    from aeos.core.paths import repo_root as _repo_root
    from aeos.doctor.model import frontend_build_command
    from aeos.operations import freshness

    # R28: a git pull changes frontend/ but not frontend/dist; rebuild a stale bundle once when that is safe, never
    # install anything and never block the start (the old bundle is served and /api/health says ui_build STALE).
    ui_build = freshness.ensure_ui_fresh(
        settings, _repo_root(), enabled=not no_ui_build,
        remedy=f"restart aeos serve, or rebuild: {frontend_build_command()}",
        echo=lambda line: console.print(line, markup=False, highlight=False))
    freshness.record_serve_start(settings, _repo_root(), ui_build=ui_build, reload=reload)
    ensure_database()
    console.print(f"DLY AEOS listening on [bold]{settings.base_url}[/bold] (login required)")
    extra: dict[str, Any] = {}
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
