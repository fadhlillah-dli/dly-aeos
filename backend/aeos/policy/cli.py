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
        mci = snapshot.mci_reference(session=s)
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
        console.print(f"MCI reference: {mci['name']} status={mci['status']} ({mci['verification_code']}) "
                      f"compliance={mci['compliance']}")
        from aeos.core.config import get_settings

        # the document is local-only (the repository is public); say where this machine looks for it
        local_dir = str(get_settings().layout.canonical_policy).encode("ascii", "replace").decode("ascii")
        console.print(f"  source: local only, location={mci['source_location'] or '-'} (directory {local_dir})")
        approved = sum(1 for p in mci["pack_mappings"] if p["mapping_status"] == "APPROVED")
        console.print(f"  packs: {approved}/{len(mci['pack_mappings'])} mappings APPROVED "
                      f"(packs_mapping_status={mci['packs_mapping_status']})")


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
    from aeos.identity.facade import verify_password
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
    policy_preset: str = typer.Option(
        None, "--policy-preset",
        help="POLICY owner attestation preset: claude_extra_usage_off (you turned paid extra usage OFF in "
             "claude.ai; needs --days; shown as owner-attested E0, never VERIFIED)"),
    target_type: str = typer.Option(None), target_id: str = typer.Option(None),
    target_version: str = typer.Option(None, help="System change: the new value the evaluation ran with "
                                                  "(e.g. prompt revision, policy version, route key)"),
    target_hash: str = typer.Option(None, "--target-hash",
                                    help="Exact target hash (system change: the EvalRun configuration_hash; MCI pack "
                                         "mapping: its binding hash)"),
    eval_run: list[str] = typer.Option(None, "--eval-run", help="System change: COMPLETED all-PASS EvalRun id bound "
                                                                "to this change (repeatable)"),
    rollback_target: str = typer.Option(None, help="System change: the version/config/decision this replaces"),
    stage_model: list[str] = typer.Option(None, "--stage-model", help="POLICY supervisor pin STAGE=exact-model-id "
                                                                      "(repeatable; target planning_baseline/"
                                                                      "stage_models; needs --eval-run of that model - "
                                                                      "this build has no supervisor evaluation path, "
                                                                      "so no such run exists yet)"),
    project: str = typer.Option(None, help="Project id (optional, must be in --domain)"),
) -> None:
    """Record one human decision with the exact target shape its consumer reads (password confirms AUTH).

    A POLICY on a system-change target (policy/model/prompt/tools/hooks/memory/fallback/context/supervisor pin) needs
    --eval-run (bound to the change: --target-version or --target-hash) and --rollback-target (master s41/s43)."""
    from datetime import timedelta

    from aeos.core.enums import Domain
    from aeos.core.errors import AeosError
    from aeos.core.timeutil import utcnow
    from aeos.models.portfolio import Project
    from aeos.policy import catalog, recording
    from aeos.policy.decisions import decision_view

    _ensure_db()
    try:
        dom = Domain(domain.upper())
    except ValueError as exc:
        raise typer.BadParameter("--domain must be PERSONAL or OFFICE") from exc
    details: dict[str, object] = {}
    if stage_model:
        if kind.upper() != "POLICY":
            raise typer.BadParameter("--stage-model is a POLICY decision")
        pin_type, pin_id = catalog.STAGE_MODEL_TARGET
        if (target_type and target_type != pin_type) or (target_id and target_id != pin_id):
            raise typer.BadParameter(f"--stage-model records the target {pin_type}/{pin_id}")
        target_type, target_id = pin_type, pin_id
        models: dict[str, str] = {}
        for pair in stage_model:
            stage, sep, model = pair.partition("=")
            if not sep or not stage.strip() or not model.strip():
                raise typer.BadParameter(f"--stage-model expects STAGE=model-id, got {pair!r}")
            models[stage.strip().upper()] = model.strip()
        details["models"] = models
    if eval_run or rollback_target:
        if kind.upper() != "POLICY":
            raise typer.BadParameter("--eval-run/--rollback-target are system-change evidence of a POLICY decision")
        details["system_change"] = {"eval_run_ids": [e.strip() for e in eval_run or [] if e.strip()],
                                    "rollback_target": rollback_target or ""}
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
                project_id=project, target_type=target_type, target_id=target_id, target_version=target_version,
                target_hash=target_hash,
                expires_at=(utcnow() + timedelta(days=days)) if days else None, provider=provider, enabled=enable,
                office_authorization=office_authorization, company_reference=company_reference,
                allowed_providers=allowed_provider or [], exception=exception, allow_bash=allow_bash,
                office_policy=overrides or None, approved_targets=list(approved_target or []),
                approved_data_classes=list(approved_data_class or []),
                approved_identities=list(approved_identity or []),
                details=details or None,
                policy_preset=policy_preset,
            )
            v = decision_view(s, d)
            console.print(f"Recorded {v['kind']} {v['id']} -> {v['target_type']}:{v['target_id']} "
                          f"status={v['status']} expires={v['expires_at'] or '-'}")
            sc = (d.details or {}).get("system_change") or {}
            if sc:
                runs = ", ".join(f"{r.get('id')} ({r.get('binding')})" for r in sc.get("eval_runs") or []) or "-"
                console.print(f"  system change: {sc.get('class')} evaluation={sc.get('evaluation')} runs={runs} "
                              f"rollback={sc.get('rollback_target') or '-'}")
    except AeosError as exc:
        typer.secho(f"{exc.code}: {exc.message}".encode("ascii", "replace").decode(), fg="red", err=True)
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
    """List the ADR register: file status (written by the file author) and the owner's recorded decision."""
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
        owner = adrs.owner_decisions(s)
        for r in rows:
            title = r.title.encode("ascii", "replace").decode("ascii")
            st = adrs.register_status(r, owner.get(r.adr_key, []))
            who = (f"owner={st['owner_status']} ({st['owner_status_state']})" if st["owner_status"]
                   else "owner=NOT_RECORDED")
            console.print(f"{r.adr_key} file=[{r.status}] {who} {title}  ({r.source_path}, {r.content_hash})")


@app.command("adr-decide")
def adr_decide_cmd(
    adr_key: str = typer.Argument(..., help="ADR key, e.g. ADR-0004"),
    outcome: str = typer.Option(..., help="ACCEPTED | REJECTED | SUPERSEDED"),
    as_user: str = typer.Option(..., "--as", help="Your SUPER_ADMIN username (the human decision actor)"),
    rationale: str = typer.Option(..., help="Why (min 10 characters)"),
    content_hash: str = typer.Option(None, "--content-hash",
                                     help="Hash of the ADR content you reviewed (default: the loaded hash, which "
                                          "must still match the file)"),
    superseded_by: str = typer.Option(None, "--superseded-by", help="SUPERSEDED only: the successor ADR key"),
) -> None:
    """Record YOUR acceptance/rejection/supersession of an ADR, bound to its content hash (password confirms AUTH).

    A later edit of the ADR file makes this owner status STALE until you decide again."""
    from aeos.core.errors import AeosError
    from aeos.policy import adrs
    from aeos.policy.decisions import decision_view

    _ensure_db()
    password = typer.prompt(f"Password for {as_user} (confirms this human decision)", hide_input=True,
                            default="", show_default=False)
    try:
        with session_scope() as s:
            uid = _admin_user_id(s, as_user, password)
            d = adrs.record_adr_decision(s, adr_key=adr_key.upper(), outcome=outcome, actor_user_id=uid,
                                         rationale=rationale, content_hash=content_hash, superseded_by=superseded_by)
            v = decision_view(s, d)
            console.print(f"Recorded ADR {v['target_id']} {v['details'].get('outcome')} decision {v['id']} "
                          f"bound to {v['target_hash']}")
    except AeosError as exc:
        typer.secho(f"{exc.code}: {exc.message}", fg="red", err=True)
        raise typer.Exit(1) from exc
