import type { DocItem, OperationView } from "./types";

// Indonesian labels for statuses that the shared StatusBadge does not map (yet); the raw code stays in
// the badge tooltip. Colour/icon still come from StatusBadge (text + icon + colour, §35).
export const STATUS_LABELS: Record<string, string> = {
  NOT_STARTED: "Belum dimulai",
  NOT_APPLICABLE: "Tidak berlaku",
  NOT_PUBLISHED: "Belum dipublikasikan",
  SKIPPED: "Dilewati (tidak berubah)",
  IN_FLIGHT: "Sedang dikirim",
  SUPERSEDED: "Digantikan versi baru",
  DRAFT: "Draf",
  PREVIEW: "Preview (belum disetujui)",
  IN_PROGRESS: "Berjalan",
  NOT_RECORDED: "Belum tercatat",
  UPDATED: "Diperbarui",
  SYNCED: "Tersinkron",
  VISUAL_PENDING: "Visual tertunda",
  NOT_IMPLEMENTED: "Tidak diimplementasikan",
  NOT_SUPPORTED: "Tidak didukung",
  NOT_APPROVED: "Belum disetujui",
  PROJECTION_ONLY: "Proyeksi (bukan authority)",
  RENDERED: "Dirender (desain, bukan bukti runtime)",
  REVIEWED: "Direview manusia (hash ini)",
  REVIEW_STALE: "Review untuk konten lama",
  NOT_REVIEWED: "Belum direview manusia",
};

/** Provider URLs (Drive/Docs/GitHub/Confluence) are data from an external API: only http(s) becomes a link. */
export function safeHref(url: string | null | undefined): string | undefined {
  if (!url) return undefined;
  return /^https?:\/\//i.test(url.trim()) ? url.trim() : undefined;
}

/** Tracker status is a projection: closed/Done is shown neutrally, never as AEOS DONE/accepted (§14, §50). */
export function trackerStatusCode(status: string): string {
  if (status.startsWith("closed") || /^(done|selesai|resolved|closed)$/i.test(status)) return "TRACKER_CLOSED";
  if (status === "open") return "QUEUED";
  return status || "UNKNOWN";
}

export const CONTENT_TYPE_LABELS: Record<string, string> = {
  STATIC: "Statis",
  DYNAMIC: "Dinamis (sumber live)",
  AI_MAINTAINED: "Narasi AI (usulan)",
  HUMAN_GOVERNED: "Keputusan manusia",
};

export const DIMENSION_LABELS: Record<string, string> = {
  APPROVED: "Disetujui",
  IMPLEMENTED: "Diimplementasikan",
  ACTIVE: "Aktif (runtime)",
  VERIFIED: "Terverifikasi",
  ARCHITECTURE_SOURCE_UPDATED: "Sumber arsitektur diperbarui",
  DOCS_PUBLISHED: "Dokumen terpublikasi",
};

export const DIMENSION_ORDER = [
  "APPROVED",
  "IMPLEMENTED",
  "ACTIVE",
  "VERIFIED",
  "ARCHITECTURE_SOURCE_UPDATED",
  "DOCS_PUBLISHED",
] as const;

const ATTENTION = new Set(["PARTIAL", "CONFLICT", "UNKNOWN", "FAILED"]);

export function needsAttention(status: string): boolean {
  return ATTENTION.has(status);
}

export function label(status: string | null | undefined): string | undefined {
  return status ? STATUS_LABELS[status] : undefined;
}

export interface TreeNode {
  doc: DocItem;
  children: TreeNode[];
}

/** Build the Product 1 tree from flat DocumentRecords using parent_logical_id (folder → document). */
export function buildTree(items: DocItem[]): TreeNode[] {
  const byId = new Map<string, TreeNode>();
  const sorted = [...items].sort((a, b) => a.order_index - b.order_index || a.logical_doc_id.localeCompare(b.logical_doc_id));
  for (const d of sorted) byId.set(d.logical_doc_id, { doc: d, children: [] });
  const roots: TreeNode[] = [];
  for (const d of sorted) {
    const node = byId.get(d.logical_doc_id)!;
    const parent = d.parent_logical_id ? byId.get(d.parent_logical_id) : undefined;
    if (parent) parent.children.push(node);
    else roots.push(node);
  }
  return roots;
}

export function opSummary(ops: OperationView[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const o of ops) out[o.status] = (out[o.status] ?? 0) + 1;
  return out;
}

export function shortId(id: string | null | undefined, n = 8): string {
  if (!id) return "—";
  return id.length > n ? id.slice(0, n) : id;
}
