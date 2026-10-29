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
   `workforce.run_memory_mb`, default 512 MB — against a fixed worker share (50 %) of the host's physical memory
   minus what LEASED/RUNNING runs reserved; momentary free memory is recorded as an observation/warning only, so
   admission does not depend on other processes' load — and one CPU per worker when the cap is 2) and on `ROLE_NOT_EVALUATED` (from the risk class in
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
6. **OFFICE route binding (§23, review).** An OFFICE task is admitted on a route only while the owner's
   `office_route/<route_key>` POLICY decision (recorded only by `POST /api/policy/office/routes`, bound to the
   route identity and the ACTIVE company authorisation) holds; otherwise it waits `OFFICE_ROUTE_NOT_AUTHORISED`.
   The generic decision endpoint refuses `office_route/*` and `workforce/*` targets
   (`DEDICATED_ENDPOINT_REQUIRED`), so neither the company-approval nor the evaluation evidence can be skipped.

## Amendment 2026-10-26 (wave 7 cluster D: owner controls, resource budget, two-worker experiment, investigations)

1. **Owner controls (§07 step 9, §17, §35).** Enabling or disabling an implementation agent, executing an approved
   plan, registering a project's trusted suite, adopting a role result and setting the concurrency mode are no
   longer API-only. Each is an owner action in the Control Center (Organisasi & Peran: agent card
   *Aktifkan/Nonaktifkan* and the role card *Adopsi hasil evaluasi*; Rencana/:id *Jalankan rencana*; Proyek/:id
   *Suite tepercaya*; Pekerjaan Live: scheduler loop toggle and the concurrency panel) and in the CLI
   (`aeos work agent-prereqs|agent-enable|agent-disable|execute|adopt-role|concurrency|experiment-grant`,
   `aeos verify suite-register|suite-status`). A CLI owner action proves the active SUPER_ADMIN by its password
   (never stored or logged); a username alone is not enough. One service (`aeos.workforce.agents`) serves both
   surfaces: enabling is refused with every unmet prerequisite (`AGENT_PREREQUISITES`, also audited as DENIED);
   under the container sandbox the pinned worker image is checked instead of the host OpenCode binary. Enabling
   or disabling re-runs the DB-only Doctor check `worker.agent_definition` (`doctor.facade.recheck`), so
   `CAN_RUN_WORKER` does not keep the result the action itself made STALE. Enabled is never "in use".
2. **Resource budget (§51 P2).** The owner's recorded `max_parallel_workers` (`aeos doctor budget`) is an upper
   bound on every cap: `concurrency_cap = min(policy or experiment cap, HARD_MAX_ACTIVE, budget)`. While no budget is
   recorded it is UNKNOWN and nothing is guessed. `concurrency_detail` states the cap, its source
   (`PILOT_DEFAULT`, `POLICY_DECISION`, `EXPERIMENT_GRANT`) and the budget; admission records it on
   `WorkerRun.caps.concurrency`.
3. **Two-worker experiment grant (§19, §44).** The permanent two-worker mode needs a completed topology-C evaluation
   that beat the sequential baseline, which needed two workers to exist first (a circular gate). The way out is a
   human EXCEPTION on `workforce/concurrency-experiment:<eval_run_id>` (dedicated endpoint
   `POST /api/workforce/concurrency/experiment`; the generic decision endpoint refuses `workforce/*`), bound to the
   run's identity, suite, configuration, conditions, topology and start time, with an owner-chosen expiry
   (1-30 days, never defaulted) in the domain of the run's tasks. While that run is RUNNING the cap is 2 only for
   TaskSpecs bound to its slots and only while every other active worker is one of them; write sets, compute,
   budget, sandbox, route and verifier are checked unchanged. A restart, the run leaving RUNNING, expiry or
   revocation ends it.
4. **Bounded-parallel evidence (§44).** `topology_benefit` now also requires `parallel_execution_evidence` PROVEN: at
   least two bound implementation attempts admitted with `caps.concurrency_cap == 2` whose recorded execution
   windows overlapped. A sequential run labelled C is not a bounded-parallel result. The adopted scope is the
   role the suite measured (`CONCURRENCY_TASK_CLASS_UNKNOWN` when the suite names none,
   `CONCURRENCY_SCOPE_OUTSIDE_EVIDENCE` for a wider role list). The critical-regression measure the benefit guard
   compares comes from the evaluation collector (W6-121, cluster C2); an UNKNOWN value is never a benefit.
5. **Ordered task set per slot (§44 B/C/D, §47).** `aeos eval bind --append` / `POST /api/eval/runs/{id}/bind`
   with `append` adds a TaskSpec to an already bound slot. `evaluation.topology.derive_slot_sample` collects one
   sample per slot: ACCEPTED only when every task was accepted; numeric measures summed with UNKNOWN kept UNKNOWN;
   latency is the wall clock from the first start to the last finish; `parallel_overlap_s` is measured from
   WorkerRun timestamps. A one-task slot is collected exactly as before.
6. **Read-only investigations (§19 "Beberapa hipotesis bug").** `run_type=INVESTIGATION`: a TaskSpec whose
   `capability_scope.operations` declares `investigation`, `investigation.timebox=<60..3600>`,
   `investigation.repair=<task_key>` and 1-5 `hypothesis:<key>=<statement>` entries (part of the approved
   PlanVersion). Each hypothesis is its own WorkerRun, one at a time under the cap; OpenCode runs with
   read/glob/grep/list only (edit/write/patch, bash, web denied); the timebox caps the run timeout; the answer is a
   JSON report in the final message, its evidence references checked against the worktree, stored as untrusted
   E0 evidence; any file change is a SCOPE_VIOLATION and nothing is committed. When every hypothesis is
   investigated the task waits in REVIEW (`INVESTIGATION_COMPLETE`). The integrator (a human owner) records the
   chosen EVIDENCED fix (verdict SUPPORTED and at least one reference found) as an APPROVAL decision bound to the
   hash of the compared reports (`POST /api/tasks/{id}/investigation/choose`, `aeos work investigation-choose`);
   the plan's one repair IMPLEMENTATION task, which depends on the investigation, then carries the finding as
   input and runs through normal admission, verification, review and acceptance. An investigation is never DONE
   or accepted. The investigation template and read-only permission set are part of `WORKER_PROMPT_REVISION`.
   Review additions: the hypothesis outcome is recorded once when its run ends (`caps.investigation_outcome`), so
   later lifecycle codes (chosen / not chosen / review findings) never make a hypothesis look uninvestigated or
   change the compared reports; only a run that ended `INVESTIGATION_REPORTED` can be evidenced (a report from a
   run that wrote a file, timed out or exited non-zero is inconclusive whatever it claims); the read-only rule is
   also checked on the worktree after the run, not only on tool events; and the repair task's dependency re-checks
   the choice's hash against the reports on record at use (a changed comparison needs a new choice). A refused CLI
   `agent-enable` keeps its DENIED audit row, as the Control Center does.

## Amendment 2026-10-29 (a dependency's accepted change is in the dependent's base)

* **`DEPENDENCY_NOT_IN_BASE` (point 6).** A dependency is DONE at acceptance, before its pull request is merged, and
  a dependent task starts from the HEAD of the project's source checkout (its `base_commit` is pinned at the first
  lease). Readiness and admission therefore also require every dependency's accepted head commit to be in that HEAD
  (`git merge-base --is-ancestor`, through `worker_runtime.facade.source_head_contains`); until its pull request is
  merged and the checkout updated, the dependent waits with `DEPENDENCY_NOT_IN_BASE`, a blocking wait code in the
  Control Center. An accepted head the checkout does not know, a missing accepted head and an unavailable checkout
  also wait (fail closed). Investigation dependencies keep their own rule. A squash merge never contains the
  accepted head, so dependent work needs merge commits, the repository's practice (LESSONS L32).
