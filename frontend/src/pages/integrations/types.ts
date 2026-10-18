// API shapes for /api/secrets/* (backend: aeos.secrets.router). Values are never part of any shape.

export interface Remedy {
  command: string;
  why: string;
}

export interface CredentialRef {
  ref: string;
  state: string; // PRESENT | MISSING | EXPIRED | UNKNOWN | HIDDEN | NO_ACCESS
  backend?: string;
  updated_at?: string | null;
  expires_at?: string | null;
  scope?: Record<string, unknown>;
}

export interface IntegrationCardData {
  id: string;
  name: string;
  domain: string | null;
  status: string;
  capability: string;
  scope: string;
  account?: Record<string, string | null>;
  credential_refs: CredentialRef[];
  credential_note?: string;
  expiry: string | null;
  proof_time: string | null;
  proof_source: string;
  remedies: Remedy[];
  warning?: string | null;
  blocked_reason?: string;
  quota?: { state: string; source: string; observed_at: string | null; details: Record<string, unknown>; free_daily: string } | null;
}

export interface IntegrationsResponse {
  items: IntegrationCardData[];
  office_hidden: boolean;
  checked_at: string;
  source: string;
  mode: string;
}

export interface SecretStatusItem {
  ref: string;
  namespace: string;
  exists: boolean | null;
  state: string;
  backend: string;
  updated_at: string | null;
  expires_at: string | null;
  scope: Record<string, unknown>;
  note: string;
  purpose: string | null;
  used_by: string | null;
  integration: string | null;
  remedy: string | null; // null for NO_ACCESS: no command would succeed
  remedy_note?: string | null;
}

export interface TokenView {
  present: boolean;
  ttl_seconds?: number;
  renewable?: boolean;
  expires_in_seconds?: number;
  policies?: string[];
}

export interface FunctionalCheck {
  status: string; // VERIFIED | FAILED | BLOCKED | NOT_TESTED
  checked_at: string | null;
  stale?: boolean;
  steps?: { id: string; status: string; detail?: string }[];
  note?: string;
}

export interface SecretStatusResponse {
  items: SecretStatusItem[];
  backend: string;
  store_error: string | null;
  reasons: string[];
  office_hidden: boolean;
  source: string;
  checked_at: string;
  mode: string;
}

export interface StoreHealth {
  status: string;
  backend: string;
  openbao: {
    addr: string;
    reachable: boolean;
    initialized: boolean | null;
    sealed: boolean | null;
    version: string | null;
    login: string;
    approle_in_keyring: boolean | null;
    token?: TokenView;
    binary_installed: boolean;
    server_pid_alive: boolean;
  };
  functional_check?: FunctionalCheck;
  keyring: { available: boolean; backend: string; platform_store: string };
  interim: { active: boolean; decision: Record<string, unknown> | null; residual_risk: string | null };
  reasons: string[];
  remedies: Remedy[];
  checked_at: string;
  source: string;
  mode: string;
  evidence_limit: string;
}
