// API shapes served by aeos.publisher.router and aeos.docs_engine.router.

export interface SectionView {
  key: string;
  title: string;
  anchor: string;
  content_type: string;
  status: string;
  note?: string;
}

export interface ExternalLink {
  provider: string;
  external_id: string;
  url: string | null;
  revision: string | null;
  sync_status: string;
  last_synced_at: string | null;
}

export interface DocItem {
  id: string;
  logical_doc_id: string;
  node: string;
  title: string;
  kind: "DOC" | "FOLDER";
  doc_type: string;
  parent_logical_id: string | null;
  owner: string | null;
  status: string;
  status_reason: string;
  content_type: string;
  timing: string;
  template_key: string | null;
  source_plan_version_id: string | null;
  content_hash: string | null;
  external_revision: string | null;
  last_reviewed_at: string | null;
  last_synced_at: string | null;
  rendered_at: string | null;
  links: Record<string, unknown>;
  pending_sections: string[];
  documentation_pending: boolean;
  sections: SectionView[];
  order_index: number;
  mode: string;
  external: ExternalLink[];
  freshness: { rendered_at: string | null; last_synced_at: string | null; external_stale: boolean };
  open_revision_inputs: number;
  evidence?: { id: string; kind: string; trusted: boolean; evidence_level: string; origin: string; mode: string }[];
}

export interface DiffResponse {
  document_id: string;
  base_revision_id: string | null;
  base_kind: string | null;
  diff: string;
  truncated: boolean;
  changed: boolean;
}

export interface RevisionItem {
  id: string;
  kind: string;
  status: string;
  observed_revision: string | null;
  expected_revision: string | null;
  created_at: string | null;
  excerpt: string;
}

export interface DocumentsResponse {
  items: DocItem[];
  product_title: string;
  source: string;
  checked_at: string | null;
  mode: string;
  summary: { documents: number; folders: number; pending: number; published: number; conflicts: number };
  remedy: string | null;
}

export interface RenderResponse {
  id: string;
  title: string;
  markdown: string;
  status: string;
  content_type: string;
  pending_sections: string[];
  rendered_at: string | null;
  source_plan_version_id: string | null;
}

export interface Dimension {
  status: string;
  detail: string;
  source: string;
}

export interface TargetView {
  purpose: string;
  family: string;
  label: string;
  status: string;
  reasons: string[];
  remedies: string[];
  config: Record<string, unknown>;
}

export interface OperationView {
  id: string;
  order_index: number;
  provider: string;
  operation: string;
  logical_id: string;
  title: string;
  status: string;
  attempts: number;
  max_attempts: number;
  external_id: string | null;
  external_url: string | null;
  external_revision: string | null;
  error: string;
  next_attempt_at: string | null;
  last_attempt_at: string | null;
  reconciled_at: string | null;
  depends_on: string[];
  idempotency_key: string;
  payload_hash: string;
}

export interface PublicationView {
  id: string;
  project_id: string;
  domain: string;
  plan_id: string | null;
  plan_version_id: string;
  plan_version_number: number | null;
  approved_hash: string;
  preview_hash: string | null;
  decision_id: string;
  publication_decision_id: string | null;
  status: string;
  status_reason: string;
  targets: TargetView[];
  blocked_targets: TargetView[];
  mode: string;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  counts: Record<string, number>;
  preview: {
    operations_total?: number;
    operations_executable?: number;
    notes?: string[];
    documents_delta?: Record<string, string[]>;
    visuals?: { key: string; title: string; status: string; reason: string }[];
    already_done?: unknown[];
  };
  status_dimensions: Record<string, Dimension>;
  approval_statement: string;
  attention: string[];
  operations?: OperationView[];
}

export interface ListResponse<T> {
  items: T[];
  source?: string;
  checked_at?: string | null;
  mode?: string;
}

export interface Candidate {
  plan_id: string;
  title: string;
  state: string;
  current_version_id: string | null;
  approval_status: string;
  approval_reason?: string;
  approved: { plan_version_id: string; version: number; approved_hash: string; is_latest: boolean } | null;
}

export interface WorkItem {
  id: string;
  task_id: string | null;
  provider: string;
  issue_id: string;
  issue_url: string | null;
  external_status: string;
  last_synced_at: string | null;
  sync_status: string;
  sync_error: string;
  logical_id: string | null;
  feature_key: string | null;
  title: string;
  authority: string;
  unlocks_runtime_dependencies: boolean;
}

export interface WorkItemsResponse extends ListResponse<WorkItem> {
  authority: string;
  note: string;
  capabilities: Record<string, string>;
}

export interface Connections {
  domain: string;
  checked_at: string;
  google?: {
    status: string;
    requested_scopes: string[];
    granted_scopes?: string[];
    remedy: string;
    reason?: string;
    client?: { exists: boolean; updated_at: string | null } | null;
    token?: { exists: boolean; updated_at: string | null } | null;
  };
  github?: { status: string; remedy: string; reason?: string; capabilities?: Record<string, string> };
  atlassian?: { status: string; reason: string; deployment_support: Record<string, string> };
}
