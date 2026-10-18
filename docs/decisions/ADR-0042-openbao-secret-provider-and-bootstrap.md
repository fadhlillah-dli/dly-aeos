# ADR-0042 — OpenBao secret provider, OS-keyring bootstrap and the interim exception

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by the module-group D build (secrets)

## Context

U-13 and master §26 make OpenBao the runtime credential source of truth with separate PERSONAL,
OFFICE, worker and per-project paths, AppRole/Agent tokens with scope and TTL, root token/unseal
material/bootstrap SecretID kept apart from application configuration, OS credential stores for the
bootstrap, status without values, and fail-closed behaviour. §07 order 3 requires evidence of vault
health, policy scope, token TTL, redaction and restart/recovery behaviour; §08 forbids auto-install
and requires remedies to show source/version/target/privilege/cost/rollback first.

Verified on 2026-10-18 against the official OpenBao v2.7.1 release (Windows amd64), installed only
into a throw-away test data directory:

* release assets are `openbao_<ver>_<os>_<arch>.zip|tar.gz` plus `checksums.txt` (+ `.gpgsig`,
  `.sigstore.json`); SHA256 verification of the asset against `checksums.txt` succeeded;
* `storage "file"` is no longer available ("unknown storage type file") and `disable_mlock` is an
  "unknown or unsupported field"; single-node integrated storage (`raft`) with `cluster_addr` works;
* `custom_metadata` values must be non-empty (empty strings → HTTP 400);
* `sys/init` on raft can take more than 10 s on Windows.

## Decision

1. **Provider selection** (`aeos.secrets.provider.get_provider`): test override → OpenBao (loopback
   address only, reachable, initialised, unsealed, AppRole login OK) → **only** with an ACTIVE human
   Decision `kind=EXCEPTION`, `domain=PERSONAL`, `details.secret_store_interim_keyring=true`
   (unexpired) an `os-keyring-interim` provider whose residual risk is surfaced in status → otherwise
   `Blocked(SECRET_STORE_UNAVAILABLE)` with reasons and remedies. The interim store never accepts
   OFFICE references.
2. **OpenBao usage**: KV v2 mount `aeos`, paths `aeos/data/<namespace>/<name>`; AppRole
   `aeos-controller` (role_id/secret_id in the OS keyring service `<keyring_service>-bootstrap`,
   i.e. `dly-aeos-bootstrap`), token cached and renewed before 80 % of its TTL, re-login once on 403;
   status/list from metadata (`updated_time`, `custom_metadata.expires_at/scope/updated_by`) — never
   values; expired secrets are refused; one pooled `httpx` client with `trust_env=False`.
3. **Scope**: callers pass a purpose whose prefix must equal the namespace (`personal:...` →
   `personal/*` only; `projects:<id>:...` → that project only). OFFICE secrets are never returned to a
   personal-scoped caller. Every fetched value (and every token/role/secret id seen) is registered
   with the redactor. Access is logged by ref/purpose/actor/result only.
4. **Lifecycle CLI** (`aeos vault`): `install` resolves the official GitHub release (latest or
   `--version`), prints the plan (source, version, asset, checksums, target `<data>/bin`, user-level
   privilege, no cost, rollback = delete), requires `--yes` or a confirmation, verifies SHA256, and
   records an inventory observation; signature verification is NOT performed (NOT-PROVEN).
   `start` writes a loopback-only raft config (TLS disabled on loopback, UI off) and runs the server
   detached with an allow-listed environment, recording pid + create time; `stop`; `status`;
   `init` (shares/threshold; unseal keys and root token printed ONCE; KV v2; policies `aeos-personal`,
   `aeos-office`, `aeos-workers`, `aeos-platform`, `aeos-projects`; AppRole bound to personal/workers/
   platform/projects with token TTL 1h/max 24h; credentials to the OS keyring; AppRole login verified;
   root token revoked; on any failure the material is still shown and the root token is kept so the
   owner can finish); `unseal` (prompted keys or, only if the owner chose `--store-unseal-in-keyring`,
   from the keyring). The office policy exists but is **not** bound to the controller (OFFICE default
   deny).
5. **Secrets CLI/API**: values only via `aeos secrets set <ref>` (hidden prompt or `--stdin`), `status`,
   `delete`; `interim-keyring` explains the exception and records nothing. The web API has
   `GET /api/secrets/status`, `/health`, `/integrations` and no write endpoint.

## Consequences

* Real evidence (E2, `tests/integration/test_secrets_openbao_live.py`, runs when a `bao` binary is
  available): init → unseal → policies → AppRole → root revoked → put/get/status → OFFICE write
  denied by policy → restart → sealed (fail closed) → unseal → data intact.
* Until the owner runs `aeos vault install/start/init` (or records the interim exception) every
  secret-dependent capability is BLOCKED and statuses are UNKNOWN, never "missing".
* OpenBao Agent, SOPS+age backup and an office AppRole are not implemented (future, decision-bound).

## Addendum 2026-10-18 - completeness review

* **Functional proof beyond CONNECTED** (§07 order 3 "vault health, policy scope, token TTL").
  `aeos vault status --verify` writes, reads back and deletes a random value at
  `platform/aeos-healthcheck` with the controller AppRole token and stores the result (status,
  steps and time, never values) in `<data>/secrets/last-verify.json`. The health view shows it
  under `functional_check`. It is **stale** when OpenBao is not the active store now or the proof
  is older than 7 days, so a past roundtrip is never shown as a current pass. The controller
  token's TTL, renewability and bound policies (never the token) are shown as `openbao.token`.
* **Unbound namespaces are NO_ACCESS**. The controller is not bound to `aeos-office`, and the
  interim keyring never serves `office/*`, so office refs show `NO_ACCESS` (neither present nor
  missing) with no `aeos secrets set` remedy, which would only be refused. The controller does not
  even ask OpenBao for namespaces outside its token policies (observed 403 on 2.7.1).
* **Project secrets**. `projects/<id>/...` is served only for a registered PERSONAL project, and is
  listed only in a session whose active domain equals the project's domain. An OFFICE project's
  credential cannot bypass the unbound office policy through the `projects/` namespace.
* **Interim exception binding**. The interim keyring is enabled only by an ACTIVE, unexpired, human
  EXCEPTION decision recorded by an active SUPER_ADMIN, for the exact target
  `secret_store/interim-keyring` with `details.secret_store_interim_keyring == true` (domain
  PERSONAL).
* OS-keyring write errors fail closed (`Blocked SECRET_STORE_UNAVAILABLE`) without echoing the value.

## Amendment 2026-10-18 (integration)

Found by the trusted acceptance suite (`tests/acceptance`, master §50) when the wave-1 modules ran
together; the provider contract is unchanged, the callers were reconciled with it.

* **Every caller states the namespace as the purpose scope.** `check_purpose` requires the purpose
  to start with the reference namespace (`personal:`, `platform:`, `workers:`, `projects:<id>:`).
  Several callers passed free text ("push main", "google status (scopes only)",
  "delivery.provider.read"), so every real read was `SECRET_SCOPE_DENIED` and the integrations
  looked unconfigured. Fixed callers: `broker.git_broker` (push/create-pr/reconcile:
  `personal:broker.*`), `broker.model_proxy` (scope derived from the ref:
  `projects:<id>:<use>` for project refs, otherwise the ref namespace), `delivery.provider`,
  `doctor.checks.adapters`, `portfolio.repositories`, `publisher.providers.github_issues`,
  `publisher.providers.google`, `publisher.providers.atlassian`. The purpose stays a label for
  audit; it never widens access (the namespace binding and project scope are still enforced by
  `check_purpose` and `check_project_scope`).
* **A status read never creates state.** `interim_exception()` returned the decision by opening a
  DB session, which created the data directory and an empty database as a side effect of a
  status read (bootstrap doctor before `aeos init`, §08/§50 "Dependency inti belum terpasang").
  When the database file does not exist yet no exception can exist, so it now returns `None`
  without opening a session.
