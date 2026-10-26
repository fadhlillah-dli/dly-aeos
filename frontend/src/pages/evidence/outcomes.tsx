import { StatusBadge } from "../../components/StatusBadge";
import { Status } from "./labels";

/**
 * Delivery / verifier outcome codes the generic badge does not know (deployment observation parts, artifact digests,
 * promotion, rollback criteria, enforcement test, impact checks, result seals, project baseline). Each maps to a
 * known badge for tone (text + icon + colour) with an Indonesian label. Anything that is not proven never gets a pass
 * tone (§35); unknown codes fall back to the evidence page's Status (muted, never a pass).
 */
const OUTCOMES: Record<string, { status: string; label: string }> = {
  // artifact digest vs provider-CI ARTIFACT_DIGEST (build once, §31) and promotion targets
  MATCH: { status: "PASS", label: "Cocok dengan digest CI" },
  MISMATCH: { status: "FAILED", label: "Digest berbeda" },
  NOT_REPORTED: { status: "NOT-PROVEN", label: "Digest tidak dilaporkan target" },
  NOT_RECORDED: { status: "NOT-PROVEN", label: "Digest CI belum tercatat" },
  NO_OBSERVATION: { status: "NOT-PROVEN", label: "Belum ada observasi" },
  NO_ARTIFACTS: { status: "NOT-PROVEN", label: "Tidak ada artefak CI" },
  RECORDED: { status: "CONFIGURED", label: "Tercatat" },
  // product SLIs: a threshold is an owner value; without it the value is report-only (never "within target")
  WITHIN_TARGET: { status: "PASS", label: "Dalam ambang pemilik" },
  BREACH: { status: "FAILED", label: "Melewati ambang pemilik" },
  NO_TARGET: { status: "NOT_CHECKED", label: "Tanpa ambang pemilik (hanya laporan)" },
  // owner decisions that do not exist yet
  UNDECIDED: { status: "NEEDS_DECISION", label: "Belum diputuskan pemilik" },
  // post-release recommendation (a human rolls back; AEOS never does)
  ROLLBACK_RECOMMENDED: { status: "FAILED", label: "Rollback manual direkomendasikan (oleh manusia)" },
  CRITERIA_NOT_MET: { status: "NEEDS_DECISION", label: "Kriteria rollback tidak terpenuhi — pemilik memutuskan" },
  // provider enforcement test (§31 direct bypass test)
  CLAIMED: { status: "UNVERIFIED", label: "Diklaim pemilik (bukan observasi provider)" },
  OBSERVED: { status: "CONFIGURED", label: "Penolakan teramati di provider" },
  NOT_OBSERVED: { status: "NOT-PROVEN", label: "Tidak ada penolakan teramati" },
  // impact checks: executed means PASS or FAIL ran, never a pass on its own
  EXECUTED: { status: "CONFIGURED", label: "Dijalankan verifier (lihat status cek)" },
  // project test baseline
  TIMED_OUT: { status: "DEGRADED", label: "Waktu habis (tercatat apa adanya)" },
  TAMPERED: { status: "BLOCKED", label: "Tamper terdeteksi" },
  HUMAN_ONLY: { status: "HUMAN_REQUIRED", label: "Hanya manusia" },
};

export function outcomeOf(status: string | null | undefined): { status: string; label: string } | undefined {
  return status ? OUTCOMES[status] : undefined;
}

/** Outcome badge: the delivery/verifier codes above, else the evidence page's Status mapping. */
export function Outcome({ status, label }: { status: string | null | undefined; label?: string }) {
  const m = outcomeOf(status);
  if (m) return <StatusBadge status={m.status} label={label ?? m.label} title={status ?? undefined} />;
  return <Status status={status} label={label} />;
}

/** Verifier check names (``CheckOutcome.name``) with their Indonesian labels; the raw name stays visible beside it. */
export const CHECK_LABELS: Record<string, string> = {
  trusted_suite: "Suite acceptance tepercaya",
  trusted_suite_integrity: "Integritas suite tepercaya",
  candidate_tests: "Tes kandidat",
  baseline: "Baseline tes pada commit dasar",
  ruff: "Lint (ruff)",
  scope: "Scope perubahan",
  leak_scan: "Pemindaian kebocoran secret",
  snapshot: "Snapshot kandidat",
  migration_rollback: "Round-trip migrasi & rollback data",
  db_credentials: "Credential migrasi terpisah dari runtime",
  contract_diff: "Diff kontrak (facade/OpenAPI)",
  contract_tests: "Tes kontrak terpilih",
  negative_tests: "Tes negatif (penolakan/izin)",
  adversarial_pack: "Paket adversarial verifier",
  integration_tests: "Tes integrasi (tool nyata)",
  dependency_diff: "Diff dependensi",
  dependency_env: "Suite di lingkungan dependensi kandidat",
  frontend_tests: "Tes frontend (vitest, typecheck, build)",
  ui_a11y: "State UI & aksesibilitas",
  result_seal: "Segel hasil per langkah",
  controller_integrity: "Integritas controller",
  trust_tables: "Tabel kepercayaan DB",
  harness_shadow: "Shadow harness",
  harness_tamper: "Tamper harness",
  step_containment: "Containment langkah",
};

/** Impact categories of a change (aeos.verification.scope.IMPACT_CATEGORIES). */
export const IMPACT_LABELS: Record<string, string> = {
  migration: "Migrasi skema/data",
  contract: "Kontrak",
  api: "API",
  auth: "Auth/izin",
  dependency: "Dependensi",
  frontend: "Frontend",
  config_flags: "Konfigurasi/flag",
  infrastructure: "Infrastruktur",
  ci: "CI",
  instructions: "Instruksi agent",
};

/** Failure conditions an observation reports; the owner names those that call for a (human) rollback. */
export const ROLLBACK_LABELS: Record<string, string> = {
  health_fail: "Health gagal",
  smoke_fail: "Smoke check gagal",
  critical_flow_fail: "Alur kritis gagal",
  sli_breach: "SLI melewati ambang",
  artifact_digest_mismatch: "Digest artefak berbeda",
};
