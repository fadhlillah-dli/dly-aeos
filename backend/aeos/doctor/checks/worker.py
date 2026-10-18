"""§08 Worker: engine/version, model tool/schema behaviour, request/response contract, resource caps."""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select

from aeos.doctor.checks import check
from aeos.doctor.checks._common import binary_fingerprint, logical
from aeos.doctor.model import CheckContext, Outcome, Remedy, aeos_cmd, npm_cmd
from aeos.models.evidence import EvidenceArtifact

OPENCODE_URL = "https://github.com/anomalyco/opencode"

# Platform-level rows are visible to every domain: OFFICE-domain evidence is never quoted here (§35).
_NOT_OFFICE = or_(EvidenceArtifact.domain.is_(None), EvidenceArtifact.domain != "OFFICE")


def _opencode_remedy(pinned: str | None) -> Remedy:
    version = pinned or "<versi dari install-manifest>"
    return Remedy(operation="Pasang OpenCode versi yang dipin (worker engine pertama, §07 langkah 5); fungsi dibuktikan "
                  "terpisah oleh uji kontrak worker",
                  command=npm_cmd(f"install -g opencode-ai@{version}"),
                  source=f"S28: {OPENCODE_URL} (paket npm opencode-ai; install-manifest/ otoritatif)",
                  target="PATH atau setelan opencode_binary (AEOS_OPENCODE_BINARY)", privilege="pengguna lokal",
                  cost="tidak ada", rollback=npm_cmd("uninstall -g opencode-ai"),
                  evidence_to_collect="worker.opencode VERIFIED (opencode --version)")


@check("worker.opencode", "WORKER", "Engine pekerja OpenCode", component_id="opencode", ttl_seconds=86400,
       invalidators=("binary:opencode",), ok={"VERIFIED", "CONFIGURED"},
       fingerprint=binary_fingerprint("opencode", "opencode_binary"), severity_fail="HIGH", observation="installation")
def worker_opencode(ctx: CheckContext) -> Outcome:
    try:
        from aeos.worker_runtime import facade as worker

        status = worker.opencode_status()
    except (ImportError, ModuleNotFoundError):
        found = ctx.runner.which("opencode")
        return Outcome("UNKNOWN", "Modul worker runtime belum tersedia; status engine tidak dapat dinilai "
                       f"({'binary ditemukan di PATH' if found else 'binary tidak ditemukan di PATH'}).",
                       evidence={"binary_on_path": bool(found), "location": logical(ctx, found) if found else None},
                       error_code="WORKER_RUNTIME_NOT_IMPLEMENTED", remedy=None if found else _opencode_remedy(None))
    except Exception as exc:
        return Outcome("UNKNOWN", f"Status OpenCode gagal dibaca ({type(exc).__name__}).", error_code="OPENCODE_STATUS_ERROR")
    if not isinstance(status, dict):
        return Outcome("UNKNOWN", "Status OpenCode tidak dikenali.", error_code="OPENCODE_STATUS_UNRECOGNISED")
    # worker_runtime contract: status INSTALLED|FAILED|MISSING, version, pinned_version, drift, functional NOT_PROVEN
    st = str(status.get("status") or "").upper()
    version = status.get("version")
    pinned = status.get("pinned_version")
    evidence: dict[str, Any] = {k: status.get(k) for k in ("status", "version", "pinned_version", "drift", "functional",
                                                            "checked_at") if k in status}
    path = status.get("binary") or status.get("path")
    if path:
        evidence["location"] = logical(ctx, str(path))
    remedy = _opencode_remedy(pinned)
    if st == "MISSING" or status.get("found") is False:
        return Outcome("MISSING", f"OpenCode tidak ditemukan (versi yang dipin: {pinned or 'UNKNOWN'}).", evidence=evidence,
                       error_code="OPENCODE_MISSING", remedy=remedy)
    if st in ("FAILED", "ERROR"):
        return Outcome("FAILED", f"OpenCode ditemukan tetapi `opencode --version` gagal ({status.get('evidence')}).",
                       evidence=evidence, error_code="OPENCODE_FAILED", remedy=remedy,
                       target_identity=evidence.get("location"))
    if st == "INSTALLED" and version:
        if status.get("drift") or (pinned and str(version) != str(pinned)):
            # §38 "Binary/version/route berubah dari pin": admission stops until reconciled (§50); the engine
            # is installed, so the inventory still records INSTALLED with the observed version.
            return Outcome("FAILED", f"OpenCode {version} terpasang tetapi berbeda dari versi yang dipin {pinned} "
                           "(drift): admission worker dihentikan sampai direkonsiliasi.", evidence=evidence,
                           component_version=str(version), target_identity=evidence.get("location"),
                           error_code="OPENCODE_PIN_DRIFT", severity="HIGH", remedy=Remedy(
                               operation="Pasang versi yang dipin, ATAU ubah pin lewat PR yang direview (install-"
                               "manifest + worker runtime) setelah uji kontrak worker pada versi baru",
                               command=npm_cmd(f"install -g opencode-ai@{pinned}"),
                               source=f"S28: {OPENCODE_URL} (install-manifest/ otoritatif)",
                               target="PATH atau setelan opencode_binary (AEOS_OPENCODE_BINARY)",
                               privilege="pengguna lokal (pin baru: reviewer manusia)", cost="tidak ada",
                               rollback=npm_cmd(f"install -g opencode-ai@{version}"),
                               evidence_to_collect="worker.opencode VERIFIED tanpa drift"),
                           observations=[{"component_id": "opencode", "dimension": "installation",
                                          "status": "INSTALLED", "observed_version": str(version)}])
        return Outcome("VERIFIED", f"OpenCode {version} terpasang sesuai pin (skenario: perintah versi; fungsi worker "
                       "NOT-PROVEN).", evidence=evidence, component_version=str(version),
                       target_identity=evidence.get("location"))
    return Outcome("UNKNOWN", f"Status OpenCode tidak konklusif ({st or 'tanpa status'}).", evidence=evidence,
                   error_code="OPENCODE_STATUS_UNRECOGNISED")


IMPLEMENTER_AGENT = "opencode-implementer"


def _agent_fp(ctx: CheckContext) -> dict[str, Any] | None:
    """The stored result goes STALE as soon as the agent definition is enabled/disabled or revised."""
    if ctx.session is None:
        return None
    from aeos.models.workforce import AgentDefinition

    a = ctx.session.scalar(select(AgentDefinition).where(AgentDefinition.agent_key == IMPLEMENTER_AGENT))
    return {"agent": IMPLEMENTER_AGENT, "present": a is not None,
            **({"enabled": bool(a.enabled), "harness": a.harness, "revision": a.revision_hash or a.revision} if a else {})}


@check("worker.agent_definition", "WORKER", "AgentDefinition opencode-implementer aktif", component_id="opencode",
       needs_db=True, ttl_seconds=3600, ok={"CONFIGURED", "VERIFIED"}, invalidators=("config:agents",),
       fingerprint=_agent_fp, severity_fail="HIGH")
def worker_agent_definition(ctx: CheckContext) -> Outcome:
    from aeos.models.workforce import AgentDefinition

    assert ctx.session is not None
    a = ctx.session.scalar(select(AgentDefinition).where(AgentDefinition.agent_key == IMPLEMENTER_AGENT))
    if a is None:
        return Outcome("MISSING", f"AgentDefinition {IMPLEMENTER_AGENT} belum ada di katalog.",
                       error_code="AGENT_MISSING", remedy=Remedy(
                           operation="Muat katalog workforce (DESIGN; agent tetap nonaktif sampai diaktifkan pemilik)",
                           command=aeos_cmd("seed"), privilege="pengguna lokal",
                           evidence_to_collect="worker.agent_definition"))
    evidence = {"agent_key": a.agent_key, "harness": a.harness, "enabled": bool(a.enabled), "mode": a.mode,
                "revision": a.revision, "enabled_reason": (a.enabled_reason or "")[:200]}
    if a.harness != "opencode":
        return Outcome("FAILED", f"{IMPLEMENTER_AGENT} bukan agent OpenCode (harness {a.harness}).", evidence=evidence,
                       error_code="AGENT_HARNESS_MISMATCH")
    if not a.enabled:
        return Outcome("MISSING", f"Agent {IMPLEMENTER_AGENT} nonaktif (default katalog); scheduler tidak akan "
                       "menjalankan worker.", evidence=evidence, error_code="AGENT_DISABLED", remedy=Remedy(
                           operation="Aktifkan agent di Workforce Console (Agen -> opencode-implementer -> Aktifkan) "
                           "setelah OpenCode, sandbox dan route siap; endpoint menolak bila prasyarat belum READY",
                           target=f"AgentDefinition {IMPLEMENTER_AGENT}", privilege="SUPER_ADMIN",
                           rollback="nonaktifkan agent", evidence_to_collect="worker.agent_definition CONFIGURED"))
    return Outcome("CONFIGURED", f"Agent {IMPLEMENTER_AGENT} aktif ({a.enabled_reason or 'tanpa alasan tercatat'}); "
                   "aktif bukan bukti sedang dipakai.", evidence=evidence, target_identity=f"agent:{IMPLEMENTER_AGENT}")


@check("worker.contract", "WORKER", "Kontrak request/response dan caps pekerja", component_id="opencode",
       needs_db=True, ttl_seconds=86400, invalidators=("binary:opencode", "model:route"), severity_fail="HIGH")
def worker_contract(ctx: CheckContext) -> Outcome:
    assert ctx.session is not None
    ev = ctx.session.scalar(select(EvidenceArtifact).where(EvidenceArtifact.kind == "WORKER_CONTRACT_TEST",
                                                           EvidenceArtifact.trusted.is_(True),
                                                           EvidenceArtifact.mode == "LIVE", _NOT_OFFICE)
                            .order_by(EvidenceArtifact.created_at.desc()).limit(1))
    if ev is None:
        return Outcome("UNKNOWN", "Belum ada bukti tepercaya uji kontrak worker (tool/schema/patch, caps); NOT-PROVEN. "
                       "Hasil worker tetap REVIEW sampai verifier independen membuktikannya.",
                       error_code="WORKER_CONTRACT_NOT_PROVEN",
                       evidence={"looked_for": "EvidenceArtifact WORKER_CONTRACT_TEST (trusted, LIVE)",
                                 "residual_risks": ["Kontrak tool/schema/patch worker NOT-PROVEN pada host ini; output "
                                                    "worker hanya REVIEW sampai verifier independen lulus"]})
    return Outcome("VERIFIED", f"Bukti uji kontrak worker {ev.id[:8]} ({ev.created_at.isoformat()}).",
                   evidence={"evidence_id": ev.id, "level": ev.evidence_level, "claim": ev.claim[:200]})
