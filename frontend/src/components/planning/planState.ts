// Planning-state presentation (§14). Each planning state maps onto a StatusBadge code (text + icon +
// colour) with an Indonesian label; the raw state stays visible in the tooltip.

export interface StateSpec {
  badge: string; // StatusBadge status code that carries the tone + icon
  label: string;
  hint: string;
}

export const PLAN_STATES: Record<string, StateSpec> = {
  DRAFT: { badge: "PENDING", label: "Draf", hint: "Konteks sedang disusun." },
  CONTEXT_READY: { badge: "QUEUED", label: "Konteks siap", hint: "Siap menjalankan empat tahap perencanaan." },
  PRIMARY_PLANNING: { badge: "RUNNING", label: "Perencanaan utama (Opus)", hint: "Primary Planner menyusun proposal." },
  CHALLENGE_REVIEW: { badge: "RUNNING", label: "Review penantang (Opus)", hint: "Planning Challenger mencari kontradiksi dan risiko." },
  SYNTHESIS: { badge: "RUNNING", label: "Sintesis (Opus)", hint: "Planning Synthesizer membuat versi baru." },
  FINAL_REVIEW: { badge: "RUNNING", label: "Review akhir (Opus)", hint: "Final Planning Reviewer memberi REVIEW/VERIFY/VERDICT." },
  READY_FOR_HUMAN_REVIEW: { badge: "REVIEW", label: "Menunggu keputusan Anda", hint: "Tinjau versi, hash dan target lalu putuskan." },
  APPROVED: { badge: "APPROVED", label: "Disetujui", hint: "Versi tertentu disetujui (terikat hash)." },
  PUBLISHING: { badge: "RUNNING", label: "Sedang dipublikasikan", hint: "Publisher menulis dari versi yang disetujui." },
  PUBLISHED: { badge: "PUBLISHED", label: "Terpublikasi", hint: "Semua target publikasi selesai." },
  REVISION_REQUESTED: { badge: "PENDING", label: "Revisi diminta", hint: "Jalankan ulang perencanaan atau edit versi baru." },
  REJECTED: { badge: "REJECTED", label: "Ditolak", hint: "Rencana ditolak pemilik (final)." },
  PLANNING_FAILED: { badge: "FAILED", label: "Perencanaan gagal", hint: "Output tidak valid/model tidak sesuai policy/error." },
  REVIEW_FAILED: { badge: "FAILED", label: "Review gagal", hint: "Review gagal atau verdict FAIL." },
  NEEDS_DECISION: { badge: "BLOCKED", label: "Perlu keputusan", hint: "Berhenti di checkpoint; tidak ada tahap yang dilewati." },
  PUBLICATION_PARTIAL: { badge: "PARTIAL", label: "Publikasi sebagian", hint: "Sebagian target gagal; yang berhasil tetap tercatat." },
  PUBLICATION_FAILED: { badge: "FAILED", label: "Publikasi gagal", hint: "Tinjau penyebab sebelum retry." },
  AWAITING_MANUAL_CLAUDE: { badge: "REVIEW", label: "Menunggu Claude manual", hint: "Jalankan paket manual di Claude Code resmi lalu impor." },
};

export const NEEDS_HUMAN_STATES = new Set([
  "READY_FOR_HUMAN_REVIEW",
  "NEEDS_DECISION",
  "AWAITING_MANUAL_CLAUDE",
  "REVIEW_FAILED",
  "PLANNING_FAILED",
  "PUBLICATION_PARTIAL",
  "PUBLICATION_FAILED",
]);

export function planStateSpec(state: string | null | undefined): StateSpec {
  if (!state) return { badge: "UNKNOWN", label: "Tidak diketahui", hint: "" };
  return PLAN_STATES[state] ?? { badge: "UNKNOWN", label: state, hint: "" };
}

// PlanningRun.status -> StatusBadge code. Never maps a non-success to a pass-coloured code.
const RUN_STATUS: Record<string, { badge: string; label: string }> = {
  PENDING: { badge: "PENDING", label: "Belum berjalan" },
  RUNNING: { badge: "RUNNING", label: "Berjalan" },
  SUCCEEDED: { badge: "SUCCEEDED", label: "Berhasil (DATA, bukan persetujuan)" },
  FAILED: { badge: "FAILED", label: "Gagal" },
  OUTPUT_INVALID: { badge: "FAILED", label: "Output tidak valid" },
  MODEL_MISMATCH: { badge: "FAILED", label: "Model tidak sesuai policy" },
  QUOTA_EXHAUSTED: { badge: "BLOCKED", label: "Kuota habis — menunggu" },
  AWAITING_MANUAL: { badge: "REVIEW", label: "Menunggu hasil manual" },
  ABANDONED: { badge: "CANCELLED", label: "Ditinggalkan" },
};

export function runStatusSpec(status: string | null | undefined): { badge: string; label: string } {
  if (!status) return { badge: "NOT_CHECKED", label: "Belum berjalan" };
  return RUN_STATUS[status] ?? { badge: "UNKNOWN", label: status };
}

export const VERDICT_BADGE: Record<string, string> = {
  "PASS-STATIC": "PASS-STATIC",
  "NOT-PROVEN": "NOT-PROVEN",
  BLOCKED: "BLOCKED",
  FAIL: "FAIL",
};

export const STAGE_ORDER = ["PRIMARY_PLANNING", "CHALLENGE_REVIEW", "SYNTHESIS", "FINAL_REVIEW"] as const;

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms} ms`;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s} dtk`;
  const m = Math.floor(s / 60);
  return `${m} mnt ${s % 60} dtk`;
}

/** Operations an approval would bind — mirrors backend aeos.planning.service.operations_for. */
export function operationsFor(
  action: "APPROVE_AND_PUBLISH" | "APPROVE",
  targets: { provider: string }[],
  catalog: { publish: Record<string, string[]>; delivery: Record<string, string[]>; plan: string[] },
): string[] {
  const ops = [...catalog.plan];
  for (const t of targets) {
    if (action === "APPROVE_AND_PUBLISH") ops.push(...(catalog.publish[t.provider] ?? []));
    ops.push(...(catalog.delivery[t.provider] ?? []));
  }
  return Array.from(new Set(ops));
}
