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

/** §12 "unmodified official binary": the OS code-signature gate the bridge applies before every native call. */
export interface BinaryAuthenticity {
  /** OK = VALID and signer matches the owner pin; UNVERIFIED = no pin / not provable; DEGRADED = unsigned; BLOCKED; NOT_CHECKED */
  gate: string;
  reason: string;
  approved_signer: string | null;
  /** PINNED | NOT_PINNED | UNREADABLE (the reviewed install-manifest cannot be read: the pin is unknown, the gate blocks) */
  pin_status?: string;
  os: string;
  signature_status: string;
  observed_signer: string | null;
  owner_step: string | null;
  remedy: { operation: string; commands: string[]; note: string } | null;
}

export interface ClaudeStatus {
  binary: {
    found: boolean;
    status: string;
    path_logical: string;
    version: string | null;
    fingerprint: string | null;
    reasons: string[];
    authenticity?: BinaryAuthenticity;
  };
  auth: AuthSummary;
  bridge_status: string;
  /** why the bridge is BLOCKED (signature / binary / auth): decides which remedy is shown */
  blocked_by?: string[];
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
  /** NEEDS_EVALUATION: the configuration changed and its previous results are not inherited (§02, §43) */
  state: "DIUJI" | "BELUM_DIUJI" | "NEEDS_EVALUATION";
  roles: Record<string, { score?: number; score_basis?: string; runs?: number; accepted?: number; suite?: string; at?: string; sample_size?: number; run_ids?: string[] }>;
  stale_roles: string[];
  note: string;
}

export interface RuntimeBinding {
  bound: Record<string, string | null>;
  current: Record<string, string | null>;
  status: "BOUND" | "DRIFT" | "NOT_ACTIVATED";
  drift: string[];
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
  active_decision: {
    id: string;
    kind: string;
    expires_at: string | null;
    budget_decision_id?: string | null;
    eval_run_ids?: string[];
    rollback_target?: { action: string; decision_id: string | null; config_hash: string | null; note?: string } | null;
  } | null;
  quota: { state: string; scope?: string; resets_at?: string | null; source?: string; note?: string };
  source_refs: string[];
  notes: string;
  // §23 per-route assessment (absent on older servers)
  region?: string | null;
  region_restricted?: boolean;
  terms_restriction?: string | null;
  /** training/trial flags still UNKNOWN: the owner records their own reading at activation */
  terms_unknown?: string[];
  reviewed_tool_set?: string[] | null;
  runtime_binding?: RuntimeBinding;
  execution_path?: { status: string; note: string };
  /** which evaluation can produce results for this route (NOT_WIRED for arena/comparator/local: DESIGN, NOT-PROVEN) */
  evaluation_path?: { status: string; note: string };
  /** the exact upstream request the broker would build (E1 documentation shape until a probe records E3) */
  upstream_request?: UpstreamRequest | null;
  /** provider rule on which client may call the route (S07: free-tier Zen models require OpenCode) */
  client_restriction?: ClientRestriction | null;
  /** false for a free route or a loopback LOCAL route (no BUDGET decision needed) */
  budget_required?: boolean;
}

export interface UpstreamRequest {
  url?: string | null;
  model?: string;
  catalog_model_id?: string;
  api?: string;
  reasoning_effort?: string;
  code?: string | null;
  problem?: string | null;
  status: "READY_SHAPE" | "REFUSED" | "UNKNOWN" | string;
  note: string;
}

export interface ClientRestriction {
  status: "OWNER_DECISION_REQUIRED" | "OWNER_ACCEPTED" | string;
  statement: string | null;
  source: string | null;
  applies_to: string | null;
  owner_note: string | null;
  note: string;
}

export interface EndpointPayload {
  endpoint: string;
  rationale: string;
  upstream_model?: string | null;
  quantization?: string | null;
  inference_backend?: string | null;
  hardware?: string | null;
}

export interface RouteDetail extends RouteItem {
  config: Record<string, unknown>;
  decisions: {
    id: string;
    kind: string;
    status: string;
    target_hash: string | null;
    matches_current_config: boolean;
    /** ACTIVE + human + unexpired + bound to the current config hash: the only state shown as approving */
    effective: boolean;
    actor_kind: string;
    created_at: string | null;
    expires_at: string | null;
    rationale: string;
  }[];
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
  terms_note?: string;
  region?: string | null;
  /** the owner's own reading of UNKNOWN training/trial terms (needs terms_reviewed) */
  terms_record?: { may_use_inputs_for_training: boolean; trial_only_no_confidential: boolean; note: string } | null;
  /** APPROVED only: COMPLETED, gate-passing EvalRuns of exactly this configuration (§23 "telah diuji") */
  eval_run_ids?: string[] | null;
  /** APPROVED only: §44 DecisionRecord of adopting the evaluated route (evidence_ids default to eval_run_ids) */
  decision_record?: { options: string[]; goal: string; cost_quality_tradeoff: string; invalidate_if: string } | null;
  /** the owner's decision on a provider client restriction (required while it is OWNER_DECISION_REQUIRED) */
  client_terms?: { accepted: boolean; note: string } | null;
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
