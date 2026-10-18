# ADR-0061 — Evidence ledger: trust from origin, levels per claim, redaction before hashing

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah)

## Context

§34 defines the evidence ladder E0–E4 per claim and states that worker attachments are untrusted
input; only an independent collector/verifier writes actual results with content hash, timestamp and
identity. Provider documentation/configuration supports E1 only. The facade
`aeos.verification.facade.collect_evidence` is frozen and has no `trusted` parameter.

## Decision

1. `trusted` is derived solely from `origin ∈ TRUSTED_COLLECTORS` (verifier, collector, doctor,
   reconciler, provider-ci, restore-test). Any other origin (worker, human attachment, unknown) is
   untrusted and capped at **E0**.
2. Level ceilings for trusted origins by kind: static artifacts (diff, scope, lint, secret scan,
   provider capability/configuration, bundles, packages) **E1**; controlled tests (TEST_REPORT,
   VERIFICATION_REPORT) **E2**; runtime observations (CI_STATUS, PROBE, RESTORE_TEST, SCREENSHOT, LOG)
   **E3**; **E4** only for verifier evidence that references the *current* trusted suite hash and test
   ids. The requested level is a ceiling request, never an upgrade; the requested/assigned level and
   reason are stored in `inline.level`.
3. Content (bytes/str/dict) is redacted (`aeos.core.redaction`) **before** it is hashed and written; the
   hash covers the stored bytes. Binary content cannot be redacted: binary content matching a credential
   pattern is refused (PolicyDenied). Files live under `DataLayout.evidence(domain, project_key)`, the
   domain evidence dir, or `<data>/platform/evidence`; `location` is relative to the data dir; the
   domain of project evidence is always the project's domain (never re-labelled).
4. Reads re-verify integrity (VERIFIED / MISMATCH / MISSING) and re-redact text; RESTRICTED content is
   shown to SUPER_ADMIN only; lists are domain-scoped (cross-domain → 404).
5. The verification report is itself an append-only EvidenceArtifact (kind VERIFICATION_REPORT) rather
   than a second "verification runs" table (§13: one concept, one table; WorkerRun creation stays with
   the scheduler).

## Consequences

* The local append-only table is not a WORM/tamper-proof store against a host administrator (§34);
  stronger audit storage requires the canonical policy (MCI, UNVERIFIED).
* Callers of the frozen facade pass through `impl(**locals())`, which forwards the facade's own `impl`
  name; the implementation accepts and ignores it.
