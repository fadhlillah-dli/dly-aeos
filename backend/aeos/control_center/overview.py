"""Overview / Readiness projection (§35 "Apakah proyek ini dapat berjalan dan apa yang belum siap?").

Content: domain/project, capability readiness, active approved plan + policy snapshot hash, blockers,
health, cost/quota, pending decisions, event-stream freshness, LIVE/MOCK mode, recovery readiness and
secret health (status only, never values). Each section is computed by code from the owning module
(through its facade) or from canonical records; a missing module makes that section UNKNOWN with
the reason. There is deliberately no single overall green badge (§35 "bukan satu badge hijau untuk
seluruh jaminan") - only per-section statuses and counts.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from aeos.control_center._items import guarded, item, unknown
from aeos.core.config import Settings
from aeos.core.enums import Capability, Domain
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.identity.service import Principal
from aeos.models.audit import Event
from aeos.models.cost import Budget, QuotaState, UsageLedgerEntry
from aeos.models.decisions import PolicySnapshot
from aeos.models.inventory import CheckResult
from aeos.models.planning import Plan, TaskSpec
from aeos.models.portfolio import Project
from aeos.models.workforce import TaskExecution, WorkerRun

MOCK_MODES = ("MOCK", "DEMO")
COST_WINDOW = timedelta(days=30)
CRITICAL_SEVERITIES = ("CRITICAL", "HIGH")
FAILED_CHECK_STATUSES = ("FAILED", "EXPIRED")
NOT_PASSING_CHECK_STATUSES = frozenset({"FAILED", "EXPIRED", "MISSING", "STALE", "UNKNOWN"})
DOMAIN_SECRET_NAMESPACES = {"personal": Domain.PERSONAL.value, "office": Domain.OFFICE.value}


# ----------------------------------------------------------------------------------------- sections


def _capabilities(session: Session, domain: Domain, project: Project | None) -> dict[str, Any]:
    from aeos.doctor import facade as doctor

    rows: list[dict[str, Any]] = []
    times: list[str] = []
    for cap in Capability:
        try:
            st = doctor.capability_state(session, cap, domain=domain.value, project_id=project.id if project else None)
        except ImportError:
            raise
        except Exception as exc:
            rows.append({"capability": cap.value, "status": "UNKNOWN", "reasons": [f"probe error: {type(exc).__name__}"],
                         "checks": [], "remedies": [], "checked_at": None})
            continue
        status = st.status
        reasons = list(st.reasons)
        checked = [str(c["last_checked_at"]) for c in st.checks if c.get("last_checked_at")]
        if status == "READY" and not st.checks:
            # Never READY without the checks that prove it (§08 "dihitung dari code/probes").
            status = "UNKNOWN"
            reasons.append("READY dilaporkan tanpa hasil pemeriksaan pendukung; diperlakukan UNKNOWN")
        elif status == "READY":
            weak = sorted({str(c.get("check_id") or "?") for c in st.checks
                           if str(c.get("status") or "UNKNOWN").upper() in NOT_PASSING_CHECK_STATUSES})
            if weak:
                # A failed/stale/unknown supporting check invalidates READY (§08 invalidation, §50
                # "Config/model/provider berubah setelah doctor -> check terkait kedaluwarsa").
                status = "UNKNOWN"
                reasons.append("READY dilaporkan, tetapi pemeriksaan pendukung belum lulus/usang: "
                               + ", ".join(weak[:10]))
        rows.append({"capability": cap.value, "status": status, "reasons": reasons, "checks": st.checks,
                     "remedies": st.remedies, "checked_at": min(checked) if checked else None})
        times.extend(str(t) for t in checked)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    worst = "BLOCKED" if counts.get("BLOCKED") else "UNKNOWN" if counts.get("UNKNOWN") else \
        "DEGRADED" if counts.get("DEGRADED") else "READY" if counts.get("READY") else "UNKNOWN"
    return item(worst, source="Readiness Doctor (aeos.doctor.facade.capability_state)",
                checked_at=min(times) if times else None,
                reason=f"{counts.get('READY', 0)} siap, {counts.get('BLOCKED', 0)} terblokir, "
                       f"{counts.get('UNKNOWN', 0)} tidak diketahui",
                evidence_limit="Status per capability dari probe; CONNECTED bukan VERIFIED.",
                data={"items": rows, "counts": counts})


def _active_plan(session: Session, project: Project | None) -> dict[str, Any]:
    if project is None:
        return unknown("planning", "Belum memilih proyek; pilih proyek di header untuk melihat rencana aktif.")
    plans = list(session.scalars(select(Plan).where(Plan.project_id == project.id).order_by(Plan.updated_at.desc())))
    if not plans:
        return item("MISSING", source="plans", checked_at=utcnow(), reason="Belum ada rencana untuk proyek ini.",
                    evidence_limit="Tanpa PlanVersion yang disetujui, pekerjaan tidak dapat dijalankan.",
                    data={"plans": 0})
    from aeos.planning import facade as planning

    approved: list[dict[str, Any]] = []
    for plan in plans:
        if not plan.approved_version_id:
            continue
        version = planning.get_approved_version(session, plan.id)  # bound to an ACTIVE decision, never "latest"
        if version is None:
            continue
        approved.append({"plan_id": plan.id, "title": plan.title, "state": plan.state, "version_id": version.id,
                         "version": version.version, "manifest_hash": version.manifest_hash,
                         "approved_at": version.created_at.isoformat() if version.created_at else None})
    pending = [{"plan_id": p.id, "title": p.title, "state": p.state} for p in plans if p.state in
               ("READY_FOR_HUMAN_REVIEW", "NEEDS_DECISION", "AWAITING_MANUAL_CLAUDE")]
    status = "APPROVED" if approved else "MISSING"
    return item(status, source="plans + planning.get_approved_version (approval binding)", checked_at=utcnow(),
                reason="" if approved else "Belum ada versi rencana yang disetujui dan masih berlaku.",
                evidence_limit="Persetujuan terikat hash versi; versi baru membutuhkan persetujuan baru.",
                data={"approved": approved, "awaiting_review": pending, "plans": len(plans)})


def _policy(session: Session) -> dict[str, Any]:
    snap = session.scalar(select(PolicySnapshot).where(PolicySnapshot.active.is_(True))
                          .order_by(PolicySnapshot.created_at.desc()))
    if snap is None:
        return item("MISSING", source="policy_snapshots", checked_at=utcnow(),
                    reason="Belum ada policy snapshot aktif (jalankan `aeos seed` / tinjau kebijakan).",
                    evidence_limit="Tanpa snapshot aktif, gate yang bergantung kebijakan tidak dapat dibuktikan.")
    mci = snap.mci_reference or {}
    return item("CONFIGURED", source="policy_snapshots (aktif)", checked_at=snap.created_at,
                reason=f"MCI: {mci.get('status', 'UNVERIFIED')}",
                evidence_limit="Kesesuaian MCI tetap UNVERIFIED sampai sumber kanonik dapat dibaca (§51).",
                data={"version": snap.version, "content_hash": snap.content_hash, "source": snap.source,
                      "mci_status": mci.get("status", "UNVERIFIED")})


def _latest_checks(session: Session, domain: Domain, project: Project | None) -> list[CheckResult]:
    rows = list(session.scalars(select(CheckResult).order_by(CheckResult.last_checked_at.desc()).limit(2000)))
    latest: dict[str, CheckResult] = {}
    for r in rows:
        scope = r.scope or {}
        if scope.get("domain") and str(scope["domain"]).upper() != domain.value:
            continue
        if scope.get("project_id") and (project is None or scope["project_id"] != project.id):
            continue
        latest.setdefault(r.check_id, r)
    return list(latest.values())


def _blockers(session: Session, principal: Principal, domain: Domain, project: Project | None) -> dict[str, Any]:
    pids = _scoped_project_ids(session, principal, domain)
    tq = select(TaskExecution).where(TaskExecution.domain == domain.value, TaskExecution.state == "BLOCKED",
                                     TaskExecution.project_id.in_(pids))
    if project is not None:
        tq = tq.where(TaskExecution.project_id == project.id)
    tasks = list(session.scalars(tq.limit(50)))
    titles = {t.id: t.title for t in session.scalars(select(TaskSpec).where(TaskSpec.id.in_([x.task_id for x in tasks])))} \
        if tasks else {}
    rq = select(WorkerRun).where(WorkerRun.domain == domain.value, WorkerRun.execution_status == "BLOCKED",
                                 WorkerRun.project_id.in_(pids))
    if project is not None:
        rq = rq.where(WorkerRun.project_id == project.id)
    runs = list(session.scalars(rq.limit(50)))
    checks = _latest_checks(session, domain, project)
    failed = [c for c in checks if c.status in FAILED_CHECK_STATUSES and c.severity in CRITICAL_SEVERITIES]
    entries: list[dict[str, Any]] = []
    entries += [{"kind": "task", "id": t.task_id, "title": titles.get(t.task_id, t.task_id), "reason": t.reason,
                 "since": t.updated_at.isoformat() if t.updated_at else None, "link": "/dag"} for t in tasks]
    entries += [{"kind": "run", "id": r.id, "title": r.role_key or r.id, "reason": r.block_reason,
                 "since": r.updated_at.isoformat() if r.updated_at else None, "link": f"/runs/{r.id}"} for r in runs]
    entries += [{"kind": "check", "id": c.check_id, "title": c.summary or c.check_id, "reason": c.error_code or c.status,
                 "since": c.last_checked_at.isoformat() if c.last_checked_at else None, "link": "/readiness",
                 "remediation": c.remediation} for c in failed]
    try:
        from aeos.workforce import facade as workforce

        if workforce.scheduling_paused(session):
            entries.append({"kind": "scheduler", "id": "scheduling", "title": "Penjadwalan sedang dijeda",
                            "reason": "paused", "since": None, "link": "/work"})
    except ImportError:
        pass
    last_check = max((c.last_checked_at for c in checks if c.last_checked_at), default=None)
    if entries:
        return item("BLOCKED", source="task_executions, worker_runs, check_results", checked_at=utcnow(),
                    reason=f"{len(entries)} blocker tercatat",
                    evidence_limit="Hanya blocker yang tercatat oleh controller/doctor.",
                    data={"items": entries})
    if not checks:
        return item("UNKNOWN", source="task_executions, worker_runs, check_results", checked_at=None,
                    reason="Doctor belum pernah berjalan; ketiadaan blocker belum terbukti. Jalankan: aeos doctor",
                    evidence_limit="Tidak ada hasil pemeriksaan.", data={"items": []})
    return item("NONE_RECORDED", source="task_executions, worker_runs, check_results", checked_at=last_check,
                reason="Tidak ada blocker tercatat pada pemeriksaan terakhir.",
                evidence_limit="Tidak ada blocker tercatat ≠ siap; lihat status capability.", data={"items": []})


def _health(session: Session, settings: Settings, domain: Domain, project: Project | None = None) -> dict[str, Any]:
    from aeos.operations import alerts as ops_alerts
    from aeos.operations import release

    subs: list[dict[str, Any]] = []
    try:
        from aeos.core.migrate import migration_status

        ms = migration_status()
        subs.append({"key": "database", "status": "CONFIGURED", "detail":
                     f"revision {ms.get('current') or 'create_all'} / head {ms.get('head') or 'none'}"})
    except Exception as exc:
        subs.append({"key": "database", "status": "UNKNOWN", "detail": type(exc).__name__})
    try:
        from aeos.workforce import facade as workforce

        subs.append({"key": "scheduler", "status": "PAUSED" if workforce.scheduling_paused(session) else "RUNNING",
                     "detail": "status penjadwal dari modul workforce"})
    except ImportError as exc:
        subs.append({"key": "scheduler", "status": "UNKNOWN", "detail": f"modul belum tersedia: {exc.name or exc}"})
    # Only checks visible in this domain/project scope (an OFFICE check time is not shown to PERSONAL).
    last_doctor = max((c.last_checked_at for c in _latest_checks(session, domain, project) if c.last_checked_at),
                      default=None)
    subs.append({"key": "doctor", "status": "CONFIGURED" if last_doctor else "UNKNOWN",
                 "detail": f"pemeriksaan terakhir {last_doctor.isoformat() if last_doctor else 'belum pernah'}"})
    open_alerts = ops_alerts.list_alerts(session, domain=domain.value, status="OPEN", limit=100)
    subs.append({"key": "alerts", "status": "DEGRADED" if open_alerts else "NONE_RECORDED",
                 "detail": f"{len(open_alerts)} alert terbuka", "items": open_alerts[:5]})
    pointer = release.read_pointer(settings)
    active = pointer.get("active") or {}
    subs.append({"key": "controller_release", "status": "CONFIGURED" if active else "NOT_APPLICABLE",
                 "detail": f"rilis aktif {active.get('release_id')}" if active else
                 "belum ada rilis terpromosi (berjalan dari checkout repo)"})
    statuses = {s["status"] for s in subs}
    worst = "UNKNOWN" if "UNKNOWN" in statuses else "DEGRADED" if statuses & {"DEGRADED", "PAUSED"} else "CONFIGURED"
    return item(worst, source="database, workforce, doctor, ops_alerts, releases", checked_at=utcnow(),
                reason="; ".join(f"{s['key']}={s['status']}" for s in subs),
                evidence_limit="Kesehatan proses lokal; bukan verifikasi fungsi end-to-end.", data={"items": subs})


def _scoped_project_ids(session: Session, principal: Principal, domain: Domain) -> set[str]:
    ids = set(session.scalars(select(Project.id).where(Project.domain == domain.value)))
    if principal.allowed_project_ids is not None:
        ids &= set(principal.allowed_project_ids)
    return ids


def _quota_visible(qs: QuotaState, domain: Domain) -> bool:
    """Provider quotas are platform-level unless their scope/details name a domain (§35 scope)."""
    scope = (qs.scope or "").strip().lower()
    named = DOMAIN_SECRET_NAMESPACES.get(scope.split("/", 1)[0].split(":", 1)[0])
    detail_domain = str((qs.details or {}).get("domain") or "").upper() or None
    return all(d is None or d == domain.value for d in (named, detail_domain))


def _visible_budgets(session: Session, domain: Domain, pids: set[str]) -> int:
    """Budgets the active domain may see: GLOBAL/ROUTE (platform), PROJECT/RUN of its own projects."""
    n = 0
    for b in session.scalars(select(Budget)):
        kind = (b.scope_type or "").upper()
        if kind in ("GLOBAL", "ROUTE"):
            n += 1
        elif kind == "PROJECT":
            n += 1 if b.scope_id in pids else 0
        elif kind == "RUN":
            run = session.get(WorkerRun, b.scope_id)
            n += 1 if run is not None and run.domain == domain.value and run.project_id in pids else 0
    return n


def _cost(session: Session, principal: Principal, domain: Domain, project: Project | None) -> dict[str, Any]:
    since = utcnow() - COST_WINDOW
    pids = _scoped_project_ids(session, principal, domain)
    q = select(UsageLedgerEntry).where(UsageLedgerEntry.domain == domain.value, UsageLedgerEntry.at >= since)
    if project is not None:
        q = q.where(UsageLedgerEntry.project_id == project.id)
    entries = [e for e in session.scalars(q) if e.project_id is None or e.project_id in pids]
    live = [e for e in entries if e.mode not in MOCK_MODES]
    actual = sum(float(e.cost_usd) for e in live if e.cost_usd is not None and not e.is_estimate)
    estimate = sum(float(e.cost_usd) for e in live if e.cost_usd is not None and e.is_estimate)
    unknown_cost = sum(1 for e in live if e.cost_usd is None)
    mock_excluded = len(entries) - len(live)
    quotas = [{"provider": qs.provider, "scope": qs.scope, "state": qs.state, "source": qs.source,
               "observed_at": qs.observed_at.isoformat() if qs.observed_at else None,
               "resets_at": qs.resets_at.isoformat() if qs.resets_at else None}
              for qs in session.scalars(select(QuotaState)) if _quota_visible(qs, domain)]
    if not any(qs["provider"] in ("claude-native", "claude") for qs in quotas):
        quotas.append({"provider": "claude-native", "scope": "Max subscription", "state": "UNKNOWN",
                       "source": "UNKNOWN", "observed_at": None, "resets_at": None,
                       "note": "Kuota Max tidak dikonversi dari token; tidak ada sumber resmi yang terbaca."})
    budgets = _visible_budgets(session, domain, pids)
    reasons: list[str] = []
    try:
        import importlib

        importlib.import_module("aeos.cost.service")
        cost_module = True
    except ImportError:
        cost_module = False
    if not cost_module:
        reasons.append("modul cost belum tersedia: penegakan budget tidak dapat dipercaya")
    if unknown_cost:
        reasons.append(f"{unknown_cost} entri biaya UNKNOWN")
    if any(qs["state"] == "UNKNOWN" for qs in quotas):
        reasons.append("sebagian kuota UNKNOWN")
    if not budgets:
        reasons.append("belum ada budget tercatat")
    status = "UNKNOWN" if reasons else "RECORDED"
    return item(status, source="usage_ledger, quota_states, budgets (30 hari)", checked_at=utcnow(),
                reason="; ".join(reasons),
                evidence_limit="Biaya tunai dari ledger; estimasi dan MOCK dipisah dan tidak dihitung sebagai aktual.",
                data={"window_days": COST_WINDOW.days, "actual_usd": round(actual, 6), "estimated_usd": round(estimate, 6),
                      "unknown_cost_entries": unknown_cost, "mock_entries_excluded": mock_excluded,
                      "entries": len(live), "budgets": budgets, "quotas": quotas})


def _decisions(session: Session, principal: Principal, domain: Domain, project: Project | None) -> dict[str, Any]:
    import importlib

    queue = importlib.import_module("aeos.policy.queue")  # contract: queue_items(session, domain, project_ids=None)
    pids = _scoped_project_ids(session, principal, domain)
    raw = queue.queue_items(session, domain.value, project_ids=[project.id] if project else sorted(pids))
    items: list[dict[str, Any]] = []
    for r in raw or []:
        d = r if isinstance(r, dict) else {k: getattr(r, k) for k in dir(r) if not k.startswith("_")
                                           and not callable(getattr(r, k))}
        dom = d.get("domain")
        if dom is not None and str(dom).upper() != domain.value:
            continue
        pid = d.get("project_id")
        if pid and pid not in pids:  # another domain's project, or outside the user's project scope
            continue
        items.append({k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in d.items()
                      if isinstance(v, (str, int, float, bool, type(None))) or hasattr(v, "isoformat")})
    return item("PENDING" if items else "NONE_RECORDED", source="Human Decision Queue (aeos.policy.queue)",
                checked_at=utcnow(), reason=f"{len(items)} keputusan menunggu",
                evidence_limit="Keputusan hanya oleh manusia; output model tidak pernah menjadi persetujuan.",
                data={"count": len(items), "top": items[:5]})


def _stream(session: Session, domain: Domain) -> dict[str, Any]:
    last = session.scalar(select(Event).where(or_(Event.domain == domain.value, Event.domain.is_(None)))
                          .order_by(Event.seq.desc()).limit(1))
    if last is None:
        return item("UNKNOWN", source="events", checked_at=None, reason="Belum ada event tercatat.",
                    evidence_limit="Kesegaran stream di browser ditampilkan terpisah (live/STALE).",
                    data={"latest_seq": 0, "last_event_at": None})
    return item("RECORDED", source="events (persisted, cursor seq)", checked_at=last.at,
                reason=f"event terakhir {last.kind}",
                evidence_limit="Event terakhir bukan bukti proses sedang berjalan; heartbeat ≠ kemajuan.",
                data={"latest_seq": last.seq, "last_event_at": last.at.isoformat(), "last_kind": last.kind})


def _secret_visible(ref: str, scope: dict[str, Any] | None, domain: Domain, project_ids: set[str],
                    project_keys: set[str]) -> bool:
    """Secret *references* are metadata too: an ``office/*`` ref never appears in a PERSONAL view (and
    vice versa); ``projects/<id|key>/*`` only for projects visible in this scope (§35, §50)."""
    ns, _, rest = (ref or "").partition("/")
    named = DOMAIN_SECRET_NAMESPACES.get(ns)
    if named is not None and named != domain.value:
        return False
    if ns == "projects":
        owner = rest.split("/", 1)[0]
        if owner not in project_ids and owner not in project_keys:
            return False
    scope_domain = str((scope or {}).get("domain") or "").upper()
    return not (scope_domain and scope_domain != domain.value)


def _secrets(session: Session, principal: Principal, domain: Domain) -> dict[str, Any]:
    from aeos.secrets import facade as secrets

    pids = _scoped_project_ids(session, principal, domain)
    keys = set(session.scalars(select(Project.project_key).where(Project.id.in_(pids)))) if pids else set()
    statuses = [s for s in secrets.list_status()
                if _secret_visible(s.ref, getattr(s, "scope", None), domain, pids, keys)]
    now = utcnow()
    soon = [s.ref for s in statuses if s.expires_at is not None and ensure_aware(s.expires_at) is not None
            and ensure_aware(s.expires_at) <= now + timedelta(days=7)]  # type: ignore[operator]
    rows = [{"ref": s.ref, "exists": s.exists, "backend": s.backend,
             "updated_at": s.updated_at.isoformat() if s.updated_at else None,
             "expires_at": s.expires_at.isoformat() if s.expires_at else None} for s in statuses]
    missing = [r["ref"] for r in rows if not r["exists"]]
    status = "DEGRADED" if missing or soon else ("CONFIGURED" if rows else "UNKNOWN")
    return item(status, source="secret store status (aeos.secrets.facade.list_status)", checked_at=now,
                reason=f"{len(rows)} referensi, {len(missing)} belum ada, {len(soon)} segera kedaluwarsa",
                evidence_limit="Hanya status/scope/expiry; nilai secret tidak pernah ditampilkan.",
                data={"items": rows, "missing": missing, "expiring_soon": soon})


def _recovery(settings: Settings, domain: Domain) -> dict[str, Any]:
    from aeos.operations import backup

    items = backup.list_backups(settings, domain=domain)
    ready = next((b for b in items if b.get("ready")), None)
    complete = [b for b in items if b.get("readiness") != backup.READINESS_INCOMPLETE]
    if ready:
        return item("VERIFIED", source="backups + RESTORE_TEST evidence", checked_at=(ready.get("last_restore_test") or {}).get("finished_at"),
                    reason=f"backup {ready['backup_id']} lulus restore test",
                    evidence_limit="Restore diuji di direktori sementara terisolasi (E2), bukan pemulihan produksi.",
                    data={"backup_id": ready["backup_id"], "evidence_id": (ready.get("last_restore_test") or {}).get("evidence_id"),
                          "backups": len(complete)})
    return item("NOT-PROVEN", source="backups", checked_at=complete[0]["created_at"] if complete else None,
                reason="Restore backup belum pernah diuji - pemulihan belum terbukti." if complete else
                "Belum ada backup. Jalankan: aeos ops backup create lalu aeos ops backup restore-test <id>",
                evidence_limit="Backup tidak dianggap siap sebelum restore test berhasil (§33).",
                data={"backups": len(complete), "latest": complete[0]["backup_id"] if complete else None})


def _emergency(session: Session) -> dict[str, Any]:
    from aeos.operations import emergency

    last = emergency.last_emergency(session)
    if not last:
        return item("NONE_RECORDED", source="system_settings ops.emergency.last", checked_at=utcnow(),
                    reason="Belum pernah ada emergency stop.", data=None)
    return item("RECORDED", source="system_settings ops.emergency.last", checked_at=last.get("at"),
                reason=f"emergency stop terakhir: {last.get('status')}", data=last)


# ----------------------------------------------------------------------------------------- assembly


def _mode(session: Session, domain: Domain, project: Project | None) -> dict[str, Any]:
    proj_mode = project.mode if project is not None else None
    counts: dict[str, int] = {}
    for model, col in ((Event, Event.mode), (WorkerRun, WorkerRun.mode), (UsageLedgerEntry, UsageLedgerEntry.mode)):
        q = select(func.count()).select_from(model).where(col.in_(MOCK_MODES), model.domain == domain.value)  # type: ignore[attr-defined]
        counts[model.__tablename__] = int(session.scalar(q) or 0)  # type: ignore[attr-defined]
    contains_mock = any(counts.values())
    mode = proj_mode if proj_mode in MOCK_MODES else ("MIXED" if contains_mock else "LIVE")
    return {"mode": mode, "project_mode": proj_mode, "contains_mock": contains_mock, "mock_rows": counts}


def build_overview(session: Session, settings: Settings, principal: Principal, domain: Domain,
                   project: Project | None) -> dict[str, Any]:
    sections = {
        "capabilities": guarded("doctor", lambda: _capabilities(session, domain, project)),
        "active_plan": guarded("planning", lambda: _active_plan(session, project)),
        "policy": guarded("policy", lambda: _policy(session)),
        "blockers": guarded("blockers", lambda: _blockers(session, principal, domain, project)),
        "health": guarded("health", lambda: _health(session, settings, domain, project)),
        "cost": guarded("cost", lambda: _cost(session, principal, domain, project)),
        "decisions": guarded("policy.queue", lambda: _decisions(session, principal, domain, project)),
        "stream": guarded("events", lambda: _stream(session, domain)),
        "secrets": guarded("secrets", lambda: _secrets(session, principal, domain)),
        "recovery": guarded("operations.backup", lambda: _recovery(settings, domain)),
        "emergency": guarded("operations.emergency", lambda: _emergency(session)),
    }
    counts: dict[str, int] = {}
    for s in sections.values():
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    project_view = None
    if project is not None:
        project_view = {"id": project.id, "name": project.name, "project_key": project.project_key,
                        "domain": project.domain, "is_platform": project.is_platform, "mode": project.mode,
                        "lifecycle": project.lifecycle}
    else:
        n = len(_scoped_project_ids(session, principal, domain))
        sections["project_hint"] = item(
            "UNKNOWN", source="projects", checked_at=utcnow(),
            reason=("Pilih proyek di header." if n else
                    "Belum ada proyek di domain ini. Jalankan `aeos seed` (mendaftarkan proyek DLY AEOS) "
                    "atau buat proyek baru di Portofolio."),
            data={"projects_in_domain": n})
    return {
        "generated_at": utcnow().isoformat(),
        "domain": domain.value,
        "environment": settings.environment,  # §35 header: domain, project, environment, freshness, mode
        "dev_mode": settings.dev_mode,
        "project": project_view,
        **_mode(session, domain, project),
        "sections": sections,
        "status_counts": counts,
        "source": "Control Center projection (doctor, planning, policy, workforce, cost, events, operations)",
        "evidence_limit": "Proyeksi dihitung kode dari record; UNKNOWN/NOT-PROVEN tidak pernah dianggap lulus.",
    }
