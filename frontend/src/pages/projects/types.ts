// API shapes of the Gateway & Portfolio, Intake and Knowledge modules (backend aeos.portfolio/intake/knowledge).
import type { Domain } from "../../auth/AuthContext";

export interface Finding {
  kind: "SECRET" | "UNTRUSTED_INSTRUCTION" | "UNSCANNED" | string;
  source?: string;
  severity?: string;
  marker?: string;
  line?: number;
  lines?: number[];
  excerpt?: string;
  message?: string;
}

export interface InventoryView {
  commit: string;
  head_commit?: string | null;
  commit_is_head?: boolean;
  branch: string | null;
  checked_at: string;
  source: string;
  files: number;
  total_bytes: number;
  truncated: boolean;
  working_tree: { dirty: boolean | null; entries?: number; error?: string };
  languages: { language: string; files: number; bytes: number }[];
  dependency_manifests: { path: string; ecosystem: string; lockfiles: string[]; lockfile_status: string }[];
  lockfiles: string[];
  tests: { directories: string[]; files_sample: string[]; frameworks: string[]; baseline: { status: string; reason: string } };
  ci: Record<string, string[]>;
  deployment: Record<string, string[]>;
  contracts: Record<string, string[]>;
  docs: Record<string, string[]>;
  symlinks: string[];
  submodules: string[];
  sensitive_paths: string[];
  coverage_gaps: string[];
  inventory_hash: string;
  evidence_level: string;
}

export interface RepositoryView {
  id: string;
  project_id: string;
  provider: "GITHUB" | "BITBUCKET" | string;
  full_name: string;
  role: "platform" | "project" | string;
  url: string;
  verification_status: string;
  verification_reason: string;
  last_verified_at: string | null;
  local_path: string | null;
  local_status: string;
  head_commit: string | null;
  local_branch: string | null;
  dirty: boolean | null;
  local_checked_at: string | null;
  provider_repo_id?: string | null;
  default_branch?: string;
  url_source?: string;
  github?: { private?: boolean; visibility?: string; fork?: boolean; parent?: string | null; archived?: boolean } | null;
  local?: { origin_url?: string | null; origin_matches?: boolean; reason?: string; error?: string; dirty_entries?: number } | null;
  last_attempt?: { at?: string; auth_mode?: string; http_status?: number; error?: string } | null;
  inventory?: InventoryView | null;
  inventory_commit?: string | null;
  inventory_at?: string | null;
}

export interface Remedy {
  issue: string;
  command: string;
}

export interface CapabilityStatus {
  configuration?: string;
  root?: string;
  root_id_name?: string | null;
  credential?: { status: string; reason?: string; backend?: string; expires_at?: string | null };
  connectivity?: string;
  functional?: string;
  write_enabled?: boolean;
  office_default_deny?: boolean;
  atlassian_deployment?: string;
  adapter_enabled?: boolean;
  /** Exact identity an approval binds and the publisher writes to ({provider, root_id}). */
  target?: { provider: string; root_id: string };
  doctor_checks?: string[];
  remedies?: Remedy[];
  computed_at?: string;
}

export interface IntegrationView {
  id: string;
  project_id: string;
  domain: Domain;
  provider: string;
  purpose: string;
  tenant_id: string | null;
  root_id: string | null;
  root_id_name: string | null;
  credential_ref: string | null;
  config: Record<string, unknown>;
  capability_status: CapabilityStatus;
  last_verified_at: string | null;
  updated_at: string | null;
  target_url: string | null;
}

export interface ProjectView {
  id: string;
  project_key: string;
  name: string;
  domain: Domain;
  lifecycle: string;
  lifecycle_reason: string;
  data_classification: string;
  project_kind: "NEW" | "EXISTING" | string;
  is_platform: boolean;
  mode: string;
  owner_user_id: string | null;
  created_at: string;
  updated_at: string;
  repositories: RepositoryView[];
  integrations: IntegrationView[];
  integration_state: { docs: string; issues: string };
  links: { plans: string; publications: string; dag: string; detail: string };
  description?: string;
  idea?: string;
  input_findings?: Finding[];
  attachments?: Record<string, number>;
}

export interface ListResponse<T> {
  items: T[];
  source?: string;
  checked_at?: string;
  mode?: string;
}

export interface AttachmentView {
  id: string;
  project_id: string;
  filename: string;
  size_bytes: number;
  sha256: string;
  declared_type: string;
  detected_type: string;
  classification: string;
  provenance: { uploaded_by: string | null; uploaded_at: string; source: string };
  scan_status: string;
  scan_findings: Finding[];
  untrusted_instruction: boolean;
  parse_status: string;
  parse_error: string;
  parse_detail: { kind?: string; meta?: Record<string, unknown>; duplicate_of?: string; archive?: { members: number } };
  extracted_chars: number;
  stored_redacted: boolean;
  trusted: boolean;
  trust: string;
  created_at: string;
}

export interface IntakePolicy {
  allowed_extensions: string[];
  max_upload_bytes: number;
  max_archive_members: number;
  notice: string;
}

export interface PackItem {
  id: string;
  kind: string;
  ref: string;
  title: string;
  content?: string;
  provenance: Record<string, unknown>;
  classification: string;
  hash: string;
  tokens: number;
  trust: string;
  truncated: boolean;
  redacted: boolean;
}

export interface ContextPackView {
  id: string;
  project_id: string;
  task_id: string | null;
  role_key: string | null;
  base_commit: string | null;
  classification: string;
  token_budget: number | null;
  token_estimate: number;
  pack_hash: string | null;
  stale: boolean;
  freshness: string;
  stale_reason: string;
  repository: string | null;
  omitted: { kind: string; ref: string; reason: string; tokens: number }[];
  warnings: string[];
  forbidden_paths: string[];
  checked_at: string | null;
  expires_at: string | null;
  superseded_by: string | null;
  item_count: number;
  kinds: string[];
  items: PackItem[];
  created_at: string;
}

export interface MemoryView {
  id: string;
  project_id: string | null;
  scope: "PROJECT" | "DOMAIN" | string;
  content: string;
  provenance: Record<string, unknown>;
  classification: string;
  status: string;
  auth_decision_id: string | null;
  expires_at: string | null;
  superseded_by: string | null;
  created_at: string;
}

export interface SearchItem {
  kind: string;
  id: string;
  title: string;
  subtitle?: string;
  snippet?: string;
  project_id: string | null;
  status?: string | null;
  url: string;
}

export interface SearchResponse {
  items: SearchItem[];
  total: number;
  counts: Record<string, number>;
  has_more: boolean;
  limit: number;
  offset: number;
  checked_at: string;
  source: string;
}

export const CLASSIFICATIONS = ["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"] as const;
export const CODE_PROVIDER: Record<Domain, "GITHUB" | "BITBUCKET"> = { PERSONAL: "GITHUB", OFFICE: "BITBUCKET" };
// Backend: ARCHIVED/RETIRED projects are read-only (PROJECT_READ_ONLY); only a lifecycle change is accepted.
export const READ_ONLY_LIFECYCLES: readonly string[] = ["ARCHIVED", "RETIRED"];
