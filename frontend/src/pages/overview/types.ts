// Shapes returned by /api/control-center/* (backend: aeos.control_center).

export interface Section<T = Record<string, unknown>> {
  status: string;
  source: string;
  checked_at: string | null;
  reason: string;
  evidence_limit: string;
  data: T | null;
}

export interface CapabilityRow {
  capability: string;
  status: string;
  reasons: string[];
  remedies: Record<string, unknown>[];
  checked_at: string | null;
}

export interface BlockerRow {
  kind: string;
  id: string;
  title: string;
  reason: string;
  since: string | null;
  link: string;
}

export interface HealthSub {
  key: string;
  status: string;
  detail: string;
}

export interface QuotaRow {
  provider: string;
  scope: string;
  state: string;
  source: string;
  observed_at: string | null;
  note?: string;
}

export interface Overview {
  generated_at: string;
  domain: string;
  environment?: string;
  dev_mode?: boolean;
  project: { id: string; name: string; project_key: string; is_platform: boolean; mode: string; lifecycle: string } | null;
  mode: string;
  contains_mock: boolean;
  mock_rows: Record<string, number>;
  status_counts: Record<string, number>;
  source: string;
  evidence_limit: string;
  sections: {
    capabilities: Section<{ items: CapabilityRow[]; counts: Record<string, number> }>;
    active_plan: Section<{ approved?: { plan_id: string; title: string; version: number; manifest_hash: string }[]; awaiting_review?: { plan_id: string; title: string; state: string }[] }>;
    policy: Section<{ version: string; content_hash: string; mci_status: string }>;
    blockers: Section<{ items: BlockerRow[] }>;
    health: Section<{ items: HealthSub[] }>;
    cost: Section<{
      window_days: number;
      actual_usd: number;
      estimated_usd: number;
      unknown_cost_entries: number;
      mock_entries_excluded: number;
      budgets: number;
      quotas: QuotaRow[];
    }>;
    decisions: Section<{ count: number; top: Record<string, unknown>[] }>;
    stream: Section<{ latest_seq: number; last_event_at: string | null; last_kind?: string }>;
    secrets: Section<{ items: { ref: string; exists: boolean; backend: string; expires_at: string | null }[]; missing: string[]; expiring_soon: string[] }>;
    recovery: Section<{ backup_id?: string; evidence_id?: string; backups: number; latest?: string | null }>;
    emergency: Section<{ incident_id: string; at: string; status: string; reason: string }>;
    project_hint?: Section<{ projects_in_domain: number }>;
  };
}

export interface ActivityItem {
  seq: number;
  at: string;
  source: string;
  kind: string;
  entity_type: string;
  entity_id: string;
  project_id: string | null;
  run_id: string | null;
  mode: string;
  payload: Record<string, unknown>;
}

export interface ActivityPage {
  items: ActivityItem[];
  next_before: number | null;
  latest_seq: number;
  checked_at: string;
}

export interface IntegrationCheck {
  check_id: string;
  group?: string;
  status: string;
  severity: string;
  summary: string;
  last_checked_at: string | null;
}

export interface IntegrationRow {
  id: string;
  project_id: string;
  project_name: string | null;
  provider: string;
  purpose: string;
  tenant_id: string | null;
  root_id: string | null;
  credential_ref: string | null;
  // Status/ref/expiry only - the backend never returns a secret value (§26, §35).
  credential: { status: string; backend?: string; updated_at?: string | null; expires_at?: string | null; reason?: string };
  last_verified_at: string | null;
  verification: string;
  checks: IntegrationCheck[];
}

export interface IntegrationList {
  items: IntegrationRow[];
  checks: IntegrationCheck[];
  source: string;
  checked_at: string;
  evidence_limit: string;
}
