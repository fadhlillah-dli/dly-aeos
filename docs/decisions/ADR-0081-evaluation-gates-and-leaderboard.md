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
