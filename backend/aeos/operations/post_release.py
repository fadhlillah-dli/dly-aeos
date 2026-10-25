"""Post-release failure response for product projects (§31 G8, §33, §40, §50 1.35).

§40: "Tindakan produksi, destruktif atau privileged tetap mengikuti AUTH dan human-only production baseline;
kegagalan memicu stop/decision queue bila kewenangan recovery belum tersedia." AEOS never deploys and never
rolls back. When the deployment observer records a FAIL observation of a production target:

* a deduplicated HIGH ``post_release_failure`` alert is raised (one per project + commit, never auto-resolved by
  the SLI loop);
* an incident is opened from that alert (or the open one is linked) with the observer's evidence and a
  **human** rollback recommendation in its timeline;
* one decision request is filed in the single Human Decision Queue: roll back by hand, forward-fix, or accept
  the risk. The request is DATA; only a human decision (or a declined request with a rationale) closes it.

The recommendation uses the owner's rollback criteria (``config.rollback_on`` of the production target). Without
recorded criteria it is ``UNDECIDED`` — AEOS does not invent a rollback rule. Services never commit.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.models.operations_ext import OpsAlert, OpsIncident
from aeos.models.portfolio import Project

RULE = "post_release_failure"
RUNBOOK = "docs/runbooks/post-release-failure.md"
REQUEST_TARGET = "post_release_failure"
OPTIONS = (
    {"key": "ROLLBACK", "label": "Rollback manual oleh manusia di platform deploy proyek (AEOS tidak melakukannya)"},
    {"key": "FORWARD_FIX", "label": "Forward-fix lewat proses perubahan normal (TaskSpec -> verifikasi -> merge manusia)"},
    {"key": "ACCEPT_RISK", "label": "Terima risiko dengan keputusan RISK_ACCEPTANCE bertanggal kedaluwarsa"},
)
NEVER = "AEOS tidak pernah melakukan deploy atau rollback; tindakan dilakukan manusia sesuai runbook."


def dedup_key(project_id: str, commit: str) -> str:
    return f"{RULE}:{project_id}:{commit.lower()[:40]}"


def recommendation(failure_conditions: list[str], rollback_on: list[str]) -> dict[str, Any]:
    """Human rollback recommendation from the owner's criteria (never invented)."""
    base = {"executed_by": "HUMAN", "aeos_action": "none", "runbook": RUNBOOK,
            "failure_conditions": sorted(failure_conditions), "rollback_on": sorted(rollback_on)}
    if not rollback_on:
        return {**base, "status": "UNDECIDED", "criteria": "UNDECIDED",
                "summary": "Kriteria rollback belum ditetapkan pemilik (config.rollback_on target produksi); pemilik "
                           "memutuskan rollback, forward-fix atau menerima risiko. " + NEVER}
    met = sorted(set(failure_conditions) & set(rollback_on))
    if met:
        return {**base, "status": "ROLLBACK_RECOMMENDED", "criteria": "MET", "criteria_met": met,
                "summary": f"Kriteria rollback pemilik terpenuhi ({', '.join(met)}): rollback manual direkomendasikan. "
                           + NEVER}
    return {**base, "status": "CRITERIA_NOT_MET", "criteria": "NOT_MET", "criteria_met": [],
            "summary": "Kegagalan teramati tetapi tidak memenuhi kriteria rollback pemilik; pemilik memutuskan "
                       "forward-fix atau menerima risiko. " + NEVER}


def _open_request(session: Session, target_id: str) -> Any:
    from aeos.models.policy_ext import DecisionRequest

    return session.scalar(select(DecisionRequest).where(
        DecisionRequest.target_type == REQUEST_TARGET, DecisionRequest.target_id == target_id,
        DecisionRequest.status == "OPEN").order_by(DecisionRequest.created_at.desc()).limit(1))


def record_failure(session: Session, *, project: Project, commit: str, target: str, observation_id: str,
                   evidence_ids: list[str], failure_conditions: list[str], rollback_on: list[str],
                   actor: str) -> dict[str, Any]:
    from aeos.operations import alerts, incident
    from aeos.policy import facade as policy

    commit = commit.lower()
    rec = recommendation(failure_conditions, rollback_on)
    title = f"Kegagalan pasca-rilis {project.name}: commit {commit[:12]}"
    alert, new_alert = alerts.raise_alert(
        session, dedup_key=dedup_key(project.id, commit), kind=RULE, severity=alerts.rule_severity(RULE), title=title,
        detail={"commit": commit, "target": target, "observation_id": observation_id, "evidence_ids": evidence_ids,
                "failure_conditions": failure_conditions, "recommendation": rec["status"], "runbook": RUNBOOK},
        domain=project.domain, project_id=project.id, source="delivery", auto_resolve=False)
    # the newest incident of this alert that is still open; open_from_alert returns the FIRST incident ever linked
    # to the alert, which may be one the human already resolved
    inc = session.scalar(select(OpsIncident).where(
        OpsIncident.source == "alert", OpsIncident.source_ref == alert.id, OpsIncident.status != "RESOLVED")
        .order_by(OpsIncident.created_at.desc()).limit(1))
    created = False
    if inc is None:
        inc, created = incident.open_from_alert(session, alert.id, actor=actor, domain=project.domain)
    if inc.status == "RESOLVED":  # a new failure after a resolved incident is a new incident (exactly one)
        inc = incident.open_incident(session, title=f"Alert: {alert.title}", actor=actor, source="alert",
                                     source_ref=alert.id, domain=project.domain, project_id=project.id,
                                     note=f"{alert.severity} {alert.kind}: kegagalan baru setelah insiden sebelumnya "
                                          "diselesaikan")
        created = True
    incident.add_timeline(session, inc.id, actor=actor, evidence_ids=evidence_ids,
                          note=f"Observasi produksi {observation_id} GAGAL ({', '.join(failure_conditions) or 'FAIL'}) "
                               f"untuk {target}. Rekomendasi: {rec['status']} - {rec['summary']} Runbook: {RUNBOOK}")
    target_id = f"{project.id}:{commit[:40]}"
    req = _open_request(session, target_id)
    if req is None:
        request_id = policy.request_decision(
            session, kind="RISK_ACCEPTANCE", title=f"Pasca-rilis gagal: putuskan rollback, forward-fix atau terima "
                                                   f"risiko ({project.name} {commit[:12]})",
            reason=f"Observasi produksi {observation_id} GAGAL ({', '.join(failure_conditions) or 'FAIL'}). "
                   f"{rec['summary']} Insiden {inc.id}, alert {alert.id}.",
            requested_by="aeos.delivery.deployment-observer", requested_by_kind="SYSTEM", project_id=project.id,
            domain=project.domain, target_type=REQUEST_TARGET, target_id=target_id,
            details={"options": list(OPTIONS), "recommendation": rec, "alert_id": alert.id, "incident_id": inc.id,
                     "observation_id": observation_id, "evidence_ids": evidence_ids, "commit": commit,
                     "target": target, "runbook": RUNBOOK})
        new_request = True
    else:
        request_id, new_request = req.id, False
    return {"alert_id": alert.id, "alert_new": new_alert, "incident_id": inc.id, "incident_created": created,
            "decision_request_id": request_id, "decision_request_new": new_request, "recommendation": rec,
            "options": [o["key"] for o in OPTIONS], "runbook": RUNBOOK}


def record_recovered(session: Session, *, project: Project, commit: str, observation_id: str,
                     actor: str) -> dict[str, Any] | None:
    """A later PASS observation of the same commit resolves the alert; the incident and the decision request stay
    with the human (a passing check does not explain the failure)."""
    from aeos.operations import alerts, incident

    alert = session.scalar(select(OpsAlert).where(OpsAlert.dedup_key == dedup_key(project.id, commit)))
    if alert is None or alert.status != "OPEN":
        return None
    alerts.resolve_alert(session, alert, resolution=f"observasi produksi {observation_id} untuk commit yang sama "
                                                    "lulus; insiden dan keputusan tetap pada manusia")
    inc = session.scalar(select(OpsIncident).where(OpsIncident.source == "alert", OpsIncident.source_ref == alert.id,
                                                   OpsIncident.status != "RESOLVED"))
    if inc is not None:
        incident.add_timeline(session, inc.id, actor=actor, evidence_ids=[observation_id],
                              note=f"Observasi produksi {observation_id} untuk commit {commit[:12]} lulus; alert "
                                   "diselesaikan. Akar masalah dan tindakan korektif tetap dicatat manusia.")
    return {"alert_id": alert.id, "alert_status": "RESOLVED", "incident_id": inc.id if inc else None}


def status_for(session: Session, project: Project, commit: str | None) -> dict[str, Any] | None:
    """The alert / incident / decision request of a post-release failure of *commit* (read-only, for G8)."""
    if not commit:
        return None
    alert = session.scalar(select(OpsAlert).where(OpsAlert.dedup_key == dedup_key(project.id, commit)))
    if alert is None:
        return None
    inc = session.scalar(select(OpsIncident).where(OpsIncident.source == "alert", OpsIncident.source_ref == alert.id)
                         .order_by(OpsIncident.created_at.desc()).limit(1))
    from aeos.models.policy_ext import DecisionRequest

    req = session.scalar(select(DecisionRequest).where(
        DecisionRequest.target_type == REQUEST_TARGET, DecisionRequest.target_id == f"{project.id}:{commit.lower()[:40]}")
        .order_by(DecisionRequest.created_at.desc()).limit(1))
    return {"alert_id": alert.id, "alert_status": alert.status, "incident_id": inc.id if inc else None,
            "incident_status": inc.status if inc else None, "decision_request_id": req.id if req else None,
            "decision_request_status": req.status if req else None,
            "recommendation": ((req.details or {}).get("recommendation") or {}).get("status") if req else None,
            "runbook": RUNBOOK}
