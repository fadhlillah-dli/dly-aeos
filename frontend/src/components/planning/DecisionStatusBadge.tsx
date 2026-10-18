import { StatusBadge } from "../StatusBadge";

const SPEC: Record<string, { badge: string; label: string }> = {
  ACTIVE: { badge: "CONFIGURED", label: "Aktif" },
  EXPIRED: { badge: "EXPIRED", label: "Kedaluwarsa" },
  INVALIDATED: { badge: "CANCELLED", label: "Dibatalkan" },
  SUPERSEDED: { badge: "STALE", label: "Digantikan" },
};

/** Decision lifecycle (§13): an ACTIVE decision is information, not a "pass". */
export function DecisionStatusBadge({ status }: { status: string }) {
  const s = SPEC[status] ?? { badge: "UNKNOWN", label: status };
  return (
    <span title={status}>
      <StatusBadge status={s.badge} label={s.label} />
    </span>
  );
}
