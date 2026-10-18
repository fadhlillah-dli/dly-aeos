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
from aeos.doctor.checks import REGISTRY, load_all, ordered_specs
from aeos.doctor.model import GROUP_LABELS, GROUPS
from aeos.doctor.runner import latest_results, latest_run, row_view
from aeos.models.doctor_ext import DoctorRun


def _visible(scope: dict[str, Any] | None, domain: str | None) -> bool:
    d = (scope or {}).get("domain")
    return d is None or d == domain


def hidden_check_ids(domain: str | None) -> set[str]:
    """Checks scoped to a domain other than the session's (never shown, counted or quoted)."""
    load_all()
    return {cid for cid, s in REGISTRY.items() if not _visible(dict(s.scope), domain)}


def _reason_check_id(reason: str) -> str:
    text = reason[len("catatan: "):] if reason.startswith("catatan: ") else reason
    return text.split(":", 1)[0].strip()


def _critical_for() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for cap, rule in RULES.items():
        for cid in rule.critical:
            out.setdefault(cid, []).append(cap.value)
    return out


def visible_counts(statuses: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for st in statuses:
        out[st] = out.get(st, 0) + 1
    return out


def run_meta(run: DoctorRun | None, counts: dict[str, int] | None = None) -> dict[str, Any] | None:
    """Run metadata. ``counts`` should be computed over the checks visible to the session; the stored
    all-domain counts are never returned to a domain view (counts are metadata too, §50)."""
    if run is None:
        return None
    return {"id": run.id, "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None, "trigger": run.trigger,
            "functional": run.functional, "groups": run.groups, "capability": run.capability,
            "counts": counts if counts is not None else {}}


def last_failure(session: Session, run: DoctorRun | None) -> dict[str, Any] | None:
    """Latest ``doctor.run.failed`` event (e.g. the startup pass) newer than the last completed run, so the
    page never presents an older run as if it were the current state. Platform-level, no details."""
    from sqlalchemy import select

    from aeos.core.timeutil import ensure_aware
    from aeos.models.audit import Event

    ev = session.scalar(select(Event).where(Event.kind == "doctor.run.failed", Event.domain.is_(None))
                        .order_by(Event.seq.desc()).limit(1))
    if ev is None:
        return None
    if run is not None and ensure_aware(run.started_at) >= ensure_aware(ev.at):
        return None
    payload = ev.payload or {}
    return {"at": ensure_aware(ev.at).isoformat(), "trigger": payload.get("trigger"), "error": payload.get("error")}


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
    counts = visible_counts([i["status"] for i in items if last is not None and i.get("run_id") == last.id])
    return {
        "items": items,
        "groups": [{"id": g, "label": label, "order": i} for i, (g, label) in enumerate(GROUPS)],
        "last_run": run_meta(last, counts),
        "last_failure": last_failure(session, last),
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
    hidden = hidden_check_ids(domain)
    for it in items:
        before = len(it["checks"])
        it["checks"] = [c for c in it["checks"] if c["check_id"] not in hidden]
        # Reasons and remedies quote check summaries/evidence: strip those of checks scoped to another
        # domain so their metadata (e.g. whether an OFFICE credential exists) never reaches this view.
        it["reasons"] = [r for r in it["reasons"] if _reason_check_id(r) not in hidden]
        it["remedies"] = [r for r in it["remedies"] if r.get("check_id") not in hidden]
        # Residual risks quote check evidence too (e.g. PERSONAL branch protection): keep only those of the
        # checks this session may see.
        seen: set[str] = set()
        it["residual_risks"] = [r for c in it["checks"] for r in (c.get("residual_risks") or [])
                                if not (r in seen or seen.add(r))]
        if len(it["checks"]) < before:
            it["reasons"].append("catatan: pemeriksaan ber-scope domain lain tidak ditampilkan; pilih domain "
                                 "PERSONAL/OFFICE untuk melihat detailnya")
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
