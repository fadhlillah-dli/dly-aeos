import { StatusBadge } from "../StatusBadge";

/**
 * Operations / Control Center statuses mapped onto the shared StatusBadge vocabulary so every
 * status keeps text + icon + colour (§35). Projection-specific codes get an Indonesian label and a
 * tone that never turns "unknown"/"not proven"/"no target" into a pass.
 */
const MAP: Record<string, [string, string]> = {
  NONE_RECORDED: ["NOT_APPLICABLE", "Tidak ada yang tercatat"],
  RECORDED: ["CONFIGURED", "Tercatat"],
  NO_TARGET: ["UNVERIFIED", "Target belum ditetapkan pemilik"],
  WITHIN_TARGET: ["PASS", "Dalam target pemilik"],
  BREACH: ["FAILED", "Di luar target"],
  RESTORE_TESTED: ["VERIFIED", "Restore teruji"],
  NOT_RESTORE_TESTED: ["NOT-PROVEN", "Restore belum diuji"],
  RESTORE_TEST_FAILED: ["FAILED", "Restore test gagal"],
  RESTORE_TEST_UNRECORDED: ["UNVERIFIED", "Lulus, bukti belum tercatat"],
  INCOMPLETE: ["FAILED", "Tidak lengkap"],
  PASSED: ["PASS", "Lulus"],
  PASSED_UNRECORDED: ["UNVERIFIED", "Lulus tanpa bukti tercatat"],
  PAUSED: ["DEGRADED", "Dijeda"],
  COMPLETED: ["SUCCEEDED", "Selesai"],
  SKIPPED: ["NOT_APPLICABLE", "Dilewati"],
  SUCCESS: ["SUCCEEDED", "Sukses"],
  OPEN: ["EXPIRED", "Terbuka"],
  RESOLVED: ["DONE", "Selesai"],
  ACTIVE: ["LIVE", "Aktif"],
  PREVIOUS: ["IDLE", "Sebelumnya"],
  AVAILABLE: ["CANDIDATE", "Tersedia"],
  OK: ["VERIFIED", "Hash cocok"],
  MISMATCH: ["FAILED", "Hash tidak cocok"],
  MIXED: ["MOCK", "Campuran LIVE + MOCK/DEMO"],
  SNAPSHOT: ["ESTIMATE", "Snapshot (bukan live)"],
  NOT_REGISTERED: ["UNVERIFIED", "Belum didaftarkan"],
  BLOCKING: ["BLOCKED", "Memblokir"],
  OWNER_ACTION: ["PENDING", "Perlu tindakan pemilik"],
  OWNER_DECISION_REQUIRED: ["PENDING", "Perlu keputusan pemilik"],
  PENDING_OWNER: ["PENDING", "Menunggu pemilik"],
  REVIEW_REQUIRED: ["PENDING", "Perlu ditinjau"],
  NOT_CONFIGURED: ["NOT_APPLICABLE", "Belum dikonfigurasi"],
  NOT_REQUIRED: ["NOT_APPLICABLE", "Tidak wajib"],
  NO_POLICY: ["UNVERIFIED", "Belum ada kebijakan retensi"],
  KEEP: ["CONFIGURED", "Disimpan"],
  LEGAL_HOLD: ["CONFIGURED", "Legal hold (tidak didisposal)"],
  DECIDED: ["CONFIGURED", "Diputuskan pemilik"],
};

export function opsBadgeSpec(status: string | null | undefined): { status: string; label?: string } {
  if (!status) return { status: "UNKNOWN" };
  if (status.startsWith("EXPIRED_BY_POLICY")) return { status: "EXPIRED", label: "Lewat retensi (hapus manual saja)" };
  const m = MAP[status];
  return m ? { status: m[0], label: m[1] } : { status };
}

export function OpsBadge({ status, label }: { status: string | null | undefined; label?: string }) {
  const spec = opsBadgeSpec(status);
  return <StatusBadge status={spec.status} label={label ?? spec.label} />;
}
