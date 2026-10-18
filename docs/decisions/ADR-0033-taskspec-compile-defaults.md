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
2. Compile happens on approval (and explicitly for the approved version only) and is idempotent per
   PlanVersion. Every §21 field gets a value: limits clamped to `max_attempts ≤ 2` (defaults 2 /
   3600 s / 32k tokens), default tools, egress `[]`, capability scope on `aeos/*` branches with a
   bounded TTL, data classification never below the project's, route policy without paid fallback or
   supervisor downgrade, reviewer and independent read-only verifier always required, default stop
   conditions and evidence requirements, risk class, artifact schema version; `spec_hash` covers all
   fields. `base_commit` is pinned later by the scheduler.
3. For the platform project the AGENTS.md protected paths are forbidden by default; a task whose
   allowed paths overlap a protected path is flagged `quarantine_special_review` and raised to R3.
4. The DAG projection joins TaskSpec with TaskExecution, active Lease and AcceptanceRecord (through the
   verification facade; UNKNOWN when unavailable). It reports critical blockers with their downstream
   impact and shared-write conflicts (overlapping allowed paths), marking whether a dependency already
   serialises them. External issue status never counts as acceptance.

## Consequences

* The scheduler can rely on TaskSpecs being complete and immutable; a task without allowed paths is
  flagged so the scheduler blocks it.
* Changing caps or defaults is a policy change recorded by a new ADR, not a code tweak.
