# ADR-0081 — Evaluation: AIConfiguration unit, eligibility gates, honest leaderboard, no automatic route change

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by module group H (evaluation)

## Context

Master §43 makes the full AIConfiguration (model/version + provider route + harness + prompt + tools
+ policy + context parameters) the unit of evaluation, makes security/authority/evidence gates
eligibility conditions rather than score components, requires failed/timeout/quota-interrupted runs
to be reported, per-role rankings with sample size, variation and ties, and states that every
candidate is "BELUM DIUJI" on the user's platform. §44 defines topology experiments A–D with
identical policy/security gates and the task classes to test. No route may change automatically
from an evaluation result.

## Decision

1. `aeos seed` (seeder `aeos.evaluation.seed`) stores eight protected, held-out suites (small bugfix, feature FE/API,
   shared schema/auth change, independent read-only reviews, docs-only change, provider quota
   interruption, self-build controller change, crash/recovery) with normal/negative/edge/adversarial/
   incident/recovery cases and acceptance criteria — **no results**. Suites are content-hashed; the
   seed never overwrites a suite whose hash differs (reported as a conflict for review).
2. Experiments A–D are code definitions sharing one `COMMON_GATES` object (one gates hash) and
   requiring a fixed model/tool configuration during topology tests.
3. `POST /api/eval/runs` only **plans** a run (status PLANNED) with the complete, normalised
   AIConfiguration (unknown values must be stated as `UNKNOWN`), its content hash and declared
   attempt/time/cost limits. Execution goes through the workforce scheduler and cost admission.
4. Results are written only via `aeos.evaluation.facade.record_result` by a trusted collector
   identity (`aeos.verification.facade.TRUSTED_COLLECTORS`), as per-sample observations; a recorded
   run is never overwritten. Metrics per §43 dimension are computed by code (Reliability includes
   provider availability: quota-interrupted or collector-flagged provider-unavailable samples);
   failures stay in every denominator. Every suite has normal, negative, edge and adversarial
   cases plus at least one incident or recovery case (tested against the definitions).
5. The leaderboard groups by configuration hash (same model on another route is a different entry),
   excludes any configuration whose security/authority/evidence gate FAILED or is NOT_RUN, ranks the
   rest by final acceptance rate with Wilson 95% intervals, and reports overlapping intervals as ties,
   with sample size and variance. No composite ranking without declared workload weights. With no
   runs, the response is `BELUM_DIUJI` with "Semua kandidat BELUM DIUJI dalam platform ini".
6. `POST /api/eval/scorecard` opens a COMPARE ChangeProposal (decision request) with the scorecard
   hash; no `ModelRoute` is modified by evaluation code.
7. Runs record the session domain they were planned from (`submitted_domain`); API views and the
   CLI leaderboard show platform runs plus runs of the chosen domain only, so Office-planned runs
   (whose notes may describe Office work) never appear or count in a Personal view. Planned-run
   notes and limits are redacted.

## Consequences

* The leaderboard is empty until the workforce executes evaluation runs and a verifier records them.
* Small samples are flagged informationally (n < 10); no universal acceptance threshold is set.

## Amendment 2026-10-19 (C5: execution, comparison, honest gaps)

* **Execution without a second scheduler.** `aeos.evaluation.executor`: an owner starts a PLANNED run (suite intact,
  route still on the planned configuration), every case x repetition becomes a slot, the owner binds a TaskSpec
  executed through the workforce to each slot, and the trusted collector (`collector`) derives every sample from
  canonical records (TaskExecution, WorkerRun, AcceptanceRecord, ReviewRecord, BrokerRequest, usage ledger) —
  status, first pass, repairs, latency/queue wait, worker cost/tokens, permission attempts, scope/security
  violations, critical regressions, Claude reviews, human interventions — and derives the gates from them. A task
  that ran on another route/harness is refused; unexecuted slots are recorded CANCELLED, never dropped. API
  `/api/eval/runs/{id}/start|bind|collect|finish`, CLI `aeos eval start|bind|collect|finish`.
* **Leaderboard.** Grouped by (suite, configuration, topology) — topologies are never pooled, and
  `/api/eval/topology` compares A-D for one fixed configuration. Critical regression excludes
  (`EXCLUDED_REGRESSION`; adjacent regressions are reported only). A registered route failing the terms/data gate
  for the workload's class (INTERNAL) is excluded (`EXCLUDED_ROUTE_TERMS`), a route changed since the run is
  `EXCLUDED_STALE_CONFIGURATION`; an unregistered route is flagged `NOT_REGISTERED`. Only runs under the reference
  conditions (limits + suite hash + environment) are ranked; a designated baseline gives deltas with conservative
  intervals; mixed environments are flagged. A combined ranking exists only with owner-declared per-suite workload
  weights (POLICY decision `eval_workload_weights/<role>`), with propagated intervals and ties.
* **§44 measures** (context/coordination/worker tokens, merge conflicts, critical vs adjacent regression, security
  violations) are computed under `metrics.usage`/outcome/contract/reliability.
* **Local builds** must state quantization/inference backend/hardware (part of the configuration hash).
* **Failures -> cases (§28).** `POST /api/eval/cases` turns trusted failure evidence, a failed run or an alert into a
  CANDIDATE case; only a human PROMOTION decision bound to the case hash adopts it, as a new protected suite (OFFICE
  failures are never promoted into platform suites).
* **Blind qualitative review**: anonymised executed samples; scores feed `product_quality`.
* **Controlled comparison (§39/§40)**: `POST /api/eval/comparisons` plans baseline + candidate runs on the same suites
  and limits for a reviewed/trial proposal; `GET /api/eval/comparisons/{proposal}` gives the side-by-side verdict
  (cost per accepted outcome, never "shorter output").
* **Protected path.** `backend/aeos/evaluation/**` is a self-build protected path (AGENTS.md + the three tuples).
* **Not available in this build (honest):** executable fixtures/graders for the held-out case definitions (each
  case needs an owner-authored fixture project + trusted suite, ADR-0060), an automatic case -> TaskSpec compiler,
  and therefore any real recorded experiment; run views say `fixtures: NOT_AVAILABLE_IN_BUILD`.

### Review addendum 2026-10-19 (C5 review)

* **Samples describe exactly one route configuration.** The collector refuses a task whose attempt began before
  the run started, and refuses to record a run whose registered route changed after the start (also when it was
  changed back: `config_changed_at` after the start) - `ROUTE_CONFIG_CHANGED`; such a run can only be finished
  (CANCELLED, never evidence).
* **No pooling across route configurations.** Leaderboard and topology entries are grouped by (suite,
  configuration, route config hash, topology); runs of an earlier route configuration are listed
  `EXCLUDED_STALE_CONFIGURATION` instead of being pooled into the current one. The environment fingerprint also
  records the observed OpenCode version.
* **Domain-first for cases and comparisons.** A failure (evidence/run/alert), case candidate or proposal bound
  to a domain is visible only from that explicitly chosen domain - never from a session without an active domain
  (NotFound, checked before any state is revealed).

### Amendment 2026-10-25 (wave 7, W6-121 / W6-141 / W6-142)

* **Critical regressions come from the verifier baseline (§34).** The collector derives a sample's
  `critical_regressions` from the trusted `VERIFICATION_REPORT` of every verified implementation attempt (the
  baseline check: trusted tests that pass on the recorded base and fail on the head), as distinct tests across
  attempts - a later repair does not redeem a regression (`regressions_by_attempt` shows each attempt). It is
  `None` (UNKNOWN, in `unknown_fields`) when a verified attempt has no trusted report or its baseline did not run
  and no regression is proven. A run whose samples state the measure but could not establish it has
  `outcome.critical_regressions = None` (`critical_regressions_unknown_samples` > 0) and is
  `EXCLUDED_REGRESSION_NOT_PROVEN` - never ranked as regression-free. The acceptance reasons
  "non-regression <key> is missing" (a review that did not argue a dimension) are a separate review-coverage
  measure, `non_regression_unargued` (`oversight`), never a regression.
* **Still not produced (stated UNKNOWN):** `adjacent_regressions` (the baseline check does not split regressed
  tests into critical/adjacent, so every one counts critical), `merge_conflicts` (no record observes PR
  mergeability) and `coordination_tokens` (the supervisor bridge's usage ledger is not linked to a task).
* **Controlled execution path per route pool.** `start_run` refuses (`EVAL_ROUTE_NOT_SERVED`, with the reason) an
  evaluation whose route has no controlled execution path: COMPARATOR/LOCAL/WATCHLIST in this build, and an ARENA
  route that fails the eval-only checks (PERSONAL only, TRIAL/APPROVED on a hash-bound decision, free, terms
  allowing the suite's workload classification). Run views show `execution.serving` (`BROKER` | `EVAL_ONLY` |
  `NONE`). A TaskSpec bound to a slot of a RUNNING run is routed to exactly the evaluated route (no ranking, no
  fallback: `EVAL_NO_FALLBACK`); an ARENA route serves only such tasks (eval-scoped broker grant, ADR-0111
  amendment). A bound task whose route cannot serve it is refused (`EVAL_ROUTE_NOT_SERVED` /
  `EVAL_ROUTE_NOT_ELIGIBLE`) instead of running elsewhere and producing an INVALID sample.
