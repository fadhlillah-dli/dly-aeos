"""`aeos routes ...` - model registry, catalog refresh, human activation (ASCII output only)."""

from __future__ import annotations

import typer
from sqlalchemy import select

app = typer.Typer(help="Model registry and routing (registry entries are never active by default).",
                  no_args_is_help=True)


def _admin_user_id(session, username: str, password: str) -> str:
    """Human-only AUTH (§19/§20): the decision actor proves it is the SUPER_ADMIN by its password.

    A username alone is not enough - any local process (an agent with a shell included) could name
    the owner. The password is read without echo and never stored or logged."""
    from aeos.core.enums import Role
    from aeos.identity.passwords import verify_password
    from aeos.models.identity import User

    user = session.scalars(select(User).where(User.username == username)).first()
    if user is None or not user.is_active or user.role != Role.SUPER_ADMIN.value:
        raise typer.BadParameter(f"{username!r} is not an active SUPER_ADMIN (decisions are human-only)")
    if not password or not verify_password(user.password_hash, password):
        raise typer.BadParameter("password check failed - no decision recorded")
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
    terms_note: str = typer.Option("", help="What you read (stored on the decision)"),
    region: str = typer.Option(None, help="Region/residency you reviewed (e.g. ID) when the provider restricts regions"),
    tool: list[str] = typer.Option(None, help="Reviewed TaskSpec tool (repeatable; default fs.read, "
                                              "fs.write:allowed_paths, tests.run)"),
    eval_run: list[str] = typer.Option(None, help="COMPLETED EvalRun id proving this configuration (APPROVED only; "
                                                  "repeatable)"),
    training: bool = typer.Option(None, "--inputs-used-for-training/--no-inputs-used-for-training",
                                  help="Your reading of the provider terms when they are UNKNOWN (with --trial-only/"
                                       "--no-trial-only and --terms-note; needs --terms-reviewed)"),
    trial_only: bool = typer.Option(None, "--trial-only/--no-trial-only",
                                    help="Provider terms: trial use only / no confidential data"),
) -> None:
    """Record a human ROUTE_ACTIVATION/TRIAL decision bound to the route's exact config hash.

    APPROVED needs --eval-run ids of completed, gate-passing evaluations of exactly this configuration."""
    from aeos.audit import service as audit
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.core.errors import AeosError
    from aeos.routing import service

    terms_record = None
    if training is not None or trial_only is not None:
        # the owner's own reading of UNKNOWN training/trial terms (incomplete -> refused by the service)
        terms_record = {"note": terms_note}
        if training is not None:
            terms_record["may_use_inputs_for_training"] = training
        if trial_only is not None:
            terms_record["trial_only_no_confidential"] = trial_only
    ensure_database()
    password = typer.prompt(f"Password for {as_user} (confirms this human decision)", hide_input=True,
                            default="", show_default=False)
    try:
        with session_scope() as s:
            uid = _admin_user_id(s, as_user, password)
            route, decision = service.activate_route(
                s, route_key, actor_user_id=uid, mode=mode.upper(), rationale=rationale,
                expected_config_hash=expected_hash, allowed_data_classes=data_class or None,
                budget_decision_id=budget_decision, expires_in_days=days, terms_reviewed=terms_reviewed,
                terms_note=terms_note, terms_record=terms_record, region=region, tool_set=tool or None,
                eval_run_ids=eval_run or None)
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
