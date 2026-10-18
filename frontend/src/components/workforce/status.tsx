import { StatusBadge } from "../StatusBadge";
import type { RunStatuses } from "./types";

// Each observed run field keeps its own badge (§35): worker-reported, verifier, review,
// acceptance and merged are NEVER folded into one check mark. Unknown/absent values render as
// "belum/ tidak diketahui", never as pass.

type Spec = { status: string; label: string };

const WORKER: Record<string, Spec> = {
  EXITED_0: { status: "PENDING", label: "Worker: exit 0 (belum terbukti)" },
  UNKNOWN: { status: "UNKNOWN", label: "Worker: tidak diketahui" },
};
const VERIFIER: Record<string, Spec> = {
  NOT_RUN: { status: "NOT_TESTED", label: "Verifier: belum dijalankan" },
  RUNNING: { status: "RUNNING", label: "Verifier: berjalan" },
  // The verifier's own vocabulary (ADR-0060 PASS/FAIL/QUARANTINED) — PASSED/FAILED kept for older rows.
  PASS: { status: "PASS", label: "Verifier: lulus" },
  FAIL: { status: "FAILED", label: "Verifier: gagal" },
  QUARANTINED: { status: "BLOCKED", label: "Verifier: karantina (review khusus)" },
  PASSED: { status: "PASS", label: "Verifier: lulus" },
  FAILED: { status: "FAILED", label: "Verifier: gagal" },
  ERROR: { status: "FAILED", label: "Verifier: error" },
  NOT_PROVEN: { status: "NOT-PROVEN", label: "Verifier: belum terbukti" },
  BLOCKED: { status: "BLOCKED", label: "Verifier: terblokir" },
};
const ACCEPTANCE: Record<string, Spec> = {
  PENDING: { status: "PENDING", label: "Acceptance: belum" },
  ACCEPTED: { status: "ACCEPTED", label: "Acceptance: diterima" },
  REJECTED: { status: "REJECTED", label: "Acceptance: ditolak" },
  STALE: { status: "STALE", label: "Acceptance: usang" },
  INVALIDATED: { status: "STALE", label: "Acceptance: dibatalkan" },
};
const MERGED: Record<string, Spec> = {
  NOT_MERGED: { status: "PENDING", label: "Merge: belum (manusia)" },
  PR_OPEN: { status: "PENDING", label: "Merge: PR terbuka" },
  MERGED: { status: "DONE", label: "Merge: oleh manusia" },
  RELEASED: { status: "DONE", label: "Rilis: oleh manusia" },
  UNKNOWN: { status: "UNKNOWN", label: "Merge: tidak diketahui" },
};

function pick(map: Record<string, Spec>, value: string | null | undefined, prefix: string): Spec {
  if (!value) return { status: "UNKNOWN", label: `${prefix}: belum ada data` };
  return map[value] ?? { status: value, label: `${prefix}: ${value}` };
}

export function reviewSpec(value: string | null | undefined): Spec {
  if (!value) return { status: "UNKNOWN", label: "Review: belum ada data" };
  if (value === "PENDING") return { status: "PENDING", label: "Review: menunggu" };
  return { status: value, label: `Review: ${value}` };
}

export function workerSpec(v: string | null | undefined): Spec {
  if (v && v.startsWith("EXITED_") && v !== "EXITED_0") return { status: "FAILED", label: `Worker: exit ${v.slice(7)}` };
  return pick(WORKER, v, "Worker");
}
export const verifierSpec = (v: string | null | undefined) => pick(VERIFIER, v, "Verifier");
export const acceptanceSpec = (v: string | null | undefined) => pick(ACCEPTANCE, v, "Acceptance");
export const mergedSpec = (v: string | null | undefined) => pick(MERGED, v, "Merge");

export function SeparateStatuses({ statuses, compact = false }: { statuses: RunStatuses; compact?: boolean }) {
  const items = [
    workerSpec(statuses.worker_reported),
    verifierSpec(statuses.verifier),
    reviewSpec(statuses.review),
    acceptanceSpec(statuses.acceptance),
    mergedSpec(statuses.merged),
  ];
  return (
    <ul className={compact ? "flex flex-col gap-1" : "flex flex-wrap gap-1.5"} aria-label="Status terpisah">
      {items.map((s) => (
        <li key={s.label}>
          <StatusBadge status={s.status} label={s.label} />
        </li>
      ))}
    </ul>
  );
}

const UI_STATE: Record<string, Spec> = {
  WAITING_QUOTA: { status: "EXPIRED", label: "Menunggu kuota" },
  WAITING_REVIEW: { status: "REVIEW", label: "Menunggu review" },
  STALE: { status: "STALE", label: "Heartbeat usang" },
};

/** Canonical §14 execution state, plus the derived UI state when it differs. */
export function ExecState({ state, uiState }: { state: string; uiState?: string | null }) {
  const derived = uiState && uiState !== state ? UI_STATE[uiState] ?? { status: uiState, label: uiState } : null;
  return (
    <div className="flex flex-col items-start gap-1">
      <StatusBadge status={state} />
      {derived && <StatusBadge status={derived.status} label={`Tampilan: ${derived.label}`} />}
    </div>
  );
}

export function ModeBadge({ mode }: { mode: string }) {
  if (mode === "LIVE") return null;
  return <StatusBadge status={mode} label={mode === "DEMO" ? "DEMO (fixture)" : mode} />;
}

export function costText(cost: number | "UNKNOWN" | null | undefined, requests?: number | null): string {
  if (cost === "UNKNOWN") return "UNKNOWN";
  if (cost === null || cost === undefined) return requests ? "UNKNOWN" : "—";
  return `$${cost.toFixed(cost < 1 ? 4 : 2)}`;
}

export function shortId(id: string | null | undefined, n = 8): string {
  return id ? id.replace(/-/g, "").slice(0, n) : "—";
}
