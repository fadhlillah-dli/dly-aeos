// API shapes for /api/claude/* and /api/routes/* (backend: aeos.claude_bridge.router, aeos.routing.router).

export interface AuthSummary {
  status: string;
  loggedIn: boolean | null;
  authMethod: string | null;
  apiProvider: string | null;
  subscriptionType: string | null;
  email: string | null;
  reasons: string[];
}

export interface CapabilityStep {
  id: string;
  status: string;
  detail?: string;
  error_code?: string | null;
  duration_ms?: number | null;
  model_resolved?: string | null;
}

export interface CapabilityTestView {
  id: string | null;
  status: string;
  result_status?: string;
  stale_reason?: string | null;
  checked_at: string | null;
  model_requested: string;
  model_resolved: string | null;
  engine_version: string | null;
  duration_ms: number | null;
  error_code: string | null;
  steps: CapabilityStep[];
  evidence_id: string | null;
  evidence_level?: string;
}

export interface ClaudeStatus {
  binary: { found: boolean; status: string; path_logical: string; version: string | null; fingerprint: string | null; reasons: string[] };
  auth: AuthSummary;
  bridge_status: string;
  env_hygiene: { status: string; forbidden_present: string[]; running_inside_claude_session: boolean; note: string };
  invocation: { argv_template: string[]; forbidden_flags: string[]; prompt_via: string; workspace: string };
  policy: { allowed_families: string[]; stages: Record<string, string>; fallback: string; mode: string };
  models: { route_key: string; alias: string | null; model_id: string; resolved_version: string; resolved_observed_at: string | null; status: string; mode: string }[];
  last_capability_test: CapabilityTestView | null;
  quota: { state: string; scope?: string; source: string; observed_at: string | null; resets_at: string | null; note: string };
  extra_usage: { state: string; note: string };
  checked_at: string;
  source: string;
  mode: string;
}

export interface EvalSummary {
  state: "DIUJI" | "BELUM_DIUJI";
  roles: Record<string, { score?: number; runs?: number; accepted?: number; suite?: string; at?: string }>;
  stale_roles: string[];
  note: string;
}

export interface RouteItem {
  route_key: string;
  pool: string;
  provider: string;
  endpoint: string | null;
  model_id: string;
  alias: string | null;
  display_name: string;
  resolved_version: string;
  resolved_observed_at: string | null;
  identity_status: string;
  is_free: "YES" | "NO" | "UNKNOWN" | string;
  effectively_free: boolean;
  free_until: string | null;
  expires_note: string | null;
  pricing: Record<string, unknown>;
  pricing_checked_at: string | null;
  data_terms: Record<string, unknown>;
  terms_checked_at: string | null;
  eligible_domains: string[];
  allowed_data_classes: string[];
  context_window: number | null;
  supports_tools: string;
  status: string;
  mode: string;
  origin: string;
  evidence_level: string;
  availability: string;
  availability_checked_at: string | null;
  harness: string | null;
  fallback_chain: string[];
  priority: number;
  config_hash: string;
  config_changed_at: string | null;
  eval_summary: EvalSummary;
  last_used: { at: string | null; run_id: string | null; model: string | null; status: string } | null;
  active_decision: { id: string; kind: string; expires_at: string | null; budget_decision_id?: string | null } | null;
  quota: { state: string; scope?: string; resets_at?: string | null; source?: string; note?: string };
  source_refs: string[];
  notes: string;
}

export interface RouteDetail extends RouteItem {
  config: Record<string, unknown>;
  decisions: { id: string; kind: string; status: string; target_hash: string | null; matches_current_config: boolean; created_at: string | null; expires_at: string | null; rationale: string }[];
  quota_states: { scope: string; state: string; source: string; observed_at: string | null; resets_at: string | null }[];
}

export interface RouteList {
  items: RouteItem[];
  source: string;
  checked_at: string | null;
  catalog_refresh: { at: string | null; status: string; results: Record<string, { status?: string; reason?: string; checked_at?: string }> } | null;
  mode: string;
  counts: { total: number; approved: number };
}

export interface ActivatePayload {
  mode: "APPROVED" | "TRIAL";
  rationale: string;
  expected_config_hash: string;
  allowed_data_classes?: string[] | null;
  budget_decision_id?: string | null;
  expires_in_days?: number | null;
  terms_reviewed?: boolean;
}

export const DATA_CLASSES = ["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"] as const;
export const POOL_LABELS: Record<string, string> = {
  SUPERVISOR: "Pengawas Max",
  WORKER: "Pekerja awal",
  COMPARATOR: "Pembanding",
  ARENA: "Arena",
  WATCHLIST: "Watchlist (berbayar)",
  LOCAL: "Lokal/open-weight",
};
