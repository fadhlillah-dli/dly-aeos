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

## Amendment 2026-10-18 (integration)

Seam group W2 (scheduler <-> cost <-> routing <-> broker <-> runtime <-> verification <-> delivery) was
made to work end to end (`tests/e2e/test_e2e_execution_chain.py`). Reconciled contracts:

1. **Reopen on STALE acceptance (§14, §50 "PR baru mengubah head atau base").** DONE is no longer
   unconditionally terminal: `DONE -> READY` and `DONE -> BLOCKED` exist, legal ONLY with reason code
   `ACCEPTANCE_STALE` (`states.REOPEN_CODES`) and only through
   `workforce.facade.reopen_for_reverification(session, task_id, *, reason, actor)`; `transition_task`
   refuses to leave DONE otherwise (`REOPEN_REQUIRES_STALE_ACCEPTANCE`). READY when an attempt is left
   and the plan version is still the approved one (a new bounded attempt is verified + reviewed again),
   otherwise BLOCKED (owner decision / new approval). The accepted run keeps its history
   (`acceptance_status=STALE`). Callers: `verification.acceptance.invalidate` (used by delivery PR sync,
   the broker's branch-update hook, prepare-pr head mismatch, `recheck` and the verifier loop's
   periodic recheck of ACCEPTED records for config/plan drift). This closes the gap recorded in
   ADR-0062 Consequences.
2. **Fixtures are never live work.** `ensure_executions` keeps the MOCK/DEMO label of a TaskSpec (or its
   PlanVersion) on the TaskExecution, and admission blocks `NON_LIVE_TASKSPEC`, so no real worker,
   repository or budget is ever used for fixture content.
3. **Scope re-check at lease time.** Admission blocks `UNSAFE_PATH` (every allowed/forbidden glob through
   `aeos.planning.dag.unsafe_path` + the runtime normaliser) and `FORBIDDEN_PATH_ALLOWED` (an allowed area
   inside a forbidden one, compared case-insensitively because Windows/macOS file systems are).
4. **Owner-only blocks.** BLOCKED tasks with `QUARANTINED` (verifier, special review §21) or
   `REVIEW_BLOCKED` (review verdict BLOCKED) are never turned into a new attempt by a tick
   (`HUMAN_BLOCK_CODES`); only `retry_task` (owner) or the quarantine release clears them. The frozen
   facade `transition_task` gained the additive keyword `reason_code` so callers can name the code.
5. **Vocabulary.** `update_run(verifier_status=...)` accepts the verifier's own statuses
   (PASS/FAIL/QUARANTINED/NOT_PROVEN/BLOCKED; PASSED/FAILED kept). A refused reservation keeps the cost
   module's code (`QUOTA_EXHAUSTED` stays QUOTA_EXHAUSTED, `BUDGET_STATE_UNTRUSTED` is shown as
   BUDGET_EXCEEDED with the original code in the reason).
6. **Reservation lifecycle.** The run-level hold reserved at lease time receives partial commits from the
   broker (ADR-0080 amendment) and is settled by `stop_run_resources` on finish/cancel/crash/policy stop;
   a MOCK run's hold is released, never committed.
7. **Review fix (2026-10-19): owner-only blocks are sticky.** `refresh_readiness` no longer overwrites a
   `QUARANTINED` / `REVIEW_BLOCKED` reason code with a co-reason (unreconciled side effect, process still
   alive, attempt cap). Before, the co-reason replaced the code and, once it cleared, the next tick moved
   the task READY without the owner's special review.
8. **Review fix (2026-10-19): an owner retry discards the blocked candidate.** When `retry_task` clears a
   `QUARANTINED` / `REVIEW_BLOCKED` block, the attempt still in REVIEW ends FAILED (`DISCARDED_BY_OWNER`,
   lease released). Left in REVIEW it kept showing "waiting for review" and could still be reviewed,
   accepted (an ACCEPTED record is written before `mark_accepted` refuses) and published by the broker
   (`_require_publishable` accepts REVIEW runs) although the task had moved on.

## Amendment 2026-10-19 (cluster C7: admission answers, fairness, concurrency mode)

1. **Fairness (§10).** `admit_ready_tasks` admits in `fair_order`: a task READY for longer than
   `FAIRNESS_MAX_WAIT` (30 min) goes first (anti-starvation); otherwise projects are served round-robin, the
   project with the oldest last lease first, one task per project per round, each project's tasks
   oldest-READY first. Deterministic, no priority taken from model output.
2. **Admission answers (§19).** Admission refuses a TaskSpec without `allowed_paths` (`NO_ALLOWED_PATHS`,
   ADR-0033), without `expected_outputs` (`NO_EXPECTED_OUTPUT`), with a role that is not a catalog executor
   (`ROLE_UNKNOWN`), with a context estimate (prompt chars/4 + output cap, or `min_context_tokens`) above the
   route's context window (`CONTEXT_TOO_LARGE`), or with an agent outside its `allowed_scope`
   (`AGENT_OUT_OF_SCOPE`). It waits on `RESOURCE_INSUFFICIENT` (per-run memory — `limits.memory_mb` or
   `workforce.run_memory_mb`, default 512 MB — against free memory minus runs already LEASED, and one CPU per
   worker when the cap is 2) and on `ROLE_NOT_EVALUATED` (from the risk class in
   `workforce.role_eval_required_from`, default R4: a PASS evaluation of the role's current revision or an
   owner EXCEPTION on `role_evaluation/<role_key>`). The answers (compute reservation, context estimate,
   verifier suite status, reviewer path, role eligibility) are recorded on `WorkerRun.caps`. The compute
   reservation is admission-level only; the process sandbox does not enforce it (WEAK, ADR-0051).
3. **Serialisation classes (§19).** Two active tasks of one project conflict when their write sets overlap
   *or* when both touch one class: migrations, schema/models, auth/IAM, control-plane policy, contracts,
   pipelines — even with different files (two migration files can create two Alembic heads).
4. **Concurrency mode (§19, §21, §44).** Only an ACTIVE human `POLICY` decision on exactly
   `workforce/concurrency` raises the cap to 2. `set_concurrency` / `POST /api/workforce/concurrency` records
   it only with a COMPLETED evaluation run whose gates all PASS, the §44 DecisionRecord fields (options, goal,
   evidence ids, cost/quality trade-off, invalidation condition), the rollback (previous mode) and an optional
   task-class scope (project ids, role keys, risk classes, domains) that `concurrency_cap(session, task)`
   applies per TaskSpec. `AgentDefinition.limits.max_active` is an upper bound on the cap.
5. **Base commit (§21).** The first attempt pins `TaskExecution.base_commit`; every later attempt of the same
   TaskSpec starts from it. `reopen_for_reverification` (stale acceptance) releases the pin explicitly.
