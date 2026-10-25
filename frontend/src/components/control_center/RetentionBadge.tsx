import { OpsBadge } from "./OpsBadge";

/** Per-record retention marker from the backend (policy/retention.yaml applied to one record, §35). */
export interface RetentionMarker {
  rule: string;
  retain_days: number | null;
  legal_hold: boolean;
  expired_by_policy: boolean;
  disposal: string;
  label: string;
}

/** Badge code for a marker: expiry only ever means manual disposal; AEOS never deletes a record. */
export function retentionStatus(m: RetentionMarker | null | undefined): string {
  if (!m) return "UNKNOWN";
  if (m.expired_by_policy) return "EXPIRED_BY_POLICY";
  if (m.legal_hold) return "LEGAL_HOLD";
  return m.retain_days === null ? "NO_POLICY" : "KEEP";
}

export function RetentionBadge({ marker }: { marker: RetentionMarker | null | undefined }) {
  return (
    <span title={marker?.label}>
      <OpsBadge status={retentionStatus(marker)} />
    </span>
  );
}
