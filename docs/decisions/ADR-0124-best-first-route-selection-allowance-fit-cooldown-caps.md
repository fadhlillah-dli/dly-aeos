# ADR-0124 — Best-first route selection: agent preference, Go first for PUBLIC (decision C'), per-task allowance fit, cool-down and concurrency caps

* Status: ACCEPTED
* Date: 2026-10-28
* Deciders: owner (Fadhlillah), decision R31 recorded in chat on 2026-10-28, with the owner's answers (1) to (4) below.
  The owner can record an ADR decision on this file (`aeos policy adr-decide ADR-0124 ...`, see the register README).
* Amends: [ADR-0041](ADR-0041-model-routing-eligibility-and-identity.md) amendment 2026-10-26, decision C, point 4
  ("free first") and the selection half of point 5 (the no-paid-fallback rule stays and is widened to every skip), and
  [ADR-0080](ADR-0080-budget-admission-and-native-usage.md) amendment 2026-10-26 (a used-up 5h window of a Go route is
  now skipped at selection instead of being waited on when another $0-marginal route can take the task). Neither ADR is
  edited (history is never rewritten).
* Related: ADR-0125 (Go WATCHLIST to WORKER owner action, new Go routes, tool-call probe evidence) and ADR-0126
  (per-role category agents that write `route_policy.preferred_routes`), built in parallel on their own branches.

## The owner decision (verbatim, exact scope)

R31, 2026-10-28: "best models first, the others after; use LiveBench or other accountable sources; the best available
models run in parallel, matched to each role's needs; applies to all usable OpenRouter and OpenCode models".

The owner's answers of the same day, which bound the scope:

1. PUBLIC tasks use OpenCode Go (flat $10/month subscription, $0 marginal) FIRST and the free OpenRouter routes as
   fallback, with PUBLIC's share of each Go route's 5-hour window capped so INTERNAL is not starved (amends ADR-0041
   decision C).
2. No broker support for Gemini 4 Argon / Qwen 3.8 Max (google/anthropic API families) now.
3. No paid OpenRouter routes.
4. Parallel workers phased: HARD_MAX_ACTIVE stays 2 in this phase.

This ADR covers selection and scheduling only. It does not add routes, change a route's pool or tool support, change
the worker prompt, `policy/` or the context strategy, or raise any global cap.

## Context

* Selection was deterministic per task and ignored load: tasks of one plan landed on the same route; a used-up Go 5h
  window kept the top Go route selected and the task waited (`SUBSCRIPTION_ALLOWANCE_EXHAUSTED`); a task the executor
  stopped at a quota waited for its old route (`scheduler.route_quota_exhausted`).
* LiveBench 2.0 (release 2026-10-25, `cost.csv`): one long agentic task costs more than a whole 5h window on the "Pro"
  Go models (kimi-k3.1-pro $7.43 against a $3 window; kimi-k3 $18.00). A long task started there would hit Go's limit
  partway and lose its work. In-flight runs held no allowance, so two concurrent runs could overshoot one window.
* OpenRouter's `:free` limit (20 requests/minute) is account-wide; Go documents no concurrency limit and monitors
  traffic for abuse, so a ban would lose every INTERNAL route.

## Decision

1. **Ranking** (`routing.service.rank`): tier -> evaluation score of the task's role -> the implementing agent's
   `route_policy.preferred_routes` position -> priority -> route key. The preference is contract (a) shared with
   ADR-0126: an ordered list of route keys that only re-orders routes that already passed every hard filter. It can
   never make an ARENA, WATCHLIST, paid-without-BUDGET route, or a `:free` route for an INTERNAL task, selectable
   (the ranking runs over eligible keys only and asserts it adds none). The scheduler passes the agent's list at
   admission (`ext.select_route_detail(..., preferred_routes=...)`); `routing.facade.select_route` takes it as an
   additive keyword.
2. **Decision C'** (answer 1): tiers for a PUBLIC task are 0 = a SUBSCRIPTION route within its plan (trusted terms,
   $0 marginal: OpenCode Go), 1 = a free OpenRouter WORKER route (decision C, now the fallback), 2 = anything else. Any
   other data class keeps the former tiers (INTERNAL ranking is unchanged and never reaches `:free`). The owner setting
   `go_public_share` (default 0.5) caps the share of each Go route's trailing 5h window that PUBLIC tasks may use
   (their recorded allowance plus the holds of their in-flight runs); past it a PUBLIC task skips the route
   (`PUBLIC_SHARE`) and goes to the free routes. INTERNAL tasks are not limited by the share.
3. **Per-task allowance fit** (`routing.capacity.allowance_fit`): a SUBSCRIPTION route admits a task only when its
   estimated allowance fits the remaining 5h window = 20% of the monthly allowance - the usage this AEOS instance
   recorded in the window - the remaining holds of its in-flight runs; equal fits. The estimate, in order: the median
   allowance of this instance's last finished runs (up to 10, at least 3) of the same role on the route; else the prior
   table (LiveBench 2.0 release 2026-10-25 `cost.csv`, `long_agentic / nq_long_agentic`, read 2026-10-28, rounded up
   to the cent: kimi-k3 18.00, step-5-preview 9.06, kimi-k3.1-pro 7.43, glm-5.3-flash 1.63, deepseek-v4.1-flash 1.45,
   mimo-v2.6-pro 1.22, minimax-m3.1-flash 1.09 USD; these models' list prices equal their Go allowance rates); else half
   the route's 5h window (conservative default). The admitted run records its estimate as
   `WorkerRun.caps.allowance_hold`; while the run is LEASED or RUNNING it holds that estimate minus what it already
   used (which the ledger counts); the hold ends when the run leaves those states. A route that does not fit is skipped
   (`ALLOWANCE_FIT`), never a failure and never a paid route. A window AEOS cannot count (`UNKNOWN`) is skipped too
   (fail closed). There is no calendar-month pace guard: the week and month windows stay advisory (ADR-0080).
4. **Cool-down** (`routing.capacity.cooldown_for` / `note_route_cooldown`): a 429 or a provider 5xx records a cool-down
   on the route's existing `QuotaState` (provider, scope = route key) - the 429 through the broker's existing quota
   write (cost module), a 5xx through `routing.service.note_rate_limited`: 60 s, doubling per repeat while the previous
   cool-down ended less than 30 minutes ago (or is still running), at most 30 minutes, or the provider's Retry-After when
   that is longer. No new column. A route in cool-down is a "limit" rejection, so new tasks skip it. A running attempt
   stays pinned to its route: the broker does not refuse its requests for a cool-down, and nothing re-routes an attempt.
   Re-selection happens only between attempts: a task the executor stopped at a quota (`QUOTA_EXHAUSTED`) stops waiting
   for its old route only when the expected wait is known and longer than the owner setting
   `route_reselect_after_minutes` (default 30); admission then re-selects and accepts only a route with an ACTIVE
   decision (eligibility), of the task's data class (eligibility), at $0 marginal (free, loopback LOCAL, or a
   SUBSCRIPTION within its plan); otherwise it waits (`QUOTA_EXHAUSTED`).
   **Relation to `scheduler.route_quota_exhausted`:** that function still reports the quota a task's route waits for,
   and admission still refuses a selected route whose own quota is exhausted ("wait/back off on the same approved route
   - never switch to a paid/unapproved one" holds). What changes is only `_quota_wait`: a quota-stopped task is no
   longer held in BLOCKED for its old route when the expected wait exceeds the threshold, and the move it then makes is
   restricted as above. A wait shorter than the threshold, an UNKNOWN reset time, or no $0-marginal alternative keeps the
   former behaviour (wait on the same route).
5. **Concurrency caps** (owner settings, never `CONFIG_FIELDS` or `policy/`): a per-route cap `route_max_active`
   (default 1 for a SUBSCRIPTION route with a monthly allowance of at most 30 USD, else 2), a total cap across the
   SUBSCRIPTION routes of one provider `go_max_active_total` (default 2), and one cap across every OpenRouter `:free`
   route `openrouter_free_max_active` (default 1, the account-wide rate limit). Every value is 1..2:
   `HARD_MAX_ACTIVE` stays 2 (answer 4) and still bounds everything. A route at a cap is skipped (`ROUTE_BUSY`). The
   active tasks are the LIVE runs that are LEASED or RUNNING.
6. **Never a paid route after a skip** (answer 3, widening ADR-0041 decision C point 5 from free routes to every skip):
   once a ranked route was skipped for load or kept out by a limit, only a $0-marginal route may serve; otherwise the
   task waits with `ROUTE_CAPACITY` (a quota wait, WAITING_QUOTA), or `FREE_TIER_LIMIT_REACHED` when a free route is at
   its limit. `paid_fallback` stays false; the GLOBAL $0 hard stop and the BUDGET rules are unchanged; Go Extra Usage is
   never used.
7. **Visibility**: skip reasons lead with their code and never carry allowance figures. The admission answer
   `route_capacity` (cap, active tasks, fit, the hold) spans domains, so `GET /api/tasks/<id>/admission` shows it only
   to a principal who may see every domain (the ADR-0080 rule for `subscription`).

## Consequences

* The best eligible route serves first; load spreads to the next ranked $0-marginal route instead of queueing behind
  one route. Pro Go routes take a task only when it fits their window; most long work lands on the cheaper Go models.
* An evaluation-bound task (`_select_eval_route`) runs on exactly its evaluated route, with no ranking and no load
  skip (unchanged). A TaskSpec `primary` route is pinned: when it is at capacity the task waits (`ROUTE_CAPACITY`).
* Identity invariance: `worker_prompt_revision()`, `policy_version`, `context_strategy` and the configuration hash of
  every seeded route are byte-identical to origin/main 27ebb58 (`tests/unit/test_routing_best_first.py` pins them and
  re-checks them after ranking, caps, fits and cool-downs). No ACTIVE TRIAL is voided.

## Evidence and NOT-PROVEN

* E2: unit bite tests on macOS arm64 (`tests/unit/test_routing_best_first.py`; each red case asserts its own reason
  text, each near miss is green). E1: LiveBench 2.0 release 2026-10-25 data files read 2026-10-28.
* NOT-PROVEN: Windows and macOS Intel until CI runs the tests; that Go answers a limit with HTTP 429 (ADR-0080); how
  far LiveBench's long agentic cost predicts an AEOS task (the prior is replaced by this instance's own history after 3
  finished runs per route and role); concurrency of two controller processes (the selection reads in-flight runs in one
  session; the scheduler is one controller); the allowance AEOS counts excludes usage of the same Go workspace from
  other clients (ADR-0080), so Go can still refuse first.
