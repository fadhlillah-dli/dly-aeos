# ADR-0011 — Readiness Doctor checks, fingerprints and capability gates

* Status: ACCEPTED (seed build under ADR-0003)
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by the seed build (native Claude, ADR-0003)

## Context

Master §08 defines a two-layer Doctor (bootstrap CLI without server/LLM; dashboard readiness), the
CheckResult fields, the status vocabulary (MISSING, CONFIGURED, CONNECTED, VERIFIED, FAILED, EXPIRED,
UNKNOWN, NOT_APPLICABLE + STALE), nine minimal capability gates, remedy previews without hidden fixes,
and invalidation when binaries/config/routes change. §50 adds: optional tools and Office must not block
unrelated capabilities; CONNECTED is not verified; config changes expire related checks.

## Decision

1. **Registry.** `aeos.doctor.checks` registers ~60 checks covering all fourteen §08 groups. Each spec
   declares group, component reference (shared with the inventory), TTL, invalidators, accepted ok
   statuses, whether it needs the DB, whether it is functional (opt-in) or leaves loopback (network).
2. **Honest statuses.** A raising check is `UNKNOWN/PROBE_ERROR`; DB-dependent checks without a DB are
   `UNKNOWN/DB_NOT_INITIALISED`; MCI is never VERIFIED (§51 P1); OpenBao health is CONNECTED at most;
   `claude auth status` yields CONNECTED only for `loggedIn`, `authMethod=claude.ai`,
   `apiProvider=firstParty`, `subscriptionType=max`, keeping no e-mail/organisation; extra billing and
   Max quota are UNKNOWN because no official probe exists.
3. **Functional checks are opt-in** (`--functional` / dashboard checkbox): the Claude bridge capability
   test (uses Max quota), containment negative tests, the keyring round-trip and read-only GitHub
   probes. A normal run skips them instead of overwriting earlier evidence.
4. **Fingerprints and invalidation.** Cheap fingerprints (binary path hash + size + mtime, relevant
   non-secret settings, file stats; never a subprocess) are stored per result. A changed fingerprint
   marks the previous result STALE on the next run and shows it as STALE immediately in views and
   gates. `aeos.doctor.facade.invalidate(invalidator)` marks matching latest results STALE (exact or
   `prefix*` match) and expires the affected components' inventory observations.
5. **Capability tiers.** Critical (fail -> BLOCKED; never-run/STALE/EXPIRED -> UNKNOWN), required
   (FAILED -> BLOCKED, otherwise DEGRADED, e.g. bridge NOT-PROVEN with manual fallback), advisory
   (reasons only, e.g. MCI UNVERIFIED, branch protection residual risk). Optional components appear only
   in the tiers of capabilities that need them. OFFICE gates read the policy module (fallback:
   `policy/office-defaults.yaml`, then fail closed) and stay BLOCKED until a recorded authorisation sets
   the specific flag. Merge/production are listed as HUMAN_ONLY, never as capabilities.
6. **Remedies** carry operation, copyable command, source/version, target, privilege, cost, rollback,
   evidence to collect and `executed_by = MANUSIA`; neither CLI nor UI executes them.
7. **CLI exit code** is 1 when any CAN_OPEN_DASHBOARD critical check in the run is not OK; the CLI
   never creates the data dir or DB as a side effect. A startup pass (`aeos.doctor.worker`) can run the
   non-functional checks after the server starts (registration in the lifecycle hooks is requested from
   the integrator). An existing database whose schema is older than the models (a column added before
   the first Alembic revision) is reported by `runtime.db` as `SCHEMA_OUTDATED` with the missing
   columns, and the CLI falls back to print-only instead of crashing.
8. **Domain scope of projections.** Platform-level rows are visible to every domain, so they never
   list or count OFFICE/per-project secret refs or OFFICE evidence. Check lists, capability reasons,
   remedies and run counts returned to a session exclude checks scoped to another domain (counts are
   metadata too, §50); the domain-less `doctor.run.completed` event carries platform-level counts only.
   Each CheckResult scope names the capabilities that read it (§08 project/domain/capability scope).
9. **OFFICE gates open only through the policy module** (a recorded company authorisation). A flag
   set to true in `policy/office-defaults.yaml` keeps the gate closed and fails `policy.office_defaults`
   (CRITICAL). OFFICE staging validation is gated like other OFFICE execution.
10. **Evidence honesty.** Restore, publication-idempotency and worker-contract proofs count only when
    the EvidenceArtifact is trusted, LIVE (never MOCK/DEMO) and not OFFICE-scoped. Connectivity
    observations derived from checks never claim more than was seen: a missing credential is
    NOT_CHECKED, an answering endpoint with a failed check is UNKNOWN, only a refused connection is
    UNREACHABLE and only a rejected credential EXPIRED_AUTH.

## Consequences

* Readiness can be UNKNOWN for a fresh install until the Doctor runs; this is intended (no green by
  default).
* Thresholds that the master leaves to real hardware (RAM, resource budgets) are reported, not
  enforced; the 1 GiB disk floor is labelled provisional.
* Checks that depend on modules built in parallel (bridge, worker runtime, interlock, verifier, secrets
  provider) report UNKNOWN/MISSING until those modules land, and turn into real probes automatically
  through the frozen facades.

## Amendment 2026-10-18 (integration)

Seam group W3 (Readiness Doctor <-> every module) was wired end to end after all §10 modules were merged.
Where the two sides of a seam disagreed, the contract the master and the module ADRs require was kept and
both sides now use it; frozen facade signatures only gained keyword arguments with defaults.

1. **Capability gates read the real modules.**
   * `CAN_RUN_WORKER` is READY only when, on every read, `aeos.worker_runtime.facade.sandbox_readiness(session,
     domain)` is READY (ADR-0051; a domain-less view is evaluated for PERSONAL and says so), *and* the stored
     checks show OpenCode INSTALLED (`worker.opencode`, worker-runtime contract `status/version/pinned_version`),
     an APPROVED/TRIAL worker route that the routing hard filter itself returns as eligible for
     PERSONAL/INTERNAL (`route.registry` now calls `aeos.routing.facade.eligible_routes`), the
     `opencode-implementer` AgentDefinition enabled (`worker.agent_definition`, new), trusted budget state
     (`route.budget_admission`, new: `aeos.cost.service.verify_budget_authority`, no reservation, no rows
     created) and the credential the broker model proxy would need for those routes (`route.worker_key`).
     The old `containment.sandbox` read `sandbox_capabilities()` with the wrong shape (it listed
     `checked_at, platform, best_available` as isolation levels); it now reports levels + the ADR-0051
     requirement, and accepted residual risks are returned separately.
   * New tier **guard** (FAILED -> BLOCKED, otherwise a note): `containment.report` (WEAK process sandbox =
     CONFIGURED + residual risks, never VERIFIED; FAIL blocks) and `worker.contract` (NOT-PROVEN; the run still
     ends in REVIEW behind the independent verifier). Keeping them in *required* made `CAN_RUN_WORKER` DEGRADED
     forever, and the scheduler admits only READY.
   * `CapabilityState.residual_risks` (additive field) carries risks that stay true even when READY (WEAK
     process sandbox, interim OS keyring, branch protection NOT_ENFORCEABLE, unproven worker contract).
     `require_capability` includes them in the `Blocked` details. They never turn a gate green.
   * `capability_state(..., exclude_checks=())` (additive): enabling the implementer agent asks `CAN_RUN_WORKER`
     without `worker.agent_definition` itself. Without it the workforce enable endpoint (which requires the gate)
     and the gate (which requires the agent) deadlocked. The facade only forwards the keyword when non-empty, so
     existing implementations/stand-ins keep their call shape.
   * `CAN_PREPARE_PR`: `delivery.branch_protection` consumes `aeos.delivery.provider` — the latest trusted LIVE
     `PROVIDER_CAPABILITY` evidence (normal run) or a live `check_capabilities` probe (`--functional`, GET only).
     `merge_gate` CONFIGURED -> CONFIGURED (enforcement still NOT-PROVEN), NOT_ENFORCEABLE / NOT_CONFIGURED ->
     MISSING + residual risk. It stays advisory: never shown as blocker-free enforcement.
   * `CAN_REVIEW_WITH_MAX` / `CAN_PLAN_PERSONAL`: `claude.capability_test` now speaks the bridge contract
     (`status` VERIFIED|DEGRADED|FAILED|BLOCKED, `mode` LIVE; the old check expected `ok`/`mode=NATIVE` and
     reported every real success as FAILED). A normal run reads the bridge's own recorded test
     (`claude_capability_tests`: 7-day window, bridge STALE marks, CLI version must match), `--functional` runs
     one tiny `claude -p` call through the bridge. Never run / BLOCKED / STALE -> UNKNOWN -> DEGRADED (manual
     package fallback), FAILED or quota exhausted -> BLOCKED, MOCK/DEMO never counts.
   * `CAN_PUBLISH_PERSONAL` additionally needs `adapter.write_enablement` (new: flag + ACTIVE human
     WRITE_ENABLEMENT decision for every PERSONAL publication provider of `aeos.portfolio.providers` -
     google-drive and github-issues; Google Docs writes are google-drive operations, there is no separate
     google-docs enablement since the wave-2 provider vocabulary). `adapter.google_oauth` checks the
     references the publisher really writes (`personal/google-oauth-client` + `personal/google-oauth-token`;
     the Doctor looked for a non-existent `personal/google-oauth`). OFFICE Bitbucket uses `office/bitbucket`.
   * OFFICE: the policy module has no `integrations_allowed` flag; it records a company authorisation with
     `authorized_providers`. The Doctor derives `integrations_allowed = office_authorization and
     authorized_providers` from the policy module only (files never open a gate) and treats
     `external_worker_allowed` without `approved_worker_routes` as closed.
2. **Invalidation producers** (same transaction as the change unless noted): secrets `put_secret`,
   `provider_put`, `provider_delete` -> `secret:<ref>` (own short transaction when no `session=` is passed,
   only if the DB exists); policy decisions recorded or leaving ACTIVE -> `config:policy` (POLICY/EXCEPTION),
   `config:office_policy` (office policy target or OFFICE POLICY), `config:write_enablement`,
   `config:budget`, `config:routes` + `model:route` (route decisions), `config:sandbox` (worker_sandbox),
   `secret:*` (secret_store); routing status changes (activate/disable), configuration identity changes
   (e.g. `eligible_domains`, a re-resolved supervisor alias = pin change) -> `config:routes` +
   `model:route` (+ `model:route:<POOL>`, see 6). DB-state fingerprints (routes/decisions, agent definition, sandbox exceptions, budgets,
   write enablement) make a stored result STALE immediately even when a producer is missed.
   Stored reasons/events show `secret:<ref>` as `secret-ref <ref>`: the raw form reads like a `secret: value`
   assignment to the redaction scanner (it was redacted and failed `secret.no_raw_exposure`).
3. **Placeholder detection** (shared with ADR-0012): only the explicit `PLACEHOLDER —/–/-/:` marker in the
   FIRST line of the module docstring counts (`aeos.inventory.probes.source_is_placeholder`); a real module
   mentioning the word is never reported as missing.
4. **Truthful remedies on Windows and macOS**: launcher per OS (`.\aeos.ps1` / `./aeos`), `npm.cmd` on Windows,
   `;` vs `&&` chaining, no `#` comments in commands (zsh does not treat `#` as a comment interactively), no
   shell redirection (PowerShell 5.1 writes UTF-16); OpenBao via the reviewed `aeos vault install|start|init|
   unseal` (the binary in `<data>/bin` is now found like `aeos vault` finds it); Claude Code via the official
   package-manager builds (winget / Homebrew cask; Linux: docs only); OpenCode pinned version from the worker
   runtime; backups via `aeos ops backup create` / `restore-test` (directories with manifest were not
   recognised before). The CLI prints the data directory as a logical name (no user name or absolute path).
5. **Startup doctor**: the `aeos.doctor.worker` lifecycle hook runs the non-functional checks in a background
   thread under the app lifespan (startup is not delayed), emits `doctor.check*`/`doctor.run.completed`
   (`doctor.run.failed` on error) and the Readiness page labels the run "otomatis saat server mulai".
6. **Review corrections (same day, adversarial review of the seam):**
   * `secret.status` depends on the store (`secret:store`, `config:openbao_addr`, `binary:bao`), not on `secret:*`.
     With `secret:*` every credential write - including the publisher's hourly Google token refresh - made a
     CAN_RUN_WORKER critical check STALE and stopped scheduler admission until a manual Doctor run. A
     `secret_store` decision still emits `secret:*`, which matches `secret:store`.
   * Domain metadata in invalidation: a result that is not scoped to the reference's domain (platform-level rows
     and their domain-less `doctor.check.stale` events, component observations) never names an `office/*` or
     `projects/*` secret reference (generic label `secret-ref <ber-scope domain>`); the secrets hook no longer puts
     the reference into the free-text reason. Platform-level stale reasons for OFFICE decisions carry no decision
     id/kind; `policy.snapshot` no longer counts OFFICE decisions (§50: counts are metadata). Residual risks are
     attached to their check and `capabilities_view` drops those of checks hidden from the session.
   * Pool-specific route invalidation: route hooks emit `config:routes`, `model:route` and `model:route:<POOL>`;
     `claude.capability_test` declares `model:route:SUPERVISOR`. A WORKER route activation/disable/config change no
     longer marks the bridge proof STALE (verification.review sends every review to the manual fallback while
     CAN_REVIEW_WITH_MAX is not READY). The "first observed resolution" hook was removed: with no previously known
     identity nothing was bound to it, and it degraded the Max gates on the first call of a second alias (fable
     after an opus test). A real re-resolution still goes through `refresh_config_hash` (identity hash changes).
   * Pin drift (§38 "Binary/version/route berubah dari pin ... hentikan admission", §50): `worker.opencode` reports
     an installed OpenCode whose version differs from the pin as FAILED (`OPENCODE_PIN_DRIFT`, CAN_RUN_WORKER BLOCKED);
     the inventory still records INSTALLED with the observed version (a check that reports its own dimension is not
     overwritten by the generic status mapping).
   * Honest labels: the live sandbox entry is VERIFIED only for a container whose containment report passed; the
     WEAK process sandbox with an owner exception is CONFIGURED (ok for the gate, never VERIFIED). A capability-test
     result without `mode` is not proof (fail closed; the bridge always records LIVE).
   * Startup failures: `doctor.run.failed` carries the exception type and a message with data/repo/home paths
     replaced by logical names; `/api/doctor/checks` returns `last_failure` while no newer run exists and the
     Readiness page says the shown results come from an older run.
