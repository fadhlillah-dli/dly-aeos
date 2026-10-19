import { StatusBadge } from "../../components/StatusBadge";

/** §02/§08 CAN_PLAN_PERSONAL as recorded by the planning run (planning.service.personal_gate). */
export interface BridgeProof {
  capability?: string;
  status?: string;
  label?: string;
  reasons?: string[];
  checked_at?: string;
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
};

export function BridgeProofNotice({ proof }: { proof?: BridgeProof | null }) {
  if (!proof || !proof.label) return null;
  const t = TEXT[proof.label] ?? { status: "UNKNOWN", label: `Status bridge: ${proof.label}`, note: "Status tidak dikenal; tidak dihitung sebagai terbukti." };
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs" aria-label="Bukti integrasi Claude lokal">
      <StatusBadge status={t.status} label={t.label} />
      <span className="text-ink-3">{t.note}</span>
      {(proof.reasons ?? []).length > 0 && <span className="text-ink-3">Alasan: {(proof.reasons ?? []).slice(0, 3).join("; ")}</span>}
    </div>
  );
}
