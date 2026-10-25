import { StatusBadge } from "../../components/StatusBadge";

/** §02/§08 CAN_PLAN_PERSONAL as recorded by the planning run (planning.service.personal_gate). */
export interface BridgeProof {
  capability?: string;
  status?: string;
  label?: string;
  reasons?: string[];
  checked_at?: string;
  /** "OPEN" when UNKNOWN still executes in this build: whether it may is the owner's decision (§02 Istilah) */
  owner_decision?: string;
  note?: string;
}

/** §02/§23 per stage: the supervisor route configuration a stage ran on that has not been evaluated. */
export type SupervisorUnevaluated = Record<
  string,
  { route_key: string; config_hash?: string | null; resolved_version?: string | null; harness_version?: string | null; note?: string | null }
>;

export function SupervisorConfigNotice({ unevaluated }: { unevaluated?: SupervisorUnevaluated | null }) {
  const entries = Object.entries(unevaluated ?? {});
  if (entries.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs" aria-label="Konfigurasi supervisor belum dievaluasi">
      <StatusBadge status="STALE" label="Konfigurasi supervisor belum dievaluasi" />
      <span className="text-ink-3">
        {entries
          .map(([stage, e]) => `${stage}: ${e.route_key}${e.resolved_version ? ` (${e.resolved_version})` : ""}${e.harness_version ? ` · Claude Code ${e.harness_version}` : ""}`)
          .join("; ")}
        . Versi berubah dan belum dievaluasi; rencana tetap berjalan pada versi baru — keputusan pemilik diperlukan.
      </span>
    </div>
  );
}

const TEXT: Record<string, { status: string; label: string; note: string }> = {
  PROVEN: {
    status: "VERIFIED",
    label: "Bridge Claude lokal terbukti",
    note: "CAN_PLAN_PERSONAL READY saat run dimulai.",
  },
  "NOT-PROVEN": {
    status: "NOT-PROVEN",
    label: "Integrasi Claude lokal belum diuji",
    note: "Readiness CAN_PLAN_PERSONAL belum pernah diperiksa: run memakai Claude Code resmi tetapi tidak dinyatakan otomatis/terbukti. Jalankan aeos doctor dan uji kapabilitas Claude.",
  },
  MANUAL_PACKAGE: {
    status: "DEGRADED",
    label: "Paket manual (bridge belum terbukti)",
    note: "CAN_PLAN_PERSONAL DEGRADED: setiap tahap memakai paket manual Claude Code resmi, tanpa Claude API.",
  },
  BLOCKED: {
    status: "BLOCKED",
    label: "Perencanaan diblokir sebelum panggilan model",
    note: "CAN_PLAN_PERSONAL BLOCKED: tidak ada panggilan model sampai penyebabnya diperbaiki.",
  },
  RECHECK_REQUIRED: {
    status: "STALE",
    label: "Pemeriksaan kritis usang — cek ulang dulu",
    note: "Pemeriksaan kritis CAN_PLAN_PERSONAL berubah sejak lulus (STALE): tidak ada panggilan model sampai Readiness Doctor memeriksanya ulang (aeos doctor).",
  },
};

export function BridgeProofNotice({ proof }: { proof?: BridgeProof | null }) {
  if (!proof || !proof.label) return null;
  const t = TEXT[proof.label] ?? { status: "UNKNOWN", label: `Status bridge: ${proof.label}`, note: "Status tidak dikenal; tidak dihitung sebagai terbukti." };
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs" aria-label="Bukti integrasi Claude lokal">
      <StatusBadge status={t.status} label={t.label} />
      <span className="text-ink-3">{t.note}</span>
      {(proof.reasons ?? []).length > 0 && <span className="text-ink-3">Alasan: {(proof.reasons ?? []).slice(0, 3).join("; ")}</span>}
      {proof.owner_decision === "OPEN" && <StatusBadge status="NEEDS_DECISION" label="Keputusan pemilik terbuka: UNKNOWN tetap dieksekusi" />}
    </div>
  );
}
