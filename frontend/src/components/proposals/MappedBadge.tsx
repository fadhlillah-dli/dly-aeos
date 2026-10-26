import { StatusBadge } from "../StatusBadge";

/**
 * Maps module-specific states onto the shared StatusBadge vocabulary so tone/icon stay honest:
 * e.g. a trial approval is rendered as "info", never as a green "accepted" (§35, §40).
 * Unknown values fall back to UNKNOWN (warning) — never to a pass.
 */
export type BadgeMap = Record<string, [base: string, label: string]>;

export function MappedBadge({ value, map, className }: { value: string | null | undefined; map: BadgeMap; className?: string }) {
  const hit = value ? map[value] : undefined;
  if (!hit) return <StatusBadge status="UNKNOWN" label={value ?? "UNKNOWN"} className={className} />;
  return <StatusBadge status={hit[0]} label={hit[1]} className={className} />;
}

export const PROPOSAL_STATUS: BadgeMap = {
  SUBMITTED: ["PENDING", "Diajukan"],
  UNDER_REVIEW: ["REVIEW", "Perlu review ulang"],
  REVIEWED: ["REVIEW", "Paket review siap — menunggu keputusan"],
  TRIAL_APPROVED: ["CONFIGURED", "Trial disetujui (bukan promosi)"],
  IN_TRIAL: ["RUNNING", "Trial berjalan"],
  PROMOTION_PENDING: ["REVIEW", "Bukti trial ada — menunggu keputusan promosi"],
  PROMOTION_APPROVED: ["CONFIGURED", "Promosi disetujui (hasil belum tercatat)"],
  ACCEPTED: ["ACCEPTED", "Diterima"],
  ROLLED_BACK: ["DEGRADED", "Di-rollback (bukan diterima)"],
  FAILED: ["FAILED", "Gagal"],
  PARTIAL: ["PARTIAL", "Sebagian"],
  KEEP: ["NOT_APPLICABLE", "Pertahankan baseline"],
  REJECTED: ["REJECTED", "Ditolak"],
  DEFERRED: ["PENDING", "Ditunda"],
  REQUEST_MORE_EVIDENCE: ["NOT-PROVEN", "Minta bukti tambahan"],
  REFERENCE_ONLY: ["NOT_APPLICABLE", "Referensi saja"],
};

export const RESEARCH_LIFECYCLE: BadgeMap = {
  DISCOVERED: ["NOT_CHECKED", "Ditemukan"],
  SOURCE_VERIFIED: ["CANDIDATE", "Sumber tercatat (kandidat)"],
  CONTROLLED_EVAL: ["RUNNING", "Evaluasi terkendali"],
  CANARY: ["REVIEW", "Canary"],
  RECOMMENDED_FOR_PROMOTION: ["REVIEW", "Direkomendasikan untuk promosi"],
  REJECTED: ["REJECTED", "Ditolak"],
  DEFERRED: ["PENDING", "Ditunda"],
  ENABLED: ["ENABLED", "Diaktifkan (keputusan sah)"],
  REFERENCE_ONLY: ["NOT_APPLICABLE", "Referensi saja"],
};

export const IDENTITY_STATUS: BadgeMap = {
  CHECKED: ["CONFIGURED", "Identitas dicek live"],
  VERIFIED_LIVE: ["CONFIGURED", "Identitas dicek live"],
  STATED_BY_MASTER: ["PENDING", "Dinyatakan master (belum dicek)"],
  CONFIGURED: ["CONFIGURED", "Dari konfigurasi (belum diverifikasi)"],
  UNVERIFIED: ["UNVERIFIED", "Belum diverifikasi"],
  CHANGED: ["DRIFTED", "Identitas berubah — trust tidak diwarisi"],
  NOT_FOUND: ["MISSING", "Tidak ditemukan"],
  INTERNAL: ["NOT_APPLICABLE", "Internal DLY AEOS"],
};

export const UPSTREAM_STATUS: BadgeMap = {
  NOT_CHECKED: ["NOT_CHECKED", "Belum dicek"],
  OK: ["CONFIGURED", "Metadata terbaca"],
  IDENTITY_CHANGED: ["DRIFTED", "Identitas berubah"],
  FORK: ["DRIFTED", "Fork, bukan upstream"],
  ARCHIVED: ["STALE", "Diarsipkan upstream"],
  NOT_FOUND: ["MISSING", "Tidak ditemukan"],
  RATE_LIMITED: ["DEGRADED", "Rate limit — coba lagi"],
  ERROR: ["FAILED", "Gagal dicek"],
};

export const BUDGET_STATE: BadgeMap = {
  OK: ["READY", "Tersedia"],
  EXHAUSTED: ["BLOCKED", "Habis (hard stop)"],
  HARD_STOP_ZERO: ["BLOCKED", "$0 hard stop — hanya rute gratis"],
  UNTRUSTED: ["UNKNOWN", "Tidak tepercaya — admission ditolak"],
};

export const QUOTA_STATE: BadgeMap = {
  OK: ["READY", "OK"],
  LOW: ["DEGRADED", "Rendah"],
  EXHAUSTED: ["BLOCKED", "Habis"],
  UNKNOWN: ["UNKNOWN", "Tidak diketahui"],
};

export const COST_KIND: BadgeMap = {
  ACTUAL: ["LIVE", "Aktual"],
  ESTIMATE: ["ESTIMATE", "Estimasi"],
  UNKNOWN: ["UNKNOWN", "UNKNOWN"],
  SUBSCRIPTION: ["NOT_APPLICABLE", "Langganan (bukan dolar)"],
  // Non-LIVE ledger rows (fixtures) are never shown as actual usage (§50).
  MOCK: ["MOCK", "MOCK — bukan usage aktual"],
  DEMO: ["DEMO", "DEMO — bukan usage aktual"],
  DESIGN: ["DESIGN", "DESIGN — bukan usage aktual"],
};

export const ELIGIBILITY: BadgeMap = {
  ELIGIBLE: ["PASS", "Lulus gate"],
  EXCLUDED_GATE_FAILED: ["FAIL", "Dikecualikan: gate gagal"],
  EXCLUDED_GATE_NOT_PROVEN: ["NOT-PROVEN", "Dikecualikan: gate belum terbukti"],
  EXCLUDED_REGRESSION: ["FAIL", "Dikecualikan: regresi kritis"],
  // §34: no verifier baseline proved the absence of a critical regression (UNKNOWN is never a pass)
  EXCLUDED_REGRESSION_NOT_PROVEN: ["NOT-PROVEN", "Dikecualikan: bebas regresi kritis belum terbukti"],
  EXCLUDED_ROUTE_TERMS: ["FAIL", "Dikecualikan: rute tidak eligible (terms/data)"],
  EXCLUDED_STALE_CONFIGURATION: ["STALE", "Dikecualikan: konfigurasi rute sudah berubah"],
  BELUM_DIUJI: ["NOT_TESTED", "BELUM DIUJI"],
  RANKED: ["CONFIGURED", "Ada hasil tercatat"],
  NO_ELIGIBLE: ["NOT-PROVEN", "Tidak ada kandidat lulus gate"],
};

export const IMPACT_STATUS: BadgeMap = {
  KNOWN: ["CONFIGURED", "Terpetakan dari inventaris"],
  UNKNOWN: ["UNKNOWN", "UNKNOWN"],
};
