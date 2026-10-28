import { StatusBadge } from "../StatusBadge";

/**
 * Inventory/Doctor vocabularies that StatusBadge does not know yet are mapped onto an existing status
 * with the same meaning/tone and an Indonesian label (§35: text + icon + colour; UNKNOWN never green).
 * The §38 admission / installation / configuration / lifecycle / design-label values have their own StatusBadge
 * spec (tests enumerate the backend enums); only vocabularies that are not backend enums stay here.
 */
const ALIASES: Record<string, { as: string; label: string }> = {
  MISMATCH: { as: "DRIFTED", label: "Berbeda dari pin" },
  MATCH: { as: "VERIFIED", label: "Sesuai pin" },
  NO_PIN: { as: "UNKNOWN", label: "Pin belum disetujui" },
  NO_APPROVED_DIGEST: { as: "UNKNOWN", label: "Digest belum disetujui untuk platform ini" },
  INVALID_APPROVED_DIGEST: { as: "FAILED", label: "Digest yang disetujui tidak valid" },
  UNREGISTERED: { as: "UNVERIFIED", label: "Tidak terdaftar" },
  DECLARED: { as: "CONFIGURED", label: "Dideklarasikan (lockfile/konfigurasi)" },
  ACTIVE: { as: "IDLE", label: "Belum dipensiunkan" },
  QUARANTINED: { as: "BLOCKED", label: "Dikarantina" },
  RETIRING: { as: "STALE", label: "Sedang dipensiunkan" },
  RETIRED: { as: "CANCELLED", label: "Dipensiunkan" },
  OBSERVED: { as: "CONNECTED", label: "Teramati" },
  HUMAN_ONLY: { as: "DISABLED", label: "Khusus manusia" },
  NOT_DECLARED: { as: "NOT_APPLICABLE", label: "Tidak dideklarasikan" },
};

export function badgeProps(status: string | null | undefined): { status: string | null | undefined; label?: string } {
  if (!status) return { status };
  const a = ALIASES[status];
  return a ? { status: a.as, label: a.label } : { status };
}

export function Badge({ status, className, label }: { status: string | null | undefined; className?: string; label?: string }) {
  const p = badgeProps(status);
  // Aliased vocabularies keep their own canonical code in the tooltip (e.g. REFERENCE_ONLY, not NOT_APPLICABLE).
  return <StatusBadge status={p.status} label={label ?? p.label} className={className} title={status ?? undefined} />;
}

export const LENS_LABELS: Record<string, string> = {
  DESIGN: "Rancangan disetujui",
  CONFIG: "Konfigurasi berlaku",
  OBSERVED: "Observasi terakhir",
};

export const DIMENSION_LABELS: Record<string, string> = {
  admission: "Admission",
  installation: "Instalasi",
  configuration: "Konfigurasi",
  connectivity: "Konektivitas",
  functional: "Fungsi",
  enablement: "Enablement",
  usage: "Penggunaan",
  lifecycle_final: "Lifecycle",
};

export const REQUIREMENT_LABELS: Record<string, string> = {
  REQUIRED: "Wajib",
  OPTIONAL: "Opsional",
  CANDIDATE: "Kandidat",
  REFERENCE_ONLY: "Rujukan",
};

export const RELATION_LABELS: Record<string, string> = {
  depends_on: "bergantung pada",
  calls: "memanggil",
  reads: "membaca",
  writes: "menulis",
  publishes_to: "memublikasikan ke",
  verifies: "memverifikasi",
  stores_in: "menyimpan di",
  runs_with: "berjalan dengan",
  implements: "mengimplementasikan",
  governed_by: "diatur oleh",
  replaces: "menggantikan",
  alternative_to: "alternatif untuk",
};

export const SEVERITY_TONE: Record<string, string> = {
  CRITICAL: "bg-bad-bg text-bad",
  HIGH: "bg-bad-bg text-bad",
  MEDIUM: "bg-warn-bg text-warn",
  LOW: "bg-muted-bg text-muted",
  INFO: "bg-muted-bg text-muted",
};

export function SeverityBadge({ severity }: { severity: string | null | undefined }) {
  const s = severity ?? "INFO";
  const label: Record<string, string> = { CRITICAL: "Kritis", HIGH: "Tinggi", MEDIUM: "Sedang", LOW: "Rendah", INFO: "Info" };
  return (
    <span className={`inline-flex rounded px-1.5 py-0.5 text-[11px] font-semibold ${SEVERITY_TONE[s] ?? SEVERITY_TONE.INFO}`} title={`Severity ${s}`}>
      {label[s] ?? s}
    </span>
  );
}
