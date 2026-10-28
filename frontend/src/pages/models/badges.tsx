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

type RouteState = Pick<RouteItem, "status" | "active_decision" | "credential">;

/** The router can authenticate the route: its key is stored (PRESENT) or it needs none (NOT_NEEDED: local, loopback,
 * claude-native). A missing block (older server) is not usable: never claim more than the data supports. */
export function credentialUsable(r: Pick<RouteItem, "credential">): boolean {
  return r.credential?.state === "PRESENT" || r.credential?.state === "NOT_NEEDED";
}

/** A route is active only when its status is TRIAL/APPROVED AND an ACTIVE decision bound to the current config hash
 * exists (`active_decision`) AND its credential is usable. Nothing resets `status` when a decision expires or is
 * invalidated or a key is missing, so the status alone would show a route the router rejects as active (2026-10-28:
 * CAN_RUN_WORKER BLOCKED while the page said "Aktif · TRIAL"). */
export function isActiveRoute(r: RouteState): boolean {
  return (r.status === "TRIAL" || r.status === "APPROVED") && !!r.active_decision && credentialUsable(r);
}

/** TRIAL/APPROVED whose decision expired or no longer matches the configuration: not active, whatever `status` says. */
export function isLapsedRoute(r: Pick<RouteItem, "status" | "active_decision">): boolean {
  return (r.status === "TRIAL" || r.status === "APPROVED") && !r.active_decision;
}

/** Activated (TRIAL/APPROVED with an ACTIVE decision) but the router cannot authenticate it: the key is not stored,
 * the store is sealed, or the credential path is not configured. A warning, never green, never counted as active. */
export function isCredentialBlockedRoute(r: RouteState): boolean {
  return (r.status === "TRIAL" || r.status === "APPROVED") && !!r.active_decision && !credentialUsable(r);
}

/** Why an activated route cannot be used, in the owner's words (badge label suffix). */
export function credentialProblem(r: Pick<RouteItem, "credential">): string {
  const c = r.credential;
  if (!c) return "status kunci tidak diketahui";
  if (c.state === "UNAVAILABLE") return "vault terkunci";
  if (c.state === "MISSING" && c.ref && c.reason?.includes("not stored")) return "kunci belum disimpan";
  return "kredensial belum dikonfigurasi";
}

/** The owner's remedy for a credential-blocked route: store the key, or unseal the store (never a value). */
export function credentialRemedy(r: Pick<RouteItem, "credential">): string | null {
  const c = r.credential;
  if (!c?.ref) return null;
  if (c.state === "MISSING") return `aeos secrets set ${c.ref}`;
  if (c.state === "UNAVAILABLE") return "aeos vault unseal";
  return null;
}

/** Route status as the owner reads it: an active TRIAL/APPROVED route is active; a catalogue row is not (never a bare
 * "Kandidat"); a TRIAL/APPROVED status without a valid decision, or without a usable key, is a warning, never green. */
export function RouteStatusBadge({ route }: { route: RouteState }) {
  if (route.status === "CANDIDATE") return <StatusBadge status="CANDIDATE" label="Katalog, belum diaktifkan" />;
  if (isLapsedRoute(route))
    return (
      <StatusBadge
        status="EXPIRED"
        label={`${route.status} · keputusan kedaluwarsa/tidak berlaku`}
        title={`${route.status}: tidak ada keputusan ACTIVE untuk konfigurasi saat ini; router menolak rute ini`}
      />
    );
  if (isCredentialBlockedRoute(route))
    return (
      <span data-testid="route-credential-blocked">
        <StatusBadge
          status="DEGRADED"
          label={`Aktif, tidak dapat dipakai: ${credentialProblem(route)}`}
          title={`${route.status}: ${route.credential?.reason ?? "status kredensial tidak dilaporkan server"}; router menolak rute ini`}
        />
      </span>
    );
  return <StatusBadge status={route.status} />;
}

/** Longest wait before the expiry refetch (a decision far in the future is re-planned after each refetch anyway). */
export const EXPIRY_REFETCH_MAX_MS = 24 * 60 * 60 * 1000;
/** Shortest wait, and the margin after `expires_at` (a server clock slightly behind the browser still says active). */
export const EXPIRY_REFETCH_MIN_MS = 1000;

/** Milliseconds until one refetch at the nearest future `active_decision.expires_at` among *rows* (no event fires at
 * the moment a decision expires), clamped to [1 s, 24 h]; null when no displayed row has a future expiry. */
export function nextExpiryRefetchMs(rows: Pick<RouteItem, "active_decision">[], now: number): number | null {
  let nearest: number | null = null;
  for (const r of rows) {
    const at = r.active_decision?.expires_at ? Date.parse(r.active_decision.expires_at) : NaN;
    if (!Number.isFinite(at) || at <= now) continue;
    if (nearest === null || at < nearest) nearest = at;
  }
  if (nearest === null) return null;
  return Math.min(EXPIRY_REFETCH_MAX_MS, Math.max(EXPIRY_REFETCH_MIN_MS, nearest - now + EXPIRY_REFETCH_MIN_MS));
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
