# ADR-0129 — The full OpenCode Go registry, Console-translation candidates, the paid DeepSeek route in the role ranking and one invariant 6 check (§23, §42)

* Status: ACCEPTED
* Date: 2026-10-28
* Deciders: owner (Fadhlillah), owner decisions R31 and R34 of 2026-10-28, recorded by the owner in chat. The mechanism
  below is the builder's design for those decisions. The owner can record an ADR decision on this file
  (`aeos policy adr-decide ADR-0129 ...`, see the register README).
* Relates to: [ADR-0124](ADR-0124-best-first-route-selection-allowance-fit-cooldown-caps.md) (best-first selection,
  "never a paid route after a skip"), [ADR-0125](ADR-0125-go-route-pool-move-and-tool-call-probe.md) (R31 Go
  candidates, the tool-call probe), [ADR-0126](ADR-0126-per-role-category-agents.md) (the generated role ranking;
  this ADR amends its "never listed" rule and its one-release-per-category rule), AGENTS.md invariant 6.

## Context

Owner decision R34 (2026-10-28), verbatim: "Lakukan yang terbaik; saya tidak ingin hanya membuang $10 di OpenRouter
dan $10 di OpenCode" (do your best; I do not want to just waste $10 on OpenRouter and $10 on OpenCode). The owner's
answers to the follow-up questions, as recorded in the decision brief of the same day:

1. Paid OpenRouter is used by **escalation** (after two failed $0-marginal attempts of the task) **and overflow**
   (when every $0-marginal route of the role is at capacity, in allowance fit or in cool-down). **PUBLIC tasks are
   never paid**: they have the free fallback.
2. No GPT-6.1 Sol. All paid credit goes to DeepSeek V4.1 Flash 1017 through OpenRouter ZDR hosts (about $8, plus a $1
   floor).
3. Yes: probe Gemini 4 Argon, Qwen 3.8 Max, Qwen 3.8 Flash, Qwen 3.7 Plus and MiniMax M3 through OpenCode Go's API
   translation on chat-completions. This amends answer 2 of R31 ("not now", ADR-0125 and ADR-0126).

R31 still holds: the best models first per role (LiveBench), parallel per role in phases, `HARD_MAX_ACTIVE` stays 2.

The registry held 13 of the Go chat-completions models. `https://opencode.ai/go` (read 2026-10-28) lists four more
(deepseek-v4-pro, hy4-preview, mimo-v2.5, mimo-v2.5-pro) and five that Go serves on the google or anthropic API family
(models.dev `providers/opencode-go`). Its privacy table states "Not used" for training and "0 days" retention for all
nine. Its docs state that every text model also works through the other APIs, which the Console translates. Whether
that translation serves chat-completions with tools is unknown until a live probe.

The ranking (ADR-0126) scored the Go DeepSeek route with the LiveBench 2026-10-25 score of the 1017 update. Go serves
`deepseek-v4.1-flash` without a date (models.dev dates it 2026-09-10, the original build). Release 2026-10-14 scored the
original build. The 1017 build is what the paid OpenRouter route serves (`deepseek/deepseek-v4.1-flash-1017`, canonical
slug dated 2026-10-17).

Invariant 6 (Claude only through the native `claude` binary) was enforced in the ranking only, as markers in
`role_agents.NEVER_PREFERRED`. The seed, the catalog discovery and eligibility had no check. Go lists a Claude model
(`claude-haiku-5-5`), and OpenRouter lists `anthropic/*`, `~anthropic/*` floating aliases and meta-routers
(`openrouter/auto`, `openrouter/fusion` and others).

## Decision (implemented)

1. **Seed the remaining Go models (`aeos.routing.seed`), as new routes only.** All are WORKER, CANDIDATE, tools
   UNKNOWN, on the chat-completions endpoint with the bare model id. Allowances and data terms come from
   `https://opencode.ai/go`, and rates from models.dev (both read 2026-10-28).
   * `deepseek-v4-pro` ($15): PUBLIC only. The DeepSeek zero-retention agreement is renewed monthly, and the current
     one runs through 2026-10-31. The model needs the Global region of the Go workspace. Priority 8.
   * `hy4-preview` ($30). Priority 9.
   * The translation candidates are `gemini-4-argon` ($30), `qwen3.8-max` ($15), `qwen3.8-flash` ($30),
     `qwen3.7-plus` ($60) and `minimax-m3` ($60). `data_terms.serving` and the notes say "served through OpenCode
     Console API translation - NOT PROVEN until the plain and --tools probes pass". `data_terms.native_api_family`
     names the family models.dev states. They get priorities 30 to 34, after the Zen pilots. The broker already speaks
     chat-completions, so no broker code changes.
   * `mimo-v2.5` ($60) and `mimo-v2.5-pro` ($15) are superseded by mimo-v2.6-flash and mimo-v2.6-pro. They have their
     own allowances and priorities 990 and 991, the lowest of the WORKER pool, so they serve as overflow only.
   * Context-tier rates (qwen3.7-plus above 256k tokens, minimax-m3 above 512k) are kept in
     `pricing.context_tier_rate_per_mtok`.
   * **Invariance.** Every one of the 37 routes seeded before this change keeps a byte-identical configuration hash.
     `tests/unit/test_routing_go_registry_probe.py` pins all 37, now including kimi-k3 and step-5-preview. Its bite
     test is red for a data-terms change of an existing route and green for a descriptive one. `worker_prompt_revision`
     inputs, `policy/*.yaml` and the CONFIG_FIELDS values of seeded routes are unchanged.
2. **One invariant 6 check: `aeos.core.model_policy`.** It is stdlib only, so the ranking script imports it too.
   * `model_family_refusal(id)` returns `claude` for a token starting with `claude` or `anthropic` (`claude-*`,
     `anthropic/*`, `~anthropic/*`, `us.anthropic.claude-*`, `opencode-go/claude-haiku-*`). It returns `meta-router`
     for OpenRouter's routing models (`auto`, `auto-*`, `*router`, `pareto*`, `fusion`, `free`, `bodybuilder`), any
     `*router` model and `~vendor/*` floating aliases, which let the provider pick the build.
   * `invariant6_refusal(*ids, provider=...)` allows Claude only on the native `claude-native` provider.
   * The callers:
     * **The seed:** `aeos seed` refuses with `PolicyDenied` (`MODEL_FAMILY_REFUSED`) before it writes anything.
     * **The OpenRouter catalog discovery:** a refused `:free` id is never registered, and the refresh reports it in
       `refused_model_family`.
     * **Eligibility:** a refused registry row gets a hard rejection reason, so a row that came in by another path never
       serves.
     * **The role ranking:** the generator refuses its own output.
     * **The agent catalog:** `role_agents.catalog_problems` now uses this check instead of `NEVER_PREFERRED`.
   * Near misses stay allowed and are tested: `openrouter/stealth-alpha:free` is a model, not a router, and
     `z-ai/autoglm-*` is allowed too.
3. **Role ranking (`scripts/role_route_ranking.py`, `role_route_preferences.json`, the committed inputs).**
   * The Go tier lists every seeded Go chat-completions route, including Argon and the Qwen models (allowed since
     answer 3; each entry carries the `serving` statement). The superseded MiMo V2.5 pair comes last in the Go tier,
     with an `overflow_only` note.
   * **The served build is scored, not a later build.** The Go DeepSeek route takes the LiveBench 2026-10-14 score of
     the original build (`served_builds`). It does so only in a category where 2026-10-14 is comparable with
     2026-10-25, which means every model both releases score has the same score there. The generator proves this
     from the committed rows (`comparable()`).
     * Coding, Design, Reasoning, Research and Data Analysis are comparable. Long Agentic is not: LiveBench re-ran it
       on 2026-10-25 (kimi-k3.1-pro 49.5 on 10-14, 39.1 on 10-25). Go DeepSeek therefore stays unscored for Long
       Agentic, with that note.
     * This amends ADR-0126's "one release per category" for the served builds only. The 2026-10-14 rows are in the
       committed inputs, with their sha256.
   * **The 1017 score goes to `openrouter/deepseek/deepseek-v4.1-flash-1017`** in a new last tier, `OPENROUTER_PAID`.
     It comes after the free fallback, so a PUBLIC task never prefers it. Its `paid_use` states owner answer 1. A
     separate branch seeds that route; the preference only re-orders routes that are already eligible, so listing its
     key makes nothing eligible.
   * Best first by score is unchanged. Argon leads Long Agentic (61.2), Data Analysis (85.2) and Reasoning (95.1). Go
     DeepSeek leads Coding (90.2) and Design (79.2).
4. **What this change does not do.**
   * It does not seed the paid route, the provider ZDR policy, the escalation counter or the overflow rule. These
     belong to their own changes.
   * It does not change `rank_tier` or ADR-0124 decision 6 ("never a paid route after a skip"). That rule stays in
     force until the overflow change lands, which amends it under answer 1.
   * It spends nothing: a paid route still needs the owner's BUDGET decision.
   * It puts no INTERNAL task on a `:free`, training or non-ZDR route.
   * It routes no Claude model through OpenRouter or Go.

## Consequences

* Every Go allowance the owner pays for can be used once the owner activates and probes the route
  ([OWNER_ACTIONS](../OWNER_ACTIONS.md) 1.11, "OpenCode Go routes of owner decision R34").
* The category agents' preferences change (regenerated file). The agents stay seeded DISABLED, and nothing is
  activated.
* A translation candidate that fails the plain probe gets no TRIAL. The owner disables it, or it stays CANDIDATE, and
  the ranking entry stays inert.

## Evidence and NOT-PROVEN

* **E2, macOS arm64.** Unit runs cover the seed, the hash pins, the invariant 6 bite tests at all five call sites, the
  ranking generator and its comparability bite test, and a mocked tool probe of the seeded Argon route that goes to
  the chat-completions endpoint with the bare id.
* **NOT-PROVEN:**
  * Whether Go's Console translation serves Argon, the Qwen models and MiniMax M3 on chat-completions, with or
    without tools. Only the owner's live probes show it.
  * That Go serves the original DeepSeek build.
  * That models.dev's `tool_call` holds for any new route.
  * The comparability of the v1 overall used to order unscored routes. The v1 `deepseek-v4-pro` row is mapped by its
    exact id, though v1 also lists `deepseek-v4-pro-0813`.
  * Anything on Windows or macOS Intel until CI runs it.
