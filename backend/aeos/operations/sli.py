"""Service level indicators computed from control-plane records (§33).

§33: "SLI/SLO ditentukan sesuai criticality, bukan angka availability generik. Monitoring mencakup
outcome, queue age, stale lease, retry, acceptance failure, budget drift, sync freshness dan
external integration health." Every SLI is computed deterministically from the database (no LLM).
**Targets are owner decisions**: no default targets exist; an SLI without a recorded target shows
``NO_TARGET`` — never "within target". Alerts for objective anomalies (expired-but-active leases,
revision conflicts, failing integrations, untested restore) are raised regardless of targets.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aeos.core.config import Settings
from aeos.core.enums import Domain
from aeos.core.errors import ValidationFailed
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.models.cost import BudgetReservation, UsageLedgerEntry
from aeos.models.evidence import AcceptanceRecord
from aeos.models.identity import SystemSetting
from aeos.models.portfolio import Project, ProjectIntegration
from aeos.models.publishing import ExternalReference, OperationalWorkProjection
from aeos.models.workforce import Lease, TaskExecution, WorkerRun

TARGETS_KEY = "ops.sli.targets"
WINDOW = timedelta(hours=24)
# Observation threshold for "no heartbeat" (not an SLO target): a lease without heartbeat for this
# long, or past its expiry while still ACTIVE, is reported as stale.
STALE_HEARTBEAT_SECONDS = 300

# key -> (Indonesian title, unit, direction of a target: "max" = lower is better, "min" = higher)
SLI_DEFS: dict[str, tuple[str, str, str]] = {
    "queue_age_seconds": ("Umur antrean tertua (QUEUED/READY)", "seconds", "max"),
    "stale_leases": ("Lease usang (kedaluwarsa/tanpa heartbeat)", "count", "max"),
    "retry_rate": ("Rasio percobaan ulang (24 jam)", "ratio", "max"),
    "acceptance_failures": ("Kegagalan verifikasi/penerimaan (24 jam)", "count", "max"),
    "accepted_outcomes": ("Outcome diterima (24 jam)", "count", "min"),
    "budget_drift_usd": ("Selisih biaya aktual vs reservasi (24 jam)", "usd", "max"),
    "unknown_cost_entries": ("Entri biaya tidak diketahui (24 jam)", "count", "max"),
    "sync_freshness_seconds": ("Umur sinkronisasi eksternal tertua", "seconds", "max"),
    "sync_problems": ("Referensi eksternal STALE/CONFLICT/UNKNOWN", "count", "max"),
    "integration_failures": ("Integrasi gagal/kedaluwarsa", "count", "max"),
    "recovery_point_age_seconds": ("Umur backup teruji-restore terbaru", "seconds", "max"),
}
PLATFORM_SLIS = frozenset({"recovery_point_age_seconds"})
WINDOWED_SLIS = frozenset({"retry_rate", "acceptance_failures", "accepted_outcomes", "budget_drift_usd",
                           "unknown_cost_entries"})


def _age(now: datetime, dt: datetime | None) -> float | None:
    dt = ensure_aware(dt)
    return None if dt is None else max(0.0, (now - dt).total_seconds())


def get_targets(session: Session) -> dict[str, Any]:
    row = session.get(SystemSetting, TARGETS_KEY)
    return dict(row.value) if row and isinstance(row.value, dict) else {}


def set_target(
    session: Session, key: str, *, maximum: float | None, minimum: float | None, actor_type: str, actor_id: str,
    rationale: str = "",
) -> dict[str, Any]:
    """Record an owner decision for an SLI target (audited). ``None`` for both clears the target."""
    if key not in SLI_DEFS:
        raise ValidationFailed(f"unknown SLI {key!r}", details={"known": sorted(SLI_DEFS)})
    targets = get_targets(session)
    previous = targets.get(key)
    if maximum is None and minimum is None:
        targets.pop(key, None)
    else:
        targets[key] = {"max": maximum, "min": minimum, "decided_by": actor_id, "decided_at": utcnow().isoformat(),
                        "rationale": rationale[:500]}
    row = session.get(SystemSetting, TARGETS_KEY)
    if row is None:
        session.add(SystemSetting(key=TARGETS_KEY, value=targets))
    else:
        row.value = targets
        row.version = (row.version or 1) + 1
    from aeos.audit import service as audit

    audit.record(session, actor_type=actor_type, actor_id=actor_id, action="ops.sli.target.set",
                 target_type="sli", target_id=key, details={"previous": previous, "new": targets.get(key)})
    session.flush()
    return {"key": key, "target": targets.get(key)}


def _status(key: str, value: float | None, targets: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    target = targets.get(key)
    if not target:
        return "NO_TARGET", None
    if value is None:
        return "UNKNOWN", target
    mx, mn = target.get("max"), target.get("min")
    if mx is not None and value > float(mx):
        return "BREACH", target
    if mn is not None and value < float(mn):
        return "BREACH", target
    return "WITHIN_TARGET", target


def _domain_project_ids(session: Session, domain: str) -> list[str]:
    return list(session.scalars(select(Project.id).where(Project.domain == domain)))


def compute(session: Session, *, domain: Domain, settings: Settings | None = None,
            now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    d = domain.value
    since = now - WINDOW
    project_ids = _domain_project_ids(session, d)
    values: dict[str, tuple[float | None, dict[str, Any]]] = {}

    # queue age
    queued = list(session.scalars(select(TaskExecution).where(
        TaskExecution.domain == d, TaskExecution.state.in_(["QUEUED", "READY"]))))
    ages = [a for a in (_age(now, t.updated_at) for t in queued) if a is not None]
    values["queue_age_seconds"] = (max(ages) if ages else 0.0, {"queued_tasks": len(queued)})

    # stale leases (ACTIVE but expired, or no heartbeat for STALE_HEARTBEAT_SECONDS)
    lease_rows = list(session.execute(
        select(Lease, TaskExecution.domain).join(TaskExecution, TaskExecution.task_id == Lease.task_id)
        .where(Lease.state == "ACTIVE", TaskExecution.domain == d)
    ))
    stale_ids: list[str] = []
    for lease, _dom in lease_rows:
        expires = ensure_aware(lease.expires_at)
        expired = expires is not None and expires < now
        beat = _age(now, lease.heartbeat_at or lease.acquired_at)
        if expired or (beat is not None and beat > STALE_HEARTBEAT_SECONDS):
            stale_ids.append(lease.id)
    values["stale_leases"] = (float(len(stale_ids)), {"active_leases": len(lease_rows), "stale_lease_ids": stale_ids[:20],
                                                       "threshold_seconds": STALE_HEARTBEAT_SECONDS})

    # retries
    runs = list(session.scalars(select(WorkerRun).where(WorkerRun.domain == d, WorkerRun.created_at >= since)))
    retries = sum(1 for r in runs if (r.attempt or 1) > 1)
    values["retry_rate"] = ((retries / len(runs)) if runs else None, {"runs": len(runs), "retries": retries})

    # acceptance failures / accepted outcomes
    acc_failed = acc_ok = 0
    if project_ids:
        acc_failed = int(session.scalar(select(func.count()).select_from(AcceptanceRecord).where(
            AcceptanceRecord.project_id.in_(project_ids), AcceptanceRecord.updated_at >= since,
            AcceptanceRecord.acceptance_status.in_(["REJECTED", "INVALIDATED"]))) or 0)
        acc_ok = int(session.scalar(select(func.count()).select_from(AcceptanceRecord).where(
            AcceptanceRecord.project_id.in_(project_ids), AcceptanceRecord.updated_at >= since,
            AcceptanceRecord.acceptance_status == "ACCEPTED")) or 0)
    verifier_failed = sum(1 for r in runs if (r.verifier_status or "").upper() == "FAILED")
    values["acceptance_failures"] = (float(acc_failed + verifier_failed),
                                     {"acceptance_rejected_or_invalidated": acc_failed, "verifier_failed_runs": verifier_failed})
    values["accepted_outcomes"] = (float(acc_ok), {"window_hours": int(WINDOW.total_seconds() // 3600)})

    # budget drift: actual recorded cost vs committed reservations for this domain's runs
    run_ids = [r.id for r in runs]
    reservations = list(session.scalars(select(BudgetReservation).where(
        BudgetReservation.run_id.in_(run_ids), BudgetReservation.status == "COMMITTED"))) if run_ids else []
    if reservations:
        res_runs = [r.run_id for r in reservations]
        reserved = sum(float(r.reserved_usd or 0) for r in reservations)
        actual = float(session.scalar(select(func.coalesce(func.sum(UsageLedgerEntry.cost_usd), 0)).where(
            UsageLedgerEntry.run_id.in_(res_runs), UsageLedgerEntry.mode == "LIVE",
            UsageLedgerEntry.cost_usd.is_not(None))) or 0)
        values["budget_drift_usd"] = (actual - reserved, {"reserved_usd": reserved, "actual_usd": actual,
                                                          "reservations": len(reservations)})
    else:
        values["budget_drift_usd"] = (None, {"reservations": 0, "note": "no committed reservations in window"})
    unknown_cost = int(session.scalar(select(func.count()).select_from(UsageLedgerEntry).where(
        UsageLedgerEntry.domain == d, UsageLedgerEntry.at >= since, UsageLedgerEntry.mode == "LIVE",
        UsageLedgerEntry.cost_usd.is_(None))) or 0)
    values["unknown_cost_entries"] = (float(unknown_cost), {})

    # sync freshness / problems
    refs = list(session.scalars(select(ExternalReference).where(ExternalReference.domain == d)))
    projs = list(session.scalars(select(OperationalWorkProjection).where(
        OperationalWorkProjection.project_id.in_(project_ids)))) if project_ids else []
    sync_ages: list[float] = []
    never = 0
    problems = conflicts = 0
    sync_items: list[ExternalReference | OperationalWorkProjection] = [*refs, *projs]
    for item in sync_items:
        age = _age(now, item.last_synced_at)
        if age is None:
            never += 1
        else:
            sync_ages.append(age)
        status = (item.sync_status or "UNKNOWN").upper()
        if status in ("STALE", "CONFLICT", "UNKNOWN"):
            problems += 1
        if status == "CONFLICT":
            conflicts += 1
    total_refs = len(refs) + len(projs)
    values["sync_freshness_seconds"] = (max(sync_ages) if sync_ages else None,
                                        {"references": total_refs, "never_synced": never})
    values["sync_problems"] = (float(problems + never) if total_refs else 0.0,
                               {"conflicts": conflicts, "never_synced": never, "references": total_refs})

    # external integration health (recorded capability status; CONNECTED is not VERIFIED)
    integrations = list(session.scalars(select(ProjectIntegration).where(ProjectIntegration.domain == d)))
    failing: list[str] = []
    unverified = 0
    for integ in integrations:
        statuses = {str(v.get("status") if isinstance(v, dict) else v).upper()
                    for v in (integ.capability_status or {}).values()}
        if statuses & {"FAILED", "EXPIRED", "EXPIRED_AUTH", "UNREACHABLE"}:
            failing.append(f"{integ.provider}:{integ.purpose}")
        if integ.last_verified_at is None:
            unverified += 1
    values["integration_failures"] = (float(len(failing)), {"integrations": len(integrations), "failing": failing[:20],
                                                            "never_verified": unverified})

    # platform-level recovery point (restore-tested backup)
    recovery_detail: dict[str, Any] = {}
    recovery_value: float | None = None
    if settings is not None:
        from aeos.operations import backup

        ready = backup.latest_ready(settings)
        if ready:
            recovery_value = _age(now, datetime.fromisoformat(str(ready["created_at"])))
            recovery_detail = {"backup_id": ready["backup_id"]}
        else:
            recovery_detail = {"note": "no restore-tested backup (recovery NOT-PROVEN)"}
    else:
        recovery_detail = {"note": "settings unavailable"}
    values["recovery_point_age_seconds"] = (recovery_value, recovery_detail)

    targets = get_targets(session)
    items = []
    for key, (title, unit, direction) in SLI_DEFS.items():
        value, detail = values[key]
        status, target = _status(key, value, targets)
        items.append({
            "key": key, "title": title, "unit": unit, "direction": direction, "value": value,
            "status": status, "target": target, "detail": detail,
            "scope": "PLATFORM" if key in PLATFORM_SLIS else d,
            "window_hours": int(WINDOW.total_seconds() // 3600) if key in WINDOWED_SLIS else None,
        })
    return {
        "domain": d,
        "checked_at": now.isoformat(),
        "source": "control-plane database (task_executions, leases, worker_runs, acceptance_records, "
                  "budget_reservations, usage_ledger, external_references, project_integrations, backups)",
        "targets_note": "SLI targets are owner decisions (§33); NO_TARGET means no decision recorded yet. "
                        "Set one with: aeos ops sli target <key> --max <value>",
        "items": items,
        "_conflicts": conflicts,
    }


def observed_conditions(result: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(domain-scoped conditions, platform-scoped conditions) derived from an SLI result."""
    d = result["domain"]
    by_key = {i["key"]: i for i in result["items"]}
    domain_conds: list[dict[str, Any]] = []
    platform_conds: list[dict[str, Any]] = []
    stale = by_key["stale_leases"]
    if (stale["value"] or 0) > 0:
        domain_conds.append({"dedup_key": f"{d}:stale_leases", "kind": "stale_leases", "severity": "HIGH",
                             "title": f"{int(stale['value'])} lease usang terdeteksi ({d})", "detail": stale["detail"]})
    if result.get("_conflicts"):
        domain_conds.append({"dedup_key": f"{d}:sync_conflict", "kind": "sync_conflict", "severity": "MEDIUM",
                             "title": f"Konflik revisi pada sinkronisasi eksternal ({d})",
                             "detail": {"conflicts": result["_conflicts"]}})
    integ = by_key["integration_failures"]
    if (integ["value"] or 0) > 0:
        domain_conds.append({"dedup_key": f"{d}:integration_failures", "kind": "integration_failure",
                             "severity": "MEDIUM", "title": f"Integrasi eksternal gagal/kedaluwarsa ({d})",
                             "detail": integ["detail"]})
    recovery = by_key["recovery_point_age_seconds"]
    if recovery["value"] is None:
        platform_conds.append({"dedup_key": "GLOBAL:restore_untested", "kind": "restore_untested", "severity": "MEDIUM",
                               "title": "Restore backup belum pernah diuji - pemulihan belum terbukti",
                               "detail": recovery["detail"], "domain": None})
    for item in result["items"]:
        if item["status"] == "BREACH":
            cond = {"dedup_key": f"{'GLOBAL' if item['scope'] == 'PLATFORM' else d}:sli_breach:{item['key']}",
                    "kind": "sli_breach", "severity": "MEDIUM",
                    "title": f"SLI di luar target pemilik: {item['title']}",
                    "detail": {"value": item["value"], "target": item["target"]}}
            (platform_conds if item["scope"] == "PLATFORM" else domain_conds).append(
                {**cond, "domain": None} if item["scope"] == "PLATFORM" else cond)
    return domain_conds, platform_conds


def evaluate(session: Session, *, domain: Domain, settings: Settings | None = None) -> dict[str, Any]:
    """Compute SLIs and reconcile alerts (raises/resolves; caller commits)."""
    from aeos.operations import alerts

    result = compute(session, domain=domain, settings=settings)
    domain_conds, platform_conds = observed_conditions(result)
    a = alerts.reconcile(session, scope_domain=domain.value, observed=domain_conds)
    b = alerts.reconcile(session, scope_domain=None, observed=platform_conds)
    result.pop("_conflicts", None)
    return {**result, "alerts": {"domain": a, "platform": b}}


def public(result: dict[str, Any]) -> dict[str, Any]:
    out = dict(result)
    out.pop("_conflicts", None)
    return out
