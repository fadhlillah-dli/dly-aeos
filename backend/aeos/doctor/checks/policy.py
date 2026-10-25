"""§08 Canonical policy: MCI reference/version, owner decisions, active policy hash, Office defaults.

MCI alignment is never reported as VERIFIED: the canonical source has not been provided (§02, §51 P1).
"""

from __future__ import annotations

from typing import Any

import yaml
from sqlalchemy import func, or_, select

from aeos.doctor.checks import check
from aeos.doctor.checks._common import file_fingerprint
from aeos.doctor.model import CheckContext, Outcome, Remedy, aeos_cmd
from aeos.doctor.office import OFFICE_FLAGS, read_office_policy


@check("policy.mci", "CANONICAL_POLICY", "Referensi MCI kanonik", ttl_seconds=86400, ok=frozenset(),
       severity_fail="HIGH", fingerprint=file_fingerprint("policy/mci-reference.yaml"), invalidators=("config:policy",))
def policy_mci(ctx: CheckContext) -> Outcome:
    path = ctx.repo / "policy" / "mci-reference.yaml"
    if not path.is_file():
        return Outcome("MISSING", "policy/mci-reference.yaml tidak ada; gate yang bergantung MCI tetap BLOCKED.",
                       error_code="MCI_REFERENCE_MISSING", severity="HIGH")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return Outcome("FAILED", "policy/mci-reference.yaml tidak valid.", error_code="MCI_REFERENCE_INVALID")
    mci = data.get("mci") or {}
    status = str(mci.get("status") or "UNAVAILABLE").upper()
    evidence: dict[str, Any] = {"status_in_file": status, "version": mci.get("version"),
                                "content_hash": mci.get("content_hash"), "alignment": "UNVERIFIED"}
    remedy = Remedy(operation="Pemilik menyediakan sumber MCI kanonik (tidak direkonstruksi dari ingatan model)",
                    target="policy/mci-reference.yaml", privilege="pemilik", evidence_to_collect="hash sumber MCI")
    if status in ("UNAVAILABLE", ""):
        return Outcome("MISSING", "MCI_V2_SOURCE_OF_TRUTH belum tersedia; kepatuhan kanonik UNVERIFIED (bukan PASS).",
                       evidence=evidence, error_code="MCI_UNAVAILABLE", severity="HIGH", remedy=remedy)
    return Outcome("CONFIGURED", f"Referensi MCI tercatat ({status}); keselarasan isi tetap UNVERIFIED oleh Doctor.",
                   evidence=evidence, component_version=str(mci.get("version") or "") or None)


MASTER_TARGET_TYPE = "master_architecture"  # Decision.target_type of a direct approval of the master's sha256


def _master_rel() -> str:
    from aeos.inventory.manifest import MASTER_REL

    return MASTER_REL


def _approved_master_binding(session: Any, sha: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(binding of *sha*, newest approved snapshot) - the binding is the newest human-approved
    ArchitectureSnapshot whose master reference is *sha*, else an ACTIVE human APPROVAL/POLICY Decision on
    ``master_architecture`` bound to *sha*. Platform/PERSONAL-scoped snapshots only (an OFFICE snapshot's label
    never reaches this platform row)."""
    from aeos.core.timeutil import ensure_aware, utcnow
    from aeos.models.decisions import ArchitectureSnapshot, Decision

    def approval(snap: ArchitectureSnapshot) -> Decision | None:
        d = session.scalar(select(Decision).where(
            Decision.kind == "APPROVAL", Decision.target_type == "architecture_snapshot",
            Decision.target_id == snap.id, Decision.status == "ACTIVE", Decision.target_hash == snap.snapshot_hash,
            Decision.actor_kind.in_(("HUMAN", "HUMAN_RECORDED"))).order_by(Decision.created_at.desc()).limit(1))
        return d

    newest: dict[str, Any] | None = None
    for snap in session.scalars(select(ArchitectureSnapshot).order_by(ArchitectureSnapshot.created_at.desc())):
        refs = snap.refs or {}
        if (refs.get("scope") or {}).get("domain") not in (None, "PERSONAL"):
            continue
        d = approval(snap)
        if d is None:
            continue
        info = {"via": "architecture_snapshot", "snapshot_id": snap.id, "label": snap.label,
                "snapshot_master_sha256": (refs.get("master") or {}).get("sha256"), "decision_id": d.id,
                "approved_at": d.created_at.isoformat() if d.created_at else None}
        newest = newest or info
        if info["snapshot_master_sha256"] == sha:
            return info, newest
    now = utcnow()
    for d in session.scalars(select(Decision).where(
            Decision.kind.in_(("APPROVAL", "POLICY")), Decision.target_type == MASTER_TARGET_TYPE,
            Decision.status == "ACTIVE", Decision.target_hash == sha,
            Decision.actor_kind.in_(("HUMAN", "HUMAN_RECORDED"))).order_by(Decision.created_at.desc())):
        exp = ensure_aware(d.expires_at) if d.expires_at else None
        if exp is not None and exp <= now:
            continue
        return {"via": "decision", "decision_id": d.id,
                "approved_at": d.created_at.isoformat() if d.created_at else None}, newest
    return None, newest


@check("policy.master_approval", "CANONICAL_POLICY", "Master arsitektur terikat persetujuan tercatat",
       component_id="policy-decision-registry", needs_db=True, ttl_seconds=3600, ok={"VERIFIED"},
       invalidators=("config:policy", "config:master"), fingerprint=file_fingerprint(
           "docs/architecture/DLY_AEOS_Master_Architecture_Final.md"),
       description="U-23: nama master tetap dan perubahan berikutnya harus punya persetujuan yang dapat ditelusuri. "
                   "Membandingkan sha256 file master dengan snapshot arsitektur terbaru yang disetujui manusia (atau "
                   "Decision APPROVAL/POLICY pada master_architecture dengan hash yang sama).")
def policy_master_approval(ctx: CheckContext) -> Outcome:
    from aeos.core.hashing import file_sha256

    assert ctx.session is not None
    rel = _master_rel()
    path = ctx.repo / rel
    if not path.is_file():
        return Outcome("MISSING", f"File master {rel} tidak ada; arsitektur kanonik tidak dapat dibaca.",
                       error_code="MASTER_MISSING", severity="HIGH")
    sha = file_sha256(path)
    binding, newest = _approved_master_binding(ctx.session, sha)
    evidence: dict[str, Any] = {"path": rel, "sha256": sha, "binding": binding, "newest_approved_snapshot": newest}
    remedy = Remedy(operation="Pemilik meninjau isi master saat ini, lalu membuat snapshot arsitektur dan "
                              "menyetujuinya (keputusan manusia terikat hash snapshot, yang memuat sha256 master)",
                    target="http://127.0.0.1:8787/system-map (panel snapshot: Buat snapshot, lalu Setujui)",
                    privilege="pemilik (SUPER_ADMIN)",
                    evidence_to_collect="policy.master_approval VERIFIED (sha256 master = snapshot yang disetujui)")
    if binding is not None:
        via = (f"snapshot '{binding.get('label')}' yang disetujui" if binding["via"] == "architecture_snapshot"
               else f"Decision {str(binding.get('decision_id'))[:8]} pada master_architecture")
        return Outcome("VERIFIED", f"sha256 master {sha[:19]} terikat {via}; perubahan master dapat ditelusuri ke "
                       "persetujuan manusia.", evidence=evidence)
    if newest is not None:
        return Outcome("UNKNOWN", f"Master berubah sejak snapshot terakhir yang disetujui ('{newest.get('label')}'): "
                       f"sha256 sekarang {sha[:19]}, snapshot memuat "
                       f"{str(newest.get('snapshot_master_sha256') or 'tanpa master')[:19]}. Persetujuan perubahan "
                       "NOT-PROVEN (hanya riwayat git).", evidence=evidence,
                       error_code="MASTER_CHANGED_SINCE_APPROVAL", severity="MEDIUM", remedy=remedy)
    return Outcome("UNKNOWN", f"Belum ada snapshot arsitektur yang disetujui untuk sha256 master {sha[:19]}; "
                   "persetujuan isi master NOT-PROVEN (hanya riwayat git).", evidence=evidence,
                   error_code="MASTER_APPROVAL_MISSING", severity="MEDIUM", remedy=remedy)


@check("policy.office_defaults", "CANONICAL_POLICY", "OFFICE default deny berlaku",
       component_id="policy-decision-registry", ttl_seconds=3600, invalidators=("config:office_policy",),
       severity_fail="CRITICAL", fingerprint=file_fingerprint("policy/office-defaults.yaml"))
def policy_office_defaults(ctx: CheckContext) -> Outcome:
    pol, source = read_office_policy(ctx.session, ctx.repo)
    evidence = {"source": source, "external_worker_allowed": pol.get("external_worker_allowed"),
                "approved_worker_routes": len(pol.get("approved_worker_routes") or []),
                "claude_max_allowed_for_office_data": pol.get("claude_max_allowed_for_office_data")}
    if source == "fail-closed":
        return Outcome("FAILED", "Kebijakan OFFICE tidak dapat dibaca; semua operasi OFFICE ditolak (fail closed).",
                       evidence=evidence, error_code="OFFICE_POLICY_UNREADABLE")
    opened = sorted(f for f in set(OFFICE_FLAGS.values()) if pol.get(f) is True)
    if (opened or pol.get("approved_worker_routes")) and source != "policy-module":
        return Outcome("FAILED", "File default membuka izin OFFICE tanpa keputusan otorisasi tercatat "
                       f"({', '.join(opened) or 'approved_worker_routes'}); gate OFFICE tetap tertutup.",
                       evidence={**evidence, "opened_flags": opened}, error_code="OFFICE_DEFAULT_NOT_DENY",
                       severity="CRITICAL")
    if not pol.get("external_worker_allowed") and not pol.get("approved_worker_routes"):
        return Outcome("VERIFIED", "OFFICE default deny berlaku: external_worker_allowed=false, approved_worker_routes=[].",
                       evidence=evidence)
    return Outcome("CONFIGURED", "OFFICE memiliki izin dari modul policy; periksa Decision otorisasi perusahaan.",
                   evidence=evidence)


@check("policy.snapshot", "CANONICAL_POLICY", "Policy snapshot aktif dan keputusan pemilik",
       component_id="policy-decision-registry", needs_db=True, ttl_seconds=3600, ok={"CONFIGURED", "VERIFIED"},
       invalidators=("config:policy",))
def policy_snapshot(ctx: CheckContext) -> Outcome:
    from aeos.models.decisions import Decision, PolicySnapshot

    assert ctx.session is not None
    snap = ctx.session.scalar(select(PolicySnapshot).where(PolicySnapshot.active.is_(True))
                              .order_by(PolicySnapshot.created_at.desc()).limit(1))
    # Platform-level row shown to every domain: OFFICE decisions are not counted (counts are metadata, §50).
    decisions = ctx.session.scalar(select(func.count()).select_from(Decision).where(
        Decision.status == "ACTIVE", or_(Decision.domain.is_(None), Decision.domain != "OFFICE"))) or 0
    evidence: dict[str, Any] = {"active_decisions": int(decisions), "counted": "platform + PERSONAL (OFFICE tidak)"}
    if snap is None:
        return Outcome("MISSING", "Belum ada policy snapshot aktif.", evidence=evidence, error_code="NO_POLICY_SNAPSHOT",
                       remedy=Remedy(operation="Muat policy default dan keputusan pemilik", command=aeos_cmd("seed"),
                                     privilege="pengguna lokal", evidence_to_collect="policy.snapshot CONFIGURED"))
    evidence.update({"version": snap.version, "content_hash": snap.content_hash, "source": snap.source})
    return Outcome("CONFIGURED", f"Policy snapshot {snap.version} aktif ({snap.content_hash[:19]}); MCI tetap UNVERIFIED.",
                   evidence=evidence, component_version=snap.version)
