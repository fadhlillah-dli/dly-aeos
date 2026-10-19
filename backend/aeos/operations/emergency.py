"""Emergency stop / incident containment (§33 incident response, §35 operator controls).

"Incident response menghentikan capability berisiko, menjaga bukti, menentukan owner dan blast
radius ..." The stop runs fixed steps, each committed and reported separately so a failure in one
step never hides the outcome of the others and nothing is claimed done that was not done:

1. pause scheduling                  (workforce facade, verified by reading the pause state back)
2. revoke every active capability     (broker facade, verified by counting remaining active grants)
3. cancel RUNNING/LEASED worker runs  (workforce facade per run, verified by re-querying)
4. preserve evidence                  (verification facade: one INCIDENT_CONTAINMENT artifact per
                                       domain partition; nothing deleted)
5. audit + alert                      (always available)

A missing module makes its step FAILED with the reason. Cancel does not undo external side effects
that already happened (§35); those need reconciliation.

The stop itself is platform-wide (an Office run is cancelled even when the operator's session is
PERSONAL), but what is *shown* is domain-scoped (§35, §50 "termasuk counts"): ``scope_report``
keeps every step status (a failure anywhere is never hidden as a pass) while counts and run ids of
other domains are removed. Platform-level records (audit row, alert, ``ops.emergency.last``) carry
statuses only; per-domain counts live in that domain's evidence artifact. The CLI (local operator)
sees the full report.

API callers need a single-use confirmation token (``prepare``) bound to their account and valid
for a short time; the CLI asks the local operator to type a confirmation instead.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import importlib
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.enums import Domain
from aeos.core.errors import Forbidden, ValidationFailed
from aeos.core.ids import new_id, new_token
from aeos.core.redaction import redact_text
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.events import service as events
from aeos.models.identity import SystemSetting
from aeos.models.routing import CapabilityGrant
from aeos.models.workforce import WorkerRun

PENDING_KEY = "ops.emergency.pending"
LAST_KEY = "ops.emergency.last"
TOKEN_TTL = timedelta(minutes=3)
ACTIVE_RUN_STATES = ("RUNNING", "LEASED")
DOMAINS = tuple(d.value for d in Domain)
STATUS_ONLY = "__status_only__"  # scope_report view for a session without a selected domain
OTHER_DOMAINS_NOTE = ("Langkah ini juga mencakup domain lain; jumlah dan ID-nya hanya terlihat di sesi domain "
                      "tersebut atau lewat CLI lokal.")

STEP_DESCRIPTIONS = [
    {"step": "pause_scheduling", "description": "Jeda penjadwalan: tidak ada pekerjaan baru yang dimulai."},
    {"step": "revoke_capabilities", "description": "Cabut semua capability grant aktif (akses model/git/PR)."},
    {"step": "cancel_runs", "description": "Batalkan semua run RUNNING/LEASED (hentikan proses, cabut akses)."},
    {"step": "preserve_evidence", "description": "Simpan bukti status saat penghentian per domain; tidak ada data yang dihapus."},
    {"step": "audit", "description": "Catat audit dan alert insiden."},
]


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def prepare(session: Session, *, user_id: str) -> dict[str, Any]:
    token = new_token(24)
    expires = utcnow() + TOKEN_TTL
    value = {"token_hash": _hash(token), "user_id": user_id, "expires_at": expires.isoformat()}
    row = session.get(SystemSetting, PENDING_KEY)
    if row is None:
        session.add(SystemSetting(key=PENDING_KEY, value=value))
    else:
        row.value = value
    session.flush()
    return {"token": token, "expires_at": expires.isoformat(), "steps": STEP_DESCRIPTIONS,
            "notice": "Token sekali pakai, berlaku 3 menit, terikat pada akun Anda."}


def consume_token(session: Session, *, user_id: str, token: str) -> None:
    row = session.get(SystemSetting, PENDING_KEY)
    pending = row.value if row is not None and isinstance(row.value, dict) else None
    if not pending:
        raise Forbidden("no pending emergency-stop confirmation; request a new token", code="CONFIRMATION_REQUIRED")
    try:
        expires_at = ensure_aware(datetime.fromisoformat(str(pending.get("expires_at"))))
        expired = expires_at is None or expires_at <= utcnow()
    except ValueError:
        expired = True
    ok = (not expired and pending.get("user_id") == user_id
          and hmac.compare_digest(str(pending.get("token_hash")), _hash(token or "")))
    if not ok:
        raise Forbidden("emergency-stop confirmation token is invalid or expired", code="CONFIRMATION_INVALID")
    assert row is not None
    row.value = None  # single use
    session.flush()


def _module_available(name: str) -> bool:
    try:
        importlib.import_module(name)
    except ImportError:
        return False
    return True


def _active_runs(session: Session, domain: str | None = None) -> list[WorkerRun]:
    q = select(WorkerRun).where(WorkerRun.execution_status.in_(ACTIVE_RUN_STATES))
    if domain is not None:
        q = q.where(WorkerRun.domain == domain)
    return list(session.scalars(q))


def _active_grants(session: Session, domain: str | None = None) -> int:
    q = select(func.count()).select_from(CapabilityGrant).where(
        CapabilityGrant.active.is_(True), CapabilityGrant.revoked_at.is_(None))
    if domain is not None:
        q = q.where(CapabilityGrant.domain == domain)
    return int(session.scalar(q) or 0)


# ----------------------------------------------------------------------------------------- scoping


def scope_report(report: dict[str, Any], domain: str | None) -> dict[str, Any]:
    """Domain-scoped copy of an emergency-stop report (``domain=None`` = full local-operator view).

    Step statuses stay global; counts/run ids are reduced to *domain*; a failure that happened only in
    another domain keeps its FAILED/PARTIAL status with a neutral reason instead of its details."""
    if domain is None:
        return copy.deepcopy(report)
    out = {k: copy.deepcopy(v) for k, v in report.items() if k not in ("steps", "derived_blast_radius")}
    if domain == STATUS_ONLY:
        out["steps"] = _statuses_only(report.get("steps") or [])
        out["scope"] = "NO_DOMAIN_SELECTED"
        return out
    if isinstance(report.get("derived_blast_radius"), dict):
        out["derived_blast_radius"] = copy.deepcopy(report["derived_blast_radius"].get(domain) or {})
    steps: list[dict[str, Any]] = []
    for step in report.get("steps") or []:
        by_domain = step.get("by_domain")
        if not isinstance(by_domain, dict):
            steps.append(copy.deepcopy(step))
            continue
        mine = copy.deepcopy(by_domain.get(domain) or {})
        entry: dict[str, Any] = {"step": step["step"], "status": step["status"], "scope": domain, **mine,
                                 "other_domains": OTHER_DOMAINS_NOTE}
        if step.get("detail"):
            entry["detail"] = step["detail"]
        if step["status"] != "SUCCEEDED":
            if step.get("cause"):  # domain-neutral cause, e.g. a module that is not installed
                entry["reason"] = step["cause"]
            elif mine.get("failed") or mine.get("remaining_active"):
                entry["reason"] = (f"Tidak tuntas di domain ini: {mine.get('remaining_active', 0)} masih aktif"
                                   + (f", {mine['failed']} gagal dibatalkan" if mine.get("failed") else ""))
            else:
                entry["reason"] = ("Domain ini tuntas, tetapi langkah tidak tuntas di domain lain; buka sesi "
                                   "domain tersebut atau laporan CLI.")
        steps.append(entry)
    out["steps"] = steps
    out["scope"] = domain
    return out


def _statuses_only(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Platform-level summary: step + status (+ module-unavailable reasons), no per-domain numbers."""
    out = []
    for s in steps:
        entry = {"step": s["step"], "status": s["status"]}
        if s.get("cause"):
            entry["reason"] = s["cause"]
        elif s.get("by_domain") is None and s.get("reason"):
            entry["reason"] = s["reason"]
        out.append(entry)
    return out


# ----------------------------------------------------------------------------------------- execute


def execute(
    session: Session,
    *,
    actor_type: str,
    actor_id: str,
    reason: str,
    incident_owner: str = "",
    blast_radius: str = "",
    visible_domain: str | None = None,
) -> dict[str, Any]:
    """Run the containment steps. Commits after each step (documented) so partial progress persists.

    Returns the report scoped to *visible_domain* (``None`` = full view for the local CLI)."""
    if len((reason or "").strip()) < 5:
        raise ValidationFailed("a reason (at least 5 characters) is required for an emergency stop")
    reason = redact_text(reason.strip())[:1000]
    actor = f"{actor_type}:{actor_id}"
    # §33 "menentukan owner": never empty - the declared owner, else the human who ran the stop.
    incident_owner = redact_text((incident_owner or "").strip())[:200] or actor[:200]
    blast_radius = redact_text(blast_radius or "")[:1000]
    incident_id = new_id()
    started = utcnow()
    stop_reason = f"emergency stop {incident_id}: {reason}"[:500]
    steps: list[dict[str, Any]] = []
    # Snapshot what is affected BEFORE containment changes it (derived blast radius, §33).
    pre_runs = [{"id": r.id, "project_id": r.project_id, "domain": r.domain, "engine": r.engine,
                 "model_route_key": r.model_route_key} for r in _active_runs(session)]
    pre_grants = [{"kind": g.kind, "project_id": g.project_id, "domain": g.domain}
                  for g in session.scalars(select(CapabilityGrant).where(CapabilityGrant.active.is_(True),
                                                                         CapabilityGrant.revoked_at.is_(None)))]
    try:
        from aeos.operations import incident as incidents

        derived = incidents.derive_blast_radius(session, pre_runs, pre_grants)
    except Exception as exc:  # derivation is best effort; containment never waits for it
        derived = {d: {"error": f"blast radius not derived: {type(exc).__name__}"} for d in DOMAINS}

    def run_step(name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            result = fn()
            status = result.pop("_status", "SUCCEEDED")
            session.commit()
            entry = {"step": name, "status": status, **result}
        except ImportError as exc:
            session.rollback()
            entry = {"step": name, "status": "FAILED", "reason": f"module unavailable: {exc.name or exc}"}
        except Exception as exc:
            session.rollback()
            entry = {"step": name, "status": "FAILED",
                     "reason": f"{type(exc).__name__}: {redact_text(str(exc))[:300]}"}
        steps.append(entry)
        return entry

    def pause() -> dict[str, Any]:
        from aeos.workforce import facade as workforce

        workforce.set_scheduling_paused(session, True, actor=actor, reason=stop_reason)
        session.flush()
        if not workforce.scheduling_paused(session):
            return {"_status": "FAILED", "reason": "pause requested but scheduler still reports not paused"}
        return {"detail": "scheduling paused (verified by reading the pause state back)"}

    def revoke() -> dict[str, Any]:
        from aeos.broker import facade as broker

        before = {d: _active_grants(session, d) for d in DOMAINS}
        revoked = broker.revoke_grants(session, all_active=True, reason=stop_reason)
        session.flush()
        after = {d: _active_grants(session, d) for d in DOMAINS}
        remaining_total = _active_grants(session)
        by_domain = {d: {"active_before": before[d], "remaining_active": after[d]} for d in DOMAINS}
        status = "SUCCEEDED" if remaining_total == 0 else "PARTIAL"
        result: dict[str, Any] = {"_status": status, "by_domain": by_domain, "revoked_total": int(revoked or 0),
                                  "remaining_total": remaining_total}
        if remaining_total:
            result["reason"] = f"{remaining_total} capability grant(s) still active after revocation"
        return result

    def cancel() -> dict[str, Any]:
        runs = _active_runs(session)
        if not _module_available("aeos.workforce.scheduler"):
            untouched = {d: {"active_runs": sum(1 for r in runs if r.domain == d),
                             "remaining_active": sum(1 for r in runs if r.domain == d), "cancelled": 0, "failed": 0,
                             "runs": []} for d in DOMAINS}
            return {"_status": "FAILED", "by_domain": untouched,
                    "cause": "module unavailable: aeos.workforce.scheduler; active runs could not be cancelled",
                    "reason": f"workforce scheduler unavailable; {len(runs)} active run(s) could not be cancelled"}
        from aeos.workforce import facade as workforce

        results: list[dict[str, Any]] = []
        for run in runs:
            try:
                workforce.cancel_run(session, run.id, reason=stop_reason, actor=actor)
                session.flush()
                results.append({"run_id": run.id, "domain": run.domain, "status": "CANCELLED"})
            except Exception as exc:
                results.append({"run_id": run.id, "domain": run.domain, "status": "FAILED",
                                "reason": f"{type(exc).__name__}: {redact_text(str(exc))[:200]}"})
        by_domain: dict[str, dict[str, Any]] = {}
        for d in DOMAINS:
            mine = [r for r in results if r["domain"] == d]
            failed = [r for r in mine if r["status"] != "CANCELLED"]
            by_domain[d] = {"active_runs": len(mine), "cancelled": len(mine) - len(failed), "failed": len(failed),
                            "remaining_active": len(_active_runs(session, d)),
                            "runs": [{k: v for k, v in r.items() if k != "domain"} for r in mine]}
        remaining = len(_active_runs(session))
        failed_all = [r for r in results if r["status"] != "CANCELLED"]
        status = "SUCCEEDED" if not failed_all and remaining == 0 else (
            "FAILED" if results and len(failed_all) == len(results) else "PARTIAL")
        out: dict[str, Any] = {"_status": status, "by_domain": by_domain, "remaining_total": remaining}
        if status != "SUCCEEDED":
            out["reason"] = f"{len(failed_all)} run(s) could not be cancelled; {remaining} still active"
        return out

    run_step("pause_scheduling", pause)
    run_step("revoke_capabilities", revoke)
    run_step("cancel_runs", cancel)

    def preserve() -> dict[str, Any]:
        from aeos.verification import facade as verification

        evidence_ids: dict[str, str] = {}
        partial = {"incident_id": incident_id, "status": "IN_PROGRESS", "steps": steps}
        for d in DOMAINS:
            scoped = scope_report(partial, d)
            content = {"incident_id": incident_id, "started_at": started.isoformat(), "reason": reason,
                       "incident_owner": incident_owner, "blast_radius": blast_radius,
                       "derived_blast_radius": derived.get(d) or {}, "domain": d,
                       "steps": scoped["steps"], "active_runs_after": len(_active_runs(session, d)),
                       "active_grants_after": _active_grants(session, d)}
            ev = verification.collect_evidence(
                session, kind="INCIDENT_CONTAINMENT", origin="collector",
                collector_identity="aeos.operations.emergency",
                claim=f"Emergency stop {incident_id}: observed step results for domain {d}",
                content=content, evidence_level="E3", subject_type="incident", subject_id=incident_id,
                domain=d, classification="INTERNAL", mode="LIVE",
            )
            evidence_ids[d] = ev.id
        return {"by_domain": {d: {"evidence_id": evidence_ids[d]} for d in DOMAINS},
                "detail": "state snapshot preserved per domain; no data deleted"}

    run_step("preserve_evidence", preserve)

    def overall() -> str:
        statuses = [s["status"] for s in steps]
        return "COMPLETED" if all(s == "SUCCEEDED" for s in statuses) else (
            "FAILED" if all(s == "FAILED" for s in statuses) else "PARTIAL")

    status_before_audit = overall()
    finished = utcnow()

    def record() -> dict[str, Any]:
        from aeos.operations import alerts

        summary = _statuses_only(steps)
        audit.record(session, actor_type=actor_type, actor_id=actor_id, action="ops.emergency_stop",
                     result="SUCCESS" if status_before_audit == "COMPLETED" else "FAILED", target_type="incident",
                     target_id=incident_id, reason=reason,
                     details={"status": status_before_audit, "steps": summary, "incident_owner": incident_owner,
                              "blast_radius": blast_radius})
        events.emit(session, source="operations", kind="ops.emergency_stop", entity_type="incident",
                    entity_id=incident_id, payload={"status": status_before_audit})
        alerts.raise_alert(session, dedup_key=f"GLOBAL:emergency_stop:{incident_id}", kind="emergency_stop",
                           severity="CRITICAL", title="Emergency stop dijalankan", source="emergency",
                           detail={"incident_id": incident_id, "status": status_before_audit, "reason": reason},
                           domain=None, auto_resolve=False)
        last = {"incident_id": incident_id, "at": finished.isoformat(), "by": actor, "status": status_before_audit,
                "reason": reason, "steps": summary}
        row = session.get(SystemSetting, LAST_KEY)
        if row is None:
            session.add(SystemSetting(key=LAST_KEY, value=last))
        else:
            row.value = last
        detail = "audited and alert raised"
        try:  # §28/§33 incident record: timeline, evidence, analysis and corrective action continue there
            from aeos.operations import incident as incidents

            preserve_step = next((s for s in steps if s["step"] == "preserve_evidence"), {})
            evidence_ids = [v.get("evidence_id") for v in (preserve_step.get("by_domain") or {}).values()
                            if isinstance(v, dict) and v.get("evidence_id")]
            with session.begin_nested():  # a failure here never loses the audit row above
                inc = incidents.open_incident(
                    session, title=f"Emergency stop: {reason}"[:256], actor=actor, owner=incident_owner,
                    source="emergency_stop", source_ref=incident_id,
                    status="CONTAINED" if status_before_audit == "COMPLETED" else "OPEN",
                    declared_blast_radius=blast_radius, derived_blast_radius=derived, evidence_ids=evidence_ids,
                    note=f"containment {status_before_audit}: "
                         + ", ".join(f"{s['step']}={s['status']}" for s in steps))
            record_ids["incident_record_id"] = inc.id
        except Exception as exc:
            detail += f"; incident record not created ({type(exc).__name__})"
        return {"detail": detail}

    record_ids: dict[str, str] = {}
    run_step("audit", record)
    report = {
        "incident_id": incident_id, "status": overall(), "started_at": started.isoformat(),
        "finished_at": finished.isoformat(), "reason": reason, "incident_owner": incident_owner,
        "blast_radius": blast_radius, "derived_blast_radius": derived, "steps": steps,
        "incident_record_id": record_ids.get("incident_record_id"),
        "notice": "Cancel stops processes and revokes access; it does not undo external side effects that already "
                  "happened (reconcile publications/branches separately). Resume scheduling explicitly when safe.",
    }
    return scope_report(report, visible_domain)


def last_emergency(session: Session) -> dict[str, Any] | None:
    row = session.get(SystemSetting, LAST_KEY)
    return row.value if row is not None and isinstance(row.value, dict) else None
