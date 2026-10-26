# ADR-0003 — Seed build authority: owner-authorised commit/push/merge for the platform repo

* Status: ACCEPTED
* Date: 2026-10-18 (wording neutralised 2026-10-26 by owner decision; the delegated scope is unchanged)
* Deciders: owner (Fadhlillah)

## Context

Master §09 lets the trusted seed be built in a trusted workspace within bounds the owner approves. It
also says that an autonomous multi-model workforce must not be declared ready before the broker, the
sandbox and the verifier exist. U-09 keeps merge to main and production human-only.

On 2026-10-18 the owner instructed, for the DLY AEOS project specifically, that the owner's development
tooling working in the owner's trusted clone may commit, push and merge, and should continue until only
manual owner actions remain.

## Decision

1. The trusted seed (this repository's initial implementation) is built with the owner's development
   tooling in the owner's trusted clone. All commits and merges are made under the owner's own identity
   and authority.
2. **Scope of the delegated authority:** commit, push, open PRs and merge PRs into `main` of
   `fadhlillah-dli/dly-aeos` while building the seed (M0–M4 code). This is an explicit owner
   decision, recorded here and as a `Decision` row (kind EXCEPTION) by `aeos seed`.
3. **Not delegated:** approval of PlanVersions inside AEOS, external publication approval, risk
   acceptance, budget/paid-route activation, production deployment, OFFICE data authorisation,
   entering credentials, OAuth consent, vault initialisation. These stay with the owner.
4. AEOS workers (OpenCode and the routed worker models) never inherit this authority: the broker can
   only push `aeos/*` task branches and open PRs; merge stays human (§31).
5. Each merge goes through a PR with CI evidence; the PR body states what is verified and what is
   NOT-PROVEN.

## Consequences

* GitHub Free private repositories may not support enforced branch protection (§31, S22). The
  doctor reports this as a residual risk instead of claiming enforcement. Since 2026-10-25 the
  repository is public and a ruleset on `main` is active (ADR-0115).
* The exception expires when the seed reaches M4 delivery enforcement or when the owner revokes it.
* Changing this file's content re-records the seed exception with the new content hash. The superseded
  decision is kept, and an owner revocation is never undone (`aeos seed`, tests/unit/test_policy_seed.py).
