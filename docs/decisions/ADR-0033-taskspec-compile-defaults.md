# ADR-0033 — TaskSpec compile defaults and self-build protected paths

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by the seed builder under ADR-0003

## Context

Master §21 lists the minimum TaskSpec contract and the pilot caps (one active worker, at most two
implementation attempts, no nested fan-out, egress default deny). §14 requires the DAG to reject
unknown nodes, self-dependencies and cycles. §09/AGENTS.md define protected paths whose changes are
quarantined for special review when DLY AEOS builds itself. §19/§22 serialise shared writes.

## Decision

1. The DAG is validated (tasks, features, milestones) before approval and before compile; any unknown
   node, self-dependency, duplicate key or cycle is `ValidationFailed` (`DAG_INVALID`) with details.
   A task write scope (`allowed_paths`) that could leave the workspace — parent traversal `..`, a
   drive letter, a UNC share, `~` or a control character — is an `UNSAFE_PATH` error (rejected, never
   normalised away), so such model output fails synthesis and human edits.
2. Compile happens on approval (and explicitly for the approved version only) and is idempotent per
   PlanVersion. Every §21 field gets a value: limits clamped to `max_attempts ≤ 2` (defaults 2 /
   3600 s / 32k tokens) plus fixed resource caps (`max_active_workers: 1`, `nested_fan_out: false`),
   default tools, egress `[]`, capability scope on `aeos/*` branches with a
   bounded TTL, data classification never below the project's, route policy without paid fallback or
   supervisor downgrade, reviewer and independent read-only verifier always required, default stop
   conditions (incl. "scope grows or the contract cannot be met → stop and file a blocker/decision
   request") and evidence requirements, risk class, artifact schema version and the provenance `mode`
   copied from the PlanVersion (a TaskSpec compiled from MOCK content is MOCK); `spec_hash` covers all
   fields. `base_commit` is pinned later by the scheduler.
3. For every project the trusted grader/pipeline paths (`tests/acceptance/**`, `.github/workflows/**`,
   `bitbucket-pipelines.yml`, §21) are forbidden by default; for the platform project the full
   AGENTS.md protected list is added. A task whose allowed paths overlap a protected path is flagged
   `quarantine_special_review` and raised to R3. Overlap is compared case-insensitively, because
   Windows and default macOS volumes are case-insensitive (`Backend/AEOS/Policy/...` is protected).
4. The DAG projection joins TaskSpec with TaskExecution, active Lease and AcceptanceRecord (through the
   verification facade; UNKNOWN when unavailable). It reports critical blockers with their downstream
   impact and shared-write conflicts (overlapping allowed paths), marking whether a dependency already
   serialises them. External issue status never counts as acceptance, and a prerequisite counts as
   accepted only for an ACCEPTED record whose validity for this TaskSpec's plan version hash is
   positively confirmed (UNKNOWN validity never opens a dependency).

## Consequences

* The scheduler can rely on TaskSpecs being complete and immutable; a task without allowed paths is
  flagged so the scheduler blocks it.
* Changing caps or defaults is a policy change recorded by a new ADR, not a code tweak.

## Amendment 2026-10-19 (cluster C7: plan content governance)

1. **Plan content schema 1, revision 2 (additive).** Top-level `feature_governance` (owner, RFC/TDD/DPIA/
   security-assessment needs, personal data YES/NO/UNKNOWN, feature threat model), `milestone_governance`
   (priority, acceptance, dependencies, owner), `rfcs`, `tdds` and `interface_contracts`. The JSON Schema title
   stays `plan-content/1` (nothing removed or renamed); stored content without the sections is read through
   `contracts.upgrade_content` (empty list = nothing requested / UNKNOWN). The docs adapter folds these
   sections into the feature-approval documents (§27, §28).
2. **More DAG rules (errors).** `CONTRACT_NOT_FIRST` (every consumer of an interface contract depends,
   transitively, on the provider task — §19/§22 contract-first), `UNKNOWN_CONTRACT_TASK`,
   `SPECIALIST_WITHOUT_DISTINCT_OUTPUT` (a role other than `feature-implementer` needs its own outputs and
   acceptance — §18), unknown features/milestones in the governance sections.
3. **Review findings (non-blocking, shown to challenger/final reviewer/owner).** `ROLE_UNKNOWN`,
   `FE_BE_SPLIT_WITHOUT_CONTRACT`, `CROSS_FEATURE_CONFLICT`; `NFR_MISSING` (§46) keeps an otherwise
   PASS-STATIC verdict at NOT-PROVEN.
4. **Compile.** `Feature.owner`/`Milestone.owner` are `UNKNOWN` unless named; `TaskSpec.role_revision_hash`
   (part of `spec_hash`) records the RoleSkillPack revision (None for a non-catalog role, which the scheduler
   refuses); `input_artifacts` are `{ref, kind, sha256, label, resolved}` bound to PRD sections and READ
   attachments, unresolved labels stay visibly unresolved. The scheduler refuses an empty write scope
   (`NO_ALLOWED_PATHS`) instead of only flagging it.
