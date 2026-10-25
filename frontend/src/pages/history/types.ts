// Shapes returned by /api/ops/* and /api/control-center/history/* (backend: aeos.operations,
// aeos.control_center).
import type { RetentionMarker } from "../../components/control_center/RetentionBadge";

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
  retention?: RetentionMarker;
}

/** One data class of GET /api/ops/retention: the owner's rule and how it applies to stored records. */
export interface RetentionRow {
  data_class: string;
  domain: string;
  rule: string;
  retain_days: number | null;
  label: string;
  records: number | null;
  expired_by_policy: number | null;
  evaluated: boolean;
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
  stats: Record<string, unknown> & { files_verified_by_partition?: Record<string, number> };
  // Domain-scoped view (§35): per-partition numbers only for the active domain + platform.
  scope?: string;
}

export interface HashVerification {
  ok: boolean;
  checked_at?: string;
  problems: string[];
  files_checked_by_partition?: Record<string, number>;
  scope?: string;
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
  last_verification?: HashVerification | null;
  last_restore_test?: RestoreTest | null;
  restore_test_count?: number;
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
  scope?: string;
}

export interface ServiceRow {
  service: string;
  kind: string;
  status: string;
  detail: string;
  version?: string;
}

export interface RunbookRow {
  id: string;
  title: string;
  trigger: string;
  path: string;
}

export interface RetirementItem {
  key: string;
  title: string;
  status: string;
  detail: Record<string, unknown>;
  owner_action: string;
}

export interface RetirementChecklist {
  project: { id: string; name: string; domain: string; lifecycle: string };
  items: RetirementItem[];
  blocking: string[];
  /** Hash of the items the owner verifies (final verification row excluded). */
  checklist_hash?: string;
  notice: string;
  checked_at: string;
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
