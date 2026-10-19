"""§08 Recovery: lease/queue restart, duplicate publication prevention, backups/restore, config rollback.

A backup that was never restored is not "recovery ready" (§33, §50 "Restore backup belum pernah diuji").
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select

from aeos.doctor.checks import check
from aeos.doctor.model import CheckContext, Outcome, Remedy, aeos_cmd
from aeos.models.evidence import EvidenceArtifact

# Platform-level rows are visible to every domain: OFFICE-domain evidence is never quoted here (§35).
_NOT_OFFICE = or_(EvidenceArtifact.domain.is_(None), EvidenceArtifact.domain != "OFFICE")


def _ops_backups(ctx: CheckContext) -> list[dict[str, Any]]:
    """Backups made by ``aeos ops backup create`` (directories with manifest.json), read-only."""
    try:
        from aeos.operations.backup import list_backups

        return [b for b in list_backups(ctx.settings) if b.get("readiness") != "INCOMPLETE"]
    except Exception:  # operations module absent or an unreadable manifest: counted as no backup, never green
        return []


@check("recovery.backups", "RECOVERY", "Backup database tersedia", component_id="operations-reliability",
       ttl_seconds=3600, ok={"CONFIGURED", "VERIFIED"}, severity_fail="MEDIUM")
def recovery_backups(ctx: CheckContext) -> Outcome:
    from datetime import UTC, datetime

    d = ctx.settings.layout.backups
    # Two producers: `aeos ops backup create` (directory + manifest, restore-testable) and the automatic
    # pre-upgrade SQLite copy of `aeos db upgrade` (database only).
    ops = _ops_backups(ctx)
    files = sorted(d.glob("*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True) if d.is_dir() else []
    remedy = Remedy(operation="Buat backup konsisten (database + evidence per domain); restore-test setelahnya",
                    command=aeos_cmd("ops backup create"), source="aeos.operations.backup (runbook operations)",
                    target="<data>/backups", privilege="pengguna lokal", rollback="hapus direktori backup baru",
                    evidence_to_collect="recovery.backups CONFIGURED lalu recovery.restore_test VERIFIED")
    if not ops and not files:
        return Outcome("MISSING", "Belum ada backup di <data>/backups (ops backup maupun salinan pra-upgrade).",
                       error_code="NO_BACKUP", remedy=remedy)
    newest_file = datetime.fromtimestamp(files[0].stat().st_mtime, UTC).isoformat() if files else None
    newest_ops = ops[0].get("created_at") if ops else None
    ready = [b["backup_id"] for b in ops if b.get("ready")]
    evidence = {"ops_backups": len(ops), "pre_upgrade_copies": len(files), "restore_tested": len(ready),
                "newest_ops_backup_at": newest_ops, "newest_pre_upgrade_at": newest_file, "location": "<data>/backups"}
    if not ops:
        return Outcome("CONFIGURED", f"Hanya {len(files)} salinan database pra-upgrade (tanpa evidence/attachment); "
                       "buat backup lengkap.", evidence=evidence, severity="MEDIUM", remedy=remedy)
    return Outcome("CONFIGURED", f"{len(ops)} backup ops (terbaru {newest_ops}); {len(ready)} sudah restore-tested.",
                   evidence=evidence)


@check("recovery.restore_test", "RECOVERY", "Restore pernah diuji", component_id="operations-reliability",
       needs_db=True, ttl_seconds=86400, severity_fail="MEDIUM", observation="functional")
def recovery_restore_test(ctx: CheckContext) -> Outcome:
    assert ctx.session is not None
    ev = ctx.session.scalar(select(EvidenceArtifact).where(EvidenceArtifact.kind == "RESTORE_TEST",
                                                           EvidenceArtifact.trusted.is_(True),
                                                           EvidenceArtifact.mode == "LIVE", _NOT_OFFICE)
                            .order_by(EvidenceArtifact.created_at.desc()).limit(1))
    if ev is None:
        ops = _ops_backups(ctx)
        cmd = aeos_cmd(f"ops backup restore-test {ops[0]['backup_id']}") if ops else aeos_cmd("ops backup create")
        return Outcome("MISSING", "Restore belum diuji; recovery tidak diklaim siap.", error_code="RESTORE_NOT_TESTED",
                       remedy=Remedy(operation="Uji restore backup ke direktori sementara terpisah dan simpan bukti "
                                     "RESTORE_TEST" + ("" if ops else " (buat backup dulu)"), command=cmd,
                                     source="aeos.operations.backup", target="<data>/backups", privilege="pemilik",
                                     evidence_to_collect="EvidenceArtifact kind RESTORE_TEST (trusted)"))
    return Outcome("VERIFIED", f"Bukti restore {ev.id[:8]} pada {ev.created_at.isoformat()}.",
                   evidence={"evidence_id": ev.id, "level": ev.evidence_level})


@check("recovery.leases", "RECOVERY", "Lease kedaluwarsa direkonsiliasi", component_id="workforce-scheduler",
       needs_db=True, ttl_seconds=900, severity_fail="HIGH")
def recovery_leases(ctx: CheckContext) -> Outcome:
    from aeos.models.workforce import Lease

    assert ctx.session is not None
    stale = ctx.session.scalar(select(func.count()).select_from(Lease).where(Lease.state == "ACTIVE",
                                                                             Lease.expires_at < ctx.now)) or 0
    active = ctx.session.scalar(select(func.count()).select_from(Lease).where(Lease.state == "ACTIVE")) or 0
    evidence = {"active_leases": int(active), "expired_but_active": int(stale)}
    if stale:
        return Outcome("FAILED", f"{stale} lease kedaluwarsa masih ACTIVE; jangan mulai duplikat sebelum rekonsiliasi.",
                       evidence=evidence, error_code="STALE_LEASES")
    return Outcome("VERIFIED", f"Tidak ada lease kedaluwarsa ({active} aktif).", evidence=evidence)


@check("recovery.publication_idempotency", "RECOVERY", "Pencegahan publikasi ganda terbukti", component_id="publisher",
       needs_db=True, ttl_seconds=86400, severity_fail="MEDIUM")
def recovery_publication_idempotency(ctx: CheckContext) -> Outcome:
    assert ctx.session is not None
    # MOCK/DEMO fixtures never count as proof (AGENTS.md invariant 7): LIVE trusted evidence only.
    ev = ctx.session.scalar(select(EvidenceArtifact).where(EvidenceArtifact.kind == "PUBLICATION_IDEMPOTENCY_TEST",
                                                           EvidenceArtifact.trusted.is_(True),
                                                           EvidenceArtifact.mode == "LIVE", _NOT_OFFICE)
                            .order_by(EvidenceArtifact.created_at.desc()).limit(1))
    if ev is None:
        return Outcome("UNKNOWN", "Pencegahan publikasi ganda (timeout/partial/replay) belum dibuktikan.",
                       error_code="IDEMPOTENCY_NOT_PROVEN", remedy=Remedy(
                           operation="Jalankan uji idempotensi publikasi (fake loopback; tanpa tenant/credential nyata)",
                           command=aeos_cmd("publish idempotency-test"), target="recovery.publication_idempotency",
                           evidence_to_collect="EvidenceArtifact PUBLICATION_IDEMPOTENCY_TEST (trusted) pada commit ini"))
    return Outcome("VERIFIED", f"Bukti uji idempotensi publikasi {ev.id[:8]}.", evidence={"evidence_id": ev.id})


@check("recovery.config_rollback", "RECOVERY", "Rilis controller sebelumnya untuk rollback",
       component_id="trusted-self-build-seed", ttl_seconds=86400, ok={"CONFIGURED", "VERIFIED"}, severity_fail="LOW")
def recovery_config_rollback(ctx: CheckContext) -> Outcome:
    d = ctx.settings.layout.released_controller
    releases = sorted(p.name for p in d.iterdir()) if d.is_dir() else []
    if not releases:
        return Outcome("MISSING", "Belum ada released controller artifact; rollback controller/konfigurasi belum tersedia.",
                       error_code="NO_RELEASE_ARTIFACT", severity="LOW")
    return Outcome("CONFIGURED", f"{len(releases)} rilis controller tersimpan untuk rollback.",
                   evidence={"releases": releases[-5:]})
