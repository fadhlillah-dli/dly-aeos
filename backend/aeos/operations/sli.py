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
from aeos.operations import alerts

TARGETS_KEY = "ops.sli.targets"
CRITICALITY_KEY = "ops.criticality"
ERROR_BUDGET_KEY = "ops.error_budget.policy"
ERROR_BUDGET_PAUSE_KEY = "ops.error_budget.paused"
CRITICALITIES = ("HIGH", "MEDIUM", "LOW")
ERROR_BUDGET_ACTIONS = ("alert", "pause_scheduling")
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
# SLIs that can be measured per project, so projects of different (owner-assigned) criticality can have
# different targets for the same SLI (§33 "SLI/SLO ditentukan sesuai criticality").
PER_PROJECT_SLIS = frozenset({"queue_age_seconds", "stale_leases", "retry_rate", "acceptance_failures",
                              "accepted_outcomes"})
_STATUS_RANK = {"NO_TARGET": 0, "WITHIN_TARGET": 1, "UNKNOWN": 2, "BREACH": 3}


def _age(now: datetime, dt: datetime | None) -> float | None:
    dt = ensure_aware(dt)
    return None if dt is None else max(0.0, (now - dt).total_seconds())


def get_targets(session: Session) -> dict[str, Any]:
    row = session.get(SystemSetting, TARGETS_KEY)
    return dict(row.value) if row and isinstance(row.value, dict) else {}


def _setting_value(session: Session, key: str) -> dict[str, Any]:
    row = session.get(SystemSetting, key)
    return dict(row.value) if row and isinstance(row.value, dict) else {}


def _put_setting(session: Session, key: str, value: Any) -> None:
    row = session.get(SystemSetting, key)
    if row is None:
        session.add(SystemSetting(key=key, value=value))
    else:
        row.value = value
        row.version = (row.version or 1) + 1


def get_criticality(session: Session) -> dict[str, Any]:
    """project_id -> {level, decided_by, decided_at, rationale} (owner decisions; none by default)."""
    return _setting_value(session, CRITICALITY_KEY)


def set_criticality(session: Session, project_id: str, level: str | None, *, actor_type: str, actor_id: str,
                    rationale: str = "") -> dict[str, Any]:
    """Owner decision: the criticality of a project (HIGH|MEDIUM|LOW) that selects its SLI targets."""
    if session.get(Project, project_id) is None:
        raise ValidationFailed("unknown project")
    if level is not None and level not in CRITICALITIES:
        raise ValidationFailed(f"criticality must be one of {CRITICALITIES}")
    current = get_criticality(session)
    previous = current.get(project_id)
    if level is None:
        current.pop(project_id, None)
    else:
        current[project_id] = {"level": level, "decided_by": actor_id, "decided_at": utcnow().isoformat(),
                               "rationale": rationale[:500]}
    _put_setting(session, CRITICALITY_KEY, current)
    from aeos.audit import service as audit

    audit.record(session, actor_type=actor_type, actor_id=actor_id, action="ops.criticality.set",
                 target_type="project", target_id=project_id, project_id=project_id,
                 details={"previous": (previous or {}).get("level"), "new": level})
    session.flush()
    return {"project_id": project_id, "criticality": level}


def set_target(
    session: Session, key: str, *, maximum: float | None, minimum: float | None, actor_type: str, actor_id: str,
    rationale: str = "", criticality: str | None = None,
) -> dict[str, Any]:
    """Record an owner decision for an SLI target (audited). ``None`` for both clears the target. With
    ``criticality`` the target applies only to projects the owner assigned that criticality."""
    if key not in SLI_DEFS:
        raise ValidationFailed(f"unknown SLI {key!r}", details={"known": sorted(SLI_DEFS)})
    if criticality is not None:
        if criticality not in CRITICALITIES:
            raise ValidationFailed(f"criticality must be one of {CRITICALITIES}")
        if key not in PER_PROJECT_SLIS:
            raise ValidationFailed(f"{key} is not measured per project; it cannot have a per-criticality target",
                                   details={"per_project": sorted(PER_PROJECT_SLIS)})
        key = f"{key}@{criticality}"
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


def _project_scoped_values(session: Session, d: str, now: datetime, since: datetime,
                           project_ids: list[str]) -> tuple[dict[str, tuple[float | None, dict[str, Any]]],
                                                            list[WorkerRun]]:
    """The PER_PROJECT_SLIS for *project_ids* of domain *d* (all domain projects = the domain value)."""
    values: dict[str, tuple[float | None, dict[str, Any]]] = {}

    # queue age
    queued = list(session.scalars(select(TaskExecution).where(
        TaskExecution.domain == d, TaskExecution.project_id.in_(project_ids),
        TaskExecution.state.in_(["QUEUED", "READY"])))) if project_ids else []
    ages = [a for a in (_age(now, t.updated_at) for t in queued) if a is not None]
    values["queue_age_seconds"] = (max(ages) if ages else 0.0, {"queued_tasks": len(queued)})

    # stale leases (ACTIVE but expired, or no heartbeat for STALE_HEARTBEAT_SECONDS)
    lease_rows = list(session.execute(
        select(Lease, TaskExecution.domain).join(TaskExecution, TaskExecution.task_id == Lease.task_id)
        .where(Lease.state == "ACTIVE", TaskExecution.domain == d, TaskExecution.project_id.in_(project_ids))
    )) if project_ids else []
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
    runs = list(session.scalars(select(WorkerRun).where(WorkerRun.domain == d, WorkerRun.created_at >= since,
                                                        WorkerRun.project_id.in_(project_ids)))) if project_ids else []
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
    return values, runs


def compute(session: Session, *, domain: Domain, settings: Settings | None = None,
            now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    d = domain.value
    since = now - WINDOW
    project_ids = _domain_project_ids(session, d)
    scoped_values, runs = _project_scoped_values(session, d, now, since, project_ids)
    values: dict[str, tuple[float | None, dict[str, Any]]] = dict(scoped_values)

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
    # Per-criticality measurement: only for levels the owner assigned to projects of this domain AND for
    # which the owner recorded a target (no decision -> nothing is invented).
    levels = get_criticality(session)
    by_level: dict[str, list[str]] = {}
    for pid in project_ids:
        level = (levels.get(pid) or {}).get("level")
        if level in CRITICALITIES:
            by_level.setdefault(level, []).append(pid)
    level_values = {
        level: _project_scoped_values(session, d, now, since, pids)[0]
        for level, pids in by_level.items()
        if any(f"{k}@{level}" in targets for k in PER_PROJECT_SLIS)
    }
    items = []
    for key, (title, unit, direction) in SLI_DEFS.items():
        value, detail = values[key]
        status, target = _status(key, value, targets)
        by_criticality: dict[str, Any] = {}
        if key in PER_PROJECT_SLIS:
            for level, lv in level_values.items():
                tkey = f"{key}@{level}"
                if tkey not in targets:
                    continue
                lvalue, ldetail = lv[key]
                lstatus, ltarget = _status(tkey, lvalue, targets)
                by_criticality[level] = {"value": lvalue, "status": lstatus, "target": ltarget,
                                         "projects": len(by_level[level]), "detail": ldetail}
        if by_criticality:
            worst = max([status, *[v["status"] for v in by_criticality.values()]], key=lambda s: _STATUS_RANK[s])
            status = worst
        items.append({
            "key": key, "title": title, "unit": unit, "direction": direction, "value": value,
            "status": status, "target": target, "detail": detail, "by_criticality": by_criticality,
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
        domain_conds.append({"dedup_key": f"{d}:stale_leases", "kind": "stale_leases", "severity": alerts.rule_severity("stale_leases"),
                             "title": f"{int(stale['value'])} lease usang terdeteksi ({d})", "detail": stale["detail"]})
    if result.get("_conflicts"):
        domain_conds.append({"dedup_key": f"{d}:sync_conflict", "kind": "sync_conflict", "severity": alerts.rule_severity("sync_conflict"),
                             "title": f"Konflik revisi pada sinkronisasi eksternal ({d})",
                             "detail": {"conflicts": result["_conflicts"]}})
    integ = by_key["integration_failures"]
    if (integ["value"] or 0) > 0:
        domain_conds.append({"dedup_key": f"{d}:integration_failures", "kind": "integration_failure",
                             "severity": alerts.rule_severity("integration_failure"), "title": f"Integrasi eksternal gagal/kedaluwarsa ({d})",
                             "detail": integ["detail"]})
    recovery = by_key["recovery_point_age_seconds"]
    if recovery["value"] is None:
        platform_conds.append({"dedup_key": "GLOBAL:restore_untested", "kind": "restore_untested", "severity": alerts.rule_severity("restore_untested"),
                               "title": "Restore backup belum pernah diuji - pemulihan belum terbukti",
                               "detail": recovery["detail"], "domain": None})
    for item in result["items"]:
        if item["status"] == "BREACH":
            cond = {"dedup_key": f"{'GLOBAL' if item['scope'] == 'PLATFORM' else d}:sli_breach:{item['key']}",
                    "kind": "sli_breach", "severity": alerts.rule_severity("sli_breach"),
                    "title": f"SLI di luar target pemilik: {item['title']}",
                    "detail": {"value": item["value"], "target": item["target"],
                               "by_criticality": {k: {"value": v["value"], "status": v["status"], "target": v["target"]}
                                                  for k, v in (item.get("by_criticality") or {}).items()}}}
            (platform_conds if item["scope"] == "PLATFORM" else domain_conds).append(
                {**cond, "domain": None} if item["scope"] == "PLATFORM" else cond)
    for b in (result.get("error_budget") or {}).get("items") or []:
        if b["status"] == "EXHAUSTED":
            scope = "GLOBAL" if b["scope"] == "PLATFORM" else d
            cond = {"dedup_key": f"{scope}:error_budget:{b['key']}:{b.get('criticality') or 'ALL'}",
                    "kind": "error_budget_exhausted", "severity": alerts.rule_severity("error_budget_exhausted"),
                    "title": f"Error budget habis: {b['key']}" + (f" ({b['criticality']})" if b.get("criticality") else ""),
                    "detail": {k: b[k] for k in ("bad", "samples", "allowed_bad", "burn_rate", "window_days")}}
            (platform_conds if b["scope"] == "PLATFORM" else domain_conds).append(
                {**cond, "domain": None} if b["scope"] == "PLATFORM" else cond)
    return domain_conds, platform_conds


# ----------------------------------------------------------------------------------------- error budget (§46)


def get_error_budget_policy(session: Session) -> dict[str, Any]:
    return _setting_value(session, ERROR_BUDGET_KEY)


def set_error_budget_policy(session: Session, *, window_days: int, objective: float, action: str, actor_type: str,
                            actor_id: str, rationale: str = "") -> dict[str, Any]:
    """Owner decision (§46 Google SRE): the SLO window, the objective (share of measured evaluations that
    must be within target) and what happens when the error budget is used up."""
    if not 1 <= int(window_days) <= 365:
        raise ValidationFailed("window_days must be between 1 and 365")
    if not 0.0 < float(objective) < 1.0:
        raise ValidationFailed("objective must be a fraction between 0 and 1 (e.g. 0.99)")
    if action not in ERROR_BUDGET_ACTIONS:
        raise ValidationFailed(f"action must be one of {ERROR_BUDGET_ACTIONS}")
    previous = get_error_budget_policy(session) or None
    policy = {"window_days": int(window_days), "objective": float(objective), "action": action,
              "decided_by": actor_id, "decided_at": utcnow().isoformat(), "rationale": rationale[:500]}
    _put_setting(session, ERROR_BUDGET_KEY, policy)
    from aeos.audit import service as audit

    audit.record(session, actor_type=actor_type, actor_id=actor_id, action="ops.error_budget.policy.set",
                 target_type="setting", target_id=ERROR_BUDGET_KEY, details={"previous": previous, "new": policy})
    session.flush()
    return policy


def record_samples(session: Session, result: dict[str, Any], *, now: datetime | None = None) -> int:
    """Persist this evaluation's measured values (the Metric history the error budget is computed from)."""
    from aeos.models.operations_ext import OpsSliSample

    at = now or utcnow()
    n = 0
    targets = get_targets(session)
    for item in result["items"]:
        scope = item["scope"]
        session.add(OpsSliSample(at=at, sli_key=item["key"], scope=scope, criticality=None, value=item["value"],
                                 status=_status(item["key"], item["value"], targets)[0]))
        n += 1
        for level, v in (item.get("by_criticality") or {}).items():
            session.add(OpsSliSample(at=at, sli_key=item["key"], scope=scope, criticality=level, value=v["value"],
                                     status=v["status"]))
            n += 1
    session.flush()
    return n


def error_budget(session: Session, *, domain: Domain, now: datetime | None = None) -> dict[str, Any]:
    """Error-budget accounting per SLI (and criticality) over the owner's SLO window: allowed bad
    evaluations = (1 - objective) x measured evaluations; UNKNOWN/NO_TARGET samples are not counted as good."""
    from aeos.models.operations_ext import OpsSliSample

    policy = get_error_budget_policy(session)
    if not policy:
        return {"policy": None, "status": "NO_POLICY", "items": [],
                "note": "Error budget policy is an owner decision (aeos ops sli budget-policy ...); none recorded."}
    now = now or utcnow()
    since = now - timedelta(days=int(policy["window_days"]))
    objective = float(policy["objective"])
    rows = list(session.scalars(select(OpsSliSample).where(OpsSliSample.at >= since,
                                                           OpsSliSample.scope.in_([domain.value, "PLATFORM"]))))
    groups: dict[tuple[str, str | None, str], list[Any]] = {}
    for r in rows:
        groups.setdefault((r.sli_key, r.criticality, r.scope), []).append(r)
    items = []
    for (key, level, scope), samples in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        measured = [s for s in samples if s.status in ("WITHIN_TARGET", "BREACH")]
        if not measured:
            continue  # no target recorded or never measured: no budget can be claimed
        bad = sum(1 for s in measured if s.status == "BREACH")
        allowed = (1.0 - objective) * len(measured)
        burn = (bad / len(measured)) / (1.0 - objective)
        items.append({"key": key, "criticality": level, "scope": scope, "samples": len(measured), "bad": bad,
                      "unknown": len(samples) - len(measured), "allowed_bad": round(allowed, 4),
                      "budget_remaining": round(1.0 - (bad / allowed), 4) if allowed > 0 else (1.0 if bad == 0 else 0.0),
                      "burn_rate": round(burn, 4), "window_days": policy["window_days"],
                      "status": "EXHAUSTED" if bad > allowed else "OK"})
    exhausted = [i for i in items if i["status"] == "EXHAUSTED"]
    return {"policy": policy, "status": "EXHAUSTED" if exhausted else ("OK" if items else "INSUFFICIENT_DATA"),
            "items": items, "domain": domain.value}


def apply_error_budget_policy(session: Session, budget: dict[str, Any]) -> dict[str, Any]:
    """The owner-approved action when the budget is used up: always an alert (via observed_conditions);
    with action ``pause_scheduling`` new work is paused (reason ERROR_BUDGET_EXHAUSTED) and lifted again
    only when the budget of EVERY domain that caused the pause recovered AND the pause is still the one
    this policy set (an emergency or a human pause is never lifted here). The scheduler pause is global,
    so one domain's recovered budget never lifts a pause another domain's exhausted budget still needs."""
    policy = budget.get("policy") or {}
    out: dict[str, Any] = {"action": policy.get("action"), "changed": None}
    if policy.get("action") != "pause_scheduling":
        return out
    try:
        from aeos.workforce import facade as workforce
    except ImportError as exc:
        out["error"] = f"workforce unavailable ({exc.name or exc}); scheduling not paused"
        return out
    domain = str(budget.get("domain") or "")
    ours = _setting_value(session, ERROR_BUDGET_PAUSE_KEY)
    causes = {str(d): sorted(v) for d, v in (ours.get("by_domain") or {}).items() if isinstance(v, list)}
    if ours.get("paused_at") and not causes:
        causes = {"UNKNOWN": list(ours.get("keys") or [])}  # legacy record: cause unknown -> never lifted by a domain
    if budget.get("status") == "EXHAUSTED":
        keys = sorted({i["key"] for i in budget["items"] if i["status"] == "EXHAUSTED"})
        if not workforce.scheduling_paused(session):
            reason = f"ERROR_BUDGET_EXHAUSTED: {', '.join(keys)}"
            workforce.set_scheduling_paused(session, True, actor="ops.error_budget", reason=reason)
            _put_setting(session, ERROR_BUDGET_PAUSE_KEY, {"paused_at": utcnow().isoformat(), "keys": keys,
                                                           "by_domain": {domain: keys}})
            out["changed"] = "paused"
        elif ours.get("paused_at"):  # our pause is in force: record this domain as a further cause
            causes[domain] = keys
            _put_setting(session, ERROR_BUDGET_PAUSE_KEY, {**ours, "by_domain": causes})
    elif ours and ours.get("paused_at"):
        causes.pop(domain, None)
        if causes:  # another domain's budget is still exhausted: the pause stays
            _put_setting(session, ERROR_BUDGET_PAUSE_KEY, {**ours, "by_domain": causes})
            out["held_by"] = sorted(causes)
        else:
            info = workforce.pause_info(session)
            if info.get("paused") and info.get("by") == "ops.error_budget":
                workforce.set_scheduling_paused(session, False, actor="ops.error_budget",
                                                reason="error budget recovered")
                out["changed"] = "resumed"
            _put_setting(session, ERROR_BUDGET_PAUSE_KEY, {})
    session.flush()
    return out


def evaluate(session: Session, *, domain: Domain, settings: Settings | None = None) -> dict[str, Any]:
    """Compute SLIs, record the samples, account the error budget, apply the owner's budget policy and
    reconcile alerts (raises/resolves; caller commits)."""
    result = compute(session, domain=domain, settings=settings)
    record_samples(session, result)
    result["error_budget"] = error_budget(session, domain=domain)
    result["error_budget_action"] = apply_error_budget_policy(session, result["error_budget"])
    domain_conds, platform_conds = observed_conditions(result)
    a = alerts.reconcile(session, scope_domain=domain.value, observed=domain_conds)
    b = alerts.reconcile(session, scope_domain=None, observed=platform_conds)
    result.pop("_conflicts", None)
    return {**result, "alerts": {"domain": a, "platform": b}}


def public(result: dict[str, Any]) -> dict[str, Any]:
    out = dict(result)
    out.pop("_conflicts", None)
    return out
