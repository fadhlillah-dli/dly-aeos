# ADR-0050 — TaskExecution state machine, admission and leases (§14, §19, §21, §22)

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by the seed build (ADR-0003)

## Context

Master §14 fixes the runtime states QUEUED/READY/LEASED/RUNNING/REVIEW/BLOCKED/DONE/FAILED/CANCELLED
and a diagram of edges, and additionally requires "cancellation pada semua state nonterminal, lease
expiry ketika RUNNING, serta kegagalan verifier/reviewer tanpa menganggap hasil lulus". The diagram
has no READY->BLOCKED and no exit for a lost lease while RUNNING; §21 caps attempts at two; §19 lists
the questions admission must answer; §22 lists stop conditions (quota, lease lost, owner cancel).

## Decision

1. `aeos.workforce.states.LEGAL_TRANSITIONS` is exactly the §14 diagram plus cancellation from every
   non-terminal state plus `RUNNING -> BLOCKED`, the latter allowed ONLY with a controller stop code
   (`LEASE_LOST`, `LEASE_LOST_PROCESS_ALIVE`, `QUOTA_EXHAUSTED`, `REASSIGNED`, `CONTROLLER_SHUTDOWN`,
   `POLICY_CHANGED`). DONE/FAILED/CANCELLED are terminal. Every illegal edge raises `Conflict`.
   `POLICY_CHANGED` (§22 "Stop berlaku saat ... policy berubah") is applied every scheduler tick by
   `enforce_stop_conditions`: an attempt whose plan approval is no longer the ACTIVE approved version,
   whose sandbox requirement no longer holds (exception revoked/expired) or whose OFFICE policy denies
   it is stopped (capability revoked first, process tree killed) and its task waits BLOCKED.
2. Every transition is a conditional `UPDATE ... WHERE state=? AND row_version=?` (optimistic
   concurrency); a lost race raises `Conflict`. READY->LEASED reserves the budget and inserts
   lease + run in the same transaction; exactly one concurrent caller wins (tested with threads).
3. DONE is reachable only via `mark_accepted` (REVIEW + matching ACCEPTED AcceptanceRecord that
   `aeos.verification.facade.acceptance_is_valid` still accepts for the run base and plan hash).
   `transition_task` refuses DONE. External tracker "Done" is never read for readiness.
4. A failed attempt (non-zero exit, timeout, scope violation) marks the *run* FAILED; the *task*
   goes `RUNNING -> REVIEW` (reason `ATTEMPT_FAILED`, "diagnosis + repair decision required") while
   attempts remain, otherwise `RUNNING -> FAILED`. Repair is `REVIEW -> READY` (new attempt by the
   implementer, never the reviewer) or `REVIEW -> BLOCKED/NEEDS_DECISION` at the cap. Extra
   attempts require an owner EXCEPTION decision bound to the TaskSpec hash.
5. READY never moves back to BLOCKED (no §14 edge): admission failures annotate the READY row with
   a reason code (`CAPABILITY_NOT_READY`, `SHARED_WRITE_CONFLICT`, `BUDGET_EXCEEDED`, ...). The UI
   derives WAITING_QUOTA / BLOCKED / STALE / WAITING_REVIEW from state + reason; the canonical state
   is always shown next to it.
6. Admission answers §19 in code: scheduling not paused, approved version, dependencies DONE with
   valid acceptance, attempts left, no nested fan-out, enabled implementation agent, OFFICE policy,
   `CAN_RUN_WORKER` (doctor facade), sandbox requirement (ADR-0051), concurrency cap (1; 2 only with
   an ACTIVE human decision `details.max_active_workers>=2`), write-set overlap (conservative prefix
   overlap -> serialised), route eligibility (routing facade + WORKER pool + domain), and a cost
   estimate (non-free routes need `limits.max_cost_usd`). Missing modules fail closed.
7. Leases expire without heartbeats. Reconciliation revokes the run's grants, checks the old pid
   with its psutil create_time, kills it, and only when it is confirmed dead may the task become
   READY again; an alive orphan keeps the task BLOCKED (`LEASE_LOST_PROCESS_ALIVE`). Stale
   heartbeats/actions from an old lease are rejected and audited.

## Consequences

* A worker crash or controller restart can never start a duplicate run while the old process is
  alive; quota-stopped or crashed attempts still count against the attempt cap (bounded retries).
* Two concurrent workers require an explicit owner decision; overlapping write sets are serialised.
* The REVIEW-after-failure interpretation keeps the diagram's edge set; it is revisited if the
  owner prefers FAILED tasks to be re-openable (would need a new edge and a new ADR).
