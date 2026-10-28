import { StatusBadge } from "../../components/StatusBadge";
import type { FreeTierView, RouteItem } from "./types";

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

/** A route is active only when its status is TRIAL/APPROVED AND an ACTIVE decision bound to the current config hash
 * exists (`active_decision`). Nothing resets `status` when a decision expires or is invalidated, so the status alone
 * would show a route the router rejects as active. */
export function isActiveRoute(r: Pick<RouteItem, "status" | "active_decision">): boolean {
  return (r.status === "TRIAL" || r.status === "APPROVED") && !!r.active_decision;
}

/** TRIAL/APPROVED whose decision expired or no longer matches the configuration: not active, whatever `status` says. */
export function isLapsedRoute(r: Pick<RouteItem, "status" | "active_decision">): boolean {
  return (r.status === "TRIAL" || r.status === "APPROVED") && !r.active_decision;
}

/** Route status as the owner reads it: an active TRIAL/APPROVED route is active; a catalogue row is not (never a bare
 * "Kandidat"); a TRIAL/APPROVED status without a valid decision is a warning, never green. */
export function RouteStatusBadge({ route }: { route: Pick<RouteItem, "status" | "active_decision"> }) {
  if (route.status === "CANDIDATE") return <StatusBadge status="CANDIDATE" label="Katalog, belum diaktifkan" />;
  if (isLapsedRoute(route))
    return (
      <StatusBadge
        status="EXPIRED"
        label={`${route.status} · keputusan kedaluwarsa/tidak berlaku`}
        title={`${route.status}: tidak ada keputusan ACTIVE untuk konfigurasi saat ini; router menolak rute ini`}
      />
    );
  return <StatusBadge status={route.status} />;
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
