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
