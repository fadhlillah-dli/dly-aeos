// Typed client for the Verification & Delivery API (§34, §35). All lists are domain-scoped by the backend.
import { get, post } from "../../api/client";

export type Level = "E0" | "E1" | "E2" | "E3" | "E4";

export interface EvidenceItem {
  id: string;
  project_id: string | null;
  domain: string | null;
  subject_type: string;
  subject_id: string;
  run_id: string | null;
  task_id: string | null;
  claim: string;
  kind: string;
  origin: string;
  collector_identity: string;
  trusted: boolean;
  content_hash: string;
  content_type: string;
  classification: string;
  has_content: boolean;
  size_bytes: number | null;
  evidence_level: Level;
  level_explanation: string;
  level_requested: string | null;
  level_reason: string | null;
  environment: Record<string, unknown>;
  created_at: string;
  mode: string;
}

export interface EvidenceList {
  items: EvidenceItem[];
  source: string;
  checked_at: string;
  mode: string;
  level_explanations: Record<string, string>;
  trusted_collectors: string[];
}

export interface EvidenceDetail extends EvidenceItem {
  inline: Record<string, unknown>;
  integrity: "VERIFIED" | "MISMATCH" | "MISSING" | "NO_CONTENT";
  content: string | null;
  content_json?: unknown;
  content_note: string | null;
}

export interface Finding {
  severity?: string;
  title?: string;
  detail?: string;
  path?: string | null;
  required_action?: string;
  source?: string;
}

export interface ReviewItem {
  id: string;
  run_id: string | null;
  task_id: string | null;
  reviewer_kind: string;
  reviewer_identity: string;
  model_requested: string | null;
  model_resolved: string | null;
  status: "COMPLETED" | "WAITING_QUOTA" | "NEEDS_DECISION" | "FAILED";
  status_reason: string;
  review: Record<string, unknown>;
  verify: Record<string, unknown>;
  non_regression: Record<string, { status?: string; note?: string }>;
  verdict: string;
  verdict_requested: string | null;
  findings: Finding[];
  evidence_refs: string[];
  unknowns: string[];
  package_hash: string | null;
  package_evidence_id: string | null;
  repair_status: string | null;
  mode: string;
  created_at: string;
  is_acceptance: false;
}

export interface AcceptanceItem {
  id: string;
  task_id: string;
  run_id: string;
  acceptance_status: string;
  artifact_hash: string;
  base_commit: string | null;
  head_commit: string | null;
  config_hash: string | null;
  plan_version_hash: string | null;
  review_verdict: string | null;
  reasons: string[];
  invalidated_reason: string | null;
  decided_by: string | null;
  mode: string;
  created_at: string;
  updated_at: string;
  valid: boolean;
  staleness: string[];
  is_human_approval: false;
}

export interface GateItem {
  gate: string;
  name: string;
  status: string;
  summary: string;
  source: string;
  evidence_refs: string[];
  reasons: string[];
}

export interface SeparateStatus {
  worker_reported: { status: string | null; exit_code?: number | null; trust: string };
  verifier: { status: string | null; evidence_id?: string | null; checked_at?: string | null };
  reviewer: { verdict: string | null; status: string | null; review_id?: string | null };
  accepted: { status: string | null; valid: boolean; acceptance_id?: string | null };
  merged: { status: string | null; merged_by?: string | null; merged_by_type?: string | null };
  security_reviewer?: { required: boolean; verdict: string | null; status: string | null; review_id: string | null };
}

export interface PRItem {
  id: string;
  task_id: string;
  repo: string;
  number: number | null;
  url: string | null;
  head_ref: string;
  base_ref: string;
  accepted_head_sha: string | null;
  observed_head_sha: string | null;
  state: string;
  merged: boolean;
  merged_by_login: string | null;
  merged_by_type: string | null;
  merge_commit_sha: string | null;
  last_synced_at: string | null;
  sync_status: string;
  sync_error: string;
  bundle_hash: string;
}

export interface GatesResponse {
  task_id: string;
  task_key: string;
  title: string;
  run_id: string | null;
  head_commit: string | null;
  gates: GateItem[];
  separate_status: SeparateStatus;
  acceptance: AcceptanceItem | null;
  pull_request: PRItem | null;
  ready_for_human_merge: boolean;
  merge_authority: "HUMAN_ONLY";
  checked_at: string;
  mode: string;
}

export interface SummaryItem extends SeparateStatus {
  run_id: string;
  task_id: string | null;
  task_key: string | null;
  task_title: string | null;
  attempt: number;
  execution_status: string;
  head_commit: string | null;
  mode: string;
}

export interface CapabilityCheck {
  key: string;
  status: string;
  summary: string;
  http_status: number | null;
  detail: Record<string, unknown>;
}

export interface ProviderCaps {
  provider: string;
  repo: string;
  branch: string | null;
  checked_at: string;
  credential: string;
  visibility: string | null;
  plan: string | null;
  checks: CapabilityCheck[];
  required_status_checks: string[];
  merge_gate: string;
  enforcement_tested: string;
  residual_risks: string[];
  source: string;
  mode: string;
}

export interface ListEnvelope<T> {
  items: T[];
  source: string;
  checked_at: string | null;
  mode: string;
  note?: string;
}

export function qs(params: Record<string, string | boolean | undefined | null>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
}

export const evidenceApi = {
  list: (filters: Record<string, string | boolean | undefined>) => get<EvidenceList>(`/api/evidence${qs(filters)}`),
  detail: (id: string) => get<EvidenceDetail>(`/api/evidence/${encodeURIComponent(id)}`),
  reviews: (filters: Record<string, string | undefined> = {}) => get<ListEnvelope<ReviewItem>>(`/api/reviews${qs(filters)}`),
  acceptance: (filters: Record<string, string | undefined> = {}) =>
    get<ListEnvelope<AcceptanceItem>>(`/api/acceptance${qs(filters)}`),
  summary: () => get<ListEnvelope<SummaryItem>>("/api/verification/summary"),
  gates: (taskId: string) => get<GatesResponse>(`/api/delivery/gates${qs({ task_id: taskId })}`),
  prs: () => get<ListEnvelope<PRItem> & { merge_authority: string }>("/api/delivery/prs"),
  capabilities: () => get<ProviderCaps>("/api/delivery/provider-capabilities"),
  verify: (runId: string) => post<{ status: string }>(`/api/runs/${encodeURIComponent(runId)}/verify`, {}),
  review: (runId: string, reviewer: "FEATURE" | "SECURITY" = "FEATURE") =>
    post<{ review: ReviewItem }>(`/api/runs/${encodeURIComponent(runId)}/review`, { reviewer }),
  recheck: (id: string) => post<AcceptanceItem>(`/api/acceptance/${encodeURIComponent(id)}/recheck`),
  preparePr: (taskId: string) =>
    post<{ pull_request: PRItem; notes?: string[] }>(`/api/tasks/${encodeURIComponent(taskId)}/prepare-pr`, {}),
  handoff: (taskId: string) => post<{ output: { result: string } }>(`/api/delivery/governance/${encodeURIComponent(taskId)}/handoff`, {}),
  syncPr: (id: string) => post<{ notes: string[] }>(`/api/delivery/prs/${encodeURIComponent(id)}/sync`),
  recordCapabilities: () => post<ProviderCaps>("/api/delivery/provider-capabilities/record", {}),
};
