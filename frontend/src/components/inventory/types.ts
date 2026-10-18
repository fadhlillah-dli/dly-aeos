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
    maintainer: { value: string | null; status: string; reason: string };
    authorization_scope: { value: string | null; status: string };
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
    config_revision: { value: string | null; status: string; reason: string };
    lockfile_refs: string[];
    provenance_note: string;
  };
  usage_role: {
    usage_role: string;
    requirement: string;
    design_label: string;
    why_selected: string;
    placement: string;
    used_by: { component_id: string; name: string; relation: string; lens: string }[];
    consuming_tasks: { items: unknown[]; status: string; reason: string };
    bootstrap_order: number | null;
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
  }[];
  cost: { design: Record<string, unknown>; actual: Record<string, unknown> };
  operations: Record<string, unknown> & { probe?: Record<string, unknown>; capability_tests?: Record<string, unknown>[] };
  changes: {
    decision_refs: string[];
    proposals: { id: string; kind: string; title: string; status: string; created_at: string | null }[];
    research_entries: { id: string; name: string; lifecycle: string; checked_at: string | null }[];
    manifest_hash: string | null;
    manifest_record: { id: string; loaded_at: string | null } | null;
    previous_accepted_configuration: { value: string | null; status: string; reason: string };
    propose_url: string;
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
  coverage: Coverage;
  source: string;
  checked_at: string | null;
  mode: string;
  note: string;
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
}

export interface SnapshotDiff {
  a: SnapshotMeta;
  b: SnapshotMeta;
  same_hash: boolean;
  nodes: { added: { id: string }[]; removed: { id: string }[]; changed: { key: string; fields: Record<string, [unknown, unknown]> }[] };
  edges: {
    added: { source: string; target: string; relation: string; lens: string }[];
    removed: { source: string; target: string; relation: string; lens: string }[];
    changed: { key: string[]; fields: Record<string, [unknown, unknown]> }[];
  };
  refs_changed: Record<string, unknown>;
  note: string;
}
