import { clsx } from "clsx";
import { StatusBadge } from "../../components/StatusBadge";

// Statuses that StatusBadge does not know are mapped to a known status for tone (text + icon +
// colour) with an Indonesian label. Unknown keys render muted — never as a pass (§35).
const MAPPED: Record<string, { status: string; label: string }> = {
  QUARANTINED: { status: "BLOCKED", label: "Dikarantina — review khusus" },
  NOT_ENFORCEABLE: { status: "DEGRADED", label: "Tidak dapat ditegakkan" },
  NOT_CONFIGURED: { status: "DEGRADED", label: "Belum dikonfigurasi" },
  WAITING_QUOTA: { status: "DEGRADED", label: "Menunggu kuota" },
  NEEDS_DECISION: { status: "NOT-PROVEN", label: "Perlu review manual" },
  COMPLETED: { status: "REVIEW_COMPLETED", label: "Review selesai" },
  NO_CHECKS: { status: "NOT_TESTED", label: "Belum ada CI" },
  NO_EVIDENCE: { status: "NOT_TESTED", label: "Belum ada bukti" },
  NOT_RUN: { status: "NOT_TESTED", label: "Tidak dijalankan" },
  INVALIDATED: { status: "STALE", label: "Digantikan" },
  OPEN: { status: "PR_OPEN", label: "PR terbuka — menunggu merge manusia" },
  PREPARED: { status: "PENDING", label: "PR disiapkan" },
  MERGED: { status: "PR_MERGED", label: "Di-merge (lihat G7)" },
  CLOSED: { status: "CANCELLED", label: "Ditutup" },
  HANDED_OFF: { status: "CONFIGURED", label: "Diserahkan" },
  AVAILABLE: { status: "CONFIGURED", label: "Tersedia" },
  UNTRUSTED_CLAIM: { status: "UNVERIFIED", label: "Klaim (tidak dihitung)" },
  // §20 verifier isolation: process-level only (same OS user, no container/egress enforcement)
  WEAK: { status: "DEGRADED", label: "Isolasi verifier lemah (proses, bukan sandbox)" },
  // §20 isolated runner: candidate code ran in the verifier container on a host whose containment PASSed
  // (a kernel boundary, not a VM): configured, never shown as a stronger "verified" claim
  CONTAINER: { status: "CONFIGURED", label: "Isolasi verifier: container (boundary terbukti di host ini)" },
};

export function Status({ status, label }: { status: string | null | undefined; label?: string }) {
  if (!status) return <StatusBadge status="UNKNOWN" label={label ?? "Belum ada"} />;
  const m = MAPPED[status];
  return <StatusBadge status={m ? m.status : status} label={label ?? m?.label} />;
}

export const LEVEL_SHORT: Record<string, string> = {
  E0: "klaim",
  E1: "statis",
  E2: "uji terkendali",
  E3: "observasi runtime",
  E4: "terlindungi regresi",
};

export function LevelBadge({ level, explanation }: { level: string; explanation?: string }) {
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-xs font-medium whitespace-nowrap",
        level === "E0" ? "border-warn/40 text-warn" : "border-line text-ink-2",
      )}
      title={explanation}
    >
      <span className="mono">{level}</span>
      <span>· {LEVEL_SHORT[level] ?? "?"}</span>
    </span>
  );
}

export function TrustBadge({ trusted }: { trusted: boolean }) {
  return trusted ? (
    <StatusBadge status="CONFIGURED" label="Kolektor tepercaya" />
  ) : (
    <StatusBadge status="UNVERIFIED" label="Tidak tepercaya (input)" />
  );
}

export const GATE_HELP: Record<string, string> = {
  G0: "Versi rencana, otorisasi, target dan scope benar (persetujuan manusia terikat hash).",
  G1: "Lint/type/secret scan dari CI provider; pemeriksaan statis verifier sebagai pendukung.",
  G2: "Suite terpercaya dijalankan verifier independen pada commit ini.",
  G3: "Job produk (frontend/UI) dari CI provider.",
  G4: "Review checkpoint Claude + bukti independen yang terikat artefak yang sama.",
  G5: "Retest pada merged state setelah merge oleh manusia.",
  G6: "Staging berizin — target staging terdaftar pemilik (deployment-target) + observasi runtime commit ini.",
  G7: "Merge oleh manusia yang tercatat sebagai approver rilis (keputusan POLICY release_approvers; diamati dari GitHub merged_by).",
  G8: "Observasi pasca-rilis pada target produksi terdaftar; AEOS tidak pernah deploy.",
};
