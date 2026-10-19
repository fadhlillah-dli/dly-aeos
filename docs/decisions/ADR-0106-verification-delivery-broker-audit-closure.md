# ADR-0106 — Verification, delivery and broker: closing the audited gaps honestly

* Status: PROPOSED
* Date: 2026-10-19
* Deciders: owner (Fadhlillah) — proposed by the C6 audit-closure change

## Context

The requirement audit of the master (§01–§52) found gaps in the verification, delivery and broker
modules. Some were defects (a valid run was REJECTED at random because PII minimisation rewrote
commit SHAs and sha256 digests that contain a Luhn-valid digit run), some were missing rules, and some
depend on things only the owner or an external system can provide (Bitbucket/OFFICE authorisation,
a real staging target, a sandboxed runner).

## Decision

1. **Identity values are never PII.** Digit runs inside alphanumeric tokens are not card numbers or
   NIK (`aeos.core.pii`), and the evidence collector stores hex identity values (commit, tree, artifact,
   config, suite hashes) bit-exact (still through the credential redactor).
2. **Review identity.** The review half of acceptance needs a *proven* Claude identity of the requested
   family. UNKNOWN models (a bare JSON pasted by a person) or another family make the review FAILED
   (re-requestable), never a pass and never a repair attempt. The manual fallback accepts the full
   `claude -p --output-format json` envelope, parsed by the bridge's own parser.
3. **Reviewer-side failures do not consume implementer attempts.** Schema-invalid output is FAILED;
   NOT-PROVEN with `requested_verification` records a verification request instead of a repair.
4. **Specialist reviewers** (§15–§19): SECURITY, A11Y (UI output / UX roles), PRIVACY (data/AI roles,
   CONFIDENTIAL/RESTRICTED data) and SRE (CI/IaC, operations roles) are separate sessions bound to the
   same artifact and required for acceptance and G4. A per-run review budget
   (`reviewer_policy.max_reviews_per_run`, default 12) bounds review sessions; `review_all` requests all
   open kinds for one package (sequentially; no concurrent SQLite writers).
5. **Acceptance checks evidence requirements** (verifier report, diff, criteria→evidence mapping);
   free-text requirements are shown as REVIEWER_ATTESTED, never as verified by code.
6. **Verifier v2** (makes earlier acceptance STALE by design): impact categories drive extra proof —
   negative tests for API/auth changes when `negative_tests_required`, DEPENDENCY_DIFF evidence with
   licences UNKNOWN, Zone 2 NOT-PROVEN when the Python dependency set changes, NOT-PROVEN checks for
   frontend and migration changes, TaskSpec `suites` select candidate test directories, an opt-in
   measured baseline on the base commit (`verifier_policy.baseline`), E4 for a fully passing trusted
   suite referenced by its current hash, and `verifier_isolation: WEAK` on every report.
7. **Protected paths** also cover the master and agent instructions, the audit/event trail, dashboard
   projections, scheduler, readiness, runtime, routing and cost code (quarantine + special review).
8. **Delivery.** Governance handoff runs inside prepare-PR (after acceptance, before push/PR) and is
   shown next to the gates; a configured adapter's BLOCKED/REJECTED stops delivery and promotion.
   G0 checks classification floor, scope and target repository; G5 fails when the merge commit brings
   protected paths beyond the verified change; G7 needs the owner's recorded release approvers;
   G6/G8 are evaluated once the owner registers an observe-only `deployment-target`. The provider check
   reads status-check identity (`app_id`) and deployment environments. A project pipeline reference
   maps CI jobs to gates explicitly. OFFICE delivery is refused with an explicit NOT_APPLICABLE reason.
9. **Broker.** Every proxied request re-checks the route's current approval (hash-bound, unexpired,
   free or budgeted). Only the WORKER pool is served (`broker.facade.SERVED_POOLS`); routing refuses
   other pools at admission (`POOL_NOT_SERVED`) instead of failing at the first model call.
10. **Design gaps are visible.** The curated AEOS MCP interface is a PLANNED manifest component; the
    ASVS selection (`contracts/security/asvs-selection.yaml`, ASVS 4.0.3 — a verification trace, kept
    out of `policy/` because every policy file is mandatory worker context) traces each control to tests
    or marks it NOT_PROVEN.

## Consequences

* Not solvable here (owner/external): Bitbucket push/PR/Pipelines adapters and OFFICE authorisation;
  a real staging/production target and its health endpoint; a container/VM runner and a separate broker
  identity; migration round-trips for managed projects' own toolchains; licence/SBOM data.
* Verifier v2 changes the config hash: accepted tasks are re-verified once after the upgrade (§14).

## Review addendum (C6 review)

* Review budget counts only sessions that produced (or failed to produce) a reviewer answer
  (COMPLETED/FAILED rows, native or manual import). A WAITING_QUOTA refusal or a pending manual
  package never consumes it, so re-requesting while the Max quota is exhausted cannot strand a run.
  `review_all` keeps sessions that already ran when a later kind is refused or the run leaves REVIEW
  (the reason is in the notes); an exhausted budget names the owner's remedy (new attempt, or a higher
  `max_reviews_per_run` in a new PlanVersion).
* G7 release approvers come only from a HUMAN POLICY decision recorded in the project's own domain.
* The a11y/UX specialist also activates for UI files in any layout (`*.tsx`, `*.jsx`, `*.vue`,
  `*.svelte`, `*.css`, `*.scss`), not only under `frontend/`.
* The Control Center shows what the backend states: verifier isolation WEAK, the required specialist
  reviews and why, the governance handoff status, and OFFICE delivery NOT_APPLICABLE with its reason; the
  run list offers one action that requests every required review (`POST /api/runs/{id}/reviews/batch`).
* §09 "a worker cannot raise its own capability" stays a residual risk under the process sandbox (a
  worker can still write the controller DB directly); it needs the container sandbox (ADR-0051).
