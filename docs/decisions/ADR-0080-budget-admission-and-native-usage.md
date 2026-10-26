# ADR-0080 — Budget admission: $0 default hard stop, serialised reservations, fail-closed state, native usage never in dollars

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by module group H (cost)

## Context

Master §42 separates cash, worker tokens/requests, native Claude usage, compute/CI/storage and human
time, and forbids converting Claude tokens into dollars or a percentage of the Max quota. §14 requires
budget/reservation updates to be atomic ("concurrency tidak boleh membuka cara melewati batas"), §22
requires reserving cost before starting, and §50 requires admission to be refused when budget
enforcement cannot be trusted ("Budget DB/gateway tidak sehat") and no paid fallback on quota
exhaustion. AGENTS.md invariant 3 makes budget increases a human decision bound to an exact target
and hash.

## Decision

1. **Default:** a GLOBAL/`*`/MONTH worker cash budget of **$0 with a hard stop** is materialised on
   first admission. Only free routes (`ModelRoute.is_free = YES`) are admitted until an owner BUDGET
   decision raises it. A missing chain falls back to this default — more restrictive, never unlimited.
2. **Budgets are never edited in place.** `POST /api/budgets` records a `BUDGET` Decision through
   `aeos.policy.facade.record_decision` whose `target_hash` is the content hash of the budget terms
   (scope, period, usd/tokens/requests limits, hard_stop); the new row stores the same `terms_hash`
   and supersedes the previous row. If the decision registry is unavailable the change is refused
   (`DECISION_REGISTRY_UNAVAILABLE`).
3. **Admission (`reserve`)** first bumps a single-row `cost_admission_lock` (SQLite write lock held
   until the caller's commit/rollback), then re-reads budget state and checks
   `committed + reserved + request <= limit` for usd, tokens and requests on every applicable
   GLOBAL/PROJECT/ROUTE/RUN budget. Amounts are `Decimal` (6 dp) to avoid float drift. Holds never
   expire silently; crash recovery must release/commit them.
4. **Untrusted state refuses admission** (`BUDGET_STATE_UNTRUSTED`): budget without a valid ACTIVE
   human BUDGET decision, terms hash mismatch (row edited outside the flow), duplicate ACTIVE budgets
   for one scope/period, negative/non-finite numbers, ledger usage with UNKNOWN cost on a non-free
   route, unregistered route, paid route without pricing/pricing check, or a $0 estimate for a paid
   route, or a request without a `route_key` estimated at $0 (it cannot be proven to be a free route).
   An EXHAUSTED provider/route quota refuses admission (`QUOTA_EXHAUSTED`) — the caller waits,
   backs off or uses an already-approved free fallback. When the admission lock cannot be taken
   within SQLite's busy timeout the request is refused (`BUDGET_LOCK_UNAVAILABLE`).
   The request's domain is required and must equal the recorded domain of its project and run.
5. **Usage ledger:** `record_usage` commits a reservation and records actual vs estimate vs UNKNOWN;
   when the actual cost is UNKNOWN the reserved estimate is *charged* to budgets (`charged_usd`) but
   never shown as an actual cost. MOCK/DEMO entries never count as actual usage or against budgets,
   are listed with their mode instead of ACTUAL, and can never commit a LIVE reservation
   (`NON_LIVE_RESERVATION_COMMIT`). A commit must match the reservation's run/project/domain/route.
   An entry without an explicit domain takes the recorded (human-chosen, immutable) domain of its
   project/run — never model output — so Office usage and its dashboard event are never
   domain-less; a project/run domain mismatch is refused (`DOMAIN_MISMATCH`).
6. **Native Claude** (`NATIVE_CLAUDE`) is recorded as subscription usage: any dollar figure passed in
   is discarded; the dashboard shows "Batas langganan berlaku; sisa kuota UNKNOWN". Only an `official`
   source may report OK/LOW remaining quota; an observed limit message may set EXHAUSTED.
7. **ccusage** is an optional local ESTIMATE (token counts only, dollar figures dropped), run only if
   already installed (`ccusage` or `npx --no-install ccusage`, `--offline`); never an invoice or quota
   authority and never auto-installed. It aggregates every local Claude Code session, so only a
   SUPER_ADMIN with unrestricted domain/project scope may run it (`ESTIMATOR_SCOPE_DENIED`).
8. **Units** (CI minutes, compute seconds, storage bytes, human minutes) are recorded in their own
   unit through the additive facade `record_unit_usage`; their cash cost stays UNKNOWN unless a real
   price/bill exists, and they are never summed with worker API cost.
9. **Visibility:** summaries/ledgers are domain- and project-scoped. GLOBAL/ROUTE budgets aggregate
   every domain and project, so their spend is shown only to a principal who may see all of them.
   For the same reason only a SUPER_ADMIN with unrestricted domain/project scope may change them
   (`BUDGET_SCOPE_DENIED`); PROJECT/RUN budgets follow the project's domain scope.

## Consequences

* Paid worker routes cannot run until the owner records a BUDGET decision; this is intentional.
* A long-held caller transaction blocks other admissions for up to SQLite's busy timeout; callers
  must roll back on `Blocked` and keep the reservation transaction short.
* New columns on `usage_ledger`, `budgets`, `budget_reservations` and the `cost_admission_lock`
  table need the first Alembic revision (integrator) for databases created before this change.

## Amendment 2026-10-18 (integration)

**Run-level holds with partial commits.** The scheduler reserves once per run (lease time) while the
broker records one ledger entry per model call; the old "first `record_usage` commits the reservation"
rule made the second call of every run fail (`Conflict`) and would have released the remaining hold
mid-run (a concurrency hole for paid routes). The frozen facade `record_usage` gained the additive
keyword `partial=True`: the entry is added to the hold (committed usd/tokens/requests accumulate under
the admission lock, the reservation stays RESERVED), `held_usage` counts only the unused remainder (no
double count with the ledger), and `release` *settles* a partially committed hold (status COMMITTED,
the unused rest freed; event `cost.reservation.settled`). An UNKNOWN cost charges the remaining reserved
estimate once (subsequent UNKNOWN calls charge 0), so a paid run is charged at most its admitted
estimate unless the upstream reports more (overrun recorded). Single-call `record_usage` without
`partial` behaves as before. MOCK/DEMO usage still never commits a hold (`NON_LIVE_RESERVATION_COMMIT`);
the broker releases it instead.

## Amendment 2026-10-26 (flat-fee SUBSCRIPTION worker routes)

Context: the owner chose OpenCode Go (ADR-0041 decision A, amendment 2026-10-26). Go is a $10/month subscription
whose usage is a per-model dollar ALLOWANCE per provider window, not a per-token bill. Within the plan,
`usage.cost` is 0. Rule 1 ("only free routes are admitted until ...") and rule 5 ("an UNKNOWN cost charges the
reserved estimate") assume per-token billing. Under them a subscription route could not run honestly: it needed a
TaskSpec `max_cost_usd`, which the planner never sets, and it was charged made-up dollars. This amendment adds a
third billing class beside free and per-token. Native Claude (rule 6) is unchanged.

1. **Billing class.** `ModelRoute.pricing.billing == "SUBSCRIPTION"` with `is_free` NO, a checked pricing record,
   `allowance_usd_month` > 0, a share in (0, 1] for each window `5h` / `week` / `month`, and input/output
   `allowance_rate_per_mtok` (`aeos.cost.subscription.subscription_terms`). Incomplete or contradictory terms are an
   unpriced paid route and are refused (`BUDGET_STATE_UNTRUSTED`), never treated as free. The money primitives moved
   to `aeos.cost.amounts`; `aeos.cost.service` re-exports them.
2. **Budget.** The plan fee is the owner's BUDGET decision at route activation (routing `needs_budget`, ADR-0041),
   and it is not a `Budget` row. The marginal estimate within the plan is $0, so the GLOBAL $0 hard stop admits it
   (`pricing_basis` SUBSCRIPTION). A caller estimate above $0 stays a CALLER_ESTIMATE against the budgets.
3. **Ledger.** For a SUBSCRIPTION route, `cost_usd` is the provider's marginal charge.
   * An UNKNOWN marginal cost is charged $0 (`marginal_cost_unreported`), never the reservation.
   * The allowance a call consumed is `details.allowance_usd` with `allowance_basis`. It is REPORTED from the
     provider's figure, which the facade passes as `details.reported_allowance_usd`. Otherwise it is ESTIMATED from
     the tokens x the allowance rates, rounded up, or from the charged-token bound x the highest rate.
   * A marginal charge above $0 is paid usage beyond the plan. It is charged to the budgets, flagged
     `beyond_plan_usd`, and emitted as `cost.subscription.beyond_plan`.

   The frozen facade `record_usage` gains the additive keyword `details`. New additive facade functions:
   `subscription_terms` and `allowance_status`.
4. **Allowance windows.** `allowance_status` counts each route's recorded allowance (LIVE `WORKER_API` entries) over
   trailing windows of the longest length each can have: 5 hours, 7 days and 31 days. The provider's anchors are
   unknown, so AEOS never counts less than the provider's own window holds.
   * `reserve` refuses `SUBSCRIPTION_ALLOWANCE_EXHAUSTED` when a window's allowance is used up (used >= share x
     monthly allowance), before the provider's 429.
   * An entry whose allowance cannot be counted refuses admission (`BUDGET_STATE_UNTRUSTED`).
   * The scheduler waits with the same code (a QUOTA wait code). The run's grant cost cap is the $0 estimate, so a
     marginal charge stops the run's next request at the broker.
5. **Views.** `summary` adds `subscriptions` (the plan and, per window, the limit, used, remaining, reported and
   estimated amounts and the request count) and `by_route[].billing` / `allowance_usd`. The window figures span
   domains, so they are hidden like GLOBAL/ROUTE spend (rule 9). `ledger_view` labels a $0 call within the plan
   `cost_kind` SUBSCRIPTION, and `aeos cost summary` prints the windows (ASCII). A subscription route is never shown
   as per-token spend.

Consequences:

* **Open, not changed here:** a per-token paid worker route still needs TaskSpec `limits.max_cost_usd`. The planner
  never sets it, so such a route waits with `COST_UNKNOWN`, and an UNKNOWN upstream cost still charges the
  reservation. A cost estimate for per-token routes is an open owner decision: route price x token cap, or a
  plan-approved limit.
* There is no allowance hold for in-flight runs. Inside a run the provider's 429 is the boundary (RATE_LIMITED, no
  fallback).
* This ADR changed, so an owner ACCEPTED registration of it is STALE until it is recorded again (OWNER_ACTIONS 2.8).
