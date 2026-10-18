"""§08 Recovery: lease/queue restart, duplicate publication prevention, backups/restore, config rollback.

A backup that was never restored is not "recovery ready" (§33, §50 "Restore backup belum pernah diuji").
"""

from __future__ import annotations

from sqlalchemy import func, select

from aeos.doctor.checks import check
from aeos.doctor.model import CheckContext, Outcome, Remedy


@check("recovery.backups", "RECOVERY", "Backup database tersedia", component_id="operations-reliability",
       ttl_seconds=3600, ok={"CONFIGURED", "VERIFIED"}, severity_fail="MEDIUM")
def recovery_backups(ctx: CheckContext) -> Outcome:
    d = ctx.settings.layout.backups
    files = sorted(d.glob("*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True) if d.is_dir() else []
    if not files:
        return Outcome("MISSING", "Belum ada backup database di <data>/backups.", error_code="NO_BACKUP",
                       remedy=Remedy(operation="Buat backup SQLite konsisten (runbook operations); backup pra-upgrade "
                                     "dibuat otomatis oleh aeos db upgrade saat ada migrasi",
                                     target="<data>/backups", privilege="pengguna lokal",
                                     evidence_to_collect="file backup + uji restore"))
    from datetime import UTC, datetime

    newest = datetime.fromtimestamp(files[0].stat().st_mtime, UTC)
    return Outcome("CONFIGURED", f"{len(files)} backup; terbaru {newest.isoformat()} (restore belum tentu diuji).",
                   evidence={"count": len(files), "newest_at": newest.isoformat(), "location": "<data>/backups"})


@check("recovery.restore_test", "RECOVERY", "Restore pernah diuji", component_id="operations-reliability",
       needs_db=True, ttl_seconds=86400, severity_fail="MEDIUM", observation="functional")
def recovery_restore_test(ctx: CheckContext) -> Outcome:
    from aeos.models.evidence import EvidenceArtifact

    assert ctx.session is not None
    ev = ctx.session.scalar(select(EvidenceArtifact).where(EvidenceArtifact.kind == "RESTORE_TEST",
                                                           EvidenceArtifact.trusted.is_(True),
                                                           EvidenceArtifact.mode == "LIVE")
                            .order_by(EvidenceArtifact.created_at.desc()).limit(1))
    if ev is None:
        return Outcome("MISSING", "Restore belum diuji; recovery tidak diklaim siap.", error_code="RESTORE_NOT_TESTED",
                       remedy=Remedy(operation="Jalankan uji restore ke lokasi terpisah dan simpan bukti RESTORE_TEST",
                                     target="<data>/backups", privilege="pemilik",
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
    from aeos.models.evidence import EvidenceArtifact

    assert ctx.session is not None
    ev = ctx.session.scalar(select(EvidenceArtifact).where(EvidenceArtifact.kind == "PUBLICATION_IDEMPOTENCY_TEST",
                                                           EvidenceArtifact.trusted.is_(True))
                            .order_by(EvidenceArtifact.created_at.desc()).limit(1))
    if ev is None:
        return Outcome("UNKNOWN", "Pencegahan publikasi ganda (timeout/partial/replay) belum dibuktikan.",
                       error_code="IDEMPOTENCY_NOT_PROVEN")
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
