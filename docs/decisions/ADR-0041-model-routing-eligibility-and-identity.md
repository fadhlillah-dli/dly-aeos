# ADR-0041 — Model registry, hard-filter routing and AIConfiguration identity

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by the module-group D build (routing)

## Context

Master §23 lists candidate pools (supervisor, initial workers, comparator, arena, identity-unopened,
watchlist, local) and requires two-stage routing: hard eligibility (domain/data/terms/tools/context/
budget/availability) first, then selection from evaluated results; the model never selects itself;
fallback only to already-approved routes; no paid fallback and no supervisor downgrade; OFFICE
`approved_worker_routes=[]`. §43 makes the AIConfiguration (model/version + route + harness + prompt +
tools + policy + context) the unit of evaluation. §50 requires that a changed alias/route is a new
configuration (UNKNOWN, not inheriting the baseline) and that 429/free-quota exhaustion never leads
to a paid fallback.

Catalog facts read on 2026-10-18 (E1): OpenCode Zen model ids and data statements (S07, e.g.
`opencode/deepseek-v4.1-flash-free` and `opencode/glm-5.3-flash-free` zero-retention/no-training,
MiMo/Big Pickle/LL Alpha/Exo "may be used to improve the model", NVIDIA Nemotron "trial only — no
personal or confidential data"); the public OpenRouter catalog (`poolside/laguna-s-2.1:free`,
context 262144, tools, `expiration_date` 2026-10-31; 16 `:free` models in total).

## Decision

1. **Seed** (`aeos.routing.seed`): every §23 pool with exact ids, `mode=DESIGN`, `status=CANDIDATE`,
   `evidence_level=E1`, `resolved_version` empty (UNKNOWN), `eligible_domains=[PERSONAL]` at most,
   data classes from the provider statement (training/trial routes and identity-unopened routes →
   PUBLIC only), `source_refs`. Nothing is approved by seeding; the seed is idempotent.
2. **Identity**: `config_hash = sha256(canonical(identity fields))` over route key, pool, provider,
   endpoint, model id, alias, resolved version, mode params, harness(+version), prompt revision,
   tool set, policy version, context strategy, fallback chain, free flag, pricing, data terms
   (without timestamps), region, domains, data classes, context window, tool support, identity
   status and free-until date. Approvals (Decision kind ROUTE_ACTIVATION or TRIAL) bind to that hash;
   the hash is recomputed from the live record on every check, so an unrecorded edit can never keep an
   old approval effective. A change invalidates related decisions (policy facade), asks the Doctor
   to mark `config:routes` STALE, emits `route.config_changed`, and eval results whose
   `config_hash` differs are ignored.
3. **Hard filters** (all reasons are returned per route): approved/trial decision bound to the
   current hash (human, ACTIVE, unexpired); domain eligible, and for OFFICE additionally
   `office_policy.external_worker_allowed` and `approved_worker_routes` (policy missing → deny);
   data class allowed; identity UNVERIFIED → PUBLIC only; tool support YES when tools are needed;
   known and sufficient context window; catalog availability not MISSING; no unexpired EXHAUSTED
   QuotaState for the provider/scope (route key, model id, alias, `free-daily`, `credits`, `*`);
   pricing/terms checked within 30 days; a route that is not effectively free (paid, UNKNOWN, or
   free period expired) also needs an ACTIVE human BUDGET decision referenced by the activation;
   MOCK/DEMO never routable.
4. **Selection**: eval score for the task role (only results recorded for the current hash), then
   deterministic priority, then key. A TaskSpec route policy may only narrow (allow-list, pool,
   min context, `primary`). The supervisor pool is never routed to workers.
5. **Fallback**: only along the failed/ineligible route's `fallback_chain`, only to routes that are
   themselves eligible, in the same pool and effectively free; otherwise `Blocked`
   (`NO_ELIGIBLE_FALLBACK`/`NO_ELIGIBLE_ROUTE`) so the task backs off/queues. SUPERVISOR →
   `NO_SUPERVISOR_DOWNGRADE`. `note_rate_limited` records observed 429s (reset from Retry-After or
   next UTC midnight for free-daily).
6. **Catalog refresh**: OpenRouter public models update pricing/context/tools/free/expiry for known
   routes (`pricing_checked_at`; the catalog is not a data-terms review, so `terms_checked_at` is not
   touched) and register new `:free` models as DISCOVERED candidates (CANDIDATE, LIVE, E1, PUBLIC only,
   `openrouter/*` authors UNVERIFIED); missing models become `availability=MISSING`. Key info (GET
   `/api/v1/key`, ref `workers/openrouter` then `personal/openrouter`) gives a credits QuotaState;
   remaining free-model requests are not reported by the API, so `free-daily` stays UNKNOWN
   (documented 50/1000 per day per S12) until a 429 is observed. The `label` field is never stored.
   `opencode models` marks OpenCode routes AVAILABLE/MISSING; without the binary: UNKNOWN.
7. **Activation API/CLI**: human admin only; requires the reviewed `expected_config_hash`; paid routes
   require a valid BUDGET decision id; restricted terms forbid widening beyond PUBLIC; OFFICE is never
   activated here; the decision registry missing → `DECISION_REGISTRY_UNAVAILABLE` (fail closed).

## Consequences

* No route is usable until the owner reviews terms and records a decision; the dashboard shows
  BELUM DIUJI / UNKNOWN instead of rankings.
* Seeded OpenCode context windows are UNKNOWN (not published in S07): tasks that set `min_context`
  stay blocked until the window is recorded from a verified source.
* LiteLLM (§07 order 6) is not introduced; the broker remains the only worker-model egress.

## Addendum 2026-10-18 - completeness review

* **CLI decisions need the actor's password**. `aeos routes activate --as <admin>` prompts for that
  SUPER_ADMIN's password (no echo) before it records the ROUTE_ACTIVATION/TRIAL decision. Without
  it, any local process (an agent with a shell included) could name the owner and self-approve
  (human-only AUTH, §19/§20). If the password is wrong or the user is not an active SUPER_ADMIN,
  nothing is recorded.
* **Approval display = authorisation**. Detail views mark a decision `effective` only when it is
  ACTIVE, human, unexpired and bound to the hash of the route's current configuration (recomputed,
  not the stored value). Expired or agent-recorded decisions are labelled "tidak berlaku" and never
  shown as approved.
* **Canonical data-class order**. Activation stores allowed data classes in enum order, so
  re-confirming the reviewed classes keeps the reviewed config hash (and eval results). Narrowing or
  widening them is a real configuration change.
* **Registry mode**. `GET /api/routes` reports `mode=LIVE` only when every listed row is LIVE. A few
  catalog-discovered rows never relabel the seeded DESIGN registry as live runtime data.
* **Last used**. The domain scope is applied in SQL, so a burst of usage from the other domain can
  neither leak through nor hide the visible usage.
* **Not done (gap)**: recording the model actually served by a *worker* route. The supervisor
  resolution is recorded from `modelUsage`. For worker routes, a first observation would change
  `resolved_version`, which is part of the config hash, and so invalidate the approval the route
  was activated under. That needs its own decision: either an "alias observed" field outside the
  identity, or treating the first observation as non-breaking. It also needs a broker/worker-runtime
  reporter. Until then the worker resolved version stays UNKNOWN, as §23 allows.

## Amendment 2026-10-18 (integration)

* **Seed credential references.** Non-OpenRouter OpenAI-compatible routes name their broker credential
  in `mode_params.credential_ref`: OpenCode Zen routes `workers/opencode-zen`, the Gemini comparator
  `workers/gemini`. This is part of the configuration identity; `seed.run` backfills it on untouched
  CANDIDATE seed rows (no approval exists for them, so nothing is invalidated) and recomputes the hash.
* **Credential eligibility.** A remote non-OpenRouter route without `credential_ref`, or with a reference
  outside the namespaces the broker allows for the domain (OFFICE -> `office/`; PERSONAL -> `personal/`,
  `workers/`, own `projects/<id>/`; never `platform/`), is rejected by the hard filter — the route the
  broker would refuse at call time is never selected (`broker.facade.credential_ref_problem`).
* **Not verified (gap):** the exact OpenCode Zen chat-completions path and upstream model-id format for a
  direct broker call (seed endpoint `https://opencode.ai/zen`, model ids `opencode/<model>`) are E1
  catalog facts not exercised against the real service; the E2E test fakes the upstream at whatever URL
  the route defines.
* **Review fix (2026-10-19).** The routing credential filter uses the broker's own loopback test (parsed
  scheme + host, so `http://localhost.example` is not loopback) and rejects an OpenRouter route without
  `credential_ref` for OFFICE (the broker's default OpenRouter references are worker/PERSONAL ones and it
  refuses such a call), so routing never selects a route the broker would refuse.

## Amendment 2026-10-19 (C5: "diizinkan dan telah diuji")

* **APPROVED needs test evidence.** `activate_route(mode=APPROVED)` requires `eval_run_ids`: COMPLETED EvalRuns,
  recorded by a trusted collector on a protected suite, made on exactly the route configuration being approved
  (`EvalRun.route_config_hash`), describing the route (model id/provider), gate-passing and without critical
  regression (`evaluation.facade.activation_evidence`). Otherwise `TEST_EVIDENCE_REQUIRED`; TRIAL (time-limited)
  needs no evaluation. The decision stores the run ids, the per-role pooled result, a config snapshot, the
  previous config hash and a **rollback target** (the most recent earlier approved configuration, else "disable
  the route"). Only this decision writes `ModelRoute.eval_results[role]` (score = Wilson 95 % lower bound, bound to
  the config hash), so selection by evaluation happens only after evaluation **and** an owner decision. A config
  change of a route with a tested role emits `route.needs_evaluation`. The acceptance builder activates its pilot
  route as TRIAL accordingly.
* **Terms re-read at activation.** Seeded worker routes no longer carry `terms_checked_at` (the master research
  reading is kept as `data_terms.read_for_research`); `aeos seed` resets an untouched seeded research date.
  Activation requires `terms_reviewed` whenever the last owner check is missing or older than 30 days
  (`TERMS_REVIEW_REQUIRED`) and records `terms_review` (who/when/classes/note) on the decision.
* **Unknown terms restrict.** A missing/None training flag (or an explicit None trial flag) is UNKNOWN and limits
  the route to PUBLIC — at activation (`DATA_TERMS_RESTRICTED`) and in the hard filter — until the owner records
  their reading (`terms_record`). Catalog discovery applies family terms (NVIDIA: trial only, no confidential data).
* **Region/residency.** A provider that restricts regions (`data_terms.regions` / `unavailable_regions`) needs the
  owner-reviewed `region` at activation (`REGION_REVIEW_REQUIRED` / `REGION_UNAVAILABLE`); the hard filter rejects a
  missing/unavailable region and, for OFFICE, a region outside an `allowed_regions` office policy.
* **Availability UNKNOWN is rejected** (never observed in a provider catalog), as MISSING was.
* **Runtime binding.** Activation binds `policy_version` (hash of `policy/*.yaml`), `prompt_revision` (worker prompt +
  OpenCode permission template), `context_strategy` (TaskSpec file + worktree, attachment budgets, default limits)
  and the observed OpenCode `harness_version`; a change of policy/prompt/context strategy makes an activated route
  ineligible until re-activated, a catalog refresh that observes another OpenCode version is a configuration
  change of activated routes, and the executor refuses a run whose engine differs (`HARNESS_VERSION_DRIFT`).
  Activation fails closed when a binding value cannot be determined (`RUNTIME_BINDING_UNKNOWN`).
* **Reviewed tool set.** The placeholder tool set is replaced by the TaskSpec tool vocabulary; activation records
  the reviewed set (default `fs.read`, `fs.write:allowed_paths`, `tests.run`), and selection rejects a TaskSpec
  tool outside it.
* **Planner seam.** `_task_policy` consumes the compiler's `requires_tools` / `min_context_tokens` (plus the old
  keys); a policy asking to relax eligible-only / paid fallback / supervisor downgrade is refused.
* **Honest status, not fixed here:** a supervisor alias moving to a new model is reported `NEEDS_EVALUATION`, but
  planning continues on it because the trusted acceptance test 1.31 requires that behaviour — blocking it is an
  owner decision. ARENA/COMPARATOR routes can be activated, but the broker serves WORKER routes only (protected
  path), so arena feasibility runs are reported `NOT_WIRED`. No live provider endpoint/terms were exercised.

### Review addendum 2026-10-19 (C5 review)

* **"Lolos evaluasi" (U-07).** APPROVED evidence additionally needs at least one accepted outcome per referenced run
  (`NO_ACCEPTED_OUTCOME`); gate-passing runs with 0 accepted samples did not pass the evaluation. A run planned in
  OFFICE is never evidence for the (PERSONAL-only) activation, so Office results never reach `eval_results`.
* **Harness binding fails closed before a catalog refresh.** The hard filter compares the bound `harness_version`
  with the OpenCode version observable now: a route activated while OpenCode was absent (nothing bound) or bound
  to another version is ineligible until re-activated (the executor's `HARNESS_VERSION_DRIFT` remains the last
  check; an engine that is not observable at all stays the executor's `ENGINE_MISSING`).
* **Availability remedy is honest per provider.** For providers no catalog source observes in this build (e.g.
  Gemini, local) the rejection says so instead of suggesting `aeos routes refresh`.
* **Owner terms record from UI and CLI.** The route view lists `terms_unknown`; the activation dialog and
  `aeos routes activate --inputs-used-for-training/--no-... --trial-only/--no-... --terms-note` record the
  owner's reading (`terms_record`), so UNKNOWN terms can be lifted above PUBLIC only by that recorded review.

## Amendment 2026-10-25 (wave 7, C1): upstream request shape and the S07 client restriction

Status of this amendment: the request-shape correction is ACCEPTED (it only aligns the seed with the provider
documentation). The client-restriction question in "Owner decision" below was **decided by the owner on 2026-10-26:
option A, OpenCode Go** (see "Amendment 2026-10-26 (decision A: OpenCode Go WORKER routes)" at the end).

### Context (E1 documentation reading, 2026-10-25; nothing exercised live)

* S07 (`https://opencode.ai/docs/zen/`, `https://opencode.ai/v2/docs/console/models/`) documents the OpenAI-compatible
  Zen endpoint `https://opencode.ai/inference/openai/v1/chat/completions`, and says direct API calls use the **bare**
  model id (`deepseek-v4.1-flash-free`); `opencode/<model-id>` is the OpenCode **config** format. Some seeded
  watchlist models are documented on other API families: `gpt-5.3-codex`, `grok-build-0.1`, `muse-spark-1.3` on
  `/inference/openai/v1/responses`, `qwen3.8-flash` on `/inference/anthropic/v1/messages`. LongCat 2.5 Preview, Space
  Bunny, LL Alpha and Exo are no longer listed.
* The Gemini API documents its OpenAI-compatible base `https://generativelanguage.googleapis.com/v1beta/openai`
  (`.../chat/completions`).
* The earlier seed sent `https://opencode.ai/zen/chat/completions` with `model: "opencode/<id>"` and
  `https://generativelanguage.googleapis.com/chat/completions`. Both contradict the documentation, so every Zen route
  (the two pilot WORKER routes included) and the comparator would most likely have failed upstream.
* S07 also states: **"Free-tier chat models require OpenCode; use paid models or Go from other clients."** The AEOS
  broker is the only worker-model egress (§25/§26) and calls the upstream as its own HTTP client; OpenCode inside the
  sandbox only talks to the broker. A broker-proxied call to a free Zen model may therefore be refused even with the
  corrected URL. Whether it is refused is NOT-PROVEN (no key stored, no live call).

### Decision (implemented)

1. **Request shape is configuration.** `ModelRoute.model_id` keeps the OpenCode catalog id (`opencode/<id>`, what
   `opencode models` lists for the catalog refresh); `mode_params.upstream_model` holds the bare API id and
   `mode_params.upstream_api` the documented API family. Seeded Zen endpoints: `https://opencode.ai/inference/openai/v1`
   (chat completions and responses) or `https://opencode.ai/inference/anthropic/v1` (messages); Gemini:
   `https://generativelanguage.googleapis.com/v1beta/openai`. All of it is part of the config hash.
2. **The broker builds exactly that request** (`broker.facade.upstream_request_shape`, `model_proxy.request_shape`):
   `<endpoint>/chat/completions` with the bare upstream model id (for an older Zen row without `upstream_model`, the
   catalog id minus `opencode/`). It refuses, before any credential is read: a non-chat-completions API
   (`ROUTE_API_UNSUPPORTED`), a superseded seed endpoint (`ROUTE_ENDPOINT_SUPERSEDED`, remedy `aeos seed`), a
   provider credential towards another host (`opencode-zen` -> `https://opencode.ai` only, `gemini` ->
   `https://generativelanguage.googleapis.com` only; `ROUTE_ENDPOINT`), and an invalid reasoning level
   (`ROUTE_MODE_PARAMS`). Routing applies the same verdict as a hard filter and activation refuses such a route
   (`ROUTE_REQUEST_SHAPE_REFUSED`), so no decision is ever recorded for a request the broker would not send.
3. **Seed backfill, never a silent rewrite of an owner decision.** `aeos seed` corrects untouched SEED CANDIDATE rows
   (endpoint, `upstream_model`/`upstream_api`/`reasoning`, client restriction) and recomputes their hash - a
   CANDIDATE carries no approval, so nothing is invalidated (`request_shape_corrected`). A TRIAL/APPROVED row is not
   rewritten; it is listed in `request_shape_pending` and refused by the broker (`ROUTE_ENDPOINT_SUPERSEDED`). The
   owner's way out: `aeos routes disable <key>` (its decisions are invalidated), then `aeos seed` - a DISABLED seed
   row on a superseded endpoint gets only the request-shape correction and stays DISABLED - then a new activation of
   the corrected configuration. Migration `0007_w7c1_broker_reasoning` changes no route data for the same reason.
4. **Client restriction recorded, fail closed.** Every free Zen route carries `data_terms.client_restriction`
   (statement, source, `applies_to`, `owner_decision: UNDECIDED`). Until the owner decides: activation is refused
   (`CLIENT_TERMS_DECISION_REQUIRED`), routing rejects the route, the broker refuses to proxy or probe it
   (`CLIENT_TERMS_RESTRICTED`, before any upstream call), and the route view / Models page / `aeos routes list` show
   `OWNER_DECISION_REQUIRED`. The restriction is part of the identity, so editing the row to look decided changes
   the hash and voids any older decision.
5. **Worker reasoning level** (§23 "mode/thinking params"): the route states it (`mode_params.reasoning`; seeded
   `PROVIDER_DEFAULT` on worker-runtime routes = the broker sends no level, the provider default applies). A
   worker-sent `reasoning_effort` is never forwarded; `BrokerRequest.reasoning_effort` (sent level or `NOT_SENT`) and
   `reasoning_requested` (the dropped worker value) record it. A stated level (`none|minimal|low|medium|high|xhigh`) is
   sent by the broker and is a new configuration.
6. **Supervisor binding** (§23 "prompt revision, policy version, context strategy"): `observe_supervisor_call` also
   binds the planning prompt revision (declared revision + fingerprint of `planning/prompts.py`), the policy version
   and the planning context-strategy fingerprint. A later change is a new configuration (NEEDS_EVALUATION); because
   it does not touch the bridge, it does not invalidate the bridge capability proof (an engine/effort change still
   does).

### Owner decision (decided 2026-10-26: A, OpenCode Go)

How AEOS should use the free Zen routes given the S07 client rule. Options:

* **A. OpenCode Go** for broker calls (a subscription). **Chosen by the owner on 2026-10-26** for broker-served
  WORKER routes; seeded as CANDIDATE routes by the 2026-10-26 amendment below. Activation, terms, budget and the
  attestations stay the owner's decisions in the app.
* **B. Paid Zen models with a BUDGET decision** (e.g. the chat-completions watchlist routes `kimi-k2.7-code`,
  `minimax-m3`, `mistral-large-4`); the existing paid-route path (BUDGET decision, no paid fallback) applies.
* **C. Another worker route** (e.g. an OpenRouter free route, or a LOCAL loopback model). Correction 2026-10-26: the
  seeded OpenRouter route is ARENA and the local route is LOCAL; only the WORKER pool is broker-served for worker
  tasks (ADR-0106 `SERVED_POOLS`), so neither serves a worker task in this build (only the feasibility probe).
* **D. Proxy the free route anyway**, under the owner's responsibility for the provider terms: activation with an
  explicit client-terms decision (`client_terms` in the API / Models page, `--accept-client-terms
  --client-terms-note` in the CLI). It is written into `data_terms.client_restriction.owner_decision`
  (`OWNER_ACCEPTED_BROKER_CLIENT`) and onto the decision, bound to the configuration hash. A feasibility probe then
  shows (E3) whether the provider accepts the broker call; acceptance stays NOT-PROVEN until then.

The owner chose A. The free Zen routes - including the pilot WORKER routes - keep their client restriction
(`owner_decision: UNDECIDED`) and stay non-activatable for broker proxying unless the owner records D for one of
them; decision A does not decide the Zen rule. AEOS does not pick an option.

### Not done here (honest status)

* No live call: the corrected shape is E1 until a probe records E3 (owner key + activation).
* New free models in the S07 catalog (e.g. `flint-40b-free`) are not seeded; there is no OpenCode catalog discovery
  in this build. (OpenCode Go is seeded by the 2026-10-26 amendment.) LongCat & co. stay seeded; `aeos routes refresh` marks them MISSING when
  `opencode models` no longer lists them.
* EvalRun still cannot execute COMPARATOR/LOCAL routes: the route view states `evaluation_path` NOT_WIRED
  (DESIGN, NOT-PROVEN, §49 M6). ARENA routes are evaluated only through the eval-only path of the W6-141
  amendment (ADR-0111; route view `evaluation_path` EVAL_ONLY, results on real routes NOT-PROVEN).

### Amendment 2026-10-25 (wave 7, W6-008 / W6-114 / W6-120): system-change evidence bound to the change

* **Evidence of exactly this change (§41/§43).** `policy.recording.system_change_evidence` (and the facade, additive
  `change=`) binds every cited EvalRun to the decision's change: `target_hash` equal to the run's
  `configuration_hash` (or `route_config_hash`), or `target_version` equal to the changed AIConfiguration field
  (`catalog.SYSTEM_CHANGE_CONFIG_FIELDS`: policy -> `policy_version`, model/supervisor -> `model_id`/`model_version`,
  route/fallback -> `provider_route`, prompt/instruction -> `prompt_revision`, tool set -> `tools` as a set,
  harness -> `harness_version`, context strategy/retrieval/memory -> a `context_params` value). Hooks, tool schemas,
  skills, schemas and agent definitions bind by the configuration hash only. UNKNOWN never binds. A run of another
  configuration is refused `SYSTEM_CHANGE_EVIDENCE_UNRELATED`; a caller that states no change binds nothing.
* **Supervisor stage-model pin (rollback of §02).** `planning_baseline/stage_models` needs, for every pinned model
  id, a cited COMPLETED all-PASS run whose `model_id`/`model_version` is exactly that id (never a hash alone);
  `planning.configured_stage_models` re-checks this live and ignores a pin whose evaluation is of another model.
  The pin is recorded from the Decisions page ("Pin model supervisor per tahap") or
  `aeos policy decide --kind POLICY --stage-model STAGE=ID --eval-run RUN --rollback-target ...`; other system
  changes use `--target-version/--target-hash --eval-run --rollback-target`.
* **The stated configuration is the executed one (review, wave 7).** An EvalRun whose AIConfiguration states another
  `model_id`/`provider` than its registered route - or a `policy_version`/`prompt_revision`/`harness`/
  `harness_version` other than the one the route is bound to (UNKNOWN states nothing) - never starts
  (`EVAL_CONFIGURATION_MISMATCH`, `evaluation.executor.configuration_mismatches`; the Evaluation page shows the
  reason before a start), so no run can carry a value that never ran into a binding. `context_params` are not
  carried by the route and stay the run's stated configuration. Consequence, stated
  in the Decisions page, the CLI help and every refused pin (`recording.SUPERVISOR_EVAL_PATH_NOTE`): this build has
  **no evaluation path for a supervisor (Claude) model** - EvalRuns execute only on broker-served WORKER/ARENA routes
  and the Claude Max supervisor is never served through the broker (§12) - so a stage-model pin stays refused and
  planning keeps the baseline alias until a supervisor evaluation path exists (it needs owner-authored planning
  evaluation fixtures, ADR-0060).
* **Memory acceptance** binds its evaluation to the record: every collected sample of a cited run must have had
  exactly this record in its executed worker context (`context_memory_ids`, derived by the evaluation collector from
  the hash-checked context pack each attempt used); a run's stated `context_params` alone never bind (review, wave 7).
  The Control Center memory tab accepts the EvalRun ids. Context packs carry ACCEPTED memory only, so no run can
  evidence a proposed record in this build (`recording.MEMORY_EVAL_PATH_NOTE`, shown in the refusal and the memory
  tab): that needs the owner-authored memory/retrieval fixture suite (ADR-0060, ADR-0081). Requiring EVALUATED
  stays an owner decision.
* **Unchanged (owner decision):** planning/review continue on a re-resolved supervisor version (protected
  acceptance 1.31); the change is stated as NEEDS_EVALUATION / `supervisor_unevaluated`.

## Amendment 2026-10-26 (decision A: OpenCode Go WORKER routes)

Status of this amendment: the owner decided option A on 2026-10-26 (OpenCode Go, a $10/month subscription) for
broker-served WORKER routes. This amendment records that decision and implements part 1 of 2: the CANDIDATE routes and
the code paths. Part 2 is not in this amendment. It covers the subscription billing and cost semantics (cost module,
scheduler, feasibility probe). Activating a route, the terms review, the budget and the attestations stay the owner's
decisions in the app. Seeding activates nothing.

### Context (E1 documentation reading, 2026-10-26; nothing exercised live)

Sources, all read on 2026-10-26: the Go docs `https://opencode.ai/v2/docs/console/go/`, the product page and FAQ
`https://opencode.ai/go`, `https://opencode.ai/v2/docs/console/harnesses/`,
`https://opencode.ai/v2/docs/console/api/inference/`, `https://opencode.ai/v2/docs/console/api/`,
`https://opencode.ai/v2/docs/console/billing/`, the terms of service `https://opencode.ai/legal/terms-of-service`,
and models.dev `providers/opencode-go` (`https://github.com/anomalyco/models.dev`, the OpenCode org's model catalog).
These pages belong to the OpenCode Console documentation that S07 cites. The research catalog keeps only the URLs the
master states, so they are cited here and in the seeded `data_terms.sources`, not added to S07.

* **Plan.** Go costs $10/month and Go Plus $40/month. Both are for single-member workspaces only, such as the
  Personal workspace.
* **Endpoint.** Chat completions are at `https://opencode.ai/inference/go/openai/v1/chat/completions` and take the
  bare model id. `opencode-go/<id>` is the OpenCode config format. Other Go models are documented on Anthropic
  messages, OpenAI responses or the Gemini path. The broker does not speak those APIs, so those models are not seeded.
* **Auth.** `Authorization: Bearer <OpenCode Console API key, oc_sk_...>` from the workspace subscribed to Go. The
  documented least privilege is permission Restricted with Models: Request. A revoked key gets 401, and a call outside
  the key's permissions gets 403.
* **Other clients are allowed.**
  * The FAQ says: "Can I use Go with other coding agents? Yes, you can use Go with any agent."
  * The Go docs say: "Your client should: Send typical coding agent traffic; Identify itself with its own user agent
    ... rather than a generic SDK or HTTP-library name; Send a stable session ID in x-opencode-session for each
    conversation".
  * The harnesses page says "Go requires it" about that session ID.
  * Free-tier chat models are different: "other clients receive HTTP 403 ... Use paid models or a Go subscription
    from other clients".
* **Limits.**
  * Each model has a monthly DOLLAR allowance. The windows are 5 hours = 20%, week = 50% and month = 100% of it.
  * Within the limits `usage.cost` is 0. `usage.opencode.allowance` reports the dollars counted, and
    `usage.opencode.billing_source` is `go` or `go-plus`.
  * At a limit the answer is HTTP 429 (`GoUsageLimitError`, `Retry-After`). This comes from OpenCode's own client test
    fixture, not from the docs.
  * The 429 only holds while "Extra Usage" is off. When it is on, Go "will fall back to your pay-as-you-go balance".
    That is a paid fallback on the provider side.
  * No requests-per-minute, tokens-per-minute or concurrency limit is documented. The docs only say "Traffic is
    monitored for abuse".
* **Privacy.**
  * Privacy is a workspace setting. Paid endpoints that train on request data are blocked by default, and the allowed
    regions default to Europe and United States.
  * Every seeded model is listed as not used for training, with 0-day retention.
  * DeepSeek V4.1 Flash and V4 Pro need the Global region. Their zero-retention agreement "is renewed monthly. The
    current agreement is valid through October 31, 2026."
* **Tool calling** is not in the Go docs. models.dev states tool support for glm-5.3-flash, mimo-v2.6-flash,
  minimax-m3.1-flash and kimi-k2.7-code. It states no tool capability for hy3.
* **Terms of service.** Users may not use the Services in a way that "automatically or programmatically extracts data
  or Output", or run "processes that run or are activated while you are not logged into the Services". The Console
  API docs, on the other hand, support scripts and integrations. How the ToS applies to an unattended broker is
  unclear. This is not a legal conclusion.
* **Not confirmed:**
  * a "$5 first month" price, which appears only on third-party mirrors;
  * whether `reasoning_effort` works on Go;
  * whether Extra Usage is off by default;
  * whether the model list needs the key.

### Decision (implemented, part 1 of 2)

1. **Seed** (`routing/seed.py`). A new `_go` builder makes the routes. `_zen` is not reused, because it attaches the
   Zen client restriction and sets tool support to UNKNOWN on paid routes. Every Go route has:
   * provider `opencode-go`, and route key and model id `opencode-go/<id>`;
   * endpoint `https://opencode.ai/inference/go/openai/v1`;
   * `mode_params`: `credential_ref` `workers/opencode-go`, the bare `upstream_model`, `upstream_api`
     `openai.chat_completions` and `reasoning` `PROVIDER_DEFAULT`;
   * PERSONAL only, CANDIDATE/DESIGN/E1, an empty `terms_checked_at` (the reading is in
     `data_terms.read_for_research`) and an empty fallback chain.

   The routes:
   * **WORKER, priority 1-5:** glm-5.3-flash, mimo-v2.6-flash, minimax-m3.1-flash, hy3 and kimi-k2.7-code, with data
     classes PUBLIC and INTERNAL. They rank above the client-restricted Zen pilots (priority 10 and 20). This is pilot
     priority, not a ranking. `supports_tools` is YES as models.dev states, except hy3, which is UNKNOWN. A route
     with UNKNOWN tool support is never eligible for tool-using tasks.
   * **WATCHLIST:** longcat-2.0, glm-5.2, glm-5.3, kimi-k3.1-pro and mimo-v2.6-pro, with PUBLIC and INTERNAL.
   * **WATCHLIST, PUBLIC only:** deepseek-v4.1-flash. Its terms and expiry note record the Global region and the
     2026-10-31 zero-retention date.
   * **Not seeded:**
     * the models on other API families;
     * the models that train on data or keep it for 30 days (Muse Spark Contributor, Grok, GPT Luna, Claude Haiku,
       Beam Free);
     * the remaining chat-completions models (kimi-k3, mimo-v2.5, mimo-v2.5-pro, hy4-preview, step-5-preview,
       deepseek-v4-pro).

     A later reviewed change can add them.

   `data_terms` holds:
   * no training and 0-day retention;
   * `regions`: the workspace setting, Europe and United States by default. A provider that restricts regions needs
     the owner's reviewed region at activation (`REGION_REVIEW_REQUIRED`).
   * `client_permission`: the FAQ quote, with the claim level NOT-PROVEN for the broker. There is no
     `client_restriction`.
   * `provider_paid_fallback`: Extra Usage must stay OFF.
   * `automation_terms`: the ToS clause is an owner risk.
   * the sources.

   `pricing`:
   * `is_free` is NO. The pricing record is `billing` SUBSCRIPTION, `plan` OpenCode Go, `monthly_fee_usd` 10,
     `allowance_usd_month`, `allowance_windows` {5h: 0.2, week: 0.5, month: 1.0}, `allowance_rate_per_mtok` and
     `checked_at`.
   * There are deliberately no `input_per_mtok`/`output_per_mtok` keys, so nothing reads the allowance rate as a
     metered price.
   * `pricing_checked_at` is 2026-10-26.
2. **Broker.**
   * `PROVIDER_ENDPOINT_HOSTS` gains `opencode-go` -> `opencode.ai`, https only. A Go row edited to another host, or
     to plain or loopback http, is refused with `ROUTE_ENDPOINT`.
   * `upstream_model_id` strips `opencode-go/` from a row without `upstream_model`.
   * For provider `opencode-go` the broker sends `User-Agent: dly-aeos-broker/<version>` and
     `x-opencode-session: ses_<first 24 hex of sha256(run id)>`. The session ID is stable per worker run and opaque:
     never the capability token, a key or the run id itself.
   * `Authorization: Bearer` comes from `mode_params.credential_ref`.
   * Everything else is the existing non-OpenRouter path. For example, a 429 becomes RATE_LIMITED with no fallback.
3. **Secrets.**
   * `workers/opencode-go` is in `KNOWN_REFS`, separate from `workers/opencode-zen`, so a stored Zen key never reads
     as Go.
   * The integrations card "OpenCode Go (langganan, rute pekerja)" carries the Extra Usage OFF warning.
   * `workers/` serves PERSONAL worker-model calls only. OFFICE refuses it.
4. **Availability.** The catalog source `opencode_go_catalog` runs in `aeos routes refresh` and
   `POST /api/routes/refresh-catalog`.
   * It sends `GET https://opencode.ai/inference/go/v1/models` with Bearer `workers/opencode-go` (purpose
     `workers:routing.catalog`).
   * A listed id becomes AVAILABLE and an absent id MISSING.
   * Without a stored key nothing is requested, and the Go routes become UNKNOWN. The same holds for 401/403, an
     unreachable endpoint, another status or an unexpected shape: UNKNOWN, never MISSING.
   * `opencode-go` is in `CATALOG_OBSERVED_PROVIDERS`. `opencode models` stays the Zen source only.
5. **Doctor and inventory.**
   * `route.worker_key` is invalidated by `secret:workers/*`, so any worker-model key counts, including
     `workers/opencode-go`. Before, only the OpenRouter keys did.
   * Its remedy names the eligible route's own key.
   * The inventory CONFIG lens maps `opencode-go` to the `opencode-console` component. The research catalog
     component hint is added.

### Consequences

* **Master §23 is extended through this ADR.** Its pool table names the free Zen pilots as the initial workers; this
  amendment adds Go WORKER candidates. The master is not edited (`docs/architecture` is protected). Go is an official
  API-key subscription that the provider documents for other clients. It is not the session/subscription-credential
  routing that §45 excludes (9Router).
* **The ToS automation clause is an owner risk.** It is recorded at activation: the TRIAL needs `terms_reviewed` and
  a terms note. This is not a legal conclusion.
* **Extra Usage must stay OFF.** AEOS cannot observe that account setting. With it ON, the provider silently falls
  back to the paid balance after an allowance (invariant 8, no silent paid fallback). Until part 2 adds an
  attestation preset, the owner step is in OWNER_ACTIONS 1.8/1.11.
* **DeepSeek on Go** needs the Global region, and its zero-retention agreement expires 2026-10-31 unless renewed. It
  stays WATCHLIST and PUBLIC only.
* **Under the current cost semantics a Go route is a paid route:**
  * a BUDGET decision is needed at activation, in routing and on every request; one decision can cover several Go
    routes through `auth_scope.routes`;
  * it is never an automatic fallback;
  * the feasibility probe refuses it (`ROUTE_NOT_FREE`), so its first live evidence is a TRIAL worker run;
  * a TaskSpec needs `max_cost_usd`;
  * when the upstream reports no cost, the reservation is charged.

  Part 2 of decision A changes this: allowance accounting, $0 within the allowance, the account-wide 429 scope and the
  Extra Usage attestation.
* **A later change to a seeded Go row voids its TRIAL.** Changing its pricing, terms or `mode_params` changes its
  config hash. The seed only backfills untouched CANDIDATE rows.
* **ADR-0041 changed again.** An owner ACCEPTED registration of it is STALE until it is recorded again (OWNER_ACTIONS
  2.8).
* **The install manifest is not updated here.** Component `opencode-console` still describes Zen only (cost
  FREE_TIER, no Go `identity_ref`). It is a protected path and is updated through a reviewed change. The egress host
  does not change: `opencode.ai`, through the broker and routing.

### NOT-PROVEN

* **No live call.** The endpoint, model ids, request shape, headers, auth, model-list shape and 429 behaviour are E1
  documentation. The unit tests exercise the AEOS side against respx mocks (E2).
* Whether Go accepts the AEOS broker as a client: typical coding-agent traffic, abuse monitoring.
* Tool calling per models.dev is E1 and not exercised on Go. hy3 is UNKNOWN.
* Whether the model list needs the key, and its exact shape. An OpenAI list is assumed; any other shape is UNKNOWN.
* The owner's subscription, key, region and Extra Usage setting. None of them are observed by AEOS.
