// Shapes returned by /api/ops/* and /api/control-center/history/* (backend: aeos.operations,
// aeos.control_center).

export interface ListMeta {
  source?: string;
  checked_at?: string | null;
  mode?: string;
  evidence_limit?: string;
}

export interface AuditRow {
  seq: number;
  at: string;
  actor_type: string;
  actor_id: string;
  action: string;
  target_type: string;
  target_id: string;
  result: string;
  reason: string;
  details: Record<string, unknown>;
}

export interface DecisionRow {
  id: string;
  kind: string;
  status: string;
  actor: string;
  target_type: string;
  target_id: string;
  target_version: string | null;
  target_hash: string | null;
  rationale: string;
  created_at: string;
  invalidated_at: string | null;
  invalidation_reason: string | null;
}

export interface SnapshotRow {
  id: string;
  label: string;
  snapshot_hash: string;
  created_at: string;
  created_by: string | null;
  previous_id: string | null;
  diff: { path: string; change: "added" | "removed" | "changed"; before?: unknown; after?: unknown }[];
}

export interface PublicationRow {
  id: string;
  status: string;
  plan_version_id: string;
  approved_hash: string;
  targets: number;
  operations: Record<string, number>;
  mode: string;
  created_at: string;
  updated_at: string;
}

export interface RestoreTest {
  status: string;
  started_at: string;
  finished_at: string;
  problems: string[];
  warnings: string[];
  evidence_id: string | null;
  evidence_note: string;
  stats: Record<string, unknown>;
}

export interface BackupRow {
  backup_id: string;
  label?: string;
  created_at?: string;
  created_by?: string;
  size_bytes?: number;
  schema_revision?: string | null;
  db_sha256?: string;
  archives?: { domain: string; file_count: number; size: number }[];
  excluded_file_count?: number;
  readiness: string;
  ready: boolean;
  retention?: string;
  last_verification?: { ok: boolean; checked_at: string; problems: string[] } | null;
  last_restore_test?: RestoreTest | null;
  note?: string;
}

export interface BackupList extends ListMeta {
  items: BackupRow[];
  recovery_ready: boolean;
  latest_restore_tested: string | null;
}

export interface ReleaseRow {
  release_id: string;
  version: string;
  commit: string;
  artifact_sha256: string;
  schema_head: string | null;
  built_at: string;
  integrity: string;
  role: string;
}

export interface ReleasePointer {
  release_id: string;
  version: string;
  activated_at: string;
  activated_by: string;
  decision_id: string | null;
  pre_backup_id: string | null;
}

export interface ReleaseList extends ListMeta {
  items: ReleaseRow[];
  active: ReleasePointer | null;
  previous: ReleasePointer | null;
  history: (ReleasePointer & { action: string; at: string; from_release_id: string | null })[];
  notice: string;
}

export interface StepResult {
  step: string;
  status: string;
  reason?: string;
  detail?: string;
  [k: string]: unknown;
}

export interface EmergencyReport {
  incident_id: string;
  status: string;
  steps: StepResult[];
  notice: string;
}

export interface SliItem {
  key: string;
  title: string;
  unit: string;
  value: number | null;
  status: string;
  target: { max?: number | null; min?: number | null } | null;
  scope: string;
}

export interface AlertRow {
  id: string;
  kind: string;
  severity: string;
  title: string;
  status: string;
  occurrences: number;
  last_seen_at: string;
  domain: string | null;
}

export interface FlagRow {
  key: string;
  kind: string;
  value: unknown;
  default: unknown;
  is_default: boolean;
  description: string;
  consumer: string;
}
