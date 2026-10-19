"""`aeos ops ...` - backup/restore, controller releases, SLIs and emergency stop (local operator).

Local CLI access to the data directory is the authority for these operations (like `aeos admin
create`). Live restore, promotion and rollback exist ONLY here, never as HTTP routes (§09).
Output is ASCII-only for legacy Windows consoles.
"""

from __future__ import annotations

import getpass
import json
from typing import Any

import typer
from sqlalchemy import select

from aeos.core.enums import Domain, Role
from aeos.core.errors import AeosError

app = typer.Typer(help="Operations: backup/restore, releases, SLIs, emergency stop.", no_args_is_help=True)
backup_app = typer.Typer(help="Backups: create, list, verify, restore-test, restore (live).", no_args_is_help=True)
release_app = typer.Typer(help="Released controller artifacts: build, list, promote, rollback.", no_args_is_help=True)
sli_app = typer.Typer(help="Service level indicators (targets are owner decisions).", invoke_without_command=True)
app.add_typer(backup_app, name="backup")
app.add_typer(release_app, name="release")
app.add_typer(sli_app, name="sli")


def _echo(text: str = "") -> None:
    typer.echo(text.encode("ascii", "replace").decode("ascii"))


def _json(data: Any) -> None:
    _echo(json.dumps(data, indent=2, ensure_ascii=True, default=str))


def _fail(exc: AeosError) -> None:
    _echo(f"ERROR {exc.code}: {exc.message}")
    if exc.details:
        _json(exc.details)
    raise typer.Exit(1)


def _settings():  # type: ignore[no-untyped-def]
    from aeos.cli import ensure_database
    from aeos.core.config import get_settings

    ensure_database()
    return get_settings()


def _local_actor() -> str:
    try:
        return f"local-operator:{getpass.getuser()}"[:64]
    except (OSError, KeyError):  # pragma: no cover
        return "local-operator"


def _operator_user_id(session: Any, username: str) -> str:
    from aeos.models.identity import User

    user = session.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None or not user.is_active or user.role != Role.SUPER_ADMIN.value:
        raise typer.BadParameter("--operator must be an active SUPER_ADMIN account (the human taking the decision)")
    return str(user.id)


# ----------------------------------------------------------------------------------------- backup


@backup_app.command("create")
def backup_create(
    label: str = typer.Option("manual", help="Short label stored in the backup id"),
    if_exists: bool = typer.Option(False, "--if-exists", help="Exit 0 without a backup when no database exists yet"),
) -> None:
    """Create a consistent backup (database + evidence/attachments/publications per domain).

    Does not migrate the database first, so it is safe as the very first bootstrap step (master
    section 07, step 0).
    """
    from aeos.core.config import get_settings

    settings = get_settings()
    if if_exists and not settings.layout.db_path.is_file():
        _echo("No database yet - nothing to back up.")
        return
    from aeos.core.db import init_engine, session_scope
    from aeos.operations import backup

    init_engine(settings)
    try:
        result = backup.create_backup(settings, label=label, actor=_local_actor())
    except AeosError as exc:
        _fail(exc)
    try:
        from aeos.audit import service as audit

        with session_scope() as s:
            audit.record(s, actor_type="cli", actor_id=_local_actor(), action="ops.backup.create",
                         target_type="backup", target_id=result["backup_id"],
                         details={"label": label, "db_sha256": result["db_sha256"]})
    except Exception as exc:  # older/partial schema: the backup exists, the audit row cannot be written
        _echo(f"  note: audit row not written ({type(exc).__name__}); backup is still valid")
    _echo(f"Backup created: {result['backup_id']}")
    _echo(f"  readiness: {result['readiness']} (run `aeos ops backup restore-test {result['backup_id']}`)")
    if result["excluded_file_count"]:
        _echo(f"  excluded credential-like files: {result['excluded_file_count']} (see manifest.json)")


@backup_app.command("list")
def backup_list(as_json: bool = typer.Option(False, "--json", help="Print JSON")) -> None:
    """List backups with readiness and retention status (nothing is deleted automatically)."""
    settings = _settings()
    from aeos.core.db import session_scope
    from aeos.models.identity import SystemSetting
    from aeos.operations import backup

    with session_scope() as s:
        row = s.get(SystemSetting, "ops.backup.retention")
        retention = row.value if row is not None and isinstance(row.value, dict) else None
    items = backup.list_backups(settings, retention=retention)
    if as_json:
        _json(items)
        return
    if not items:
        _echo("No backups yet. Create one: aeos ops backup create")
        return
    for b in items:
        _echo(f"{b['backup_id']}  {b.get('readiness')}  {b.get('size_bytes', 0)} bytes  "
              f"retention={b.get('retention')}")
    if not any(b.get("ready") for b in items):
        _echo("WARNING: no backup has passed a restore test - recovery is NOT-PROVEN.")


@backup_app.command("verify")
def backup_verify(backup_id: str) -> None:
    """Re-hash every file against the manifest (this is not a restore test)."""
    settings = _settings()
    from aeos.audit import service as audit
    from aeos.core.db import session_scope
    from aeos.operations import backup

    try:
        result = backup.verify_backup(settings, backup_id)
        platform_view = backup.scope_result(result, None, platform_only=True) or {}
        with session_scope() as s:
            audit.record(s, actor_type="cli", actor_id=_local_actor(), action="ops.backup.verify",
                         result="SUCCESS" if result["ok"] else "FAILED", target_type="backup", target_id=backup_id,
                         details={"problems": platform_view.get("problems", []), "ok": result["ok"]})
    except AeosError as exc:
        _fail(exc)
    _echo(f"{'OK' if result['ok'] else 'FAILED'}: {result['files_checked']} file(s) checked")
    for p in result["problems"]:
        _echo(f"  - {p}")
    if not result["ok"]:
        raise typer.Exit(1)


@backup_app.command("restore-test")
def backup_restore_test(backup_id: str) -> None:
    """Restore into an isolated temp directory and prove it; records RESTORE_TEST evidence on success."""
    settings = _settings()
    from aeos.core.db import session_scope
    from aeos.operations import backup

    try:
        with session_scope() as s:
            result = backup.restore_test(settings, backup_id, session=s, actor=_local_actor())
    except AeosError as exc:
        _fail(exc)
    _echo(f"Restore test {result['status']}")
    for p in result["problems"]:
        _echo(f"  problem: {p}")
    for w in result["warnings"]:
        _echo(f"  warning: {w}")
    if result.get("evidence_id"):
        _echo(f"  evidence: {result['evidence_id']}")
    if result.get("evidence_note"):
        _echo(f"  note: {result['evidence_note']}")
    if result["status"] != "PASSED":
        raise typer.Exit(1)


@backup_app.command("restore")
def backup_restore(
    backup_id: str,
    force_maintenance: bool = typer.Option(False, "--force-maintenance",
                                           help="Restore even though the server appears to be running"),
    yes: bool = typer.Option(False, "--yes", help="Skip the typed confirmation"),
) -> None:
    """Restore a restore-tested backup into the LIVE data directory (takes a pre-restore backup first).

    Works even when the live database is damaged: it is not opened or migrated before the restore.
    """
    from aeos.core.config import get_settings
    from aeos.operations import backup

    settings = get_settings()

    if not yes:
        typed = typer.prompt(f"This replaces the live database. Type the backup id to confirm ({backup_id})")
        if typed.strip() != backup_id:
            _echo("Confirmation did not match; nothing changed.")
            raise typer.Exit(1)
    try:
        report = backup.restore_live(settings, backup_id, actor=_local_actor(), force_maintenance=force_maintenance)
    except AeosError as exc:
        _fail(exc)
    _echo(f"Restored {backup_id}. Pre-restore backup: {report['pre_restore_backup_id']}")
    _echo(report["notice"])


@backup_app.command("retention")
def backup_retention(
    keep_last: int = typer.Option(0, help="Keep at least the newest N backups (0 = no count rule)"),
    keep_days: int = typer.Option(0, help="Keep backups younger than N days (0 = no age rule)"),
    clear: bool = typer.Option(False, help="Remove the retention policy"),
) -> None:
    """Record the owner's retention policy (listing only; backups are never deleted automatically)."""
    _settings()
    from aeos.audit import service as audit
    from aeos.core.db import session_scope
    from aeos.models.identity import SystemSetting

    value = None if clear else {"keep_last": keep_last, "keep_days": keep_days}
    with session_scope() as s:
        row = s.get(SystemSetting, "ops.backup.retention")
        previous = row.value if row else None
        if row is None:
            s.add(SystemSetting(key="ops.backup.retention", value=value))
        else:
            row.value = value
        audit.record(s, actor_type="cli", actor_id=_local_actor(), action="ops.backup.retention.set",
                     target_type="setting", target_id="ops.backup.retention",
                     details={"previous": previous, "new": value})
    _echo(f"Retention policy: {value}")


# ----------------------------------------------------------------------------------------- release


@release_app.command("build")
def release_build(
    ref: str = typer.Option("HEAD", help="Commit/ref to release (must be on main)"),
    repo: str = typer.Option("", help="Repository path (default: this checkout)"),
) -> None:
    """Build a release artifact (git archive of a clean, human-merged main commit) with provenance."""
    from pathlib import Path

    settings = _settings()
    from aeos.core.db import session_scope
    from aeos.operations import release

    try:
        with session_scope() as s:
            prov = release.build_release(settings, repo=Path(repo) if repo else None, ref=ref,
                                         builder=_local_actor(), session=s)
    except AeosError as exc:
        _fail(exc)
    _echo(f"Release {prov['release_id']}{' (already built)' if prov.get('already_built') else ''}")
    _echo(f"  commit {prov['commit']}  tree {prov['tree_hash']}")
    _echo(f"  artifact {prov['artifact']}  {prov['artifact_sha256']}")
    _echo(f"  schema head: {prov.get('schema_head') or 'none (create_all baseline)'}")


@release_app.command("list")
def release_list(as_json: bool = typer.Option(False, "--json")) -> None:
    """List releases with integrity and ACTIVE/PREVIOUS role."""
    settings = _settings()
    from aeos.operations import release

    data = release.list_releases(settings)
    if as_json:
        _json(data)
        return
    if not data["items"]:
        _echo("No releases built yet. Build one from main: aeos ops release build")
        return
    for r in data["items"]:
        _echo(f"{r['role']:<9} {r['release_id']}  integrity={r['integrity']}  built {r['built_at']}")


def _transition(action: str, release_id: str | None, operator: str, reason: str, force_maintenance: bool,
                yes: bool, proposal_id: str | None = None) -> None:
    settings = _settings()
    from aeos.core.db import session_factory
    from aeos.operations import release

    label = release_id or "previous release"
    if not yes:
        typed = typer.prompt(f"Human-only {action} of {label}. Type '{action}' to confirm")
        if typed.strip().lower() != action:
            _echo("Confirmation did not match; nothing changed.")
            raise typer.Exit(1)
    session = session_factory()()
    try:
        op_id = _operator_user_id(session, operator)
        if action == "promote":
            assert release_id is not None
            report = release.promote(settings, release_id, session=session, operator_user_id=op_id,
                                     actor_label=f"{_local_actor()}:{operator}"[:64], reason=reason,
                                     force_maintenance=force_maintenance, proposal_id=proposal_id)
        else:
            report = release.rollback(settings, session=session, operator_user_id=op_id,
                                      actor_label=f"{_local_actor()}:{operator}"[:64], reason=reason,
                                      force_maintenance=force_maintenance)
    except AeosError as exc:
        session.rollback()
        _fail(exc)
    finally:
        session.close()
    for step in report["steps"]:
        _echo(f"  {step['step']:<15} {step['status']}  {step.get('reason', '')}")
    _echo(f"{action.upper()} {report['status']}: active release is now {report.get('active_after')}")
    if report["status"] != "SUCCEEDED":
        raise typer.Exit(1)
    _echo(report.get("notice", ""))


@release_app.command("promote")
def release_promote(
    release_id: str,
    operator: str = typer.Option(..., help="Your SUPER_ADMIN username (recorded on the PROMOTION decision)"),
    reason: str = typer.Option("", help="Why this release is promoted"),
    force_maintenance: bool = typer.Option(False, "--force-maintenance"),
    yes: bool = typer.Option(False, "--yes"),
    proposal: str = typer.Option(None, "--proposal",
                                 help="ChangeProposal id whose bound PROMOTION decision this release implements"),
) -> None:
    """Promote a built release (backup + restore test + smoke test first; old release kept on failure)."""
    _transition("promote", release_id, operator, reason, force_maintenance, yes, proposal_id=proposal)


@release_app.command("rollback")
def release_rollback(
    operator: str = typer.Option(..., help="Your SUPER_ADMIN username"),
    reason: str = typer.Option("", help="Why the rollback is needed"),
    force_maintenance: bool = typer.Option(False, "--force-maintenance"),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    """Return the active pointer to the previous release (code only; data rollback = backup restore)."""
    _transition("rollback", None, operator, reason, force_maintenance, yes)


# ----------------------------------------------------------------------------------------- SLI


@sli_app.callback()
def sli_show(
    ctx: typer.Context,
    domain: Domain = typer.Option(None, help="PERSONAL or OFFICE (explicit, never inferred)"),
    raise_alerts: bool = typer.Option(False, "--raise-alerts", help="Also reconcile alerts (alert.raised events)"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show SLIs for one domain. Use `aeos ops sli target ...` to record owner targets."""
    if ctx.invoked_subcommand is not None:
        return
    if domain is None:
        raise typer.BadParameter("--domain PERSONAL|OFFICE is required (domain-first, U-05)")
    settings = _settings()
    from aeos.core.db import session_scope
    from aeos.operations import sli

    with session_scope() as s:
        result = sli.evaluate(s, domain=domain, settings=settings) if raise_alerts else \
            sli.public(sli.compute(s, domain=domain, settings=settings))
    if as_json:
        _json(result)
        return
    _echo(f"SLIs for {domain.value} (checked {result['checked_at']})")
    for item in result["items"]:
        value = "UNKNOWN" if item["value"] is None else f"{item['value']:.2f}"
        _echo(f"  {item['key']:<28} {value:>12} {item['unit']:<8} {item['status']}")
    _echo(result["targets_note"])
    if raise_alerts:
        _json(result["alerts"])


@sli_app.command("target")
def sli_target(
    key: str,
    maximum: float = typer.Option(None, "--max", help="Upper bound (lower is better)"),
    minimum: float = typer.Option(None, "--min", help="Lower bound (higher is better)"),
    clear: bool = typer.Option(False, help="Remove the target"),
    rationale: str = typer.Option("", help="Why this target (criticality)"),
) -> None:
    """Record an owner decision for an SLI target (audited)."""
    _settings()
    from aeos.core.db import session_scope
    from aeos.operations import sli

    try:
        with session_scope() as s:
            result = sli.set_target(s, key, maximum=None if clear else maximum, minimum=None if clear else minimum,
                                    actor_type="cli", actor_id=_local_actor(), rationale=rationale)
    except AeosError as exc:
        _fail(exc)
    _echo(f"Target for {key}: {result['target']}")


# ----------------------------------------------------------------------------------------- emergency stop


@app.command("emergency-stop")
def emergency_stop(
    reason: str = typer.Option(..., help="Why everything must stop now"),
    owner: str = typer.Option("", help="Incident owner"),
    blast_radius: str = typer.Option("", help="Known blast radius"),
    yes: bool = typer.Option(False, "--yes", help="Skip the typed confirmation"),
) -> None:
    """Pause scheduling, revoke all capabilities, cancel active runs, preserve evidence, audit."""
    _settings()
    from aeos.core.db import session_factory
    from aeos.operations import emergency

    if not yes:
        typed = typer.prompt("Type STOP to run the emergency stop")
        if typed.strip() != "STOP":
            _echo("Confirmation did not match; nothing changed.")
            raise typer.Exit(1)
    session = session_factory()()
    try:
        report = emergency.execute(session, actor_type="cli", actor_id=_local_actor(), reason=reason,
                                   incident_owner=owner, blast_radius=blast_radius)
        session.commit()
    except AeosError as exc:
        session.rollback()
        _fail(exc)
    finally:
        session.close()
    for step in report["steps"]:
        _echo(f"  {step['step']:<20} {step['status']}  {step.get('reason') or step.get('detail') or ''}")
    _echo(f"Emergency stop {report['status']} (incident {report['incident_id']})")
    _echo(report["notice"])
    if report["status"] != "COMPLETED":
        raise typer.Exit(1)
