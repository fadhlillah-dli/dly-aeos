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
* **Memory acceptance** binds its evaluation to the record (a `context_params` value naming the record id or its
  content hash); the Control Center memory tab accepts the EvalRun ids. Requiring EVALUATED stays an owner decision.
* **Unchanged (owner decision):** planning/review continue on a re-resolved supervisor version (protected
  acceptance 1.31); the change is stated as NEEDS_EVALUATION / `supervisor_unevaluated`.
