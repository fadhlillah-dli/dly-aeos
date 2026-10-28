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
   | `coder` | Coding (2.0, 2026-10-25) | qa-test-designer |
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
     catalog, no role has two agents, and every executor role has a worker agent unless it is listed below.

   **Executor roles left without a worker agent** (`role_agents.UNMAPPED_EXECUTOR_ROLES`):

   * `independent-verifier`: the deterministic verifier runner (agent `independent-verifier`, harness
     `verifier-runner`, ADR-0060), not an LLM worker, and a separate trust context from the implementer (§19). A
     TaskSpec with this role is refused with `ROLE_UNKNOWN` instead of being run by the implementer as before.
     Mapping it to an LLM worker (the review suggested a route of another model family than the implementer's) is a
     new question for the owner, not part of this decision.

   The other 15 roles of the catalog are planning, review, human or service roles that never run as worker tasks.

3. **Route preference per agent (shared contract a).** `route_policy.preferred_routes` is an ordered list of route
   keys. It only re-orders routes that are already eligible: it can never make an ARENA or WATCHLIST route, a paid
   route without BUDGET, or a `:free` route for INTERNAL work eligible (ADR-0124 ranks tier -> eval -> preferred
   position -> priority -> key). The list is generated, never hand-edited:
   * Data file: `backend/aeos/workforce/role_route_preferences.json` (not under `policy/`, so `policy_version` is
     unchanged). Every scored entry carries `source_refs` `{url, release, category, score, nq, date}`.
   * Generator: `scripts/role_route_ranking.py`, from the committed snapshot
     `docs/evidence/livebench-2026-10-28/role-ranking-inputs.json` (only the LiveBench rows of the mapped models,
     with the SHA-256 of each downloaded file). `tests/unit/test_workforce_role_agents.py` fails when the data file
     is not exactly the generator output for the committed inputs.
   * Method: one LiveBench release per category; CI-aware buckets with the noise band `1.96 * 50 / sqrt(nq)` points
     (Long Agentic 23.1, Research 20.9, Design 19.6, Coding 18.2, Data Analysis 8.0, Reasoning 6.9); inside a bucket
     the larger monthly allowance first, then the higher score, then the key; unscored routes after the scored ones.
     v1 publishes no question count, so 50 is assumed for Instruction Following (band 13.9, NOT-PROVEN).
   * Content: the nine servable OpenCode Go chat-completions routes (kimi-k3.1-pro, mimo-v2.6-pro,
     minimax-m3.1-flash, kimi-k3, glm-5.3-flash, glm-5.3, glm-5.2, step-5-preview, kimi-k2.7-code) first, then the
     free OpenRouter routes whose catalog entry lists tools (PUBLIC only by data class). Gemini 4 Argon, Qwen 3.8 Max
     and DeepSeek are never listed (answer 2; DeepSeek is PUBLIC-only), and a test refuses them.
   * Model identity: LiveBench ids map to routes by exact id; one alias is stated in the data file
     (`gemma-4-31b` is the `-it` release). The free `nemotron-3.5-super-vl` route is left unscored because LiveBench
     lists two builds of it (full precision and nvfp4) and the endpoint does not say which one it serves.

4. **`HARD_MAX_ACTIVE` is one constant.** It moves to `aeos.workforce.limits` (still 2, answer 4) and is the bound
   of `scheduler.set_concurrency`, the `aeos work concurrency` argument and the API body `ConcurrencyIn`. A test
   refuses a literal bound in any of the three. Raising it remains an ADR amendment with the §44 evidence gate.

5. **What does not change.** No route, route pool, `CONFIG_FIELDS` value, decision, budget or `paid_fallback`
   changes; no file under `policy/`; the worker prompt, `BASE_ALLOW`, `ALWAYS_DENY`, `RUN_MESSAGE`, the OpenCode
   permission template and the context strategy are untouched, so every ACTIVE route TRIAL stays bound (tested:
   `worker_prompt_revision()`, `policy_version` and `context_strategy` are byte-identical before and after seeding
   and enabling the category agents).

## Consequences

* A task whose role has a category agent waits with `AGENT_DISABLED` until the owner enables that agent; until then
  only `feature-implementer` tasks run. This is deliberate: the owner chooses when each category starts.
  `CAN_RUN_WORKER` still requires `opencode-implementer` to be enabled.
* With 18 Long Agentic questions, all six scored Go models fall into one bucket, so the allowance decides the order
  (glm-5.3-flash, then minimax-m3.1-flash, then the $15 models by score). This is the honest reading of the
  evidence, not a claim that the smaller model is better.
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
* The v1 question count (50 assumed), the snapshots and reasoning effort Go serves (LiveBench runs at high effort),
  and tool calling on Go routes (models.dev declares it; the probe is ADR-0125).
* Admission and routing with the category agents on a real host (unit tests on macOS arm64 only, E2); Windows and
  macOS Intel run them only in CI.
