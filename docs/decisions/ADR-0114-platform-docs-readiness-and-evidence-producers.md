# ADR-0114 — Platform, documentation readiness and real evidence producers (K4 audit closure)

* Status: PROPOSED
* Date: 2026-10-25
* Deciders: owner (Fadhlillah) — proposed by the K4 platform/docs audit-closure change

## Context

The re-audit of the master found documentation sections that could never be filled (no source), documents
that could never leave DRAFT (the evidence kind they waited for was never produced), readiness criteria that
were reported but never enforced, and Doctor checks that only noticed permission changes when someone ran
`aeos doctor --functional`.

## Decision

1. **Real producers instead of waiting kinds.** Operations documents read the deployment observer's trusted
   production `RUNTIME_OBSERVATION` (newest observation decides; a newer failed one returns them to DRAFT).
   Incidents (`OpsIncident`) render as 09 INCIDENT_REPORTs and evidenced root causes as PROBLEM_RECORDs. Trusted
   collectors exist for screenshots (`aeos docs screenshot`: registered target, commit proven by its health
   endpoint, page text checked for secrets/PII), VA (`aeos inventory va-scan`, pip-audit) and external PT reports
   (`aeos docs register-pt-report`: uploaded through intake, provenance `EXTERNAL_HUMAN_PROVIDED`, AEOS does not
   claim to have tested anything).
2. **Plan schema 1, revision 3 (additive, nested).** `operations` gains `support`, `log_policy`, `alert_policy`,
   `backup.storage`, `maintenance_checks`, `routine_activities`; the diagram enum gains `cicd`. Older content
   is upgraded with empty values (= documentation pending). The new fields carry no descriptions so the
   synthesis schema still fits the Claude CLI command line with its annotations.
3. **§41 readiness is enforced before implementation.** Workforce admission WAITS (`DOCS_NOT_READY`) while a
   PROJECT_CREATION required section, or the task's own FEATURE_APPROVAL PRD/test plan section, is
   documentation pending. A check that cannot run is a wait (`DOCS_READINESS_UNVERIFIED`), never a pass.
   Task-only DAGs without Product 1 content are not affected.
4. **Documentation checkpoints.** An approved ArchitectureSnapshot (platform) or an accepted milestone records
   an immutable `DOC_CHECKPOINT` package on `03-architecture-overview` (changed documents and sections by hash,
   architecture/diagram delta, material flag, a Technical Writer / Diagram-Visual Author TaskSpec DRAFT that
   runs only after the owner approves it into a plan version). Claude reviews a material package on request
   (advisory).
5. **Doctor sees permission changes during normal use.** The publisher's response-observer hook feeds 401/403
   (not rate limits), rejected token refreshes and GitHub scope changes to the Doctor, which marks the
   credential's checks STALE; read-only, zero-cost remote identity probes are then re-run functionally in the
   background. Quota-bearing or heavy checks are never re-run implicitly.
6. **Every /api response names its version/config** (`X-AEOS-Version`, `X-AEOS-Schema-Version`,
   `X-AEOS-Settings-Hash`) and every list/claim endpoint states `source` + `checked_at`/`generated_at`; the
   freshness ratchet is closed.

## Consequences

* Plans whose required project-creation sections are empty now wait before implementation; the reason names
  the documents and sections.
* Owner steps remain: retention durations, approved digests/publisher identities, a registered deployment
  target (screenshots, production validation), Docker containment, OFFICE company authorisation.
