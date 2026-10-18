// API shapes of the planning & policy modules (backend/aeos/planning, backend/aeos/policy).

export interface VersionSummary {
  id: string;
  version: number;
  manifest_hash: string;
  parent_version_id: string | null;
  source: string;
  source_run_ids: string[];
  created_by: string | null;
  created_at: string | null;
  mode: string;
  schema_version: string;
}

export interface VersionView extends VersionSummary {
  content: PlanContent;
  hash_valid: boolean;
  is_current?: boolean;
  is_approved?: boolean;
}

export interface PlanSummary {
  id: string;
  title: string;
  project_id: string;
  project_name: string | null;
  domain: string;
  state: string;
  state_reason: string;
  needs_decision: boolean;
  current_version: VersionSummary | null;
  approved_version: VersionSummary | null;
  running: boolean;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface RunView {
  id: string;
  stage: string;
  role: string;
  cycle: number;
  status: string;
  model_requested: string;
  model_resolved: string | null;
  model_family: string;
  model_matches_policy: boolean | null;
  engine: string;
  engine_version: string | null;
  mode: string;
  input_hash: string | null;
  output_hash: string | null;
  usage: Record<string, unknown>;
  trace_ref: string | null;
  error_code: string | null;
  error: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  input_version_id: string | null;
  output_version_id: string | null;
  manual_package_id: string | null;
  artifact: { id: string; kind: string; content_hash: string } | null;
}

export interface StageView {
  stage: string;
  role: string;
  model: string;
  family: string;
  latest: RunView | null;
  history: RunView[];
}

export interface Statement {
  statement: string;
  source?: string;
  impact?: string;
}

export interface MaterialQuestion {
  question: string;
  category: string;
  blocking: boolean;
}

export interface AttachmentInput {
  id: string;
  filename: string;
  sha256: string;
  status: string;
  reason: string;
  classification: string;
  instruction_flags: string[];
  excerpt: string;
  trusted: boolean;
}

export interface ContextView {
  id: string;
  prompt: string;
  facts: Statement[];
  assumptions: Statement[];
  unknowns: Statement[];
  material_questions: MaterialQuestion[];
  constraints: { kind: string; statement: string; flags?: string[]; attachment_id?: string; source?: "prompt" | "attachment" }[];
  attachments: AttachmentInput[];
  repo_inventory: Record<string, unknown>;
  integration_capabilities: Record<string, unknown>;
  policy_ref: string | null;
  context_hash: string | null;
  status: string;
}

export interface Finding {
  key: string;
  severity: string;
  section?: string;
  description: string;
  recommendation: string;
  blocking?: boolean;
}

export interface ChallengeFindings {
  summary: string;
  overall_severity: string;
  contradictions: Finding[];
  missing_dependencies: Finding[];
  risks: Finding[];
  scope_issues: Finding[];
  other_findings: Finding[];
}

export interface FinalReview {
  review: Record<string, string | string[]>;
  verify: { claims: { claim: string; evidence: string; status: string }[]; test_ids: string[]; unknowns: string[]; environment: string; old_behavior: string };
  verdict: string;
  verdict_rationale: string;
  findings: Finding[];
}

export interface DecisionView {
  id: string;
  kind: string;
  status: string;
  stored_status: string;
  project_id: string | null;
  domain: string | null;
  actor_user_id: string;
  actor: string;
  actor_kind: string;
  auth_scope: Record<string, unknown>;
  target_type: string;
  target_id: string;
  target_version: string | null;
  target_hash: string | null;
  allowed_operations: string[];
  rationale: string;
  details: Record<string, unknown>;
  expires_at: string | null;
  created_at: string | null;
  invalidated_at: string | null;
  invalidation_reason: string | null;
  superseded_by: string | null;
}

export interface ManualInfo {
  stage: string;
  package_id: string;
  package_path: string;
  reason: string;
  steps: string[];
  note: string;
}

export interface ConfiguredTarget {
  provider: string;
  root_id: string;
  purpose?: string;
  integration_id?: string;
  label?: string;
}

export interface OperationCatalog {
  providers: string[];
  publish: Record<string, string[]>;
  delivery: Record<string, string[]>;
  plan: string[];
  /** Exact {provider, root_id} targets configured in the Integration Registry. */
  configured_targets?: ConfiguredTarget[];
}

export interface PlanDetail extends PlanSummary {
  policy_baseline: { stages?: { stage: string; role: string; model: string; family: string }[]; skippable?: boolean; change_rule?: string; policy_snapshot?: string };
  checkpoint: Record<string, unknown>;
  context: ContextView | null;
  versions: VersionSummary[];
  stages: StageView[];
  findings: ChallengeFindings | null;
  resolutions: { finding_key: string; action: string; note: string }[];
  verdict: FinalReview | null;
  verdict_effective: string | null;
  /** Version the final reviewer judged; a later human edit has not been reviewed by Fable. */
  verdict_version_id?: string | null;
  current_version_reviewed?: boolean;
  decisions: DecisionView[];
  manual: ManualInfo | null;
  allowed: { run: boolean; import_manual: boolean; approve: boolean; revise_or_reject: boolean; edit: boolean; compile: boolean };
  operation_catalog: OperationCatalog;
  evidence_limit: string;
  checked_at?: string;
}

export interface DiffSection {
  section: string;
  status: "unchanged" | "changed" | "added" | "removed";
  added?: string[];
  removed?: string[];
  changed?: string[];
  fields_changed?: string[];
  added_count?: number;
  removed_count?: number;
  count?: { from: number; to: number };
}

export interface DiffView {
  plan_id: string;
  from: { id: string; version: number; manifest_hash: string } | null;
  to: { id: string; version: number; manifest_hash: string };
  sections: DiffSection[];
  changed_sections: string[];
}

// Plan content (contracts/planning/plan-content.schema.json). Typed loosely: the viewer renders what is present.
export type PlanContent = Record<string, unknown> & {
  product_home?: Record<string, unknown>;
  overview?: Record<string, unknown>;
  requirements?: Record<string, unknown>[];
  architecture?: { overview?: Record<string, unknown>; diagrams?: { key: string; title: string; kind: string; mermaid: string; verification_source: string }[] };
  features?: { key: string; title: string; requirement: string; priority: string; acceptance: string[]; depends_on: string[]; milestone: string }[];
  milestones?: { key: string; title: string; exit_evidence: string; order: number }[];
  tasks?: Record<string, unknown>[];
  /** Planner output (DATA): its own facts/assumptions/unknowns/material questions for this version. */
  facts?: Statement[];
  assumptions?: Statement[];
  unknowns?: Statement[];
  material_questions?: MaterialQuestion[];
};

export interface DagNode {
  key: string;
  id: string | null;
  title: string;
  feature_key: string | null;
  goal: string;
  role_key: string;
  risk_class: string;
  allowed_paths: string[];
  forbidden_paths: string[];
  depends_on: string[];
  spec_hash: string | null;
  limits: Record<string, number | boolean>;
  data_classification: string;
  quarantine: boolean;
  acceptance_criteria: string[];
  /** TaskSpec provenance (LIVE | MOCK | DEMO). */
  mode?: string;
  runtime: { state: string; reason: string; attempts_used: number; active_run_id: string | null; updated_at?: string };
  lease: { owner: string; run_id: string; expires_at: string; heartbeat_at: string | null } | null;
  acceptance: { status: string; valid: boolean | null; source?: string };
  warnings: string[];
  accepted_prerequisites: string[];
  dependencies_ready: boolean;
  conflicts_with: string[];
}

export interface DagView {
  plan_id: string;
  plan_version_id?: string;
  version?: number;
  manifest_hash?: string;
  approved?: boolean;
  compiled: boolean;
  mode: string;
  /** Provenance of the plan content (LIVE | MOCK | DEMO). */
  data_mode?: string;
  nodes: DagNode[];
  edges: { id: string; source: string; target: string }[];
  critical_blockers: { task_key: string; state: string; reason: string; blocks: string[]; impact: number }[];
  shared_write_conflicts: { tasks: string[]; paths: string[]; serialized_by_dependency: boolean; concurrent_risk: boolean }[];
  validation_errors: Record<string, unknown>[];
  runtime_note?: string;
  checked_at: string;
  remedy?: string;
}

export interface QueueItem {
  id: string;
  kind: string;
  title: string;
  target: Record<string, unknown> & { id: string; type: string; hash?: string | null; version?: number | null };
  why: string;
  requested_decision: string;
  link: string;
  created_at: string | null;
  age_seconds: number | null;
  project_id: string | null;
  domain: string | null;
  priority: number;
  mode?: string;
}

export interface AdrSummary {
  key: string;
  title: string;
  status: string;
  source_path: string | null;
  content_hash: string | null;
  decision_id: string | null;
  supersedes: string | null;
  date: string | null;
  deciders: string | null;
  updated_at: string | null;
}

export interface AdrDetail extends AdrSummary {
  sections: Record<string, string>;
  meta: Record<string, unknown>;
  linked_decisions: { id: string; kind: string; status: string }[];
}
