export type Kind = "ADD" | "REPLACE" | "UPGRADE" | "REMOVE" | "COMPARE";

export interface ProposalRow {
  id: string;
  kind: Kind;
  title: string;
  status: string;
  domain: string;
  scope_type: string;
  project_id: string | null;
  parent_id: string | null;
  duplicate_of: string | null;
  source_refs: string[];
  canonical_identities: string[];
  existing_component_ids: string[];
  submitted_by: string;
  baseline_hash: string | null;
  candidate_hash: string | null;
  candidate_pin: string | null;
  reviewed_at: string | null;
  created_at: string | null;
  parts_count: number;
  outcome_status: string | null;
  allowed_actions: string[];
}

export interface DecisionView {
  id: string;
  kind: string;
  action: string | null;
  status: string;
  actor_user_id: string;
  target_hash: string | null;
  baseline_hash: string | null;
  candidate_hash: string | null;
  scope: Record<string, unknown>;
  rationale: string;
  created_at: string | null;
  invalidated_at: string | null;
  invalidation_reason: string | null;
}

export interface Dimension {
  status: "KNOWN" | "UNKNOWN";
  items: string[];
  source: string | null;
  reason?: string;
  /** rollback: previous pins are targets, never proof that rollback works (§40). */
  targets?: string[];
}

export interface BaselineComponent {
  component_id: string;
  status: string;
  reason?: string;
  name?: string;
  approved_pin?: string;
  manifest_hash?: string;
  admission_status?: string;
  enablement?: string;
  function?: string;
  consumers?: { source_id: string; relation: string; lens: string }[];
  dependencies?: { target_id: string; relation: string; lens: string }[];
  observations?: Record<string, { status: string; observed_version: string | null; observed_at: string | null }>;
  evidence_note?: string;
}

export interface AdvisorySummary {
  status: "NOT_CHECKED" | "ERROR" | "NONE" | "NOT_AFFECTED" | "UNKNOWN" | "AFFECTED";
  affected: string[];
  unknown: string[];
  count: number;
  checked_at: string | null;
  note: string;
}

export interface SourceIdentity {
  identity: string;
  status: string;
  research_entry_id: string | null;
  research_lifecycle: string | null;
  evidence_level: string;
  official_repo: string;
  release_or_commit: string;
  maintainer: string;
  license: string;
  distribution: string;
  terms: string;
  /** Date the owner last read the data/terms page (null = never). */
  terms_checked_at?: string | null;
  checked_at: string | null;
  research_date: string | null;
  /** §40 known security advisories for the candidate pin (from Check Upstream). */
  advisories?: AdvisorySummary;
  flags: string[];
  note: string;
}

/** §39 Security/Privacy assessment by code: PASS / UNKNOWN / BLOCKED per dimension. */
export interface SecurityPrivacy {
  status: "PASS" | "UNKNOWN" | "BLOCKED";
  dimensions: Record<string, { status: string; note: string; items?: string[] }>;
  performed_by: string;
  rule: string;
}

export interface RemovalItem {
  id: string;
  kind: string;
  item: string;
  status: "OPEN" | "CLOSED" | "WAIVED";
  note: string;
  evidence_id: string | null;
}

export interface AcceptanceCriterion {
  id: string;
  criterion: string;
  metric: string;
  baseline_value: string;
  threshold: string;
  suite: string | null;
  required: boolean;
}

export interface OwnerPlan {
  acceptance?: AcceptanceCriterion[];
  operations_owner?: string;
  replacement?: {
    cutover_order?: string;
    retire_old_version_after?: string;
    rollback_or_forward_recovery?: string;
    version_schema_data_compatibility?: string;
    operations_owner?: string;
    coexistence_safe?: boolean;
    backup_restore_evidence_id?: string;
  };
  canary?: { target: string; cap: string; observation_window: string; stop_criteria: string[] };
  removal?: Record<string, { status: string; note: string; evidence_id?: string }>;
  canary_evidence_ids?: string[];
}

export interface TrialRecord {
  status?: "LAUNCHED" | "STOPPED";
  launched_at?: string;
  launched_by?: string;
  decision_id?: string;
  plan_id?: string;
  plan_version_id?: string;
  task_ids?: string[];
  environment?: string;
  caps?: Record<string, unknown>;
  execution?: { status: string; code?: string; reasons?: string[]; note?: string };
  stopped_at?: string;
  stop_reason?: string;
}

export interface CheckpointView {
  status: string;
  mode?: string;
  verdict?: string;
  rationale?: string;
  reason?: string;
  label: string;
  at: string;
  evidence_ids: string[];
  package_hash: string;
  gate?: string | null;
  gate_note?: string | null;
  review?: { summary: string; consistency?: string; risks?: string[] };
  verify?: { claims: { claim: string; status: string }[]; unknowns?: string[] };
  package?: Record<string, unknown>;
}

export interface LinkViews {
  tasks: { id: string; task_key: string; title: string; plan_version_id: string; state: string }[];
  publications: { id: string; status: string; plan_version_id: string }[];
  eval_run_ids: string[];
  release_ids: string[];
  adr_keys: string[];
  observation_ids: string[];
  plan_version_ids: string[];
}

export interface ClaudeReview {
  status: string;
  label: string;
  reason?: string;
  review?: { summary: string; upstream_downstream?: string; risks?: string[]; contradictions?: string[] };
  verify?: { claims: { claim: string; status: string; evidence?: string }[]; unknowns?: string[] };
  verdict?: string;
  recommendation?: string;
  rationale?: string;
  model_requested?: string;
  model_resolved?: string | null;
  mode?: string;
  sent_metadata?: string[];
  /** Hash of the public metadata the review judged; a changed hash makes the old result STALE (§41). */
  metadata_hash?: string | null;
  reused?: boolean;
  reuse_note?: string;
  previous_at?: string | null;
  previous_verdict?: string | null;
}

export interface CostItem {
  value: number | null;
  basis: string;
  note?: string;
  cap_usd?: number;
  /** Non-dollar quantity from the ledger (tokens, minutes, bytes). */
  quantity?: number;
  unit?: string | null;
}

export interface OutcomeEntry {
  status: string;
  accepted?: boolean;
  at?: string;
  actor?: string;
  evidence_ids?: string[];
  accepted_configuration?: Record<string, unknown> | null;
  partial_failures?: unknown[];
  rollback?: Record<string, unknown> | null;
  notes?: string;
  note?: string;
  inventory_docs_update?: string;
  acceptance?: { criteria: { id: string; criterion: string; status: string; evidence_id: string | null }[]; unmet: string[]; waiver_decision_id: string | null };
  finalization?: { inventory: { status: string; reason?: string | null }; adr: { status: string; keys: string[] }; docs: { status: string; note: string }; ownership: { operations_owner: string; status: string } };
  rollback_decision_id?: string;
}

export interface ReviewPackage {
  summary: {
    kind: Kind;
    decision_question: string;
    problem: string;
    expected_benefit: string;
    relevance: { name: string; placement: string; relevance: string; lifecycle: string }[] | "UNKNOWN";
    plain_language: string[];
    attachments: { attachment_id: string; note: string }[];
  };
  source_identity: SourceIdentity[];
  baseline: {
    components: BaselineComponent[];
    baseline_hash: string;
    note: string | null;
    architecture_snapshot?: { id: string; label: string; snapshot_hash: string } | null;
    /** §39 ADD: existing components with the same capability ("mengapa yang ada tidak cukup?"). */
    overlap?: { component_id: string; name: string; kind: string; enablement: string; function: string }[];
  };
  alternatives: { option: string; applicable: boolean; summary: string; tradeoffs: string; unknowns: string[] }[];
  impact: Record<string, Dimension | string[]>;
  experiment_plan: Record<string, unknown> & { type: string; pins?: Record<string, string | null> };
  replacement_plan: Record<string, unknown> | null;
  removal_plan?: { items: RemovalItem[]; open: string[]; status: string; rule: string } | null;
  upgrade_delta?: { components: Record<string, unknown>[]; source: string } | null;
  security_privacy?: SecurityPrivacy;
  review_contributors?: { role: string; performed_by: string }[];
  cost: Record<string, CostItem | string | Record<string, unknown>>;
  claude_review: ClaudeReview;
  human_decision: {
    required: boolean;
    bind: { baseline_hash: string; candidate_hash: string };
    scope_fields: string[];
    options: Record<string, string>;
    allowed_now: string[];
    rules: string[];
    /** §50: an UNVERIFIED/CHANGED/NOT_FOUND source identity blocks TRIAL/PROMOTE admission. Optional for older packages. */
    source_identity_gate?: { status: "OK" | "BLOCKED"; blocking: { identity: string; status: string }[]; remedy: string | null };
    next_actions: string[];
  };
  outcome: OutcomeEntry;
  parts: { child_id: string; title: string; kind: string }[];
  unknown_fields: string[];
  generated_at: string;
  generated_by: string;
  binding_note?: string;
}

export interface ProposalDetail extends ProposalRow {
  problem: string;
  expected_benefit: string;
  limits: Record<string, unknown>;
  candidate_component_ids?: string[];
  attachment_ids: string[];
  unknown_fields: string[];
  parts: { child_id: string; title: string; kind: string; depends_on: string[] }[];
  review_package: ReviewPackage | Record<string, never>;
  review_hash: string | null;
  decisions: DecisionView[];
  evidence_ids: string[];
  outcome: { current?: OutcomeEntry; history?: OutcomeEntry[] };
  duplicates: { id: string; title: string; status: string }[];
  /** Same-domain proposals with the same component+kind or a similar goal - suggestions, never merged. */
  possible_duplicates?: { id: string; title: string; status: string; kind: string; reason: string }[];
  research_entries: { id: string; name: string; lifecycle: string; identity_status: string; checked_at: string | null }[];
  delivery_dimensions: Record<string, { status: string; note: string; at?: string | null }>;
  owner_plan?: OwnerPlan;
  links?: LinkViews;
  trial?: TrialRecord;
  checkpoint_review?: CheckpointView | null;
  candidate_component_ids?: string[];
  guarantees: { installs: boolean; activates: boolean; raises_budget: boolean; sends_project_data: boolean; note: string };
}

export interface Meta {
  kinds: { kind: Kind; question: string }[];
  actions: string[];
  outcomes: string[];
  scope_required: string[];
  platform_project: { id: string; name: string; domain: string } | null;
  attachments: string;
  guarantees: string;
}

export interface Timeline {
  items: { at: string | null; type: string; action: string | null; actor: string; result: string; details: Record<string, unknown> }[];
  outcome_history: OutcomeEntry[];
  before_after: {
    before: { baseline_hash: string | null; components: { component_id: string; name?: string; approved_pin?: string; status: string }[] };
    after: { candidate_hash: string | null; candidate_pin: string | null; accepted_configuration: Record<string, unknown> | null } | null;
    status: string;
    note: string | null;
    snapshots: { before: { label: string; snapshot_hash: string } | null; after: { label: string; snapshot_hash: string } | null };
  };
}

export function hasPackage(p: ProposalDetail): p is ProposalDetail & { review_package: ReviewPackage } {
  return Boolean(p.review_package && "summary" in p.review_package);
}
