# ADR-0063 — Delivery: PR via broker only, G0–G8 from evidence, read-only provider checks, Null governance

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah)

## Context

§31: implementer has no remote credential; only the trusted broker writes allowed branches and opens
PRs; merge to main/production is human-only; GitHub protected branches are not available on every
Free private plan — when enforcement is not available, do not claim required checks block merge.
§30: CI results come from the provider for the commit. §32: the GovernanceHarnessAdapter sits after
verification/review and before delivery trust promotion; the real downstream contract has not been
inspected.

## Decision

1. `prepare_pr` requires a valid ACCEPTED acceptance, `CAN_PREPARE_PR` READY (doctor), an `aeos/*`
   task branch, a registered GitHub repository and an allowed external-write interlock decision
   (policy). It builds the PR evidence bundle (task/plan ids + hashes, verifier evidence table, review
   verdict + findings, acceptance, gates, unknowns/NOT-PROVEN, rollback notes, human-only merge
   statement), stores it as PR_BUNDLE evidence and calls only `broker.push_task_branch` +
   `broker.create_pull_request`. A pushed head that differs from the accepted head makes acceptance
   STALE. Refusals are audited (DENIED/FAILED) after rollback.
2. There is **no merge, deploy, force-push or branch-deletion call** in delivery or verification;
   a static AST test enforces this and that the GitHub adapter uses GET only.
3. Provider capability check (GET only): repository visibility/permissions, account plan when visible,
   branch protection, rulesets, branch rules, required status checks. `403 Upgrade…` or `404` on a Free
   private repository → `NOT_ENFORCEABLE` with the residual-risk message "Branch protection tidak dapat
   ditegakkan pada paket GitHub ini — merge tetap tindakan manusia". Even CONFIGURED protection is
   `enforcement_tested: NOT-PROVEN` until broker bypass tests exist. No credential → no call, UNKNOWN.
4. CI: check-runs for the exact commit (other commits' runs ignored) → provider-ci CI_STATUS evidence
   (E3). Jobs map to gates by name (backend/frontend → G1, frontend → G3).
5. Gates per task: G0 approval binding (planning facade), G1 provider CI static + verifier static,
   G2 verifier, G3 CI frontend job (NOT_APPLICABLE without frontend change/job), G4 completed review
   bound to the verified artifact + G2 PASS, G5 CI on the merge commit after a human merge with
   matching head, G6 NOT_APPLICABLE (no staging target), G7 merged_by observed as a GitHub `User`
   (bot/app → FAIL), G8 NOT_APPLICABLE (no deployment target). Worker-reported, verifier, reviewer,
   accepted and merged are returned as separate fields (§35).
6. GovernanceHarnessAdapter contract v1.0 (frozen dataclasses, immutable payload, no DB session for
   adapters). The configured adapter is `NullGovernanceHarness` → NOT_CONFIGURED with downstream checks
   BLOCKED. Changed artifact hash / stale acceptance / moved PR head → REJECTED before the adapter runs;
   a payload mutated by an adapter or an output bound to another hash/contract → REJECTED.

## Consequences

* On GitHub Free private repositories merge gating is a residual risk shown in the UI; the human owner
  remains the only merge authority (ADR-0003 delegation applies to the seed build only).
* OFFICE delivery (Bitbucket) is NOT_APPLICABLE until a Bitbucket adapter exists.

## Amendment 2026-10-18 (integration)

1. **Capabilities for the step.** `prepare_pr` issues a GIT_BRANCH grant (`repo`, `branch`) and a
   PR_CREATE grant (`repo`, `head`, `base`) through `broker.facade.issue_grant` (15 min TTL, 3 requests)
   before the interlock and the push; they are revoked in a `finally` whatever happens. The broker
   refused every delivery push/PR before, because nothing issued these grants.
2. **Interlock binding.** The interlock intent carries the ACTIVE human APPROVAL decision of the task's
   plan version (whose `allowed_operations` include `create_pr` and whose targets name the repository)
   and the grant expiry; `decision_id=None`/no expiry made the real interlock refuse every PR.
3. **Results are not exceptions.** The push is sha-pinned (`expected_head_sha=<accepted head>`); a
   FAILED result raises `BROKER_PUSH_FAILED`, an UNKNOWN one is reconciled with
   `broker.facade.reconcile_side_effect` first and continues only when it reconciles SUCCEEDED (otherwise
   `BROKER_PUSH_UNKNOWN`); the same for PR creation (`BROKER_PR_FAILED`/`BROKER_PR_UNKNOWN`). Before any
   push/PR, PENDING/UNKNOWN side effects of the run are reconciled (§14 "... sebelum retry"); an accepted
   head already SUCCEEDED in the outbox is not pushed again.
4. The API commits before raising for these broker failure codes (and HEAD_MISMATCH), so the outbox and
   audit rows that later reconciliation reads are never rolled back.
5. The GitHub read token uses the secrets purpose `personal:delivery.provider-read`.
