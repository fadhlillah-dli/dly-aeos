# ADR-0128 — Per-token paid routes only by ESCALATION or OVERFLOW, ranked below every $0-marginal route

* Status: ACCEPTED
* Date: 2026-10-28
* Deciders: owner (Fadhlillah), decision R34 recorded in chat on 2026-10-28, with the owner's answers below.
  The owner can record an ADR decision on this file (`aeos policy adr-decide ADR-0128 ...`, see the register README).
* Amends: [ADR-0124](ADR-0124-best-first-route-selection-allowance-fit-cooldown-caps.md) decision 6 ("never a paid
  route after a skip") and answer 3 ("no paid OpenRouter routes"), and
  [ADR-0041](ADR-0041-model-routing-eligibility-and-identity.md) amendment 2026-10-26, decision C point 5 (no paid
  fallback). Neither ADR is edited (history is never rewritten).
* Related: the critic review of the phase 1c design (finding 1, tiering; finding 2, overflow needs its own owner answer),
  the sibling branches that seed the paid route (`feat/routing-go-full-registry`) and make BUDGET scopes match exact
  route keys (`fix/budget-scope-exact-routes`).

## The owner decision (verbatim, exact scope)

R34, 2026-10-28: "Lakukan yang terbaik; saya tidak ingin hanya membuang $10 di OpenRouter dan $10 di OpenCode".

The owner's answers of the same day, which bound the scope of this ADR:

1. Paid OpenRouter is used by ESCALATION (after 2 failed $0-marginal attempts of the task) AND OVERFLOW (when every
   $0-marginal route for the role is at capacity / allowance fit / cool-down).
2. PUBLIC tasks are never paid (they have the free fallback).
3. No GPT-6.1 Sol: all paid credit goes to DeepSeek V4.1 Flash 1017 via OpenRouter ZDR (about $8 plus a $1 floor).
   Seeding that route and its BUDGET are owner actions outside this ADR.
4. R31 stays: best models first per role, parallel per role, phased; `HARD_MAX_ACTIVE` stays 2.

This ADR covers ranking and admission only. It adds no route, changes no route's configuration identity
(`CONFIG_FIELDS`), no `policy/` file, no worker prompt and no context strategy, and never spends without the owner's
BUDGET decision.

## Decision

1. **Tiering** (`routing.service.rank_tier`). A *per-token paid* route (`routing.paid.per_token_paid`: it needs a
   BUDGET decision and is not a flat-fee SUBSCRIPTION route, so not free, not a loopback LOCAL route, not OpenCode Go)
   gets its own tier below every $0-marginal tier: PUBLIC 0 Go within its plan, 1 free OpenRouter, 2 any other route,
   **3 paid**; any other data class 0 free OpenRouter (never INTERNAL), 1 the rest, **2 paid**. Eval scores and
   `preferred_routes` re-order only inside a tier, so a paid route with a better score or an earlier preference is
   never chosen while a $0-marginal route can take the task.
2. **Admission** (`routing.paid.paid_admission`, used by `select_route` and re-checked by the scheduler's admission for
   every selection path, a pinned TaskSpec `primary` included). A per-token paid route may take a task only when:
   * the task is INTERNAL. PUBLIC never runs on a paid route (answer 2). CONFIDENTIAL and RESTRICTED do not either:
     the decision admits paid routes for INTERNAL work only, and widening it is a new owner question (LESSONS L11).
     And
   * **ESCALATION**: the task has at least 2 finished LIVE implementation attempts on $0-marginal routes that failed
     for a quality reason. A quality failure is a run that ended FAILED with outcome `REVIEW_FINDINGS` (what
     `request_repair` records for an attempt after a verifier FAIL or reviewer findings), or with verifier status
     `FAIL`/`FAILED`, or acceptance status `REJECTED`, and whose outcome is none of `QUOTA_EXHAUSTED`,
     `CONTROLLER_SHUTDOWN`, `LEASE_LOST`, `ORPHANED_ALIVE`, `TIMEOUT`, `RESOURCE_LIMIT`, `CANCELLED`, `REASSIGNED`,
     `POLICY_CHANGED`, `WORKER_BLOCKER`, `SCOPE_VIOLATION`, `EXIT_NONZERO`. While escalation applies the paid routes
     are tried first; a busy paid route still leaves the $0-marginal routes after it. Or
   * **OVERFLOW**: every $0-marginal route eligible for the task (domain, data class, tools, the TaskSpec allow-list) is
     blocked right now by a load skip (`ROUTE_BUSY`, `ALLOWANCE_FIT`) or a limit (cool-down, free-tier limit), and at
     least one such route exists. A paid route that is the task's only eligible route is not overflow.

   Otherwise the task waits as before: `ROUTE_CAPACITY` after a skip (the paid refusal is in its reasons), else
   `PAID_NOT_ADMITTED` (a new READY wait code, re-evaluated every tick).
3. **Every earlier gate still holds.** The paid route must be eligible (ACTIVE decision bound to its configuration, the
   owner's BUDGET decision covering the route, data terms, domain, tools, credential), the TaskSpec needs
   `limits.max_cost_usd` (else `COST_UNKNOWN`), and the lease reserves against the GLOBAL budget. `paid_fallback` in a
   TaskSpec stays refused, the 429 fallback chain (`_walk_fallback`) stays free-only, and a quota-stopped task still
   moves between attempts only to a $0-marginal route (ADR-0124 decision 4 is unchanged). A task bound to an
   evaluation slot runs on its evaluated route as before (its EvalRun is the owner's decision for that route).
4. **Owner switches** (settings, never `CONFIG_FIELDS` or `policy/`): `paid_escalation` and `paid_overflow`, both
   `true` by default (the owner's answer), each turning its path off ([OPERATIONS.md](../OPERATIONS.md), "Environment
   variables"). PUBLIC tasks are never paid whatever they say.
5. **Never silent.** Selection says `paid ESCALATION (ADR-0128)` or `paid OVERFLOW (ADR-0128)` in its reason. The
   admission answer carries `paid_admission`. When the lease is taken the run records `caps.paid_admission` (reason,
   quality-failure run ids, the blocked $0-marginal routes, the cost cap), the event `route.paid_admitted` (entity the
   run, the run's domain) and the audit record `routing.route.paid_admitted` (target the route).

## Consequences

* Go allowance is used first; credits are spent only where the owner said: on a task Go failed twice, or when every Go
  route is busy, out of window or cooling down.
* A paid route can no longer be a task's ordinary route: a deployment whose only eligible route is paid now waits with
  `PAID_NOT_ADMITTED` instead of running on it (no such deployment exists today: ADR-0124 answer 3 had no paid route).
* Two tests encoding the amended rules changed: ADR-0124's "never a paid route after a skip" now runs with
  `paid_overflow=false`, and the decision C test no longer selects a paid route that is the only eligible one.
* Identity invariance: `worker_prompt_revision()`, `policy_version` and every seeded route's configuration hash are
  unchanged (`tests/unit/test_routing_paid_escalation.py` pins them and re-checks them after escalation and overflow
  selections).

## Evidence and NOT-PROVEN

* E2: unit bite tests on macOS arm64 (`tests/unit/test_routing_paid_escalation.py`; fake routes, fake runs, no network
  and no spend; each red case asserts its own reason text, each near miss is green).
* NOT-PROVEN: Windows and macOS Intel until CI runs the tests; that a BUDGET scope matches only exact route keys (until
  `fix/budget-scope-exact-routes` is merged, a `routes=` scope is matched as written); the paid route itself, its
  per-request cost bound and its ZDR provider policy (other branches); concurrency of two controller processes (the
  selection reads in-flight runs in one session); that two quality failures predict a better result on the paid model.
