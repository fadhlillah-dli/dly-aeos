"""Deduplicated alerts (§33 monitoring, §35 "alert").

An alert is raised once per ``dedup_key`` while its condition holds: the first observation creates
an OPEN ``OpsAlert`` and emits an ``alert.raised`` event; repeated observations only bump
``occurrences``/``last_seen_at``. When an auto-resolving condition disappears the alert is RESOLVED
and ``alert.resolved`` is emitted. Alerts carry the domain of their data so Office alerts never
reach a Personal session (events and listings are domain-filtered).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aeos.core.redaction import redact
from aeos.core.timeutil import utcnow
from aeos.events import service as events
from aeos.models.operations_ext import OpsAlert
from aeos.operations._util import iso

SEVERITIES = ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")

# The alert rules this controller raises (kind -> severity, condition). One source for the producers (sli,
# emergency, release) and for the Operations documents of the platform project (§27 08. Alerts).
ALERT_RULES: dict[str, dict[str, str]] = {
    "stale_leases": {"severity": "HIGH", "condition": "lease kedaluwarsa/tanpa heartbeat masih ACTIVE (per domain)"},
    "sync_conflict": {"severity": "MEDIUM", "condition": "konflik revisi pada sinkronisasi eksternal (per domain)"},
    "integration_failure": {"severity": "MEDIUM", "condition": "integrasi eksternal gagal/kedaluwarsa (per domain)"},
    "restore_untested": {"severity": "MEDIUM", "condition": "belum ada backup yang lolos restore test (platform)"},
    "sli_breach": {"severity": "MEDIUM", "condition": "SLI di luar target yang ditetapkan pemilik"},
    "error_budget_exhausted": {"severity": "HIGH", "condition": "error budget SLI habis dalam jendela pengukuran"},
    "release_failure": {"severity": "HIGH", "condition": "build/promote/rollback rilis controller gagal"},
    "post_release_failure": {"severity": "HIGH", "condition": "observasi pasca-rilis target produksi proyek GAGAL "
                                                              "(G8; per proyek + commit) - insiden + keputusan "
                                                              "rollback/forward-fix oleh manusia"},
    "emergency_stop": {"severity": "CRITICAL", "condition": "emergency stop dijalankan operator"},
}
NOTIFICATION = ("Event alert.raised / alert.resolved ke Control Center (stream SSE, Riwayat → SLI, alert & flag) "
                "dan GET /api/ops/alerts; tidak ada kanal eksternal (email/chat) yang dikonfigurasi.")
ESCALATION = ("Alert → insiden (POST /api/ops/alerts/{id}/incident) dengan owner manusia → bila perlu emergency stop "
              "(runbook incident-containment) → usulan perubahan / eval case untuk review manusia.")


def rule_severity(kind: str) -> str:
    return ALERT_RULES.get(kind, {}).get("severity", "MEDIUM")


def alert_view(a: OpsAlert) -> dict[str, Any]:
    return {
        "id": a.id, "dedup_key": a.dedup_key, "kind": a.kind, "severity": a.severity, "title": a.title,
        "detail": a.detail or {}, "domain": a.domain, "project_id": a.project_id, "source": a.source,
        "status": a.status, "occurrences": a.occurrences, "first_seen_at": iso(a.first_seen_at),
        "last_seen_at": iso(a.last_seen_at), "resolved_at": iso(a.resolved_at), "resolution": a.resolution,
    }


def raise_alert(
    session: Session,
    *,
    dedup_key: str,
    kind: str,
    severity: str,
    title: str,
    detail: dict[str, Any] | None = None,
    domain: str | None = None,
    project_id: str | None = None,
    source: str = "sli",
    auto_resolve: bool = True,
) -> tuple[OpsAlert, bool]:
    """Return (alert, newly_raised). An ``alert.raised`` event is emitted only when newly raised."""
    if severity not in SEVERITIES:
        severity = "MEDIUM"
    now = utcnow()
    alert = session.scalar(select(OpsAlert).where(OpsAlert.dedup_key == dedup_key))
    clean = redact(detail or {})
    if alert is not None and alert.status == "OPEN":
        alert.last_seen_at = now
        alert.occurrences = (alert.occurrences or 0) + 1
        alert.detail = clean
        alert.severity = severity
        session.flush()
        return alert, False
    if alert is None:
        alert = OpsAlert(dedup_key=dedup_key, kind=kind, severity=severity, title=title, detail=clean,
                         domain=domain, project_id=project_id, source=source, auto_resolve=auto_resolve,
                         first_seen_at=now, last_seen_at=now, occurrences=1)
        session.add(alert)
    else:  # re-opened after resolution
        alert.status = "OPEN"
        alert.title = title
        alert.detail = clean
        alert.severity = severity
        alert.first_seen_at = now
        alert.last_seen_at = now
        alert.occurrences = 1
        alert.resolved_at = None
        alert.resolution = ""
    session.flush()
    events.emit(session, source="operations", kind="alert.raised", entity_type="alert", entity_id=alert.id,
                domain=domain, project_id=project_id,
                payload={"dedup_key": dedup_key, "kind": kind, "severity": severity, "title": title})
    return alert, True


def resolve_alert(session: Session, alert: OpsAlert, *, resolution: str) -> None:
    if alert.status != "OPEN":
        return
    alert.status = "RESOLVED"
    alert.resolved_at = utcnow()
    alert.resolution = resolution[:500]
    session.flush()
    events.emit(session, source="operations", kind="alert.resolved", entity_type="alert", entity_id=alert.id,
                domain=alert.domain, project_id=alert.project_id,
                payload={"dedup_key": alert.dedup_key, "resolution": alert.resolution})


def reconcile(
    session: Session, *, scope_domain: str | None, observed: list[dict[str, Any]]
) -> dict[str, Any]:
    """Raise every observed condition and resolve auto-resolving OPEN alerts of the same scope that
    are no longer observed. *observed* items: dedup_key, kind, severity, title, detail."""
    raised: list[str] = []
    seen: set[str] = set()
    for cond in observed:
        alert, new = raise_alert(session, dedup_key=cond["dedup_key"], kind=cond["kind"],
                                 severity=cond.get("severity", "MEDIUM"), title=cond["title"],
                                 detail=cond.get("detail"), domain=cond.get("domain", scope_domain),
                                 project_id=cond.get("project_id"), source=cond.get("source", "sli"))
        seen.add(alert.dedup_key)
        if new:
            raised.append(alert.dedup_key)
    q = select(OpsAlert).where(OpsAlert.status == "OPEN", OpsAlert.auto_resolve.is_(True), OpsAlert.source == "sli")
    q = q.where(OpsAlert.domain.is_(None)) if scope_domain is None else q.where(OpsAlert.domain == scope_domain)
    resolved: list[str] = []
    for alert in session.scalars(q):
        if alert.dedup_key not in seen:
            resolve_alert(session, alert, resolution="condition no longer observed")
            resolved.append(alert.dedup_key)
    return {"raised": raised, "resolved": resolved, "observed": len(observed)}


def list_alerts(
    session: Session, *, domain: str | None, status: str | None = "OPEN", limit: int = 200
) -> list[dict[str, Any]]:
    """Alerts of *domain* plus platform-level alerts (domain NULL); never another domain's alerts."""
    q = select(OpsAlert).order_by(OpsAlert.last_seen_at.desc()).limit(min(limit, 500))
    if domain is None:
        q = q.where(OpsAlert.domain.is_(None))
    else:
        q = q.where(or_(OpsAlert.domain == domain, OpsAlert.domain.is_(None)))
    if status:
        q = q.where(OpsAlert.status == status)
    return [alert_view(a) for a in session.scalars(q)]
