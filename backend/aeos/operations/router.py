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
def create_backup(body: BackupIn, principal: AdminPrincipal, domain: ActiveDomain, db: DB,
                  settings: SettingsDep) -> dict[str, Any]:
    result = backup.create_backup(settings, label=body.label, actor=principal.user.id, session=db, actor_type="user")
    db.commit()
    return backup.summarize(settings, result["backup_id"], domain=domain)


@router.post("/api/ops/backups/{backup_id}/verify")
def verify_backup(backup_id: str, principal: AdminPrincipal, domain: ActiveDomain, db: DB,
                  settings: SettingsDep) -> dict[str, Any]:
    from aeos.audit import service as audit

    result = backup.verify_backup(settings, backup_id)
    platform_view = backup.scope_result(result, None, platform_only=True) or {}
    audit.record(db, actor_type="user", actor_id=principal.user.id, action="ops.backup.verify",
                 result="SUCCESS" if result["ok"] else "FAILED", target_type="backup", target_id=backup_id,
                 details={"problems": platform_view.get("problems", []), "ok": result["ok"]})
    db.commit()
    return backup.scope_result(result, domain) or result


@router.post("/api/ops/backups/{backup_id}/restore-test")
def restore_test(backup_id: str, principal: AdminPrincipal, domain: ActiveDomain, db: DB,
                 settings: SettingsDep) -> dict[str, Any]:
    result = backup.restore_test(settings, backup_id, session=db, actor=principal.user.id, actor_type="user")
    db.commit()
    return backup.scope_result(result, domain) or result


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
    return {**sli.public(sli.compute(db, domain=domain, settings=settings)), "mode": "LIVE",
            "error_budget": sli.error_budget(db, domain=domain)}


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
    # The stop works without a selected domain (it is an emergency), but the browser only ever sees
    # the active domain's counts/run ids — or statuses only when no domain is selected (§35, §50).
    active = principal.active_domain
    domain = active.value if active is not None and principal.can_access_domain(active) else emergency.STATUS_ONLY
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
    return {**retirement.checklist(db, settings, project), "checked_at": _now(), "mode": "LIVE",
            "source": "project records + ops_retirements (read-only projection)"}


class LegalHoldIn(BaseModel):
    active: bool
    reason: str = Field(min_length=5, max_length=1000)


class RetirementStepIn(BaseModel):
    status: str = Field(pattern="^(IN_PROGRESS|DONE|NOT_APPLICABLE)$")
    owner: str = Field(default="", max_length=200)
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)
    note: str = Field(default="", max_length=1000)


@router.put("/api/ops/retirement/{project_id}/legal-hold")
def retirement_legal_hold(project_id: str, body: LegalHoldIn, principal: AdminPrincipal, domain: ActiveDomain,
                          db: DB) -> dict[str, Any]:
    project = load_project(db, principal, project_id, domain=domain)
    hold = retirement.set_legal_hold(db, project, active=body.active, reason=body.reason, actor=principal.user.id)
    db.commit()
    return {"project_id": project.id, "legal_hold": hold}


@router.put("/api/ops/retirement/{project_id}/steps/{key}")
def retirement_step(project_id: str, key: str, body: RetirementStepIn, principal: AdminPrincipal,
                    domain: ActiveDomain, db: DB) -> dict[str, Any]:
    project = load_project(db, principal, project_id, domain=domain)
    entry = retirement.record_step(db, project, key, status=body.status, actor=principal.user.id, owner=body.owner,
                                   evidence_ids=body.evidence_ids, note=body.note)
    db.commit()
    return {"project_id": project.id, "step": key, "recorded": entry}


@router.get("/api/ops/retention")
def retention_policy(principal: CurrentPrincipal, domain: ActiveDomain) -> dict[str, Any]:
    from aeos.operations import retention

    policy = retention.load_policy()
    return {"items": [retention.resolve(policy, c, domain.value) for c in retention.DATA_CLASSES],
            "status": policy.get("status"), "legal_hold_blocks_disposal": policy.get("legal_hold_blocks_disposal"),
            "source": "policy/retention.yaml (owner decisions; protected path)", "checked_at": _now(),
            "mode": "LIVE", "notice": "AEOS tidak pernah menghapus data; kedaluwarsa = disposal manual saja."}


# ----------------------------------------------------------------------------------------- incidents


class IncidentIn(BaseModel):
    title: str = Field(min_length=3, max_length=256)
    owner: str = Field(default="", max_length=200)
    note: str = Field(default="", max_length=2000)
    blast_radius: str = Field(default="", max_length=1000)
    project_id: str | None = None


class IncidentNoteIn(BaseModel):
    note: str = Field(min_length=3, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)


class IncidentAnalysisIn(BaseModel):
    impact: str = Field(default="", max_length=4000)
    hypotheses: list[str] = Field(default_factory=list, max_length=20)
    root_cause: str = Field(default="", max_length=4000)
    root_cause_evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    root_cause_ref: str | None = Field(default=None, max_length=64)


class CorrectiveActionIn(BaseModel):
    description: str = Field(min_length=5, max_length=2000)
    ref: str | None = Field(default=None, max_length=64)


class RegressionIn(BaseModel):
    test_ids: list[str] = Field(default_factory=list, max_length=20)
    eval_case_refs: list[str] = Field(default_factory=list, max_length=20)


class FollowUpIn(BaseModel):
    component_ids: list[str] = Field(min_length=1, max_length=20)
    title: str = Field(default="", max_length=200)


def _incident_domain(principal: Any) -> str | None:
    active = principal.active_domain
    return active.value if active is not None and principal.can_access_domain(active) else None


@router.get("/api/ops/incidents")
def list_incidents(principal: CurrentPrincipal, db: DB,
                   status: str | None = Query(None, pattern="^(OPEN|CONTAINED|RESOLVED)$")) -> dict[str, Any]:
    from aeos.operations import incident

    return {"items": incident.list_incidents(db, domain=_incident_domain(principal), status=status),
            "source": "ops_incidents", "checked_at": _now(), "mode": "LIVE"}


@router.get("/api/ops/incidents/{incident_id}")
def get_incident(incident_id: str, principal: CurrentPrincipal, db: DB) -> dict[str, Any]:
    from aeos.operations import incident

    return incident.get_incident(db, incident_id, domain=_incident_domain(principal))


@router.get("/api/ops/incidents/{incident_id}/report")
def incident_report(incident_id: str, principal: CurrentPrincipal, db: DB,
                    kind: str = Query("INCIDENT_REPORT", pattern="^(INCIDENT_REPORT|POST_MORTEM)$")) -> dict[str, Any]:
    from aeos.operations import incident

    view = incident.get_incident(db, incident_id, domain=_incident_domain(principal))
    return {"incident_id": incident_id, "kind": kind, "markdown": incident.render_report(view, kind=kind),
            "generated_at": _now(), "mode": "SNAPSHOT"}


@router.post("/api/ops/incidents")
def open_incident(body: IncidentIn, principal: AdminPrincipal, domain: ActiveDomain, db: DB) -> dict[str, Any]:
    from aeos.operations import incident

    project_id = None
    if body.project_id:
        project_id = load_project(db, principal, body.project_id, domain=domain).id
    inc = incident.open_incident(db, title=body.title, actor=principal.user.id, owner=body.owner, source="manual",
                                 domain=domain.value, project_id=project_id, declared_blast_radius=body.blast_radius,
                                 note=body.note)
    db.commit()
    return incident.scoped(inc, domain.value, db)


def _incident_write(db: DB, principal: Any, incident_id: str) -> str | None:
    from aeos.operations import incident

    domain = _incident_domain(principal)
    incident.get_incident(db, incident_id, domain=domain)  # 404 for another domain's incident
    return domain


@router.post("/api/ops/incidents/{incident_id}/timeline")
def incident_note(incident_id: str, body: IncidentNoteIn, principal: AdminPrincipal, db: DB) -> dict[str, Any]:
    from aeos.operations import incident

    domain = _incident_write(db, principal, incident_id)
    inc = incident.add_timeline(db, incident_id, actor=principal.user.id, note=body.note, evidence_ids=body.evidence_ids)
    db.commit()
    return incident.scoped(inc, domain, db)


@router.post("/api/ops/incidents/{incident_id}/analysis")
def incident_analysis(incident_id: str, body: IncidentAnalysisIn, principal: AdminPrincipal, db: DB) -> dict[str, Any]:
    from aeos.operations import incident

    domain = _incident_write(db, principal, incident_id)
    inc = incident.record_analysis(db, incident_id, actor=principal.user.id, impact=body.impact,
                                   hypotheses=body.hypotheses, root_cause=body.root_cause,
                                   root_cause_evidence_refs=body.root_cause_evidence_refs,
                                   root_cause_ref=body.root_cause_ref)
    db.commit()
    return incident.scoped(inc, domain, db)


@router.post("/api/ops/incidents/{incident_id}/corrective-actions")
def incident_corrective(incident_id: str, body: CorrectiveActionIn, principal: AdminPrincipal,
                        db: DB) -> dict[str, Any]:
    from aeos.operations import incident

    domain = _incident_write(db, principal, incident_id)
    inc = incident.add_corrective_action(db, incident_id, actor=principal.user.id, description=body.description,
                                         ref=body.ref)
    db.commit()
    return incident.scoped(inc, domain, db)


@router.post("/api/ops/incidents/{incident_id}/regression")
def incident_regression(incident_id: str, body: RegressionIn, principal: AdminPrincipal, db: DB) -> dict[str, Any]:
    from aeos.operations import incident

    domain = _incident_write(db, principal, incident_id)
    inc = incident.link_regression(db, incident_id, actor=principal.user.id, test_ids=body.test_ids,
                                   eval_case_refs=body.eval_case_refs)
    db.commit()
    return incident.scoped(inc, domain, db)


@router.post("/api/ops/incidents/{incident_id}/follow-up")
def incident_follow_up(incident_id: str, body: FollowUpIn, principal: AdminPrincipal, domain: ActiveDomain,
                       db: DB) -> dict[str, Any]:
    from aeos.operations import incident

    _incident_write(db, principal, incident_id)
    result = incident.open_follow_up(db, incident_id, actor_user_id=principal.user.id, domain=domain.value,
                                     component_ids=body.component_ids, title=body.title)
    db.commit()
    return result


@router.post("/api/ops/incidents/{incident_id}/resolve")
def incident_resolve(incident_id: str, principal: AdminPrincipal, db: DB) -> dict[str, Any]:
    from aeos.operations import incident

    domain = _incident_write(db, principal, incident_id)
    inc = incident.resolve(db, incident_id, actor=principal.user.id)
    db.commit()
    return incident.scoped(inc, domain, db)


class RetirementDecisionIn(BaseModel):
    rationale: str = Field(min_length=10, max_length=2000)
    checklist_hash: str | None = Field(default=None, max_length=80)


@router.post("/api/ops/retirement/{project_id}/decision", status_code=201)
def retirement_decision(project_id: str, body: RetirementDecisionIn, principal: AdminPrincipal, domain: ActiveDomain,
                        db: DB, settings: SettingsDep) -> dict[str, Any]:
    """§33 final verification by the legitimate owner: a RETIREMENT Decision bound to this project and the hash
    of the checklist shown (refused while an item is BLOCKING). Nothing is deleted or revoked here."""
    from aeos.policy import decisions as registry

    project = load_project(db, principal, project_id, domain=domain)
    d = retirement.record_decision(db, settings, project, actor_user_id=principal.user.id,
                                   checklist_hash_seen=body.checklist_hash, rationale=body.rationale)
    db.commit()
    return {"decision": registry.decision_view(db, d), "checklist": retirement.checklist(db, settings, project)}


def _now() -> str:
    from aeos.core.timeutil import utcnow

    return utcnow().isoformat()
