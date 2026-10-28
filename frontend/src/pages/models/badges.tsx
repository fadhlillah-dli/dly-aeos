import { StatusBadge } from "../../components/StatusBadge";
import type { FreeTierView } from "./types";

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

/** Route status as the owner reads it: TRIAL/APPROVED are active; a catalogue row is not (never a bare "Kandidat"). */
export function RouteStatusBadge({ status }: { status: string }) {
  if (status === "CANDIDATE") return <StatusBadge status="CANDIDATE" label="Katalog, belum diaktifkan" />;
  return <StatusBadge status={status} />;
}

/** Compact free-tier badge for the table: today's count against the daily limit (account-wide, this instance only). */
export function FreeTierBadge({ tier }: { tier: FreeTierView }) {
  return (
    <StatusBadge
      status={tier.state}
      label={`Gratis: ${tier.requests_today_utc}/${tier.daily_limit} hari ini`}
      title={`${tier.state} · ${tier.counted}`}
    />
  );
}
