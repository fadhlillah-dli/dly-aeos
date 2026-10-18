# ADR-0053 — Capability broker: run-bound tokens, model proxy and git/PR broker (§25, §26, §31, §42)

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah)

## Context

§26: workers use limited capabilities through a broker; provider keys never sit in a sandbox. §31:
only a trusted broker writes allowed branches and creates PRs; the boundary must be tested against
direct push, refspec tricks, merge API, deletion, force push and credential escape. §42/§50: on free
quota exhaustion/429 there is no paid fallback.

## Decision

1. Capability grants are bound to one run, project, domain, kind (MODEL_PROXY | GIT_BRANCH |
   PR_CREATE | READ_CONTEXT), scope, caps and expiry. The raw `aeoscap_` token is returned once,
   only its SHA-256 is stored, and it is registered for redaction. Counters are updated with
   conditional UPDATEs so concurrent requests cannot pass a cap.
2. `/broker/v1/chat/completions` and `/broker/v1/models` (OpenAI-compatible) accept ONLY
   `Authorization: Bearer <capability token>`; a request carrying the dashboard session cookie is
   rejected. The worker names a model from its scope; the broker maps it to the route's exact
   upstream model id, strips fields that could make the upstream choose another model
   (`models`, `route`, `provider`, ...), forces `n=1`, clamps `max_tokens`, and enforces
   request/token/cost caps (a free route gets a cost cap of 0). Upstream keys come from the
   secrets facade (`workers/openrouter`, then `personal/openrouter` for PERSONAL; other providers
   via `ModelRoute.mode_params.credential_ref`; loopback endpoints may run without a key).
3. Upstream 429 is returned to the worker as 429 + `Retry-After`; a QuotaState row is recorded and
   no other route is tried. Because OpenCode retries 429 indefinitely (verified on 1.19.3), the
   executor stops the run after repeated 429s (`QUOTA_EXHAUSTED`, task BLOCKED until the quota
   window passes). Cap exhaustion returns a non-retryable 403 so the worker exits.
4. Usage (prompt/completion tokens; cost only when the upstream reports it, otherwise UNKNOWN)
   goes to `aeos.cost.facade.record_usage` and to the grant counters; each call is a
   `BrokerRequest` row (no secrets).
5. Git broker: only `refs/heads/aeos/X:refs/heads/aeos/X` (built by the broker), never the default/
   protected branches, force, deletion, tags or multiple refspecs; remote URL built from the repo
   full name (https, no userinfo). The candidate is fetched into a fresh broker-private bare
   mirror and pushed from there with hooks disabled; the credential is passed to that single git
   invocation as `GIT_CONFIG_COUNT`/`http.<url>.extraHeader` env (never a file, never argv).
   PRs are created as drafts via `POST /repos/{o}/{r}/pulls`; timeouts and "already exists" are
   reconciled with a GET before any retry; side effects are recorded in an outbox table.
6. Every remote write needs an active GIT_BRANCH/PR_CREATE grant for the run and the policy
   external-write interlock; a missing interlock fails closed. The broker contains no operation
   that integrates a PR into a branch; a test greps the package to keep it that way.

## Consequences

* A cancelled run's grants are revoked first, so its worker immediately loses model access.
* Cost is UNKNOWN for upstreams that do not report it; the dashboard shows UNKNOWN, never $0.
* GitHub branch protection on Free private repositories may be unavailable (§31); the broker's
  restrictions are the tested boundary, and merge stays a human action outside AEOS.
