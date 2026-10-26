// API shapes of the observe-only deployment targets (backend aeos.delivery.deployment, delivery router):
// GET /api/delivery/targets?project_id=, PUT /api/delivery/targets/{integration_id}/checks,
// POST /api/delivery/targets/observe. AEOS never deploys, releases or rolls back (§31, §40).

export type Environment = "staging" | "production";

export interface SmokeCheck {
  name: string;
  path: string;
  expect_status: number;
  json_keys: string[];
  kind: "smoke" | "critical_flow" | string;
}

export interface SliCheck {
  name: string;
  path: string;
  json_key: string;
  /** owner thresholds; both null = NO_TARGET (report only, never "within target") */
  max: number | null;
  min: number | null;
}

export interface TargetSettings {
  integration_id: string;
  base_url: string;
  health_path?: string;
  smoke_checks?: SmokeCheck[];
  sli_checks?: SliCheck[];
  rollback_on?: string[];
  rollback_criteria?: "RECORDED" | "UNDECIDED" | string;
  require_artifact_digest?: boolean;
}

export interface ArtifactState {
  status?: string; // NOT_REPORTED | NOT_RECORDED | MATCH | MISMATCH
  reported_digest?: string | null;
  recorded_digests?: string[];
  digest_evidence_id?: string | null;
}

export interface ObservationView {
  status: string; // PASS | FAIL | STALE | NOT_PROVEN | NO_EVIDENCE
  evidence_id: string | null;
  reason?: string | null;
  commit?: string;
  failure_conditions?: string[];
  check_evidence_ids?: string[];
  artifact?: ArtifactState;
  observed_at?: string | null;
}

export interface TargetView {
  integration_id: string;
  project_id: string;
  domain: string;
  environment: Environment | string;
  settings: TargetSettings;
  config_problem: string | null;
  target_config_hash: string | null;
  latest_observation: ObservationView;
  observe_only: boolean;
  note?: string;
  source?: string;
  checked_at?: string | null;
  generated_at?: string;
}

export interface TargetList {
  items: TargetView[];
  source?: string;
  checked_at?: string | null;
  generated_at?: string;
  mode?: string;
  config_fields?: Record<string, unknown>;
}

export interface CheckResult {
  name: string;
  kind?: string;
  path?: string;
  status: string; // smoke: PASS | FAIL | NOT_PROVEN; SLI: WITHIN_TARGET | BREACH | NO_TARGET | UNKNOWN
  reason?: string;
  http_status?: number | null;
  value?: number | null;
  evidence_id?: string;
}

export interface PostRelease {
  alert_id?: string;
  alert_new?: boolean;
  alert_status?: string;
  incident_id?: string | null;
  incident_created?: boolean;
  decision_request_id?: string | null;
  decision_request_new?: boolean;
  /** an object on a FAIL ({status, summary, criteria_met, ...}); absent on a recovery */
  recommendation?: { status?: string; summary?: string; criteria_met?: string[] } | string | null;
  options?: string[];
  runbook?: string;
}

export interface ObserveResult {
  status: string; // PASS | FAIL | NOT_PROVEN
  reason: string;
  evidence_id: string;
  target: string;
  environment: string;
  checks: CheckResult[];
  slis: CheckResult[];
  artifact: ArtifactState;
  failure_conditions: string[];
  post_release: PostRelease | null;
}

/** Same rules as the backend (aeos.delivery.deployment); the backend stays authoritative and answers 422. */
export const TARGET_PATH = /^\/(?!\/)[A-Za-z0-9._~/-]{0,200}$/;
export const CHECK_NAME = /^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$/;
export const JSON_KEY = /^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}(?:\.[A-Za-z0-9_][A-Za-z0-9_-]{0,63}){0,4}$/;
export const ROLLBACK_CONDITIONS = ["health_fail", "smoke_fail", "critical_flow_fail", "sli_breach", "artifact_digest_mismatch"] as const;
export const MAX_CHECKS = 20;
export const MAX_JSON_KEYS = 10;

/** The one target-path rule: absolute path on the target itself, no host, query or '..' segment. */
export function targetPathOk(path: string): boolean {
  return TARGET_PATH.test(path) && !path.split("/").includes("..");
}
