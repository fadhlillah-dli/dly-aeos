import { Building2, Crown, User } from "lucide-react";
import type { Domain } from "../../auth/AuthContext";
import { StatusBadge } from "../../components/StatusBadge";

// Module statuses are rendered through the shared StatusBadge (text + icon + colour). Each raw
// code maps to the canonical badge whose tone carries the same meaning, with an Indonesian label.
// Unverified/unscanned/unknown never map to a "pass" tone (§34, §35).
type Kind = "scan" | "parse" | "verify" | "local" | "lifecycle" | "config" | "freshness" | "memory" | "credential";

const MAP: Record<Kind, Record<string, [string, string]>> = {
  scan: {
    CLEAN: ["PASS", "Bersih (lolos pemindaian)"],
    FLAGGED: ["DEGRADED", "Ditandai — perlu ditinjau"],
    UNSCANNED: ["UNKNOWN", "Tidak dapat dipindai"],
    REJECTED: ["REJECTED", "Ditolak"],
    PENDING: ["PENDING", "Menunggu pemindaian"],
  },
  parse: {
    PARSED: ["SUCCEEDED", "Teks diekstrak"],
    SKIPPED: ["NOT_APPLICABLE", "Metadata saja"],
    FAILED: ["FAILED", "Gagal diurai"],
    PENDING: ["PENDING", "Menunggu"],
  },
  verify: {
    VERIFIED: ["VERIFIED", "Identitas terverifikasi"],
    UNVERIFIED: ["UNVERIFIED", "Belum diverifikasi"],
  },
  local: {
    OK: ["CONFIGURED", "Checkout lokal cocok"],
    MISMATCH: ["CONFLICT", "Origin tidak cocok"],
    FAILED: ["FAILED", "Checkout tidak terbaca"],
    NOT_CHECKED: ["NOT_CHECKED", "Tidak ada checkout lokal"],
  },
  lifecycle: {
    ACTIVE: ["ENABLED", "Aktif"],
    ARCHIVED: ["DISABLED", "Diarsipkan"],
    RETIRING: ["DEGRADED", "Sedang dipensiunkan"],
    RETIRED: ["CANCELLED", "Dipensiunkan"],
  },
  config: {
    VERIFIED: ["VERIFIED", "Terverifikasi"],
    CONFIGURED: ["CONFIGURED", "Terkonfigurasi (belum diuji)"],
    MISSING: ["MISSING", "Belum lengkap"],
    NOT_CONFIGURED: ["NOT_CHECKED", "Belum diatur"],
    NOT_CHECKED: ["NOT_CHECKED", "Belum diperiksa"],
    NOT_TESTED: ["NOT_TESTED", "Belum diuji"],
    NOT_APPLICABLE: ["NOT_APPLICABLE", "Tidak berlaku"],
  },
  credential: {
    CONFIGURED: ["CONFIGURED", "Credential tersimpan"],
    MISSING: ["MISSING", "Credential belum ada"],
    UNKNOWN: ["UNKNOWN", "Status credential tidak diketahui"],
  },
  freshness: {
    CURRENT: ["LIVE", "Terkini"],
    STALE: ["STALE", "Usang — bangun ulang"],
    UNKNOWN: ["UNKNOWN", "Tidak dapat dicek"],
  },
  memory: {
    PROPOSED: ["PENDING", "Diusulkan"],
    ACCEPTED: ["ACCEPTED", "Diterima"],
    REJECTED: ["REJECTED", "Ditolak"],
    EXPIRED: ["EXPIRED", "Kedaluwarsa"],
    SUPERSEDED: ["CANCELLED", "Digantikan"],
  },
};

export function mapStatus(kind: Kind, value: string | null | undefined): [string, string] {
  if (!value) return ["UNKNOWN", "Tidak diketahui"];
  return MAP[kind][value] ?? [value, value];
}

export function ModuleBadge({ kind, value }: { kind: Kind; value: string | null | undefined }) {
  const [status, label] = mapStatus(kind, value);
  return (
    <span title={value ?? "UNKNOWN"}>
      <StatusBadge status={status} label={label} />
    </span>
  );
}

export function DomainBadge({ domain }: { domain: Domain | string }) {
  const Icon = domain === "OFFICE" ? Building2 : User;
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full border border-line px-2 py-0.5 text-xs font-semibold"
      title="Domain dipilih manusia; tidak dapat diubah"
    >
      <Icon className="h-3.5 w-3.5" aria-hidden />
      {domain}
    </span>
  );
}

export function PlatformBadge() {
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-info-bg px-2 py-0.5 text-xs font-medium text-info">
      <Crown className="h-3.5 w-3.5" aria-hidden />
      Platform DLY AEOS
    </span>
  );
}

export function ClassificationBadge({ value }: { value: string }) {
  const sensitive = value === "CONFIDENTIAL" || value === "RESTRICTED";
  return (
    <span
      className={
        "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium " +
        (sensitive ? "bg-warn-bg text-warn" : "bg-muted-bg text-muted")
      }
      title="Klasifikasi data"
    >
      {value}
    </span>
  );
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
