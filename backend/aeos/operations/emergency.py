"""Emergency stop / incident containment (§33 incident response, §35 operator controls).

"Incident response menghentikan capability berisiko, menjaga bukti, menentukan owner dan blast
radius ..." The stop runs fixed steps, each committed and reported separately so a failure in one
step never hides the outcome of the others and nothing is claimed done that was not done:

1. pause scheduling                  (workforce facade, verified by reading the pause state back)
2. revoke every active capability     (broker facade, verified by counting remaining active grants)
3. cancel RUNNING/LEASED worker runs  (workforce facade per run, verified by re-querying)
4. preserve evidence                  (verification facade: INCIDENT_CONTAINMENT artifact; nothing deleted)
5. audit + alert                      (always available)

A missing module makes its step FAILED with the reason. Cancel does not undo external side effects
that already happened (§35); those need reconciliation.

API callers need a single-use confirmation token (``prepare``) bound to their account and valid
for a short time; the CLI asks the local operator to type a confirmation instead.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
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

STEP_DESCRIPTIONS = [
    {"step": "pause_scheduling", "description": "Jeda penjadwalan: tidak ada pekerjaan baru yang dimulai."},
    {"step": "revoke_capabilities", "description": "Cabut semua capability grant aktif (akses model/git/PR)."},
    {"step": "cancel_runs", "description": "Batalkan semua run RUNNING/LEASED (hentikan proses, cabut akses)."},
    {"step": "preserve_evidence", "description": "Simpan bukti status saat penghentian; tidak ada data yang dihapus."},
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
    ok =(not expired and pending.get("user_id") == user_id
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


def _active_runs(session: Session) -> list[WorkerRun]:
    return list(session.scalars(select(WorkerRun).where(WorkerRun.execution_status.in_(ACTIVE_RUN_STATES))))


def _active_grants(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(CapabilityGrant).where(
        CapabilityGrant.active.is_(True), CapabilityGrant.revoked_at.is_(None))) or 0)


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
    """Run the containment steps. Commits after each step (documented) so partial progress persists."""
    if len((reason or "").strip()) < 5:
        raise ValidationFailed("a reason (at least 5 characters) is required for an emergency stop")
    incident_id = new_id()
    started = utcnow()
    actor = f"{actor_type}:{actor_id}"
    stop_reason = f"emergency stop {incident_id}: {reason}"[:500]
    steps: list[dict[str, Any]] = []

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

        before = _active_grants(session)
        revoked = broker.revoke_grants(session, all_active=True, reason=stop_reason)
        session.flush()
        remaining = _active_grants(session)
        status = "SUCCEEDED" if remaining == 0 else "PARTIAL"
        return {"_status": status, "active_before": before, "revoked": int(revoked or 0), "remaining_active": remaining}

    def cancel() -> dict[str, Any]:
        runs = _active_runs(session)
        if not _module_available("aeos.workforce.scheduler"):
            return {"_status": "FAILED", "active_runs": len(runs),
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
        remaining = len(_active_runs(session))
        failed = [r for r in results if r["status"] != "CANCELLED"]
        status = "SUCCEEDED" if not failed and remaining == 0 else ("FAILED" if len(failed) == len(results) and results
                                                                     else "PARTIAL")
        visible = [r for r in results if visible_domain is None or r["domain"] == visible_domain]
        return {"_status": status, "active_runs": len(runs), "cancelled": len(results) - len(failed),
                "failed": len(failed), "remaining_active": remaining,
                "runs": [{k: v for k, v in r.items() if k != "domain"} for r in visible],
                "hidden_other_domain": len(results) - len(visible)}

    run_step("pause_scheduling", pause)
    run_step("revoke_capabilities", revoke)
    run_step("cancel_runs", cancel)

    snapshot = {"incident_id": incident_id, "started_at": started.isoformat(), "reason": reason,
                "incident_owner": incident_owner, "blast_radius": blast_radius,
                "steps": [{k: v for k, v in s.items() if k != "runs"} for s in steps],
                "active_runs_after": len(_active_runs(session)), "active_grants_after": _active_grants(session)}

    def preserve() -> dict[str, Any]:
        from aeos.verification import facade as verification

        ev = verification.collect_evidence(
            session, kind="INCIDENT_CONTAINMENT", origin="collector",
            collector_identity="aeos.operations.emergency", claim=f"Emergency stop {incident_id} step results",
            content=snapshot, evidence_level="E3", subject_type="incident", subject_id=incident_id,
            classification="INTERNAL", mode="LIVE",
        )
        return {"evidence_id": ev.id, "detail": "state snapshot preserved; no data deleted"}

    run_step("preserve_evidence", preserve)

    statuses = [s["status"] for s in steps]
    overall = "COMPLETED" if all(s == "SUCCEEDED" for s in statuses) else (
        "FAILED" if all(s == "FAILED" for s in statuses) else "PARTIAL")
    finished = utcnow()
    report = {
        "incident_id": incident_id, "status": overall, "started_at": started.isoformat(),
        "finished_at": finished.isoformat(), "reason": reason, "incident_owner": incident_owner,
        "blast_radius": blast_radius, "steps": steps,
        "notice": "Cancel stops processes and revokes access; it does not undo external side effects that already "
                  "happened (reconcile publications/branches separately). Resume scheduling explicitly when safe.",
    }

    def record() -> dict[str, Any]:
        from aeos.operations import alerts

        audit.record(session, actor_type=actor_type, actor_id=actor_id, action="ops.emergency_stop",
                     result="SUCCESS" if overall == "COMPLETED" else "FAILED", target_type="incident",
                     target_id=incident_id, reason=reason,
                     details={"status": overall, "steps": snapshot["steps"], "incident_owner": incident_owner,
                              "blast_radius": blast_radius})
        events.emit(session, source="operations", kind="ops.emergency_stop", entity_type="incident",
                    entity_id=incident_id, payload={"status": overall})
        alerts.raise_alert(session, dedup_key=f"GLOBAL:emergency_stop:{incident_id}", kind="emergency_stop",
                           severity="CRITICAL", title="Emergency stop dijalankan", source="emergency",
                           detail={"incident_id": incident_id, "status": overall, "reason": reason},
                           domain=None, auto_resolve=False)
        last = {"incident_id": incident_id, "at": finished.isoformat(), "by": actor, "status": overall,
                "reason": reason}
        row = session.get(SystemSetting, LAST_KEY)
        if row is None:
            session.add(SystemSetting(key=LAST_KEY, value=last))
        else:
            row.value = last
        return {"detail": "audited and alert raised"}

    run_step("audit", record)
    report["steps"] = steps
    statuses = [s["status"] for s in steps]
    report["status"] = "COMPLETED" if all(s == "SUCCEEDED" for s in statuses) else (
        "FAILED" if all(s == "FAILED" for s in statuses) else "PARTIAL")
    return report


def last_emergency(session: Session) -> dict[str, Any] | None:
    row = session.get(SystemSetting, LAST_KEY)
    return row.value if row is not None and isinstance(row.value, dict) else None
