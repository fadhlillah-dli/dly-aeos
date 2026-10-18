// Runtime/acceptance presentation for DAG nodes. Acceptance is shown only from AcceptanceRecord —
// never inferred from execution state or external issue status (§14, §50).

export function runtimeBadge(state: string): { badge: string; label: string } {
  if (state === "NOT_SCHEDULED") return { badge: "NOT_CHECKED", label: "Belum dijadwalkan" };
  const labels: Record<string, string> = {
    QUEUED: "Antre",
    READY: "Siap",
    LEASED: "Di-lease",
    RUNNING: "Berjalan",
    REVIEW: "Menunggu review/bukti",
    BLOCKED: "Terblokir",
    DONE: "Selesai (diterima)",
    FAILED: "Gagal",
    CANCELLED: "Dibatalkan",
  };
  // "READY" in StatusBadge means capability-ready; for tasks use a neutral queued tone instead.
  return { badge: state === "READY" ? "QUEUED" : state, label: labels[state] ?? state };
}

export function acceptanceBadge(a: { status: string; valid: boolean | null }): { badge: string; label: string } {
  if (a.status === "ACCEPTED" && a.valid !== false) return { badge: a.valid ? "ACCEPTED" : "UNVERIFIED", label: a.valid ? "Diterima" : "Diterima (validitas belum diperiksa)" };
  if (a.status === "ACCEPTED") return { badge: "STALE", label: "Penerimaan usang" };
  if (a.status === "NONE") return { badge: "NOT_TESTED", label: "Belum ada penerimaan" };
  if (a.status === "UNKNOWN") return { badge: "UNKNOWN", label: "Penerimaan tidak diketahui" };
  return { badge: a.status, label: a.status };
}
