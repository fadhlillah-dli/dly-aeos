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
