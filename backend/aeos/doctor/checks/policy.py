"""§08 Canonical policy: MCI reference/version, owner decisions, active policy hash, Office defaults.

MCI alignment is never reported as VERIFIED (§02, §51 P1). ``policy.mci`` reuses the policy module's own
verification of the reference (``aeos.policy.facade.mci_reference``): a declared VERIFIED reference whose LOCAL
source (``<data dir>/policy/canonical/`` or aeos.yaml ``mci_source_path``; the public repository never holds MCI
content) is missing, refused or hash-mismatched is FAILED, never CONFIGURED; a hash-matching source is CONFIGURED
(the source is present on this machine), while compliance stays NOT-PROVEN.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, or_, select

from aeos.doctor.checks import check
from aeos.doctor.checks._common import file_fingerprint
from aeos.doctor.model import CheckContext, Outcome, Remedy, aeos_cmd, os_install_command
from aeos.doctor.office import OFFICE_FLAGS, read_office_policy

MCI_REFERENCE = "policy/mci-reference.yaml"
MCI_DEFAULT_FILE = "MCI_V2_SOURCE_OF_TRUTH.md"
# verification_code of aeos.policy.snapshot -> Doctor error code of a declared VERIFIED that does not verify
MCI_FAILURE_CODES = {
    "SOURCE_MISSING": "MCI_SOURCE_MISSING", "HASH_MISMATCH": "MCI_HASH_MISMATCH",
    "INCOMPLETE_REFERENCE": "MCI_REFERENCE_INCOMPLETE", "LOCATION_NOT_LOCAL": "MCI_LOCATION_NOT_LOCAL",
    "LOCATION_OUTSIDE_LOCAL_DIR": "MCI_LOCATION_OUTSIDE_LOCAL_DIR", "SOURCE_IN_REPOSITORY": "MCI_SOURCE_IN_REPOSITORY",
}


def _fp_mci(ctx: CheckContext) -> dict[str, Any]:
    """The reference file, the local source file and the local pack mapping: replacing the MCI document (or its
    mapping) makes the result STALE."""
    from aeos.policy import facade as policy

    material: dict[str, Any] = {"file": MCI_REFERENCE, **(ctx.runner.stat(str(ctx.repo / MCI_REFERENCE))
                                                         or {"present": False})}
    source = policy.mci_source_path(repo=ctx.repo, settings=ctx.settings)
    if source is not None:
        material["source"] = ctx.runner.stat(str(source)) or {"present": False}
    mapping = ctx.settings.layout.canonical_policy / "mci-pack-mapping.yaml"
    material["pack_mapping"] = ctx.runner.stat(str(mapping)) or {"present": False}
    return material


def _hash_command(path: Path) -> str | None:
    """Copyable sha256 command for the local source (single-quoted: the macOS data dir has a space)."""
    win, posix = str(path).replace("'", "''"), str(path).replace("'", "'\\''")
    return os_install_command(
        windows=f"\"sha256:\" + (Get-FileHash -Algorithm SHA256 -LiteralPath '{win}').Hash.ToLower()",
        macos=f"echo \"sha256:$(shasum -a 256 '{posix}' | cut -d' ' -f1)\"",
        linux=f"echo \"sha256:$(sha256sum '{posix}' | cut -d' ' -f1)\"")


@check("policy.mci", "CANONICAL_POLICY", "Referensi MCI kanonik", ttl_seconds=86400, ok=frozenset(),
       severity_fail="HIGH", fingerprint=_fp_mci, invalidators=("config:policy",))
def policy_mci(ctx: CheckContext) -> Outcome:
    from aeos.policy import facade as policy

    path = ctx.repo / MCI_REFERENCE
    if not path.is_file():
        return Outcome("MISSING", "policy/mci-reference.yaml tidak ada; gate yang bergantung MCI tetap BLOCKED.",
                       error_code="MCI_REFERENCE_MISSING", severity="HIGH")
    try:
        yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return Outcome("FAILED", "policy/mci-reference.yaml tidak valid.", error_code="MCI_REFERENCE_INVALID")
    ref = policy.mci_reference(repo=ctx.repo, settings=ctx.settings)
    declared, code = ref["declared_status"], ref["verification_code"]
    evidence: dict[str, Any] = {
        "status_in_file": declared, "source_status": ref["status"], "verification_code": code,
        "version": ref["version"], "content_hash": ref["content_hash"], "location": ref["location"],
        "source_location": ref["source_location"], "source_placement": ref["source_placement"],
        "alignment": "UNVERIFIED", "compliance": ref["compliance"], "packs_mapping_status": ref["packs_mapping_status"],
        "pack_mapping_state": ref["pack_mapping_state"]}
    local_dir = ctx.settings.layout.canonical_policy
    source = policy.mci_source_path(repo=ctx.repo, settings=ctx.settings) or local_dir / MCI_DEFAULT_FILE
    remedy = Remedy(operation="Pemilik menyediakan sumber MCI kanonik (tidak direkonstruksi dari ingatan model) di mesin "
                    f"ini saja: simpan di {local_dir} (tidak pernah di repositori publik), lalu catat status VERIFIED, "
                    f"version, content_hash dan location: local:{MCI_DEFAULT_FILE} di policy/mci-reference.yaml",
                    target=MCI_REFERENCE, privilege="pemilik", evidence_to_collect="hash sumber MCI")
    version = str(ref["version"] or "") or None
    if declared in ("UNAVAILABLE", ""):
        return Outcome("MISSING", "MCI_V2_SOURCE_OF_TRUTH belum tersedia; kepatuhan kanonik UNVERIFIED (bukan PASS).",
                       evidence=evidence, error_code="MCI_UNAVAILABLE", severity="HIGH", remedy=remedy)
    if declared == "VERIFIED" and ref["status"] == "VERIFIED":
        return Outcome("CONFIGURED", f"Sumber MCI {version or ''} ada di mesin ini dan hash-nya cocok; kepatuhan tetap "
                       f"NOT-PROVEN dan pemetaan pack {ref['packs_mapping_status']} (Doctor tidak memverifikasi "
                       "keselarasan isi).", evidence=evidence, component_version=version)
    if declared == "VERIFIED":
        return Outcome("FAILED", f"MCI dinyatakan VERIFIED tetapi tidak terverifikasi di mesin ini ({code}): "
                       f"{ref['verification_note']}", evidence=evidence,
                       error_code=MCI_FAILURE_CODES.get(code, "MCI_NOT_VERIFIED"), severity="HIGH",
                       component_version=version,
                       remedy=Remedy(operation=f"Salin dokumen MCI asli ke {source} (lokal, tidak di-commit; repositori "
                                     "publik), pastikan location di policy/mci-reference.yaml adalah "
                                     f"local:{source.name} (atau mci_source_path di aeos.yaml), hitung sha256 dari byte "
                                     "file itu dan cocokkan dengan content_hash (perubahan policy/ lewat commit yang "
                                     "direview)", command=_hash_command(source), target=str(source),
                                     privilege="pemilik", evidence_to_collect="policy.mci CONFIGURED (hash cocok)"))
    if declared == "REFERENCED":
        return Outcome("CONFIGURED", "Referensi MCI tercatat (REFERENCED) tanpa verifikasi hash; keselarasan isi tetap "
                       "UNVERIFIED.", evidence=evidence, component_version=version, remedy=remedy)
    return Outcome("FAILED", f"Status MCI {declared!r} tidak dikenal (UNAVAILABLE | REFERENCED | VERIFIED); "
                   "diperlakukan UNVERIFIED (fail closed).", evidence=evidence, error_code="MCI_STATUS_UNKNOWN",
                   severity="HIGH", remedy=remedy)


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
