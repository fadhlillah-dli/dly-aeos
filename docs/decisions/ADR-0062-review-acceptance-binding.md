# ADR-0062 — Checkpoint review and acceptance: verifier PASS + review verdict, hash-bound, STALE on change

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah)

## Context

§15/§19/§22/§34: Claude reviews structured checkpoints read-only (REVIEW / VERIFY / VERDICT), never
repairs what it approves, and a reviewer verdict alone is never acceptance (U-14). §12: when the
native bridge is not proven, the user runs a manual package in official Claude Code — never the
Claude API. §14/§50: a changed head/base invalidates acceptance and triggers re-verification.

## Decision

1. Review requires the latest verification report of the run to be PASS for the run's head. The
   package contains TaskSpec summary, diff summary/excerpt (redacted), changed files, verifier evidence
   refs/summaries, worker claims labelled untrusted and the eight §34 non-regression scope questions —
   no transcripts or heartbeats. It is stored as REVIEW_PACKAGE evidence and its hash is bound.
2. The bridge is called only when the doctor reports `CAN_REVIEW_WITH_MAX` READY. Otherwise (or when
   the bridge module is unavailable) the review is `NEEDS_DECISION` with a manual package; results are
   imported later (bridge reader or JSON submitted by the owner) and validated identically
   (mode MANUAL_FALLBACK). Quota exhausted → `WAITING_QUOTA`, verdict BLOCKED, no gate, no repair.
3. Output is validated against a JSON schema; invalid output → NOT-PROVEN. Verdicts are clamped to the
   evidence (no staging/production evidence → at most PASS-CONTROLLED; PASS with own HIGH/CRITICAL
   findings → NOT-PROVEN; model outside `reviewer_policy.allowed_models` → NOT-PROVEN). Non-PASS
   completed verdicts request a repair through `aeos.workforce.facade.request_repair` (new attempt);
   BLOCKED moves the task to BLOCKED.
4. Acceptance (deterministic, `acceptance-engine`) is ACCEPTED only when the verifier PASSed with
   trusted LIVE evidence for the accepted head **and** a completed non-MOCK review bound to the same
   artifact hash returned PASS-CONTROLLED or better; PASS-STATIC only for docs-only changes whose
   TaskSpec allows static evidence (R0 or `PASS-STATIC`/`STATIC`/`DOCS_ONLY`). The record binds artifact
   hash, base/head, config hash, PlanVersion hash and TaskSpec hash; then
   `aeos.workforce.facade.mark_accepted` moves the task to DONE.
5. Validity is re-derived on read (`staleness`: run head, PR head/base, plan hash, current verifier
   config hash). Persisted invalidation (`invalidate`, `recheck`, PR sync, branch-update hook) sets
   STALE with the reason; re-verification and review of the new state are required.
6. §15 Security Reviewer: a separate review session (`reviewer_kind=SECURITY`, own prompt focus, own
   record) is **required** for R3/R4 tasks, when `reviewer_policy.security_review` is set, or when the
   change touches security-sensitive controls (identity, secrets, policy, broker, API security/deps,
   redaction, paths, worker runtime, Claude bridge, CI workflows). Acceptance is Blocked until it is
   COMPLETED and must pass under the same verdict rules; G4 waits for it.
7. §31 high-impact changes (routing, hooks, IAM, secrets, CI, migrations, schema/models, facades,
   flags/config, tools, instructions) are flagged by the verifier (`impact: HIGH`); acceptance then
   requires every §34 non-regression dimension of the review to be COVERED or NOT_APPLICABLE.
8. A deterministic trigger (`aeos.verification.loop`, `aeos verify pending`) verifies REVIEW runs once
   per head commit; a BLOCKED result is not retried automatically.

## Consequences

* A task that became STALE after DONE needs a new attempt from the scheduler; there is no workforce
  facade to reopen it yet (recorded as a gap).
* Acceptance is not a human approval: merge to main and release remain human decisions (ADR-0063).

## Amendment 2026-10-18 (integration)

1. **Binding = the WorkerRun's identity.** The AcceptanceRecord stores the run's runtime-recorded
   `head_commit`/`base_commit` (the verifier's view only when the run has none), exactly what
   `workforce.mark_accepted` compares; a verification made against another base is REJECTED.
2. **STALE reopens.** `invalidate` (PR sync, branch-update hook, prepare-pr head mismatch, `recheck`, the
   loop's periodic recheck of ACCEPTED records) calls `workforce.facade.reopen_for_reverification` when an
   ACCEPTED record became STALE, closing the gap noted in the Consequences above (ADR-0050 amendment).
3. **Quota exhausted (§50 "Fable Max quota habis").** The review stays `WAITING_QUOTA` / verdict BLOCKED;
   the task stays in REVIEW (no repair, no new attempt, no gate); the run carries
   `review_verdict=BLOCKED`, `quota_status=WAITING_QUOTA` (UI WAITING_QUOTA), and the observed limit is
   recorded through `aeos.cost.facade.set_quota_state(provider="claude-native", state="EXHAUSTED",
   source="observed-limit")` — never as dollars or a % of Max. A later review of the same run proceeds.
4. A BLOCKED review verdict moves the task to BLOCKED with reason code `REVIEW_BLOCKED`, which only an
   owner action clears (never a scheduler tick).
5. **Review fix (2026-10-19): native quota scope.** The observed Max limit is recorded per model family
   (`scope=fable|opus|...`, else `max`) — the scope the claude bridge itself records and clears — not `*`:
   a `*` row without reset time was never cleared and showed Max EXHAUSTED forever. The review writes it in
   its own session (the bridge's separate side-effect transaction cannot while the review holds the SQLite
   write lock). A later review that COMPLETED sets that family back to `UNKNOWN`
   (`observed-successful-call`, never `OK`), and the run's `quota_status=WAITING_QUOTA` is cleared as soon
   as a review got past the quota. MOCK reviews never touch the native quota state.
