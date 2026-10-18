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
