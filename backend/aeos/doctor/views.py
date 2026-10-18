"""Dashboard Readiness projections (§08 layer 2, §35 Overview/Readiness)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from aeos.core.timeutil import utcnow
from aeos.doctor.capabilities import (
    RULES,
    capability_matrix,
    checks_for_capability,
    effective_status,
    live_fingerprints,
)
from aeos.doctor.checks import REGISTRY, ordered_specs
from aeos.doctor.model import GROUP_LABELS, GROUPS
from aeos.doctor.runner import latest_results, latest_run, row_view
from aeos.models.doctor_ext import DoctorRun


def _visible(scope: dict[str, Any] | None, domain: str | None) -> bool:
    d = (scope or {}).get("domain")
    return d is None or d == domain


def _critical_for() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for cap, rule in RULES.items():
        for cid in rule.critical:
            out.setdefault(cid, []).append(cap.value)
    return out


def run_meta(run: DoctorRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {"id": run.id, "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None, "trigger": run.trigger,
            "functional": run.functional, "groups": run.groups, "capability": run.capability, "counts": run.counts}


def checks_view(session: Session, *, domain: str | None, group: str | None = None) -> dict[str, Any]:
    specs = [s for s in ordered_specs() if _visible(dict(s.scope), domain)]
    if group:
        specs = [s for s in specs if s.group == group.upper()]
    ids = [s.check_id for s in specs]
    rows = {cid: row_view(r) for cid, r in latest_results(session, ids).items()}
    fps = live_fingerprints(session, ids)
    now = utcnow()
    crit = _critical_for()
    items = []
    for s in specs:
        row = rows.get(s.check_id)
        eff, why = effective_status(row, now, fps.get(s.check_id))
        base = row or {
            "check_id": s.check_id, "title": s.title, "group": s.group, "component_id": s.component_id,
            "component_version": None, "scope": dict(s.scope), "target_identity": None, "status": "UNKNOWN",
            "severity": "LOW", "summary": "Belum pernah diperiksa." + (" Uji fungsi: jalankan dengan opsi fungsional."
                                                                       if s.functional else ""),
            "observed_evidence": {}, "last_checked_at": None, "expires_at": None, "invalidators": list(s.invalidators),
            "fingerprint": None, "error_code": None, "remediation": {}, "requires_destructive": False,
            "requires_paid_write": False, "run_id": None,
        }
        items.append({**base, "title": s.title, "group_label": GROUP_LABELS[s.group], "effective_status": eff,
                      "stale_reason": why, "never_run": row is None, "functional_only": s.functional,
                      "network": s.network, "critical_for": crit.get(s.check_id, []),
                      "ok_statuses": sorted(s.ok_statuses),
                      "component_link": f"/tools/{s.component_id}" if s.component_id else None})
    last = latest_run(session)
    return {
        "items": items,
        "groups": [{"id": g, "label": label, "order": i} for i, (g, label) in enumerate(GROUPS)],
        "last_run": run_meta(last),
        "source": "Readiness Doctor: probe kode tanpa LLM (CheckResult)",
        "checked_at": last.finished_at.isoformat() if last and last.finished_at else None,
        "mode": "LIVE",
        "domain": domain,
        "status_legend": {
            "CONNECTED": "probe koneksi berhasil; bukan verifikasi izin/keamanan",
            "VERIFIED": "skenario fungsi tertentu berhasil pada target yang disebut; bukan label 'aman' universal",
            "STALE": "binary/konfigurasi berubah atau di-invalidasi; perlu dicek ulang",
            "EXPIRED": "hasil melewati masa berlaku",
            "UNKNOWN": "tidak dapat diputuskan; tidak dianggap lulus",
        },
    }


def capabilities_view(session: Session, *, domain: str | None) -> dict[str, Any]:
    from aeos.doctor.capabilities import HUMAN_ONLY

    items = capability_matrix(session, domain=domain)
    for it in items:
        it["checks"] = [c for c in it["checks"] if _visible(dict(REGISTRY[c["check_id"]].scope), domain)
                        or c["check_id"] not in REGISTRY]
    last = latest_run(session)
    return {
        "items": items,
        "human_only": [{"label": h, "status": "HUMAN_ONLY",
                        "note": "Keputusan manusia; bukan capability worker (§08, U-09)."} for h in HUMAN_ONLY],
        "source": "Capability gates dari CheckResult terbaru (§08)",
        "checked_at": last.finished_at.isoformat() if last and last.finished_at else None,
        "mode": "LIVE",
        "domain": domain,
    }


def history_view(session: Session, check_id: str, *, domain: str | None, limit: int = 50) -> dict[str, Any]:
    from aeos.core.errors import NotFound
    from aeos.doctor.runner import history

    spec = REGISTRY.get(check_id)
    if spec is None or not _visible(dict(spec.scope), domain):
        raise NotFound("check not found")
    rows = [row_view(r) for r in history(session, check_id, limit=limit)]
    caps = [c.value for c in RULES if check_id in checks_for_capability(c)]
    return {"check": {"check_id": spec.check_id, "title": spec.title, "group": spec.group,
                      "group_label": GROUP_LABELS[spec.group], "component_id": spec.component_id,
                      "ttl_seconds": spec.ttl_seconds, "invalidators": list(spec.invalidators),
                      "functional_only": spec.functional, "capabilities": caps},
            "items": rows, "source": "CheckResult history", "checked_at": rows[0]["last_checked_at"] if rows else None,
            "mode": "LIVE"}
