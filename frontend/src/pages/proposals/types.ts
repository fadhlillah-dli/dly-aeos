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
  checked_at: string | null;
  research_date: string | null;
  flags: string[];
  note: string;
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
}

export interface CostItem {
  value: number | null;
  basis: string;
  note?: string;
  cap_usd?: number;
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
  baseline: { components: BaselineComponent[]; baseline_hash: string; note: string | null; architecture_snapshot?: { id: string; label: string; snapshot_hash: string } | null };
  alternatives: { option: string; applicable: boolean; summary: string; tradeoffs: string; unknowns: string[] }[];
  impact: Record<string, Dimension | string[]>;
  experiment_plan: Record<string, unknown> & { type: string; pins?: Record<string, string | null> };
  replacement_plan: Record<string, unknown> | null;
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
  attachment_ids: string[];
  unknown_fields: string[];
  parts: { child_id: string; title: string; kind: string; depends_on: string[] }[];
  review_package: ReviewPackage | Record<string, never>;
  review_hash: string | null;
  decisions: DecisionView[];
  evidence_ids: string[];
  outcome: { current?: OutcomeEntry; history?: OutcomeEntry[] };
  duplicates: { id: string; title: string; status: string }[];
  research_entries: { id: string; name: string; lifecycle: string; identity_status: string; checked_at: string | null }[];
  delivery_dimensions: Record<string, { status: string; note: string }>;
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
