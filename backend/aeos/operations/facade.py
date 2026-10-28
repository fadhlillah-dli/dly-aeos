"""Cross-module contract of ``aeos.operations`` (§09 released controller, §33 retirement). Keep stable."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from aeos.core.config import Settings
from aeos.models.decisions import Decision
from aeos.models.portfolio import Project


def controller_report(settings: Settings, session: Session | None = None) -> dict[str, Any]:
    """Which code runs as the controller compared with the ACTIVE release, plus config drift (§09 zones)."""
    from aeos.operations.release import controller_report as impl

    return impl(settings, session)


def ui_bundle_state(settings: Settings, repo: Path, dist: Path | None = None) -> dict[str, Any]:
    """Whether the Control Center bundle belongs to the code in *repo* (FRESH | STALE | UNVERIFIED | MISSING)."""
    from aeos.operations.freshness import bundle_state

    return bundle_state(settings, repo, dist)


def frontend_head_tree(repo: Path) -> str | None:
    """``git rev-parse HEAD:frontend``; None outside a git checkout."""
    from aeos.operations.freshness import frontend_head_tree as impl

    return impl(repo)


def serve_code_drift(settings: Settings) -> dict[str, Any]:
    """Does the running ``aeos serve`` run the backend code its checkout's HEAD has (see freshness.code_drift)."""
    from aeos.operations.freshness import code_drift

    return code_drift(settings)


class RetentionMarkers:
    """Per-record retention markers for audit/evidence lists (§35): the policy and legal holds are read once per
    list, then ``audit(row)`` / ``evidence(row)`` resolve the rule of one record. Read-only; nothing is deleted."""

    def __init__(self, session: Session) -> None:
        from aeos.operations import retention

        self._r = retention
        self.policy = retention.load_policy()
        self.held = retention.legal_hold_project_ids(session)

    def audit(self, row: Any) -> dict[str, Any]:
        return self._r.marker(self.policy, "audit_events", row.domain, row.at,
                              legal_hold=row.project_id is not None and row.project_id in self.held)

    def evidence(self, row: Any) -> dict[str, Any]:
        return self._r.marker(self.policy, "evidence", row.domain, row.created_at,
                              legal_hold=self._r.evidence_legal_hold(row.retention_policy, row.project_id, self.held))


def platform_operations_facts(domain: str) -> dict[str, str]:
    """§27 08. Operations for the PLATFORM project (DLY AEOS documents itself): the sections the plan schema has
    no field for, read from this controller's own rules and configuration (alert catalog, retention policy,
    backup layout, Doctor registry, flags). Stable facts only - no live counts (documents link live data)."""
    from aeos.doctor.facade import check_catalog
    from aeos.operations import alerts, flags, retention

    policy = retention.load_policy()
    audit_rule = retention.resolve(policy, "audit_events", domain)
    ev_rule = retention.resolve(policy, "evidence", domain)
    backup_rule = retention.resolve(policy, "backups", domain)
    rules = sorted(alerts.ALERT_RULES.items(), key=lambda kv: (alerts.SEVERITIES.index(kv[1]["severity"]) * -1, kv[0]))
    catalog = check_catalog()
    spec = flags.REGISTRY.get("ops.sli_loop_interval_seconds")
    interval = spec.default if spec is not None else None
    return {
        "logging.audit_logs": "Audit trail controller: tabel `audit_events` append-only (trigger DB, guard dicek "
                              "Doctor `evidence.append_only_guard`; bukan jaminan WORM, §34) — Riwayat → Audit, "
                              "GET /api/audit.",
        "logging.security_logs": "Kejadian keamanan tercatat di audit trail yang sama: hasil DENIED/FAILED (login "
                                 "gagal/lockout, origin/CSRF ditolak, policy denial, akses domain lain ditolak); "
                                 "pemindaian exposure secret oleh Doctor.",
        "logging.retention": f"Menurut policy/retention.yaml (keputusan pemilik): audit_events {domain}: "
                             f"{audit_rule['label']} Evidence: {ev_rule['label']} AEOS tidak pernah menghapus data.",
        "logging.access": "Baca: sesi lokal SUPER_ADMIN/VIEWER pada domain yang dipilih sendiri (data OFFICE tidak "
                          "tampil di sesi PERSONAL); tulis/aksi: SUPER_ADMIN; tanpa akses jaringan selain loopback.",
        "alerting.severity": "\n".join(f"- `{k}`: {v['severity']} — {v['condition']}" for k, v in rules),
        "alerting.notification": alerts.NOTIFICATION,
        "alerting.escalation": alerts.ESCALATION,
        "backup.storage": f"Direktori lokal `<data>/backups` (manifest + hash per backup, dibuat `aeos ops backup "
                          f"create`); retensi backup: {backup_rule['label']} Salinan di luar mesin = keputusan pemilik.",
        "maintenance.checks": f"Readiness Doctor: {catalog['count']} pemeriksaan tanpa LLM dalam grup "
                              f"{', '.join(catalog['groups'])}; pemeriksaan lokal yang STALE dicek ulang otomatis.",
        "maintenance.routine_activities": (f"Evaluasi SLI + alert berkala tiap {interval} detik (flag "
                                           "`ops.sli_loop_interval_seconds`); " if interval else "") +
                                          "backup + restore test (runbook backup-restore); rekonsiliasi inventory "
                                          "(`aeos inventory reconcile`); rotasi credential (runbook credential-rotation).",
        "overview.support": "Pemilik lokal sebagai Operations Owner; insiden dicatat dan ditindaklanjuti di AEOS "
                            "Operations (Riwayat → Insiden); prosedur di docs/runbooks.",
    }


def project_incidents(session: Session, project: Project) -> list[dict[str, Any]]:
    """Domain-scoped views of the incidents that concern *project* (§27 09.), oldest first (read-only)."""
    from aeos.operations.incident import incidents_for_project as impl

    return impl(session, project)


def post_release_failure(session: Session, *, project: Project, commit: str, target: str, observation_id: str,
                         evidence_ids: list[str], failure_conditions: list[str], rollback_on: list[str],
                         actor: str) -> dict[str, Any]:
    """Additive (§31 G8, §40): a FAIL production observation raises the deduplicated ``post_release_failure``
    alert, opens/links its incident with a HUMAN rollback recommendation and files one decision request
    (rollback / forward-fix / accept). AEOS never deploys or rolls back. Does not commit."""
    from aeos.operations.post_release import record_failure

    return record_failure(session, project=project, commit=commit, target=target, observation_id=observation_id,
                          evidence_ids=evidence_ids, failure_conditions=failure_conditions, rollback_on=rollback_on,
                          actor=actor)


def post_release_recovered(session: Session, *, project: Project, commit: str, observation_id: str,
                           actor: str) -> dict[str, Any] | None:
    """Additive: a later PASS observation of the same commit resolves the alert (incident/decision stay human)."""
    from aeos.operations.post_release import record_recovered

    return record_recovered(session, project=project, commit=commit, observation_id=observation_id, actor=actor)


def post_release_status(session: Session, project: Project, commit: str | None) -> dict[str, Any] | None:
    """Additive, read-only: alert / incident / decision request of a post-release failure of *commit*."""
    from aeos.operations.post_release import status_for

    return status_for(session, project, commit)


def require_retirement_decision(session: Session, settings: Settings, project: Project) -> Decision:
    """Refuse lifecycle RETIRED unless the owner's retirement decision binds the current checklist (§33)."""
    from aeos.operations.retirement import require_retirement_decision as impl

    return impl(session, settings, project)
