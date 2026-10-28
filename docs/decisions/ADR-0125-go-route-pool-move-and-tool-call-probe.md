# ADR-0125 — OpenCode Go WATCHLIST/WORKER owner move, the R31 Go WORKER candidates and tool-call probe evidence (§23, §43)

* Status: PROPOSED
* Date: 2026-10-28
* Deciders: owner (Fadhlillah) — owner decision of 2026-10-28, recorded by the owner in chat (register R31). The
  mechanism below (the guarded owner move, the seeded candidates, the tool-call probe and its evidence) is the
  builder's design for that decision; the owner can record an ADR decision on this file
  (`aeos policy adr-decide ADR-0125 ...`, see the register README).
* Relates to: [ADR-0041](ADR-0041-model-routing-eligibility-and-identity.md) (decision A, OpenCode Go; decision C, the
  owner pool move of free OpenRouter routes), [ADR-0080](ADR-0080-budget-admission-and-native-usage.md) (subscription
  routes, the $0 GLOBAL hard stop), [ADR-0111](ADR-0111-office-code-delivery-and-route-feasibility.md) (the
  feasibility probe). ADR-0041 and ADR-0080 are not edited (history is never rewritten). Sibling ADRs of the same owner
  decision: ADR-0124 (best-first selection) and ADR-0126 (per-role agents).

## Context

The owner's decision R31 (2026-10-28): "best models first, the others after; use LiveBench or other accountable
sources; the best available models run in parallel, matched to each role's needs; applies to all usable OpenRouter
and OpenCode models". The owner's answers that bound this ADR: no paid OpenRouter routes; no broker support for the
Go models on the google or anthropic API families (Gemini 4 Argon, Qwen 3.8 Max) now; PUBLIC tasks use OpenCode Go
first with free OpenRouter routes as fallback (that selection change is ADR-0124, not this ADR).

Three gaps stood between that decision and the Go models the broker can serve:

1. **The better Go models are WATCHLIST rows.** glm-5.3, glm-5.2, kimi-k3.1-pro, mimo-v2.6-pro, longcat-2.0 and
   deepseek-v4.1-flash were seeded as WATCHLIST (ADR-0041 decision A). `aeos seed` never changes a pool, the catalog
   refresh never does either, and `set_route_pool` moved only OpenRouter `:free` routes (decision C).
2. **Two servable Go models were not in the registry.** models.dev (`providers/opencode-go`, read 2026-10-28) lists
   `kimi-k3` and `step-5-preview` on the chat-completions family (`https://opencode.ai/inference/go/openai/v1`) with
   `tool_call` true; `https://opencode.ai/go` (read 2026-10-28) gives each a $15 monthly allowance, no training and
   0-day retention.
3. **Tool support was a catalog claim only.** `supports_tools` came from models.dev or the OpenRouter catalog. It is
   part of the configuration identity, so raising it after an activation voids the TRIAL; the feasibility probe sends
   no tools and runs only on an activated route. "Probe before activation" can therefore never be satisfied, and a
   route seeded with tools UNKNOWN could never become tool-capable without a new configuration. models.dev now reports
   `tool_call` true for every Go model, including one it reported without tool capability on 2026-10-26: it is not
   proof.

## Decision (implemented)

1. **Owner move of an OpenCode Go route between WATCHLIST and WORKER.** `aeos routes set-pool <route_key> --pool
   WORKER|WATCHLIST --as <admin> --rationale ...` (password-confirmed) and `POST /api/routes/{route_key}/pool`
   (SUPER_ADMIN; `pool` now also accepts WATCHLIST) call `routing.service.set_route_pool`, which now also moves a route
   of provider `opencode-go`, either way between WATCHLIST and WORKER, **only while it is CANDIDATE and no decision of
   any kind or status names it**: no decision targets its route key (ACTIVE, INVALIDATED, expired, any kind) and no
   BUDGET decision lists it in `auth_scope.routes`. Otherwise it is refused with `ROUTE_POOL_MOVE_NOT_CANDIDATE` (the
   status, e.g. TRIAL) or `ROUTE_POOL_MOVE_HAS_DECISIONS` (the decision ids). So the move never voids an owner decision.
   Every other move keeps decision C's rules (`ROUTE_POOL_MOVE_UNSUPPORTED`). The move is a new configuration (the
   pool is part of the identity): the existing path recomputes the hash, readiness of both pools goes STALE,
   `route.config_changed` and the one `route.pool_set` event are emitted, and the CLI and the API audit
   `route.set_pool`. The owner then activates the WORKER configuration as a TRIAL with the existing hash-bound decision
   and a BUDGET decision for the plan fee. `aeos seed` and the catalog refresh still never change a pool. This amends
   decision C's scope ("`set-pool` moves only `:free` routes") for OpenCode Go only; ADR-0041 is not edited.
2. **R31 Go WORKER candidates.** `aeos seed` adds `opencode-go/kimi-k3` and `opencode-go/step-5-preview` as WORKER
   CANDIDATE routes with priority after the earlier Go WORKER pilots (priority is not part of the identity and not a
   ranking), `supports_tools` UNKNOWN, the Go subscription pricing ($10/month plan, $15 monthly allowance each,
   allowance rates per models.dev), the Go data terms (no training, 0-day retention, provider claims) read
   2026-10-28, chat-completions request shape, no fallback chain and `source_refs` naming the URLs and the date.
   Every route seeded before keeps a byte-identical configuration hash; a test pins the hash of each of them
   (`tests/unit/test_routing_go_registry_probe.py`, with the worker prompt revision pinned).
3. **Tool-call probe.** `aeos broker probe-route --tools <route_key>` and `POST /api/broker/routes/{route_key}/tool-probe`
   (SUPER_ADMIN, PERSONAL) run the feasibility probe's gates unchanged: TRIAL/APPROVED on a hash-bound human decision,
   PERSONAL, PUBLIC data allowed, and no money (free within its period, a loopback LOCAL route, or a subscription
   route whose state admits; a per-token paid route is `ROUTE_NOT_FREE`). It sends ONE synthetic PUBLIC request with
   one trivial function definition (`report_status`, `tool_choice` auto) under its own output cap
   `TOOL_PROBE_MAX_TOKENS` = 1024 (`broker/feasibility.py`; room for the short reasoning preamble of reasoning models,
   at most about $0.015 of plan allowance at the highest seeded Go rate), no streaming, no retry, never another route.
   **PASS** only for an HTTP 200 answer whose every tool call has an `id`, type `function` (or none), the defined
   function name and `arguments` that are a JSON string of exactly `{"status": "OK"}`; anything else, a 429 or a
   timeout included, is **FAIL** with the reason. The verdict is `PROBE` evidence (E3, trusted `collector` origin,
   collector `aeos-broker/route-tool-probe`) whose environment carries `probe: TOOL_CALL`, `verdict` and the route's
   `config_hash`; the usage ledger, the audit (`broker.route_tool_probe`), readiness STALE and the event
   `route.tool_probe` follow. **The probe never changes `supports_tools`** or any other configuration field.
4. **Contract (b), `routing.facade.tool_capable(session, route)`.** A route is tool-capable when `supports_tools` is
   YES, or when a trusted PASS of the tool-call probe exists whose `config_hash` equals the hash recomputed from the
   route's current record. Routing's hard filter uses it wherever a task needs tools. A configuration change (a new
   hash) makes older probe evidence irrelevant, as it does for decisions and eval results; evidence from any other
   origin (a worker attachment) never counts.

Owner flow (OWNER_ACTIONS 1.11): move a Go WATCHLIST route to WORKER (or take a seeded R31 candidate), activate it as
TRIAL with tools UNKNOWN - it is then eligible only for tasks that need no tools - and run the tool-call probe; a PASS
makes it eligible for tool-using tasks under that configuration.

## Consequences

* **Invariants kept.** No money is spent: `paid_fallback` stays false, the GLOBAL $0 hard stop is unchanged, a probe
  of a per-token paid route is refused and a subscription probe uses plan allowance only (Extra Usage stays the
  owner's setting). INTERNAL data never reaches a `:free` route (unchanged). `worker_prompt_revision()` inputs,
  `policy/*.yaml` and the context strategy are untouched, so no ACTIVE TRIAL is voided.
* **A probe PASS can change eligibility without a new decision.** That is the point of contract (b): `supports_tools`
  is part of the identity, the probe evidence is not. The evidence is bound to the exact configuration that answered.
* **Contract (b) is literal.** Any PASS for the current hash makes the route tool-capable; a later FAIL for the same
  hash does not revoke it (one well-formed call proves the configuration can produce one). A provider that degrades
  shows up in worker runs and the route-behaviour check, not here.
* **The Go WORKER pool grows.** More routes can be activated; each still needs the owner's terms review, region and
  BUDGET decision. Ranking among them is ADR-0124.

## NOT-PROVEN

* No live call: the Go answer shape for tool calls (ids, `type`, string `arguments`), whether Go accepts `tool_choice`
  and tools on these models, and the allowance figures are E1 (models.dev and opencode.ai/go read 2026-10-28) and E2
  (unit tests against respx mocks on macOS arm64). Windows and macOS Intel are NOT-PROVEN until CI runs.
* Whether 1024 output tokens always suffice for a reasoning model to reach its tool call: a FAIL whose reason names
  `finish_reason length` means the cap, not the model.
* One well-formed call of a trivial function says nothing about the quality of multi-step tool use; that stays an
  evaluation question (§43, ADR-0081).
