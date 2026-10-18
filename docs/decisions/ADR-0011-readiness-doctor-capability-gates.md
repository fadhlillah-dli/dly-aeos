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
   the integrator).

## Consequences

* Readiness can be UNKNOWN for a fresh install until the Doctor runs; this is intended (no green by
  default).
* Thresholds that the master leaves to real hardware (RAM, resource budgets) are reported, not
  enforced; the 1 GiB disk floor is labelled provisional.
* Checks that depend on modules built in parallel (bridge, worker runtime, interlock, verifier, secrets
  provider) report UNKNOWN/MISSING until those modules land, and turn into real probes automatically
  through the frozen facades.
