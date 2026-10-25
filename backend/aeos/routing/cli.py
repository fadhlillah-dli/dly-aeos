"""`aeos routes ...` - model registry, catalog refresh, human activation (ASCII output only)."""

from __future__ import annotations

from typing import Any

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
        from aeos.routing import service

        for r in rows:
            flags = ""
            if service.client_restriction_problem(r):
                flags += " client-terms=OWNER_DECISION_REQUIRED"
            if r.pool != "SUPERVISOR" and service.request_shape_rejections(r):
                flags += " request=REFUSED"
            typer.echo(
                f"{r.pool:<10} {r.status:<10} {r.mode:<6} free={r.is_free:<7} {r.route_key:<46} "
                f"resolved={r.resolved_version or 'UNKNOWN'} hash={r.config_hash or '-'}{flags}"
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
    option: list[str] = typer.Option(None, help="DecisionRecord: an option you weighed (APPROVED; repeatable)"),
    goal: str = typer.Option(None, help="DecisionRecord: the goal of adopting this route (APPROVED)"),
    tradeoff: str = typer.Option(None, help="DecisionRecord: cost/quality trade-off (APPROVED)"),
    invalidate_if: str = typer.Option(None, help="DecisionRecord: when this decision stops applying (APPROVED)"),
    training: bool = typer.Option(None, "--inputs-used-for-training/--no-inputs-used-for-training",
                                  help="Your reading of the provider terms when they are UNKNOWN (with --trial-only/"
                                       "--no-trial-only and --terms-note; needs --terms-reviewed)"),
    trial_only: bool = typer.Option(None, "--trial-only/--no-trial-only",
                                    help="Provider terms: trial use only / no confidential data"),
    accept_client_terms: bool = typer.Option(
        False, "--accept-client-terms",
        help="Your decision on a provider client restriction (e.g. S07: free-tier chat models require OpenCode): "
             "let the AEOS broker call this route as its own client. Needs --client-terms-note"),
    client_terms_note: str = typer.Option("", help="What you read/confirmed about the client restriction (min 10 "
                                                   "chars; stored on the decision)"),
) -> None:
    """Record a human ROUTE_ACTIVATION/TRIAL decision bound to the route's exact config hash.

    APPROVED needs --eval-run ids of completed, gate-passing evaluations of exactly this configuration. A route whose
    provider restricts which client may call it is refused unless you pass --accept-client-terms."""
    from aeos.audit import service as audit
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.core.errors import AeosError
    from aeos.routing import service

    terms_record: dict[str, Any] | None = None
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
                eval_run_ids=eval_run or None,
                decision_record={"options": list(option or []), "goal": goal, "cost_quality_tradeoff": tradeoff,
                                 "invalidate_if": invalidate_if},
                client_terms=({"accepted": True, "note": client_terms_note} if accept_client_terms else None))
            audit.record(s, actor_type="cli", actor_id=uid, action="route.activate", target_type="model_route",
                         target_id=route.route_key, details={"mode": mode, "decision_id": decision.id})
            typer.echo(f"{route.route_key} -> {route.status} (decision {decision.id}, hash {route.config_hash})")
    except AeosError as exc:
        typer.secho(f"{exc.code}: {exc.message}", fg="red", err=True)
        raise typer.Exit(1) from exc


@app.command("set-endpoint")
def set_endpoint_cmd(
    route_key: str = typer.Argument(..., help="A LOCAL route, e.g. local/qwen3.8-flash-next"),
    endpoint: str = typer.Option(..., help="OpenAI-compatible base of your local server on this host, e.g. "
                                           "http://127.0.0.1:8080/v1 (loopback only)"),
    as_user: str = typer.Option(..., "--as", help="Your SUPER_ADMIN username"),
    rationale: str = typer.Option(..., help="Why (min 10 chars)"),
    model: str = typer.Option(None, help="Model name the local server serves, when it differs from the route model id"),
    quantization: str = typer.Option(None, help="Build: quantization (e.g. Q4_K_M)"),
    backend: str = typer.Option(None, help="Build: inference backend (e.g. llama.cpp-b5000)"),
    hardware: str = typer.Option(None, help="Build: hardware (e.g. M3-Max-64GB)"),
) -> None:
    """Point a LOCAL route at a loopback server. A new configuration: activate a new TRIAL afterwards."""
    from aeos.audit import service as audit
    from aeos.cli import ensure_database
    from aeos.core.db import session_scope
    from aeos.core.errors import AeosError
    from aeos.routing import service

    ensure_database()
    password = typer.prompt(f"Password for {as_user} (confirms this configuration change)", hide_input=True,
                            default="", show_default=False)
    try:
        with session_scope() as s:
            uid = _admin_user_id(s, as_user, password)
            route, change = service.set_local_endpoint(
                s, route_key, endpoint=endpoint, rationale=rationale, upstream_model=model,
                build={"quantization": quantization or "", "inference_backend": backend or "",
                       "hardware": hardware or ""})
            audit.record(s, actor_type="cli", actor_id=uid, action="route.set_endpoint", target_type="model_route",
                         target_id=route.route_key, reason=rationale[:500],
                         details={"endpoint": route.endpoint, "previous_endpoint": change["previous"]["endpoint"],
                                  "config_hash": route.config_hash, "needs_new_trial": change["needs_new_trial"]})
            typer.echo(f"{route.route_key} endpoint={route.endpoint} hash={route.config_hash}")
            typer.echo("Next: aeos routes activate " + route.route_key + " --mode TRIAL --expected-hash "
                       f"{route.config_hash} ... (a new configuration needs a new TRIAL)")
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
