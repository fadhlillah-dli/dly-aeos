# ADR-0001 — Master architecture is the platform baseline; MCI alignment UNVERIFIED

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah)
* Source: `docs/architecture/DLY_AEOS_Master_Architecture_Final.md`
  (sha256 `8F21F1F126A586C0E77558DDFBA175CCCB64F71BA9F0F3D4A672CC10A0D07C49` at adoption)

## Context

The owner supplied the consolidated master architecture (status DESIGN/RECOMMENDATION). The
repository `fadhlillah-dli/dly-aeos` was empty at the start (M0 inventory: no prior implementation,
no AGENTS.md, no canonical policy files). `DLY_AEOS_PHASE1_FINAL_IMPLEMENTATION_SPEC.md` and the
MCI_V2 source of truth referenced by the master are **not available** in this workspace.

## Decision

1. The master is stored in the platform repo as the versioned source artifact and is updated in
   place under the same file name (U-23); history is kept by git, ADRs and approvals.
2. Implementation follows the master's module boundaries (§10), data contracts (§13), state
   machines (§14) and milestones M0–M6 (§49).
3. MCI alignment is **UNVERIFIED**. The platform records `policy/mci-reference.yaml` with status
   UNAVAILABLE and never claims canonical compliance or MCI-dependent READY states. No MCI kernel,
   pack, risk threshold or compliance claim is invented (§02, §34, §51 P1).
4. Because no previous implementation exists, M0 "inspect and preserve existing behaviour" reduces
   to recording this empty baseline; nothing was overwritten.

## Consequences

* Gates that depend on MCI remain BLOCKED/NOT-PROVEN until the owner provides the canonical source.
* The coverage matrix (`docs/IMPLEMENTATION_COVERAGE.md`) maps every master section to code,
  tests and evidence level, including what is still NOT-PROVEN.
