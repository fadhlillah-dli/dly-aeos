"""`aeos policy ...` - policy snapshot, decision register and ADR index (ASCII output only)."""

from __future__ import annotations

import typer
from rich.console import Console
from sqlalchemy import select

from aeos.core.db import session_scope

app = typer.Typer(help="Policy & Decision Registry: snapshot, decisions, ADRs.", no_args_is_help=True)
console = Console(highlight=False, markup=False)  # ADR titles/decision targets are DATA, never markup


def _ensure_db() -> None:
    from aeos.cli import ensure_database

    ensure_database()


@app.command("snapshot")
def snapshot_cmd(
    refresh: bool = typer.Option(False, "--refresh", help="Activate a new snapshot if policy files/decisions changed"),
) -> None:
    """Show the active policy snapshot hash and MCI reference status."""
    from aeos.core.hashing import content_hash
    from aeos.policy import snapshot

    _ensure_db()
    with session_scope() as s:
        snap = snapshot.ensure_active_snapshot(s, source="cli refresh") if refresh else snapshot.active_snapshot(s)
        current = content_hash(snapshot.compute_content(s))
        mci = snapshot.mci_reference()
        if snap is None:
            console.print("No active policy snapshot. Run: aeos seed --only aeos.policy.seed")
        else:
            console.print(f"Active snapshot v{snap.version}: {snap.content_hash}")
            console.print(f"  created {snap.created_at.isoformat()} source={snap.source}")
            for name, f in (snap.content.get("files") or {}).items():
                console.print(f"  file {name}: {f.get('sha256')} [{f.get('status')}]")
            console.print(f"  decisions bound: {len(snap.content.get('decisions') or [])}")
            if snap.content_hash != current:
                console.print("  DRIFT: policy files or decisions changed; run with --refresh")
        console.print(f"MCI reference: {mci['name']} status={mci['status']} compliance={mci['compliance']}")


@app.command("decisions")
def decisions_cmd(
    kind: str = typer.Option(None, help="Filter by kind (APPROVAL, EXCEPTION, POLICY, ...)"),
    status: str = typer.Option(None, help="Filter by effective status (ACTIVE, EXPIRED, INVALIDATED, SUPERSEDED)"),
    limit: int = typer.Option(50, help="Maximum rows"),
) -> None:
    """List recorded human decisions (newest first)."""
    from aeos.models.decisions import Decision
    from aeos.policy.decisions import effective_status

    _ensure_db()
    with session_scope() as s:
        q = select(Decision).order_by(Decision.created_at.desc()).limit(max(1, min(limit, 500)))
        if kind:
            q = q.where(Decision.kind == kind)
        rows = [d for d in s.scalars(q) if not status or effective_status(d) == status]
        if not rows:
            console.print("No decisions recorded.")
        for d in rows:
            exp = d.expires_at.isoformat() if d.expires_at else "-"
            console.print(f"{d.created_at.isoformat()[:19]} {d.kind:16} {effective_status(d):11} "
                          f"{d.target_type}:{d.target_id} actor={d.actor_user_id} ({d.actor_kind}) "
                          f"ops={','.join(d.allowed_operations or []) or '-'} expires={exp}")


def _admin_user_id(session, username: str, password: str) -> str:
    """Human-only AUTH (§19/§20): the actor proves it is the SUPER_ADMIN by its password (never stored/logged).

    A username alone is not enough - any local process (an agent with a shell included) could name the owner."""
    from aeos.core.enums import Role
    from aeos.identity.passwords import verify_password
    from aeos.models.identity import User

    user = session.scalars(select(User).where(User.username == username)).first()
    if user is None or not user.is_active or user.role != Role.SUPER_ADMIN.value:
        raise typer.BadParameter(f"{username!r} is not an active SUPER_ADMIN (decisions are human-only)")
    if not password or not verify_password(user.password_hash, password):
        raise typer.BadParameter("password check failed - no decision recorded")
    return user.id


def _scope(pairs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in pairs or []:
        k, sep, v = p.partition("=")
        if not sep or not k.strip():
            raise typer.BadParameter(f"--scope expects key=value, got {p!r}")
        out[k.strip()] = v.strip()
    return out


_LIST_OVERRIDES = frozenset({"approved_worker_routes", "trace_destinations_allowed"})


def _office_overrides(pairs: list[str] | None) -> dict[str, object]:
    """``KEY=VALUE`` pairs -> office policy overrides (the recording layer refuses unknown keys).

    VALUE is JSON (``true``, ``["a","b"]``); a bare word is a string (``atlassian_deployment=CLOUD``) and the list
    keys also take ``a,b`` - Windows PowerShell 5.1 drops embedded double quotes when calling a native program. A
    value with a comma must be quoted on Windows PowerShell (``'approved_worker_routes=a,b'``): it splits an unquoted
    comma into separate arguments."""
    import json

    out: dict[str, object] = {}
    for p in pairs or []:
        k, sep, v = p.partition("=")
        key = k.strip()
        if not sep or not key:
            raise typer.BadParameter(f"--office-policy expects KEY=VALUE, got {p!r}")
        try:
            value: object = json.loads(v)
        except ValueError:
            raw = v.strip()
            if key in _LIST_OVERRIDES:
                value = [x.strip() for x in raw.split(",") if x.strip()]
            elif raw.startswith(("[", "{")):
                raise typer.BadParameter(f"--office-policy {key}: {raw!r} is not valid JSON") from None
            else:
                value = raw
        if key in _LIST_OVERRIDES and isinstance(value, str):
            value = [x.strip() for x in value.split(",") if x.strip()]
        out[key] = value
    return out


@app.command("decide")
def decide_cmd(
    kind: str = typer.Option(..., help="EXCEPTION | RISK_ACCEPTANCE | WRITE_ENABLEMENT | POLICY | BUDGET"),
    as_user: str = typer.Option(..., "--as", help="Your SUPER_ADMIN username (the human decision actor)"),
    domain: str = typer.Option(..., help="PERSONAL or OFFICE (explicit, never inferred)"),
    rationale: str = typer.Option(..., help="Why (min 10 characters)"),
    scope: list[str] = typer.Option(None, "--scope", help="Explicit auth scope key=value (repeatable)"),
    days: int = typer.Option(None, help="Expiry in days (required for EXCEPTION / RISK_ACCEPTANCE)"),
    exception: str = typer.Option(None, help="EXCEPTION preset: secret_store_interim_keyring | worker_sandbox_process"),
    allow_bash: bool = typer.Option(None, "--allow-bash/--no-allow-bash", help="worker_sandbox_process only"),
    provider: str = typer.Option(None, help="WRITE_ENABLEMENT provider (google-drive, github-issues, ...)"),
    enable: bool = typer.Option(None, "--enable/--disable", help="WRITE_ENABLEMENT on/off"),
    office_authorization: bool = typer.Option(False, "--office-authorization", help="POLICY: company authorisation"),
    company_reference: str = typer.Option(None, help="Company approval reference (office authorisation)"),
    allowed_provider: list[str] = typer.Option(None, "--allowed-provider", help="OFFICE provider (repeatable)"),
    approved_target: list[str] = typer.Option(
        None, "--approved-target",
        help="Company-approved OFFICE target (repeatable), e.g. 'confluence:tenant=acme.atlassian.net,space_id=98765' "
             "| 'jira:tenant=acme.atlassian.net,root_id=ENG' | 'bitbucket:workspace=acme,root_id=acme/app'. Keep "
             "the single quotes: Windows PowerShell splits an unquoted comma into separate arguments"),
    approved_data_class: list[str] = typer.Option(
        None, "--approved-data-class", help="Company-approved data class for AI processing (repeatable): "
                                            "PUBLIC | INTERNAL | CONFIDENTIAL | RESTRICTED"),
    approved_identity: list[str] = typer.Option(
        None, "--approved-identity", help="Company-issued identity of an office/ credential (repeatable), e.g. "
                                          "office/atlassian=<account id or email>"),
    office_policy: list[str] = typer.Option(
        None, "--office-policy", help="OFFICE policy override KEY=VALUE (repeatable; VALUE is JSON or a bare word), "
                                      "e.g. atlassian_deployment=CLOUD, claude_max_allowed_for_office_data=true, "
                                      "'approved_worker_routes=route-a,route-b' (quoted: Windows PowerShell splits an "
                                      "unquoted comma); applies only with an ACTIVE company authorisation"),
    target_type: str = typer.Option(None), target_id: str = typer.Option(None),
    project: str = typer.Option(None, help="Project id (optional, must be in --domain)"),
) -> None:
    """Record one human decision with the exact target shape its consumer reads (password confirms AUTH)."""
    from datetime import timedelta

    from aeos.core.enums import Domain
    from aeos.core.errors import AeosError
    from aeos.core.timeutil import utcnow
    from aeos.models.portfolio import Project
    from aeos.policy import recording
    from aeos.policy.decisions import decision_view

    _ensure_db()
    try:
        dom = Domain(domain.upper())
    except ValueError as exc:
        raise typer.BadParameter("--domain must be PERSONAL or OFFICE") from exc
    auth_scope = _scope(scope) or {"recorded_via": "aeos policy decide", "kind": kind.upper()}
    overrides = _office_overrides(office_policy)
    password = typer.prompt(f"Password for {as_user} (confirms this human decision)", hide_input=True,
                            default="", show_default=False)
    try:
        with session_scope() as s:
            uid = _admin_user_id(s, as_user, password)
            if project:
                p = s.get(Project, project)
                if p is None or p.domain != dom.value:  # another domain's project is "not found"
                    raise typer.BadParameter("project not found in this domain")
            d = recording.record_generic_decision(
                s, actor_user_id=uid, domain=dom, kind=kind.upper(), rationale=rationale, auth_scope=auth_scope,
                project_id=project, target_type=target_type, target_id=target_id,
                expires_at=(utcnow() + timedelta(days=days)) if days else None, provider=provider, enabled=enable,
                office_authorization=office_authorization, company_reference=company_reference,
                allowed_providers=allowed_provider or [], exception=exception, allow_bash=allow_bash,
                office_policy=overrides or None, approved_targets=list(approved_target or []),
                approved_data_classes=list(approved_data_class or []),
                approved_identities=list(approved_identity or []),
            )
            v = decision_view(s, d)
            console.print(f"Recorded {v['kind']} {v['id']} -> {v['target_type']}:{v['target_id']} "
                          f"status={v['status']} expires={v['expires_at'] or '-'}")
    except AeosError as exc:
        typer.secho(f"{exc.code}: {exc.message}", fg="red", err=True)
        raise typer.Exit(1) from exc


@app.command("office-route")
def office_route_cmd(
    route_key: str = typer.Argument(..., help="Worker route key the company approved (approved_worker_routes)"),
    as_user: str = typer.Option(..., "--as", help="Your SUPER_ADMIN username (the human decision actor)"),
    credential_ref: str = typer.Option(..., "--credential-ref", help="office/<provider> credential reference"),
    rationale: str = typer.Option(..., help="Why (min 10 characters)"),
) -> None:
    """Make a company-approved worker route OFFICE-eligible (section 23 controlled path; password confirms AUTH).

    Needs an ACTIVE company authorisation whose approved_worker_routes lists the route; the route becomes
    OFFICE-only. Activation stays a separate, hash-bound decision (aeos routes activate)."""
    from aeos.core.errors import AeosError
    from aeos.policy import office_routes
    from aeos.policy.decisions import decision_view

    _ensure_db()
    password = typer.prompt(f"Password for {as_user} (confirms this human decision)", hide_input=True,
                            default="", show_default=False)
    try:
        with session_scope() as s:
            uid = _admin_user_id(s, as_user, password)
            d = office_routes.authorise_office_route(s, route_key=route_key, credential_ref=credential_ref,
                                                     actor_user_id=uid, rationale=rationale)
            v = decision_view(s, d)
            console.print(f"Recorded POLICY {v['id']} -> {v['target_type']}:{v['target_id']} (OFFICE-only, "
                          f"credential {credential_ref}); activate it next: aeos routes activate {route_key} ...")
    except AeosError as exc:
        typer.secho(f"{exc.code}: {exc.message}", fg="red", err=True)
        raise typer.Exit(1) from exc


@app.command("revoke")
def revoke_cmd(
    decision_id: str = typer.Argument(...),
    as_user: str = typer.Option(..., "--as", help="Your SUPER_ADMIN username"),
    reason: str = typer.Option(..., help="Why the decision is revoked"),
) -> None:
    """Revoke (invalidate) an ACTIVE decision; consumers fail closed immediately."""
    from aeos.core.errors import AeosError
    from aeos.policy.decisions import revoke_decision

    _ensure_db()
    password = typer.prompt(f"Password for {as_user} (confirms this human decision)", hide_input=True,
                            default="", show_default=False)
    try:
        with session_scope() as s:
            uid = _admin_user_id(s, as_user, password)
            d = revoke_decision(s, decision_id, actor_user_id=uid, reason=reason)
            console.print(f"Revoked {d.kind} {d.id} ({d.target_type}:{d.target_id})")
    except AeosError as exc:
        typer.secho(f"{exc.code}: {exc.message}", fg="red", err=True)
        raise typer.Exit(1) from exc


@app.command("adrs")
def adrs_cmd(load: bool = typer.Option(False, "--load", help="(Re)load docs/decisions/ADR-*.md first")) -> None:
    """List the ADR register."""
    from aeos.models.decisions import ArchitectureDecisionRecord
    from aeos.policy import adrs

    _ensure_db()
    with session_scope() as s:
        if load:
            res = adrs.load_adrs(s)
            console.print(f"Loaded ADRs: created={res['created']} updated={res['updated']} unchanged={res['unchanged']}")
        rows = list(s.scalars(select(ArchitectureDecisionRecord).order_by(ArchitectureDecisionRecord.adr_key)))
        if not rows:
            console.print("No ADRs loaded. Run: aeos policy adrs --load")
        for r in rows:
            title = r.title.encode("ascii", "replace").decode("ascii")
            console.print(f"{r.adr_key} [{r.status}] {title}  ({r.source_path}, {r.content_hash})")
