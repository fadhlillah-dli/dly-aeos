import { StatusBadge } from "../../components/StatusBadge";

/** Statuses produced by the bridge/routing that the shared badge does not name. Never rendered as pass. */
const EXTRA: Record<string, { status: string; label: string }> = {
  EXHAUSTED: { status: "BLOCKED", label: "Kuota habis - antre menunggu" },
  WARN: { status: "DEGRADED", label: "Peringatan" },
  OK: { status: "VERIFIED", label: "Bersih" },
  NOT_OK: { status: "FAILED", label: "Gagal" },
};

export function Badge({ status, label }: { status: string | null | undefined; label?: string }) {
  const extra = status ? EXTRA[status] : undefined;
  if (extra) return <StatusBadge status={extra.status} label={label ?? extra.label} />;
  return <StatusBadge status={status} label={label} />;
}
