"""`aeos routes ...` - model registry, catalog refresh, human activation (ASCII output only)."""

from __future__ import annotations

import typer
from sqlalchemy import select

app = typer.Typer(help="Model registry and routing (registry entries are never active by default).",
                  no_args_is_help=True)


def _admin_user_id(session, username: str) -> str:
    from aeos.core.enums import Role
    from aeos.models.identity import User

    user = session.scalars(select(User).where(User.username == username)).first()
    if user is None or not user.is_active or user.role != Role.SUPER_ADMIN.value:
        raise typer.BadParameter(f"{username!r} is not an active SUPER_ADMIN (decisions are human-only)")
    return user.id


@app.command("list")
def list_cmd(pool: str = typer.Option(None, help="Filter by pool"), status: str = typer.Option(None)) -> None:
    """List routes: pool, status, free/paid, resolved version, config hash."""
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.models.routing import ModelRoute

    ensure_database()
    with session_scope() as s:
        q = select(ModelRoute).order_by(ModelRoute.pool, ModelRoute.priority)
        if pool:
            q = q.where(ModelRoute.pool == pool)
        if status:
            q = q.where(ModelRoute.status == status)
        rows = list(s.scalars(q))
        if not rows:
            typer.echo("No routes registered. Run: aeos seed --only aeos.routing.seed")
            return
        for r in rows:
            typer.echo(
                f"{r.pool:<10} {r.status:<10} {r.mode:<6} free={r.is_free:<7} {r.route_key:<46} "
                f"resolved={r.resolved_version or 'UNKNOWN'} hash={r.config_hash or '-'}"
            )


@app.command("refresh")
def refresh_cmd() -> None:
    """Refresh OpenRouter catalog + key info and the OpenCode model list (never activates)."""
    from aeos.audit import service as audit
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.routing import catalog

    ensure_database()
    with session_scope() as s:
        row = catalog.refresh_all(s, actor="cli")
        audit.record(s, actor_type="cli", actor_id="local-owner", action="routes.catalog_refresh",
                     target_type="model_catalog", target_id="openrouter+opencode", details={"status": row.status})
        for name, res in row.results.items():
            typer.echo(f"{name:<20} {res.get('status')} {res.get('reason', '')}")
            for k in ("discovered", "config_changed", "missing"):
                if res.get(k):
                    typer.echo(f"  {k}: {', '.join(res[k])}")
        typer.echo(f"overall: {row.status}")


@app.command("activate")
def activate_cmd(
    route_key: str = typer.Argument(...),
    as_user: str = typer.Option(..., "--as", help="Your SUPER_ADMIN username (the human decision actor)"),
    mode: str = typer.Option("TRIAL", help="TRIAL or APPROVED"),
    rationale: str = typer.Option(..., help="Why this route may be used (min 10 chars)"),
    expected_hash: str = typer.Option(..., help="Config hash you reviewed (see `aeos routes list`/dashboard)"),
    data_class: list[str] = typer.Option(None, help="Allowed data classes (repeatable)"),
    budget_decision: str = typer.Option(None, help="BUDGET decision id (required for paid routes)"),
    days: int = typer.Option(None, help="Decision expiry in days"),
    terms_reviewed: bool = typer.Option(False, help="You re-read pricing and data terms now"),
) -> None:
    """Record a human ROUTE_ACTIVATION/TRIAL decision bound to the route's exact config hash."""
    from aeos.audit import service as audit
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.core.errors import AeosError
    from aeos.routing import service

    ensure_database()
    try:
        with session_scope() as s:
            uid = _admin_user_id(s, as_user)
            route, decision = service.activate_route(
                s, route_key, actor_user_id=uid, mode=mode.upper(), rationale=rationale,
                expected_config_hash=expected_hash, allowed_data_classes=data_class or None,
                budget_decision_id=budget_decision, expires_in_days=days, terms_reviewed=terms_reviewed)
            audit.record(s, actor_type="cli", actor_id=uid, action="route.activate", target_type="model_route",
                         target_id=route.route_key, details={"mode": mode, "decision_id": decision.id})
            typer.echo(f"{route.route_key} -> {route.status} (decision {decision.id}, hash {route.config_hash})")
    except AeosError as exc:
        typer.secho(f"{exc.code}: {exc.message}", fg="red", err=True)
        raise typer.Exit(1) from exc


@app.command("disable")
def disable_cmd(route_key: str = typer.Argument(...), reason: str = typer.Option(..., help="Reason")) -> None:
    """Disable a route (always allowed; active decisions are invalidated)."""
    from aeos.audit import service as audit
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.core.errors import AeosError
    from aeos.routing import service

    ensure_database()
    try:
        with session_scope() as s:
            route = service.disable_route(s, route_key, reason=reason)
            audit.record(s, actor_type="cli", actor_id="local-owner", action="route.disable",
                         target_type="model_route", target_id=route.route_key, reason=reason)
            typer.echo(f"{route.route_key} -> DISABLED")
    except AeosError as exc:
        typer.secho(f"{exc.code}: {exc.message}", fg="red", err=True)
        raise typer.Exit(1) from exc
