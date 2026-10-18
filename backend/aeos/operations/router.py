"""Operations & Reliability API (§33, §35 History/Audit/Recovery, §41 export).

Write endpoints are SUPER_ADMIN only (VIEWER is rejected by the backend, §50). Live restore and
controller promotion/rollback intentionally have **no** HTTP route: they are local, human-only CLI
operations (``aeos ops backup restore``, ``aeos ops release promote|rollback``) so a worker or a
candidate build can never reach them (§09).
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from aeos.api.deps import DB, ActiveDomain, AdminPrincipal, CurrentPrincipal, SettingsDep, load_project
from aeos.models.identity import SystemSetting
from aeos.operations import alerts, backup, emergency, export, flags, release, retirement, services, sli

router = APIRouter(tags=["operations"])
RETENTION_KEY = "ops.backup.retention"


class BackupIn(BaseModel):
    label: str = Field(default="manual", min_length=1, max_length=40)


class FlagIn(BaseModel):
    value: bool | int | str
    reason: str = Field(default="", max_length=500)


class EmergencyIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    reason: str = Field(min_length=5, max_length=1000)
    incident_owner: str = Field(default="", max_length=200)
    blast_radius: str = Field(default="", max_length=1000)


def _retention(db: DB) -> dict[str, Any] | None:
    row = db.get(SystemSetting, RETENTION_KEY)
    return row.value if row is not None and isinstance(row.value, dict) else None


# ----------------------------------------------------------------------------------------- backups


@router.get("/api/ops/backups")
def list_backups(principal: CurrentPrincipal, domain: ActiveDomain, db: DB, settings: SettingsDep) -> dict[str, Any]:
    items = backup.list_backups(settings, domain=domain, retention=_retention(db))
    ready = next((i for i in items if i.get("ready")), None)
    return {
        "items": items,
        "recovery_ready": ready is not None,
        "latest_restore_tested": ready["backup_id"] if ready else None,
        "retention_policy": _retention(db),
        "source": "backup manifests + results on disk (<data>/backups)",
        "checked_at": _now(),
        "mode": "LIVE",
        "evidence_limit": "Backup dianggap siap hanya setelah restore test berhasil dan tercatat sebagai evidence.",
    }


@router.post("/api/ops/backups")
def create_backup(body: BackupIn, principal: AdminPrincipal, db: DB, settings: SettingsDep) -> dict[str, Any]:
    result = backup.create_backup(settings, label=body.label, actor=principal.user.id, session=db, actor_type="user")
    db.commit()
    return result


@router.post("/api/ops/backups/{backup_id}/verify")
def verify_backup(backup_id: str, principal: AdminPrincipal, db: DB, settings: SettingsDep) -> dict[str, Any]:
    from aeos.audit import service as audit

    result = backup.verify_backup(settings, backup_id)
    audit.record(db, actor_type="user", actor_id=principal.user.id, action="ops.backup.verify",
                 result="SUCCESS" if result["ok"] else "FAILED", target_type="backup", target_id=backup_id,
                 details={"problems": result["problems"], "files_checked": result["files_checked"]})
    db.commit()
    return result


@router.post("/api/ops/backups/{backup_id}/restore-test")
def restore_test(backup_id: str, principal: AdminPrincipal, db: DB, settings: SettingsDep) -> dict[str, Any]:
    result = backup.restore_test(settings, backup_id, session=db, actor=principal.user.id, actor_type="user")
    db.commit()
    return result


# ----------------------------------------------------------------------------------------- releases


@router.get("/api/ops/releases")
def list_releases(principal: CurrentPrincipal, settings: SettingsDep) -> dict[str, Any]:
    data = release.list_releases(settings)
    return {**data, "source": "release provenance manifests + releases/ACTIVE.json", "checked_at": _now(),
            "mode": "LIVE",
            "notice": "Promote/rollback hanya lewat CLI lokal oleh manusia: aeos ops release promote|rollback."}


# ----------------------------------------------------------------------------------------- SLI / alerts


@router.get("/api/ops/sli")
def get_sli(principal: CurrentPrincipal, domain: ActiveDomain, db: DB, settings: SettingsDep) -> dict[str, Any]:
    return {**sli.public(sli.compute(db, domain=domain, settings=settings)), "mode": "LIVE"}


@router.post("/api/ops/sli/evaluate")
def evaluate_sli(principal: AdminPrincipal, domain: ActiveDomain, db: DB, settings: SettingsDep) -> dict[str, Any]:
    result = sli.evaluate(db, domain=domain, settings=settings)
    db.commit()
    return result


@router.get("/api/ops/alerts")
def list_alerts(
    principal: CurrentPrincipal, domain: ActiveDomain, db: DB,
    status: str = Query("OPEN", pattern="^(OPEN|RESOLVED|ALL)$"), limit: int = Query(200, ge=1, le=500),
) -> dict[str, Any]:
    items = alerts.list_alerts(db, domain=domain.value, status=None if status == "ALL" else status, limit=limit)
    return {"items": items, "source": "ops_alerts (deduplicated; raised as alert.raised events)",
            "checked_at": _now(), "mode": "LIVE"}


# ----------------------------------------------------------------------------------------- emergency stop


@router.post("/api/ops/emergency-stop/prepare")
def emergency_prepare(principal: AdminPrincipal, db: DB) -> dict[str, Any]:
    result = emergency.prepare(db, user_id=principal.user.id)
    db.commit()
    return result


@router.post("/api/ops/emergency-stop")
def emergency_stop(body: EmergencyIn, principal: AdminPrincipal, db: DB) -> dict[str, Any]:
    emergency.consume_token(db, user_id=principal.user.id, token=body.token)
    db.commit()  # the token is spent even if a later step fails
    domain = principal.active_domain.value if principal.active_domain else None
    report = emergency.execute(db, actor_type="user", actor_id=principal.user.id, reason=body.reason,
                               incident_owner=body.incident_owner, blast_radius=body.blast_radius,
                               visible_domain=domain)
    db.commit()
    return report


@router.get("/api/ops/emergency-stop/last")
def emergency_last(principal: CurrentPrincipal, db: DB) -> dict[str, Any]:
    paused: dict[str, Any]
    try:
        from aeos.workforce import facade as workforce

        paused = {"status": "PAUSED" if workforce.scheduling_paused(db) else "RUNNING"}
    except ImportError as exc:
        paused = {"status": "UNKNOWN", "reason": f"workforce module unavailable ({exc.name or exc})"}
    return {"last": emergency.last_emergency(db), "scheduling": paused, "steps": emergency.STEP_DESCRIPTIONS,
            "checked_at": _now()}


# ----------------------------------------------------------------------------------------- flags


@router.get("/api/ops/flags")
def list_flags(principal: CurrentPrincipal, db: DB) -> dict[str, Any]:
    return {"items": flags.list_flags(db), "source": "system_settings flags.*", "checked_at": _now(), "mode": "LIVE",
            "notice": "Flag hanya mempersempit perilaku; tidak memberi AUTH, rute berbayar, atau akses OFFICE."}


@router.put("/api/ops/flags/{key}")
def set_flag(key: str, body: FlagIn, principal: AdminPrincipal, db: DB) -> dict[str, Any]:
    result = flags.set_flag(db, key, body.value, actor_type="user", actor_id=principal.user.id, reason=body.reason)
    db.commit()
    return result


# ----------------------------------------------------------------------------------------- export


@router.get("/api/ops/export", response_model=None)
def export_snapshot(
    principal: CurrentPrincipal, domain: ActiveDomain, db: DB,
    kind: str = Query(..., pattern="^(inventory|architecture|evidence|audit|decisions)$"),
    project_id: str | None = None, download: bool = False,
) -> dict[str, Any] | Response:
    if project_id is not None:
        load_project(db, principal, project_id, domain=domain)
    data = export.build_export(db, kind=kind, domain=domain, actor_user_id=principal.user.id,
                               allowed_project_ids=principal.allowed_project_ids, project_id=project_id)
    db.commit()
    if download:
        name = f"aeos-{kind}-{domain.value.lower()}-{data['generated_at'][:19].replace(':', '')}.json"
        return Response(content=json.dumps(data, indent=2, ensure_ascii=False, default=str),
                        media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    return data


@router.get("/api/ops/exports")
def list_exports(principal: CurrentPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    return {"items": export.list_snapshots(db, domain=domain), "source": "ops_export_snapshots",
            "checked_at": _now(), "mode": "SNAPSHOT"}


# ----------------------------------------------------------------------------------------- services / runbooks


@router.get("/api/ops/services")
def list_services(principal: CurrentPrincipal, settings: SettingsDep, probe: bool = True) -> dict[str, Any]:
    return services.service_inventory(settings, probe=probe)


@router.get("/api/ops/runbooks")
def list_runbooks(principal: CurrentPrincipal) -> dict[str, Any]:
    return {"items": services.runbooks(), "source": "docs/runbooks/*.md (repository)", "checked_at": _now()}


# ----------------------------------------------------------------------------------------- retirement


@router.get("/api/ops/retirement/{project_id}")
def retirement_checklist(project_id: str, principal: CurrentPrincipal, domain: ActiveDomain, db: DB,
                         settings: SettingsDep) -> dict[str, Any]:
    project = load_project(db, principal, project_id, domain=domain)
    return {**retirement.checklist(db, settings, project), "checked_at": _now(), "mode": "LIVE"}


def _now() -> str:
    from aeos.core.timeutil import utcnow

    return utcnow().isoformat()
