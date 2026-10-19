# ADR-0083 — Research & Technology Registry: dated catalog, no guessed identities, manual GitHub upstream checks

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by module group H (research)

## Context

Master §45 keeps every discussed tool/method findable as a dated design catalog — not an inventory of
installed components — and forbids guessed URLs for unresolved identities. §47 defines the research
lifecycle (DISCOVERED → SOURCE_VERIFIED → CONTROLLED_EVAL → CANARY → RECOMMENDED_FOR_PROMOTION, or
REJECTED/DEFERRED; ENABLED only after a legitimate decision and configuration change), allows manual
"Check Upstream" and add-by-URL, and forbids sending Office code/prompts/documents to public research.
§38/§50: "ada update" is not "compatible", and a rename/redirect/fork must not inherit trust.

## Decision

1. `aeos seed` (seeder `aeos.research.seed`) loads one entry per tool/method in every §45 row (multi-tool rows split),
   the §46 practice references, the §47 comparison rows, Qwen3.8-Flash-Next (§23/S60) and all 64
   references S01–S64 with the master's URLs verbatim and the master research date 2026-10-17/18.
   Evidence: E1 for official docs/repos/papers (documented capability), E0 for community/forum.
   Lifecycle: SOURCE_VERIFIED where the master states the canonical repo/docs (identity status
   `STATED_BY_MASTER`, not live-checked), REFERENCE_ONLY for pattern-only items and S-references,
   DISCOVERED otherwise. Unresolved identities (DDorchestrator, codebase-memory-mcp, Goose, Pi,
   OpenHands, Deep Agents, Docker MCP Gateway, Infisical, Langfuse, OpenTelemetry, Promptfoo, Copier,
   SOPS, age, …) are `UNVERIFIED` with **no URL**. Tests compare the catalog against the master text.
   The DLY AEOS entry takes its URL only from the owner's `platform_repo_slug` configuration
   (identity `CONFIGURED`, followed until a live check exists); without it the entry is
   `UNVERIFIED` with no URL (§45 "Repository pengguna belum diberikan"). The `source-reference`
   category is reserved for the master's S01–S64; manual entries cannot pose as references.
2. Entries link to inventory `component_id` only when an existing ComponentReference matches by id
   hint, name or canonical source identity — never invented.
3. **Check Upstream** (admin, manual) supports GitHub-hosted identities only: unauthenticated
   `GET /repos/{owner}/{repo}` plus latest release (fallback: latest tag) with 15 s timeouts and
   manual, host-checked redirects (api.github.com only). It records `checked_at`, latest tag/release,
   archived/fork/license/owner and redirect chain. A different `full_name` or any redirect sets
   identity `CHANGED`, keeps the recorded identity, and resets a trusted lifecycle to DISCOVERED (trust
   not inherited); forks are flagged the same way; 404 → NOT_FOUND; rate limits/errors never update
   `checked_at`. A newer tag than a pinned `source_version` emits an "update detected" notification
   event only; compatibility stays UNKNOWN and nothing is installed, updated or activated.
4. Add-by-URL creates a DISCOVERED entry without any network call; credentials in URLs are rejected.
   Entries added from an OFFICE session are visible only in OFFICE and can never be checked publicly;
   OFFICE sessions cannot run public upstream checks at all. List freshness (`checked_at`) and
   upstream events are computed/emitted only within the entry's domain scope. Routine
   UPSTREAM_CHECK history rows are capped at 50; identity/lifecycle/decision history is never dropped.
5. Entries older than 30 days since the last live check are shown STALE; never-checked entries show
   "NOT_CHECKED" with the master research date.

## Consequences

* Non-GitHub sources (vendor docs) are reviewed manually; the API answers `UPSTREAM_CHECK_NOT_SUPPORTED`.
* Scheduled update checks are out of scope until configured as an opt-in feature (§47).

## Amendment 2026-10-19 (audit gap closure, §39 item 2, §40 source verification, §47, §52)

1. **Owner-recorded fields.** `PATCH /api/research/{id}` (admin) records maintainer, source version
   (pin), data/terms with the date they were read (`terms_checked_at`), compatibility, distribution
   channel and supersession; known limitations only on manual rows (seeded rows keep the master's text).
   Shared rows are never edited from OFFICE; §52 references are read-only. Check Upstream fills an empty
   maintainer from the observed owner of an unchanged identity.
2. **Lifecycle stages are reachable.** `POST /api/research/{id}/confirm-source` moves DISCOVERED to
   SOURCE_VERIFIED only after a live check with status OK and identity CHECKED. A TRIAL decision moves
   linked SOURCE_VERIFIED entries to CONTROLLED_EVAL (CANARY for a canary trial); trial evidence moves
   them to RECOMMENDED_FOR_PROMOTION; ENABLED only with an ACCEPTED outcome after the lawful
   configuration change; a rollback/failure returns the previous stage. Each step is a history event.
3. **Advisories and dependency diff by code.** Check Upstream also reads the repository's published
   security advisories (public); `advisories.summary` evaluates GitHub vulnerable ranges against the
   pin (unparseable = UNKNOWN, a failed read = ERROR, never "clean"). An advisory affecting the
   candidate pin blocks trial/promotion admission. `POST /api/research/{id}/dependency-diff`
   (base/head refs) fetches the manifests/lockfiles at both refs through the public contents API and
   stores a dated diff (licenses of new packages stay UNKNOWN).
4. **Freshness and manual review.** `POST /api/research/{id}/mark-reviewed` records a dated, audited
   manual review of a source that Check Upstream cannot read; freshness uses the later of the live check
   and the manual review (STALE after 30 days). It never changes identity status or lifecycle.
5. **Platform results and notices.** `test_results` receives this platform's trial evidence (E2) and
   outcomes (E3) from proposals; the list shows the latest as "platform result". An update of a pinned
   entry linked to an inventory component produces a concise prefilled UPGRADE draft link
   (`update_notice`, event payload `relevant`/`draft_link`) — never an automatic proposal or install.
6. **Stated limits.** Every listing carries the §47 conclusion ("belum ada bukti DLY AEOS mengungguli
   seluruh metode") and the curation/coverage limit (GitHub Trending not verifiable; not exhaustive;
   popularity is not quality); every §46 practice carries the non-affiliation limitation.
