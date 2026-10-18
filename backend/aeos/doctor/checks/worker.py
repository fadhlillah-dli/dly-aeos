"""§08 Worker: engine/version, model tool/schema behaviour, request/response contract, resource caps."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from aeos.doctor.checks import check
from aeos.doctor.checks._common import binary_fingerprint, logical
from aeos.doctor.model import CheckContext, Outcome, Remedy

OPENCODE_URL = "https://github.com/anomalyco/opencode"


@check("worker.opencode", "WORKER", "Engine pekerja OpenCode", component_id="opencode", ttl_seconds=86400,
       invalidators=("binary:opencode",), ok={"VERIFIED", "CONFIGURED"},
       fingerprint=binary_fingerprint("opencode", "opencode_binary"), severity_fail="HIGH", observation="installation")
def worker_opencode(ctx: CheckContext) -> Outcome:
    remedy = Remedy(operation="Pasang OpenCode dari source resmi; versi dipin setelah capability test",
                    source=f"S28: {OPENCODE_URL}", target="PATH atau setelan opencode_binary",
                    privilege="pengguna lokal", rollback="uninstall OpenCode", evidence_to_collect="worker.opencode VERIFIED")
    try:
        from aeos.worker_runtime import facade as worker

        status = worker.opencode_status()
    except (ImportError, ModuleNotFoundError):
        found = ctx.runner.which("opencode")
        return Outcome("UNKNOWN", "Modul worker runtime belum tersedia; status engine tidak dapat dinilai "
                       f"({'binary ditemukan di PATH' if found else 'binary tidak ditemukan di PATH'}).",
                       evidence={"binary_on_path": bool(found), "location": logical(ctx, found) if found else None},
                       error_code="WORKER_RUNTIME_NOT_IMPLEMENTED", remedy=None if found else remedy)
    except Exception as exc:
        return Outcome("UNKNOWN", f"Status OpenCode gagal dibaca ({type(exc).__name__}).", error_code="OPENCODE_STATUS_ERROR")
    if not isinstance(status, dict):
        return Outcome("UNKNOWN", "Status OpenCode tidak dikenali.", error_code="OPENCODE_STATUS_UNRECOGNISED")
    st = str(status.get("status") or "").upper()
    version = status.get("version")
    evidence: dict[str, Any] = {k: status.get(k) for k in ("status", "version", "found") if k in status}
    if status.get("path"):
        evidence["location"] = logical(ctx, str(status["path"]))
    if st == "MISSING" or status.get("found") is False:
        return Outcome("MISSING", "OpenCode tidak ditemukan.", evidence=evidence, error_code="OPENCODE_MISSING",
                       remedy=remedy)
    if version:
        return Outcome("VERIFIED", f"OpenCode {version} ditemukan (skenario: status engine).", evidence=evidence,
                       component_version=str(version), target_identity=evidence.get("location"))
    if st in ("FAILED", "ERROR"):
        return Outcome("FAILED", "OpenCode ditemukan tetapi status gagal.", evidence=evidence, error_code="OPENCODE_FAILED")
    return Outcome("CONFIGURED", "OpenCode ditemukan; versi tidak dilaporkan (UNKNOWN).", evidence=evidence)


@check("worker.contract", "WORKER", "Kontrak request/response dan caps pekerja", component_id="opencode",
       needs_db=True, ttl_seconds=86400, invalidators=("binary:opencode", "model:route"), severity_fail="HIGH")
def worker_contract(ctx: CheckContext) -> Outcome:
    from aeos.models.evidence import EvidenceArtifact

    assert ctx.session is not None
    ev = ctx.session.scalar(select(EvidenceArtifact).where(EvidenceArtifact.kind == "WORKER_CONTRACT_TEST",
                                                           EvidenceArtifact.trusted.is_(True),
                                                           EvidenceArtifact.mode == "LIVE")
                            .order_by(EvidenceArtifact.created_at.desc()).limit(1))
    if ev is None:
        return Outcome("UNKNOWN", "Belum ada bukti tepercaya uji kontrak worker (tool/schema/patch, caps); NOT-PROVEN.",
                       error_code="WORKER_CONTRACT_NOT_PROVEN", evidence={"looked_for": "EvidenceArtifact WORKER_CONTRACT_TEST"})
    return Outcome("VERIFIED", f"Bukti uji kontrak worker {ev.id[:8]} ({ev.created_at.isoformat()}).",
                   evidence={"evidence_id": ev.id, "level": ev.evidence_level, "claim": ev.claim[:200]})
