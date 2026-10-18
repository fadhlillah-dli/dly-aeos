// Response shapes of /api/doctor/* (backend: aeos.doctor.views).

export interface Remedy {
  operation: string;
  command: string | null;
  source: string | null;
  target: string | null;
  privilege: string;
  cost: string;
  rollback: string;
  evidence_to_collect: string;
  executed_by: string;
}

export interface CheckItem {
  check_id: string;
  title: string;
  group: string;
  group_label: string;
  component_id: string | null;
  component_version: string | null;
  scope: Record<string, unknown>;
  target_identity: string | null;
  status: string;
  effective_status: string;
  stale_reason: string | null;
  severity: string;
  summary: string;
  observed_evidence: Record<string, unknown>;
  last_checked_at: string | null;
  expires_at: string | null;
  invalidators: string[];
  fingerprint: string | null;
  error_code: string | null;
  remediation: Partial<Remedy>;
  requires_destructive: boolean;
  requires_paid_write: boolean;
  never_run: boolean;
  functional_only: boolean;
  network: boolean;
  critical_for: string[];
  /** Statuses that count as OK for this check (e.g. CONFIGURED is not OK for policy.mci). */
  ok_statuses: string[];
  component_link: string | null;
}

export interface RunMeta {
  id: string;
  started_at: string;
  finished_at: string | null;
  trigger: string;
  functional: boolean;
  groups: string[];
  capability: string | null;
  counts: Record<string, number>;
}

export interface ChecksResponse {
  items: CheckItem[];
  groups: { id: string; label: string; order: number }[];
  last_run: RunMeta | null;
  source: string;
  checked_at: string | null;
  mode: string;
  status_legend: Record<string, string>;
}

export interface CapabilityItem {
  capability: string;
  label: string;
  description: string;
  domain: string | null;
  status: string;
  reasons: string[];
  checks: { check_id: string; title: string; tier: string; status: string | null; effective_status: string; ok: boolean; last_checked_at: string | null; component_id: string | null }[];
  remedies: (Partial<Remedy> & { check_id: string })[];
}

export interface CapabilitiesResponse {
  items: CapabilityItem[];
  human_only: { label: string; status: string; note: string }[];
  source: string;
  checked_at: string | null;
  mode: string;
}

export interface HistoryResponse {
  check: { check_id: string; title: string; group: string; group_label: string; component_id: string | null; ttl_seconds: number; invalidators: string[]; functional_only: boolean; capabilities: string[] };
  items: (CheckItem & { id: string })[];
}
