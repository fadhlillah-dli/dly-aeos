// Response shapes of the Workforce Console API (backend/aeos/workforce/views.py).

export interface RunStatuses {
  worker_reported: string | null;
  verifier: string | null;
  review: string | null;
  acceptance: string | null;
  merged: string | null;
}

export interface RunUsage {
  requests: number | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  cost_usd: number | "UNKNOWN" | null;
  cost_known_usd: number | null;
  cost_unknown_requests: number;
  quota_status: string;
  ledger: string | null;
}

export interface RunRow {
  run_id: string;
  run_type: string;
  attempt: number;
  mode: string;
  project: { id: string; name: string | null; key: string | null };
  domain: string;
  agent_key: string | null;
  role_key: string | null;
  role_name: string | null;
  task: { id: string | null; key: string | null; title: string | null; feature_key: string | null };
  route: { key: string | null; model_resolved: string; engine: string | null; engine_version: string };
  execution_state: string;
  task_state: string | null;
  ui_state: string;
  wait_reason: string;
  reason_code: string;
  current_action: string;
  current_action_source: string;
  /** RUNNER (observed tool action) | CONTROLLER (status text) | UNKNOWN — set by the writer, not the UI. */
  current_action_writer?: string;
  /** External resources the broker touched for this run (branch, PR, document). */
  resources?: { kind: string; target: string | null; status: string; external_url: string | null }[];
  started_at: string | null;
  finished_at: string | null;
  last_heartbeat_at: string | null;
  heartbeat_age_seconds: number | null;
  heartbeat_stale: boolean;
  touched_paths: string[];
  touched_count: number;
  statuses: RunStatuses;
  usage: RunUsage;
  caps: Record<string, unknown>;
  capability: { active: boolean; expires_at: string | null; used: Record<string, number>; caps: Record<string, unknown>; revoked_reason: string | null } | null;
  evidence_refs: string[];
  quarantined: boolean;
  exit_code: number | null;
  owner_requirements: string[];
  links: { run: string };
}

export interface RunList {
  items: RunRow[];
  source: string;
  checked_at: string;
  mode: string;
}

export interface Blocker {
  task_id: string;
  task_key: string | null;
  state: string;
  reason_code: string;
  reason: string;
  ui_state: string;
  attempts_used: number;
  since: string | null;
}

export interface WorkforceSummary {
  checked_at: string;
  domain: string;
  mode: string;
  source: string;
  scheduler: {
    enabled: boolean;
    paused: boolean;
    paused_reason: string;
    paused_by: string | null;
    concurrency_cap: number;
    cap_decision_id: string | null;
    /** §19/§51 P2: where the cap comes from and the owner's resource budget (UNKNOWN while not set). */
    cap_source?: string;
    cap_reason?: string;
    budget_max_parallel_workers?: number | null;
    budget_note?: string;
    controller_id: string;
    loop_running: boolean;
  };
  task_counts: Record<string, number>;
  active_runs: number;
  demo_runs: number;
  blockers: Blocker[];
  cost: {
    source: string;
    entries: number;
    input_tokens: number;
    output_tokens: number;
    requests: number;
    cost_known_usd: number | null;
    cost_unknown_entries: number;
    cost_status: string;
    quota: { provider: string; scope: string; state: string; source: string; observed_at: string | null; resets_at: string | null }[];
  };
  sandbox: SandboxReadiness;
  engine: { status: string; version: string | null; pinned_version: string; functional: string; evidence: string; checked_at: string };
}

/** One finding of the host-wide integrity hold (backend worker_runtime.integrity; this domain's findings only). */
export interface IntegrityFinding {
  kind?: string;
  path?: string;
  change?: string;
  detail?: string;
  source?: string;
  subject_id?: string | null;
  domain?: string | null;
}

/** Wave 7 (W6-000/005): a controller/config/bytecode/DB tamper finding holds every worker in every domain. */
export interface IntegrityHold {
  id: string | null;
  detected_at: string | null;
  source: string | null;
  /** null when the run/verification that raised it belongs to another domain (invariant 9) */
  subject_id: string | null;
  findings: IntegrityFinding[];
  /** findings raised in another domain: shown only as a count */
  other_domain_findings: number;
}

/** Sandbox readiness (/api/workforce/summary .sandbox and /api/worker-runtime/sandbox). */
export interface SandboxReadiness {
  status: string;
  kind: string | null;
  reasons: string[];
  remedies: string[];
  residual_risks: string[];
  /** INTEGRITY_HOLD while the host-wide hold is active (absent on older servers) */
  code?: string | null;
  integrity_hold?: IntegrityHold | null;
}

/** WorkerRun.caps keys the runtime records (backend worker_runtime.executor); absent = not recorded. */
export interface RunRuntimeCaps {
  integrity_hold?: boolean;
  db_trust_window?: {
    status?: string;
    foreign_count?: number;
    tolerated_count?: number;
    tables?: number;
    rows?: number;
    own_writes?: number;
    error?: string;
    note?: string;
  };
  cpu_cap?: { enforced?: boolean; by?: string; max_cpus?: number | null; job_cpu_rate?: number | null };
  command_logs?: {
    present?: boolean;
    files?: number;
    bytes?: number;
    truncated?: boolean;
    skipped?: number;
    removed_from_candidate?: boolean;
    evidence_ref?: string;
    rejected?: string;
    ignored?: string;
  };
  controller_integrity?: { checked_files?: number; changed?: number };
  context_pack?: {
    used?: boolean;
    reason?: string;
    freshness?: string;
    pack_id?: string;
    pack_hash?: string;
    items?: number;
    token_budget?: number | null;
    artifacts_in_pack?: boolean;
    artifacts_omitted?: number;
    build?: {
      built?: boolean;
      reason?: string;
      context_pack_id?: string;
      pack_hash?: string;
      items?: number;
      token_budget?: number | null;
      token_estimate?: number | null;
      omitted?: number;
      warnings?: string[];
    } | null;
  };
}

export interface ActionEvent {
  seq: number;
  at: string;
  kind: string;
  source: string;
  attempt: number | null;
  lease_id: string | null;
  mode: string;
  payload: Record<string, unknown>;
  label: string;
}

export interface RunDetail extends RunRow {
  checked_at: string;
  task_spec: {
    task_key: string;
    title: string;
    goal: string;
    feature_key: string | null;
    acceptance_criteria: unknown[];
    expected_outputs: unknown[];
    allowed_paths: string[];
    forbidden_paths: string[];
    tools_allowed: string[];
    egress_allowed: string[];
    limits: Record<string, unknown>;
    stop_conditions: unknown[];
    evidence_requirements: unknown[];
    risk_class: string;
    data_classification: string;
    depends_on: string[];
    spec_hash: string;
    plan_version_id: string;
    plan_version_hash: string;
    base_commit: string | null;
    capability_ttl_seconds: number;
  } | null;
  task_execution: {
    state: string;
    reason: string;
    reason_code: string;
    attempts_used: number;
    max_attempts: number | null;
    row_version: number;
    pending_findings: unknown[];
    assigned_agent_key: string | null;
  } | null;
  base_commit: string | null;
  head_commit: string | null;
  branch: string | null;
  workspace: { sandbox_kind: string | null; strategy: string | null; present: boolean };
  lease: { id: string; state: string; owner: string; expires_at: string | null; heartbeat_at: string | null; release_reason: string } | null;
  diff_summary: { files?: number; added?: number; deleted?: number; paths?: { path: string; status: string; mode: string | null }[]; patch_truncated?: boolean; strategy?: string };
  quarantine_reasons: { path: string; rule: string; detail: string }[];
  actions: ActionEvent[];
  attempts: { run_id: string; attempt: number; status: string; outcome_code: string; agent_key: string | null; exit_code: number | null; started_at: string | null; finished_at: string | null; quarantined: boolean; current: boolean }[];
  evidence: { id: string; kind: string; origin: string; trusted: boolean; level: string; claim: string; content_hash: string; created_at: string | null; mode: string }[];
  reviews: { id: string; verdict: string; reviewer_kind: string; findings: unknown[]; unknowns: unknown[]; created_at: string | null; mode: string }[];
  acceptance: { id: string; status: string; review_verdict: string | null; base_commit: string | null; head_commit: string | null; invalidated_reason: string | null; created_at: string | null }[];
  decisions: { id: string; kind: string; target_type: string; target_id: string; target_hash?: string | null; status: string; rationale: string; created_at: string | null }[];
  side_effects: { id: string; kind: string; status: string; target: Record<string, unknown>; external_url: string | null; commit_sha: string | null; updated_at: string | null; note: string }[];
  model_calls: {
    at: string | null;
    route_key: string | null;
    outcome: string;
    status: number | null;
    prompt_tokens: number | null;
    completion_tokens: number | null;
    cost_usd: number | "UNKNOWN";
    model_resolved: string | null;
    latency_ms: number | null;
    /** level the broker sent upstream (NOT_SENT = provider default); null = recorded before the column existed */
    reasoning_effort?: string | null;
    /** level the worker asked for, dropped and never forwarded (a known level or UNRECOGNISED); null = none */
    reasoning_requested?: string | null;
  }[];
  error: string;
  block_reason: string;
}

export interface RoleCard {
  role_key: string;
  name: string;
  kind: string;
  group: string;
  description: string;
  outputs: string[];
  boundaries: string[];
  verification: string[];
  forbidden_actions: string[];
  deterministic_service: boolean;
  engine_hint: string;
  source_section: string;
  version: string;
  revision_hash: string | null;
  eval_status: string;
  mode: string;
  active_runs: number;
  demo_runs: number;
  /** §17 assignment: non-terminal tasks naming this role (projection). */
  assigned_tasks?: number;
  availability: string;
  /** §17/§46 competence definition: inputs, example outputs, failure modes, evaluation case references. */
  inputs?: string[];
  example_outputs?: string[];
  failure_modes?: string[];
  evaluation_cases?: { suite_key: string; task_class?: string; case_ids: string[] }[];
  practice_sources?: string[];
  /** §44/§46 executor roles: the current evaluation of this revision and the owner's adoption of it. */
  evaluation?: { status: string; run_id: string | null; suite_key?: string | null; basis: string };
  adoption?: { decision_id: string | null; eval_run_id: string | null; scope: Record<string, string[]> | null; current: boolean };
  adoptable?: boolean;
}

export interface AgentPrerequisites {
  agent_key: string;
  harness: string;
  enabled: boolean;
  ready: boolean;
  reasons: string[];
  sandbox: { status: string | null; kind: string | null } | null;
  excluded_check: string;
  checked_at: string;
  source: string;
}

export interface ConcurrencyDetail {
  cap: number;
  decision_id: string | null;
  source: string;
  reason: string;
  policy_cap: number;
  budget_max_parallel_workers: number | null;
  budget_note: string;
  budget_limited: boolean;
}

export interface ExperimentRow {
  eval_run_id: string;
  topology: string | null;
  status: string;
  grant_decision_id: string | null;
  /** the grant applies right now (run RUNNING, decision ACTIVE, unexpired and bound to this start) */
  grant_active?: boolean;
  grantable: boolean;
  problems: string[];
  bound_tasks: number;
  parallel_execution: { status: string; problems: string[]; cap2_runs: number; overlaps: { runs: string[]; overlap_s: number }[] };
}

export interface ConcurrencyState {
  items: ExperimentRow[];
  concurrency: ConcurrencyDetail;
  source: string;
  checked_at: string;
  mode: string;
}

export interface RoleGroup {
  key: string;
  label: string;
  roles: RoleCard[];
}

export interface AgentDef {
  agent_key: string;
  role_key: string;
  harness: string;
  description: string;
  route_policy: Record<string, unknown>;
  tools: string[];
  limits: Record<string, unknown>;
  allowed_scope: Record<string, unknown>;
  enabled: boolean;
  enabled_reason: string;
  deterministic: boolean;
  revision: string;
  revision_hash: string | null;
  mode: string;
  owner: string;
  active_runs: number;
  availability: string;
  /** the stored owner toggle (``enabled`` follows the owning module's gate for reviewer/verifier agents) */
  enabled_flag?: boolean;
  enabled_source?: string;
}
