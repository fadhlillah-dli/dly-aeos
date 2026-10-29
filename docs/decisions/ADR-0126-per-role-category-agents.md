# ADR-0126 — Per-role category agents, the role -> agent map and the LiveBench route preference (§15-§17, §19, §23)

* Status: ACCEPTED
* Date: 2026-10-28
* Deciders: owner (Fadhlillah), owner decision R31 of 2026-10-28 and its answers 1-4, recorded by the owner in the
  chat of that day; implemented by the workforce branch of that decision. ACCEPTED is the file status; the owner can
  still record an ADR decision on this file (`aeos policy adr-decide ADR-0126 ...`, see the register README).
* Relates to: [ADR-0041](ADR-0041-model-routing-eligibility-and-identity.md) (routing eligibility, decision C) and
  [ADR-0080](ADR-0080-budget-admission-and-native-usage.md) (budget and subscription allowance). Neither is edited.
  ADR-0124 (best-first selection, which consumes `preferred_routes`) and ADR-0125 (the Go registry and the tool-call
  probe) belong to the same owner decision and are written by their own branches.

## Context

* **Owner decision R31 (2026-10-28), verbatim:** "best models first, the others after; use LiveBench or other
  accountable sources; the best available models run in parallel, matched to each role's needs; applies to all
  usable OpenRouter and OpenCode models". Answers of the same day: (1) PUBLIC tasks use OpenCode Go first and free
  OpenRouter routes as the fallback (ADR-0124); (2) no broker support for Gemini 4 Argon or Qwen 3.8 Max (Go serves
  them on the google and anthropic API families) now; (3) no paid OpenRouter routes; (4) parallel workers are
  phased: `HARD_MAX_ACTIVE` stays 2 in this phase.
* **Owner's words of the same day (R31, R32, R34), verbatim:** "model-model terbaik saat ini yang berjalan,
  selanjutnya model lainnya" (the best models run first, the others after); "saya juga ingin menggunakan DeepSeek"
  (I also want to use DeepSeek); "jangan sampai model terbaik yang bisa kita gunakan malah tidak kita gunakan" (the
  best model we can use must never go unused); and every servable model of the $10 OpenCode Go plan is used.
* **Before this ADR every task ran on `opencode-implementer`, whatever its role** (the scheduler assigned that key at
  enqueue and fell back to it at admission and repair). A role's needs never reached route selection, and a role
  without an agent was silently run by the implementer.
* **LiveBench changed shape.** LiveBench 2.0 (from 2026-10-14; latest release 2026-10-25) has the categories Long
  Agentic, Reasoning, Coding, Browser, Data Analysis, Research and Design. Instruction Following and Language exist
  only in the frozen v1 (last release 2026-06-25). Several categories have few questions (Long Agentic 18, Research
  22, Design 25, Coding 29), so many score differences are inside the noise (the 2026-10-28 review, finding H7).

## Decision

1. **Seven category agents, seeded DISABLED.** `aeos.workforce.catalog.CATEGORY_AGENTS` adds one OpenCode agent per
   category. Each one is the implementer's configuration (harness, tools, limits, pool WORKER, `paid_fallback`
   false, domain and project scope) plus `allowed_scope.roles` and an ordered `route_policy.preferred_routes`. There
   is one worker prompt for all agents: no agent has a prompt, template or permission of its own, so
   `worker_prompt_revision()` is unchanged (a per-agent prompt would make a run differ from the configuration a
   route was evaluated with, §43). `opencode-implementer` itself is not changed (its revision hash and so the
   Doctor fingerprint of `worker.agent_definition` stay the same); it stays the agent of `feature-implementer`.

   | Agent | LiveBench category (release) | Roles |
   |---|---|---|
   | `agentic-coder` | Long Agentic (2.0, 2026-10-25) | backend-api-engineer, frontend-engineer, db-data-engineer, integration-engineer, data-ai-feature-engineer, platform-devsecops-engineer, security-engineer |
   | `coder` | Coding (2.0, 2026-10-25) | qa-test-designer, independent-verifier (amendment below) |
   | `designer` | Design (2.0, 2026-10-25) | ux-ui-designer, diagram-visual-author |
   | `reasoner` | Reasoning (2.0, 2026-10-25) | solution-architect, engineering-delivery-analyst, incident-analyst, sre-operations-assistant |
   | `researcher` | Research (2.0, 2026-10-25) | ux-research-assistant, product-analyst, supply-chain-analyst |
   | `writer` | Instruction Following (v1, 2026-06-25) | technical-writer, privacy-grc-analyst, release-coordinator, retirement-assistant, context-curator |
   | `analyst` | Data Analysis (2.0, 2026-10-25) | model-evaluation-analyst, finops-quota-analyst |

2. **Role -> agent map, no silent default.** An agent serves the roles in `allowed_scope.roles`, else its own
   `role_key` (`catalog.served_roles`). `aeos.workforce.role_agents.resolve` takes the map from the reviewed code
   catalog, so readiness, enqueue, repair and admission agree. Enqueue records the mapped agent in
   `assigned_agent_key`, the lease records the agent that ran, and an earlier assignment (for example an execution
   enqueued before this ADR with `opencode-implementer`) never overrides the map.
   * A role that no OpenCode worker agent serves, a role two agents claim, or a role served by a non-worker agent is
     refused with **`ROLE_UNKNOWN`** and a reason that says so (readiness blocks the task; admission refuses it). It
     is never given to `opencode-implementer`.
   * A mapped agent that is disabled or not seeded makes the task wait with **`AGENT_DISABLED`** naming that agent;
     the owner clears it with `aeos work agent-enable` (or `aeos seed`).
   * A reassign (`POST /api/tasks/{id}/reassign`) accepts only the agent that serves the task's role, which stops
     and restarts the attempt on it. Any other agent is refused with **`AGENT_OUT_OF_SCOPE`** (with `ROLE_UNKNOWN`
     when no worker agent serves the role). It is never accepted and audited, then overridden by the map.
   * `role_agents.catalog_problems` keeps the catalog sound (tested): every served role exists in the 40-role
     catalog, no role has two worker agents, and every executor role has a worker agent unless it is listed below.
     Only OpenCode worker agents count: a non-worker agent of the same role (the deterministic verifier runner) works
     in its own trust context and never runs a worker task, so it is neither the role's worker nor a duplicate.

   **Executor roles left without a worker agent** (`role_agents.UNMAPPED_EXECUTOR_ROLES`): none since the
   amendment of 2026-10-28.

   * **Amendment (2026-10-28): `independent-verifier` -> `coder`.** Verification of code and tests is Coding work,
     so a TaskSpec with role `independent-verifier` runs on the `coder` category agent (it waits with
     `AGENT_DISABLED` until the owner enables `coder`). The first draft of this ADR left the role unmapped and
     refused such a task with `ROLE_UNKNOWN`; the deterministic verifier runner (agent `independent-verifier`,
     harness `verifier-runner`, ADR-0060) is unchanged and still runs the controller's trusted suite in its own
     trust context (§19). **Not yet enforced (follow-up):** that the `coder` route of a verification task is of a
     different model family from the route that implemented the task under verification. Until that check
     exists, a verifier task may run on the same model family as its implementer, so its result is not an
     independent second opinion in the §19 sense.

   The other 15 roles of the catalog are planning, review, human or service roles that never run as worker tasks.

3. **Route preference per agent (shared contract a).** `route_policy.preferred_routes` is an ordered list of route
   keys. It only re-orders routes that are already eligible: it can never make an ARENA or WATCHLIST route, a paid
   route without BUDGET, or a `:free` route for INTERNAL work eligible (ADR-0124 ranks tier -> eval -> preferred
   position -> priority -> key). The list is generated, never hand-edited:
   * Data file: `backend/aeos/workforce/role_route_preferences.json` (not under `policy/`, so `policy_version` is
     unchanged). Every scored entry carries `source_refs` `{url, release, category, score, nq, band, date}`; every
     unscored entry carries `order_evidence` (the LiveBench v1 overall or the monthly allowance that placed it).
   * Generator: `scripts/role_route_ranking.py`, from the committed snapshot
     `docs/evidence/livebench-2026-10-28/role-ranking-inputs.json` (only the LiveBench rows of the mapped models,
     with the SHA-256 of each downloaded file). `tests/unit/test_workforce_role_agents.py` fails when the data file
     is not exactly the generator output for the committed inputs, and the same test runs
     `scripts/role_route_ranking.py --check` (registered in `tests/unit/test_ci_contract.py` `TEST_CALLERS`).
   * Method, best first ("model-model terbaik saat ini yang berjalan, selanjutnya model lainnya"): one LiveBench
     release per category; the scored routes of a tier are ordered by the category score, highest first; an equal
     score goes to the larger monthly allowance, then the key. The noise band `1.96 * 50 / sqrt(nq)` points (Long
     Agentic 23.1, Research 20.9, Design 19.6, Coding 18.2, Data Analysis 8.0, Reasoning 6.9) is kept only as
     metadata (`source_refs[].band`, for later load spreading among near-equal routes); it never re-orders against
     the score. v1 publishes no question count, so 50 is assumed for Instruction Following (band 13.9, NOT-PROVEN).
   * Never omitted ("jangan sampai model terbaik yang bisa kita gunakan malah tidak kita gunakan"): a route without
     a score in the category's release follows the scored routes of its tier, ordered by its best other evidence:
     the LiveBench v1 overall (mean of the v1 category means, release 2026-06-25) where the model has one, highest
     first, then the larger monthly allowance, then the key. The v1 overall only orders unscored routes; it is
     never a category score, so one release per category still holds for scores.
   * Content: every servable OpenCode Go chat-completions WORKER-capable route of the registry (kimi-k3.1-pro,
     mimo-v2.6-pro, minimax-m3.1-flash, kimi-k3, glm-5.3-flash, glm-5.3, glm-5.2, step-5-preview, kimi-k2.7-code,
     mimo-v2.6-flash, longcat-2.0, hy3, deepseek-v4.1-flash) is in every list, first, then the free OpenRouter
     routes whose catalog entry lists tools (PUBLIC only by data class). DeepSeek is listed ("saya juga ingin
     menggunakan DeepSeek"): it is PUBLIC-only by its data class and needs the Global region of the Go workspace,
     so its eligibility and probes decide whether it serves; the preference never excludes it. Never listed, and
     refused by `role_agents.NEVER_PREFERRED` in the catalog check (tested): Gemini 4 Argon and the Qwen family
     (answer 2; not servable by the broker until a separate Go-translation probe proves otherwise), any
     Claude-family model id (AGENTS.md invariant 6) and meta-routers (`openrouter/openrouter/...`).
   * Model identity: LiveBench ids map to routes by exact id; two aliases are stated in the data file
     (`gemma-4-31b` is the `-it` release; `deepseek-v4.1-flash-1017` is the only build of the model in the 2.0
     release, the official 1017 update per research refs S41/S61; that Go serves that build is NOT-PROVEN). The v1
     `deepseek-v4.1-flash-max` is an effort variant and is not mapped. The free `nemotron-3.5-super-vl` route is left
     unscored because LiveBench lists two builds of it (full precision and nvfp4) and the endpoint does not say which
     one it serves.

4. **`HARD_MAX_ACTIVE` is one constant.** It is defined once in `aeos.core.config` (still 2, answer 4; core cannot
   import workforce) and re-exported by `aeos.workforce.limits`. It is the bound of `scheduler.set_concurrency`, the
   `aeos work concurrency` argument and the API body `ConcurrencyIn`, and the per-route caps of ADR-0124
   (`MAX_ROUTE_ACTIVE`, a separate concept: tasks at once on one route or provider) are derived from it rather than
   stated as a second number. Tests refuse a literal bound in any of the three validators and any second numeric
   assignment of `HARD_MAX_ACTIVE` or `MAX_ROUTE_ACTIVE` in `backend/aeos`. Raising it remains an ADR amendment
   with the §44 evidence gate.

5. **What does not change.** No route, route pool, `CONFIG_FIELDS` value, decision, budget or `paid_fallback`
   changes; no file under `policy/`; the worker prompt, `BASE_ALLOW`, `ALWAYS_DENY`, `RUN_MESSAGE`, the OpenCode
   permission template and the context strategy are untouched, so every ACTIVE route TRIAL stays bound (tested:
   `worker_prompt_revision()`, `policy_version` and `context_strategy` are byte-identical before and after seeding
   and enabling the category agents).

## Consequences

* A task whose role has a category agent waits with `AGENT_DISABLED` until the owner enables that agent; until then
  only `feature-implementer` tasks run. This is deliberate: the owner chooses when each category starts.
  `CAN_RUN_WORKER` still requires `opencode-implementer` to be enabled.
* With 18 Long Agentic questions, all scored Go models fall into one noise band. The order is still the score
  (deepseek-v4.1-flash 40.9, kimi-k3.1-pro 39.1, mimo-v2.6-pro 35.2, ... glm-5.3-flash 19.0): the owner asked for the
  best first, and the band only records that the differences inside it are within the noise. An earlier draft of
  this ADR ordered inside a band by the larger allowance, which put glm-5.3-flash (19.0) before kimi-k3.1-pro (39.1);
  that was "biggest allowance first", against the owner's words, and was replaced before merge.
* A route the best-first order puts first may not be eligible (a WATCHLIST route, DeepSeek outside the Global
  region, a `:free` route for INTERNAL work): ADR-0124 then takes the next eligible entry. The list never makes a
  route eligible and never moves a route out of its pool.
* A regenerated ranking changes the category agents' `route_policy`, so `aeos seed` gives them a new revision; their
  enabled state is kept. Enabling never happens by seeding.

## Regenerating the ranking

1. Download the LiveBench release files into an empty directory (`<dir>/<release>/table.csv`, `cost.csv`,
   `categories.json`, and the v1 table as `<dir>/v1.csv` with its categories file as `<dir>/v1cat.json`); a new
   release needs the release constants in the script changed in the same pull request.
2. `python scripts/role_route_ranking.py --snapshot --livebench-dir <dir>`, then `python scripts/role_route_ranking.py`.
3. Review the diff of the snapshot and the data file in a pull request; `python scripts/role_route_ranking.py --check`
   and the unit tests must pass.

## NOT-PROVEN

* Whether a category's LiveBench score predicts this platform's task outcome: no AEOS evaluation per role exists;
  adopted `eval_results` still rank before the preference (ADR-0124).
* The v1 question count (50 assumed), the snapshots and reasoning effort Go serves (LiveBench runs at high effort;
  the deepseek-v4.1-flash-1017 build in particular), and tool calling on Go routes (models.dev declares it; the probe
  is ADR-0125; hy3 has no declared tool capability).
* That an `independent-verifier` task runs on another model family than its implementer: not enforced (amendment
  of decision 2, follow-up).
* Admission and routing with the category agents on a real host (unit tests on macOS arm64 only, E2); Windows and
  macOS Intel run them only in CI.

## Amendment 2026-10-28 (owner decision R34, ADR-0129)

* Gemini 4 Argon and the Qwen models are listed again (owner answer 3 of R34: probe them through Go's chat-completions
  translation). Until their probes pass, their eligibility keeps them from serving, not this list.
  `role_agents.NEVER_PREFERRED` is replaced by the one invariant 6 check `aeos.core.model_policy`, which refuses
  Claude-family models and meta-routers in the catalog check and in the generator.
* One release per category gains one exception, `served_builds`. A route takes the release that scored the build it
  serves (Go deepseek-v4.1-flash: release 2026-10-14), and only in a category where that release is comparable. The
  1017 score goes to the paid `openrouter/deepseek/deepseek-v4.1-flash-1017`, in the last tier, `OPENROUTER_PAID`.
