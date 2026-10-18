# ADR-0003 — Seed build by native Claude; owner-authorised commit/push/merge for the platform repo

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah)

## Context

Master §09: "Initial coding untuk membangun seed menggunakan native Claude dalam trusted workspace
dengan batas yang disetujui pengguna; sebelum broker/sandbox/verifier tersedia, jangan menyatakan
autonomous multi-model workforce siap." U-09 keeps merge to main and production human-only.

On 2026-10-18 the owner instructed, for the DLY AEOS project specifically: Claude may commit, push
and merge, and should continue until only manual owner actions remain.

## Decision

1. The trusted seed (this repository's initial implementation) is built by the official Claude
   Code client under the owner's Max subscription, working in the owner's trusted clone.
2. **Scope of the delegated authority:** commit, push, open PRs and merge PRs into `main` of
   `fadhlillah-dli/dly-aeos` while building the seed (M0–M4 code). This is an explicit owner
   decision, recorded here and as a `Decision` row (kind EXCEPTION) by `aeos seed`.
3. **Not delegated:** approval of PlanVersions inside AEOS, external publication approval, risk
   acceptance, budget/paid-route activation, production deployment, OFFICE data authorisation,
   entering credentials, OAuth consent, vault initialisation. These stay with the owner.
4. Workers (OpenCode and other non-Claude models) never inherit this authority: the broker can
   only push `aeos/*` task branches and open PRs; merge stays human (§31).
5. Each merge goes through a PR with CI evidence; the PR body states what is verified and what is
   NOT-PROVEN.

## Consequences

* GitHub Free private repositories may not support enforced branch protection (§31, S22); the
  doctor reports this as a residual risk instead of claiming enforcement.
* The exception expires when the seed reaches M4 delivery enforcement or when the owner revokes it.
