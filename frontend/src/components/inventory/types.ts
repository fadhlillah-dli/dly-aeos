// Response shapes of /api/inventory/* (backend: aeos.inventory.views / snapshots).

export interface DimensionValue {
  value: string;
  effective: string;
  at: string | null;
  expires_at: string | null;
  stale: boolean;
  source: string;
  observation_id: string | null;
  version: string | null;
  reason: string | null;
  // usage from run/lease/ledger records: the run/operation reference (§38 "Digunakan saat ini")
  refs?: Record<string, unknown>;
  basis?: string[];
  decision_refs?: string[];
}

/** §35 "tindakan berikutnya": one deterministic next step a human can take (never executed by AEOS). */
export interface NextAction {
  code: string;
  label: string;
  command: string | null;
  link: string | null;
}

export interface ProjectRepository {
  id: string;
  repository_id: string;
  kind: "project_repository";
  project_id: string;
  project_name: string;
  domain: string;
  provider: string;
  full_name: string;
  provider_repo_id: string | null;
  identity: string | null;
  identity_status: string;
  role: string;
  default_branch: string;
  head_commit: string | null;
  dirty: boolean | null;
  verification_status: string;
  last_verified_at: string | null;
  link: string;
}

export interface ProjectService {
  id: string;
  kind: "project_service";
  name: string;
  detail?: string | null;
  project_id: string;
  project_name: string;
  domain: string;
  status: string;
  observed: string;
  observed_reason: string;
  provenance: string;
  lifecycle: string;
}

export type Dimensions = Record<
  "admission" | "installation" | "configuration" | "connectivity" | "functional" | "enablement" | "usage" | "lifecycle_final",
  DimensionValue
>;

export interface VersionStatus {
  approved_pin: string | null;
  observed_version: string | null;
  status: "MATCH" | "MISMATCH" | "UNKNOWN" | "NO_PIN";
  pin_note: string | null;
}

export interface DriftMini {
  drift_class: string;
  label: string;
  severity: string;
}

export interface ComponentRow {
  component_id: string;
  name: string;
  friendly_name: string;
  kind: string;
  category: string;
  category_label: string;
  domains: string[];
  requirement: string;
  design_label: string;
  identity_verified: string;
  source_url: string | null;
  source_kind: string;
  usage_role: string;
  bootstrap_order: number | null;
  version: VersionStatus;
  dimensions: Dimensions;
  drift: DriftMini[];
  config_lens: { present: boolean; basis: string[] };
  tombstoned_at: string | null;
  mode: string;
  function?: string;
  why_selected?: string;
  next_action?: NextAction;
  // advanced view
  source_refs?: string[];
  upstream_identity?: string | null;
  license?: string | null;
  manifest_hash?: string | null;
  relations?: number;
  placement?: string;
  decision_refs?: string[];
}

export interface CoverageGap {
  code: string;
  severity: string;
  message: string;
}

export interface Coverage {
  totals: Record<string, number>;
  gaps: CoverageGap[];
  last_reconciled_at: string | null;
  last_full_reconciled_at: string | null;
  sources?: { source: string; status: string; entries: number; at: string; detail: Record<string, unknown> }[];
}

export interface ComponentList {
  items: ComponentRow[];
  total: number;
  page: number;
  page_size: number;
  view: string;
  facets: { kinds: string[]; requirements: string[]; categories: string[]; design_labels: string[] };
  coverage: Coverage;
  manifest: { record_id: string; content_hash: string; loaded_at: string; manifest_version: string } | null;
  source: string;
  checked_at: string | null;
  mode: string;
  project_repositories?: ProjectRepository[];
  project_services?: ProjectService[];
  routes?: RouteRecord[];
  roles?: RoleRecord[];
}

export interface RouteRecord {
  id: string;
  kind: "route";
  route_key: string;
  status: string;
  pool: string;
  provider: string;
  provider_component: string | null;
}

export interface RoleRecord {
  id: string;
  kind: "role";
  role_key: string;
  name: string;
  agents: { agent_key: string; harness: string }[];
  harness_components: string[];
}

export interface EdgeTest {
  check_id: string;
  status: string;
  last_checked_at: string | null;
  summary: string;
  about: string;
}

export interface DependencyItem {
  name: string;
  version: string | null;
  relation: "direct" | "transitive";
  direct: boolean;
  required_by: string[];
  parent_path: string[];
  ecosystem: string;
  purl: string;
  dev?: boolean;
  marker?: string | null;
  used_by_build?: { component_id: string; lockfile: string; label: string };
}

export interface DependencyPage {
  items: DependencyItem[];
  total: number;
  page: number;
  page_size: number;
  status: string;
  reason: string | null;
  counts?: { direct: number; transitive: number; total: number };
  lockfile: { path: string; status: string; sha256: string | null; matches_last_reconcile: boolean | null } | null;
  coverage: { images: string };
}

export interface RelationView {
  id: string;
  source: string;
  target: string;
  source_name: string;
  target_name: string;
  relation: string;
  lens: string;
  status: string;
  provenance: string;
  data_description: string;
  operation: string;
  boundary: string | null;
  identity_ref: string | null;
  direction: string;
  last_observed_at: string | null;
  freshness: { at: string | null; stale: boolean };
  tombstoned_at: string | null;
  evidence: string;
  // §36 edge card "hasil pengujian": latest Doctor results about either end
  tests?: EdgeTest[];
  created_at?: string | null;
}

export interface Claim {
  claim: string;
  level: string | null;
  status: string;
  source: string;
  at: string | null;
  note?: string | null;
}

export interface ComponentDetail {
  component_id: string;
  mode: string;
  tombstoned_at: string | null;
  identity: {
    component_id: string;
    kind: string;
    name: string;
    friendly_name: string;
    function: string;
    category: string;
    category_label: string;
    source_kind: string;
    upstream_identity: string | null;
    upstream_identity_status: string;
    instances: string[] | null;
    instances_note: string | null;
  };
  scope: {
    level: string;
    domains: string[];
    environment: string;
    owner: string;
    maintainer: { value: string | null; status: string; reason?: string };
    authorization_scope: {
      value: string | null;
      status: string;
      integrations?: { project_id: string; provider: string; purpose: string; tenant_id: string | null; root_id: string | null }[];
      routes?: { route_key: string; status: string; eligible_domains: string[] }[];
    };
  };
  source: {
    source_url: string | null;
    source_url_note: string | null;
    source_refs: string[];
    identity_verified: string;
    publisher: { value: string | null; status: string; reason: string };
    license: { value: string | null; status: string; reason: string | null };
    retrieval_time: string | null;
    unresolved_identity: boolean;
  };
  versions: VersionStatus & {
    config_revision: { value: unknown; status: string; reason?: string };
    lockfile_refs: string[];
    provenance_note: string;
    approved_digest?: string | null;
    /** §07: the <os>_<arch> the approved digest was selected for (this host) */
    approved_digest_platform?: string | null;
    /** platforms the reviewed per-platform mapping records ([] for the one-digest form) */
    approved_digest_platforms?: string[];
    observed_digest?: string | null;
    digest_status?: string;
    distribution?: Record<string, unknown>;
    signature?: Record<string, unknown>;
    tool_schema_hash?: string | null;
  };
  usage_role: {
    usage_role: string;
    requirement: string;
    design_label: string;
    why_selected: string;
    placement: string;
    used_by: { component_id: string; name: string; relation: string; lens: string }[];
    consuming_tasks: {
      items: { run_id: string; task_key: string | null; status: string; route_key: string | null; finished_at: string | null }[];
      status: string;
      reason: string | null;
      routes?: { route_key: string; status: string; pool: string }[];
      agents?: { agent_key: string; role_key: string; enabled: boolean }[];
    };
    bootstrap_order: number | null;
  };
  next_action?: NextAction;
  admission?: {
    status: string;
    decision_refs: string[];
    capability_tests: { id: string; result: string; evidence_id: string | null; at: string | null; stale: boolean | null }[];
  };
  lifecycle: Dimensions;
  config_lens: { present: boolean; basis: string[]; note: string };
  access: Record<string, unknown>;
  relations: { outgoing: RelationView[]; incoming: RelationView[] };
  evidence: {
    checks: { check_id: string; title: string; group: string; status: string; severity: string; summary: string; last_checked_at: string | null; expires_at: string | null }[];
    observations: {
      id: string;
      dimension: string;
      status: string;
      observer: string;
      observed_version: string | null;
      observed_identity: string | null;
      location: string | null;
      operation_ref: string | null;
      observed_at: string;
      expires_at: string | null;
      stale: boolean;
      detail: Record<string, unknown>;
    }[];
    claims: Claim[];
    last_checked_at: string | null;
    last_observed_at: string | null;
    last_used_at: string | null;
    drift: { id: string; drift_class: string; label: string; severity: string; summary: string; first_detected_at: string | null }[];
  };
  dependencies: {
    source: string | null;
    observed_at: string | null;
    stale: boolean;
    packages: { name: string; spec: string; resolved?: string | null; installed?: string | null; group: string }[] | null;
    actions: { uses: string; ref: string | null; pinned_sha: boolean; file: string }[] | null;
    recommendations: string[] | null;
    transitive_packages: number | null;
    package: string | null;
    spec: string | null;
    version: string | null;
    used_by_build?: { component_id: string; lockfile: string; label: string } | null;
    dependencies_url?: string | null;
  }[];
  cost: {
    design: Record<string, unknown>;
    actual: Record<string, unknown>;
    free_expiry?: { value: string | null; status: string; source?: string; reason?: string };
    budget_source?: { items: { budget_id: string; scope_type: string; scope_id: string; period: string; limit_usd: number | null }[]; status: string; reason: string | null };
    quota_source?: { items: { provider: string; state: string; source: string; observed_at: string | null }[]; status: string; reason: string | null };
  };
  operations: Record<string, unknown> & { probe?: Record<string, unknown>; capability_tests?: Record<string, unknown>[] };
  changes: {
    decision_refs: string[];
    proposals: { id: string; kind: string; title: string; status: string; created_at: string | null }[];
    research_entries: { id: string; name: string; lifecycle: string; checked_at: string | null }[];
    manifest_hash: string | null;
    manifest_record: { id: string; loaded_at: string | null } | null;
    previous_accepted_configuration: { value: unknown; status: string; reason?: string; at?: string | null };
    propose_url: string;
    decisions?: { id: string; kind: string; status: string; via: string; binding_hash: string | null; baseline_hash: string | null; candidate_hash: string | null; created_at: string | null; proposal_id: string | null }[];
  };
  checked_at: string;
}

export interface MapNode {
  id: string;
  label: string;
  name: string;
  kind: string;
  category: string;
  requirement: string;
  design_label: string;
  domains: string[];
  identity_verified: string;
  present: boolean;
  status: string;
  at: string | null;
  stale: boolean;
  basis: string[];
  unknown?: boolean;
  drift: DriftMini[];
  function: string;
  // §36 zoom-in: authorized active runs on this component (never fictional agent instances)
  runs?: { run_id: string; task_key: string | null; status: string; attempt: number; lease_expires_at: string | null }[];
  record?: boolean;
}

export interface MapGroup {
  id: string;
  label: string;
  node_ids: string[];
  counts: { total: number; present: number; drift: number };
}

export interface SystemMap {
  lens: string;
  lens_label: string;
  lens_source: string;
  nodes: MapNode[];
  edges: RelationView[];
  groups: MapGroup[];
  drift: { component_id: string; drift_class: string; label: string; severity: string; summary: string }[];
  drift_summary: Record<string, number>;
  legend: { key: string; label: string }[];
  coverage: Coverage | null;
  source: string;
  checked_at: string | null;
  mode: string;
  note: string;
  filters?: { project_id: string | null; environment: string | null; category: string | null; environments: string[] };
  snapshot?: SnapshotMeta;
}

export interface BlastRadius {
  root: string;
  direction: string;
  items: { component_id: string; name: string; category: string; requirement: string; depth: number; path: string[]; via: string[] }[];
  edges: { source: string; target: string }[];
  summary: { affected: number; by_category: Record<string, number> };
  note: string;
}

export interface PathResult {
  found: boolean;
  source: string;
  target: string;
  path: string[];
  edges: { source: string; target: string; relation: string }[];
  directed: boolean | null;
  note: string | null;
}

export interface SnapshotMeta {
  id: string;
  label: string;
  snapshot_hash: string;
  created_at: string;
  created_by: string | null;
  scope: { domain: string | null; lenses: string[] } | null;
  taken_at: string | null;
  counts: { nodes: number; edges: number; decisions: number };
  manifest_hash: string | null;
  approved?: boolean;
  approval?: { decision_id: string; approved_at: string | null } | null;
}

export interface SnapshotDiff {
  a: SnapshotMeta;
  b: SnapshotMeta;
  same_hash: boolean;
  both_approved?: boolean;
  approval_note?: string | null;
  nodes: { added: { id: string }[]; removed: { id: string }[]; changed: { key: string; fields: Record<string, [unknown, unknown]> }[] };
  edges: {
    added: { source: string; target: string; relation: string; lens: string }[];
    removed: { source: string; target: string; relation: string; lens: string }[];
    changed: { key: string[]; fields: Record<string, [unknown, unknown]> }[];
  };
  refs_changed: Record<string, unknown>;
  note: string;
}
