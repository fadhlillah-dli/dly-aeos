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

## Amendment 2026-10-18 (integration)

1. **Sha-pinned push.** The push refspec is `<runtime head sha>:refs/heads/aeos/X` (never `+`, never a
   deletion, never another destination; `validate_refspec` accepts a full 40/64-hex sha source or the
   same-name form, nothing else). `broker.facade.push_task_branch` gained the additive keyword
   `expected_head_sha` (must equal the runtime head, else `HEAD_MISMATCH`) and ALWAYS returns `head_sha`
   with `status` PUSHED | FAILED | UNKNOWN. FAILED/UNKNOWN are returned, not raised.
2. **Branch-update hook.** After a successful push the broker calls
   `aeos.delivery.sync.on_branch_updated(session, repo=, branch=, head_sha=, actor="broker")`, so open PR
   records of the branch re-observe the head (acceptance STALE when it moved).
3. **Interlock binding.** The broker passes the ACTIVE human APPROVAL decision of the run's exact plan
   version + hash that allows the operation (`find_active_decision(..., operation=)`) and the grant
   expiry to `check_external_write`. Before, `decision_id=None` made every real interlock refuse.
4. **Usage and quota through the cost module.** Each model call is one ledger entry recorded with the
   run's `reservation_id` and `partial=True` (the hold stays open until the scheduler settles it); a
   MOCK/DEMO run records with its mode and its hold is released, never committed. Observed 429s are
   written through `aeos.cost.facade.set_quota_state` (source `observed-429`, resets from Retry-After);
   the broker no longer writes `QuotaState` rows directly. A ledger refusal never breaks the worker's
   response: it is recorded on the BrokerRequest (`LEDGER_REJECTED:<code>`).
5. **Credential namespaces (§26).** `mode_params.credential_ref` must be a valid reference in a namespace
   allowed for the domain: OFFICE -> `office/` only; PERSONAL -> `personal/`, `workers/` or the run's own
   `projects/<project_id>/`; `platform/` never. Additive facade `credential_ref_problem(domain, ref, *,
   project_id=None)`; routing eligibility uses the same rule (ADR-0041 amendment).
6. **Secret purposes.** The broker uses the secrets contract's `<namespace>:<use>` purposes
   (`workers:broker.model-proxy`, `personal:broker.github`, `projects:<id>:broker.model-proxy`); the
   previous free-text purposes were refused by the real OpenBao/keyring providers (`SECRET_SCOPE_DENIED`).
7. **Review fix (2026-10-19).** A refused branch-update observation (delivery hook) is audited and
   reported (`pr_observation: OBSERVATION_FAILED (<code>)`), never raised over a push that already landed:
   the SUCCEEDED outbox row must persist with the caller's commit. The E2E now pushes through the real
   `https://github.com/...` remote path (push interlock + owner token through the secrets contract); only
   the git transfer is redirected into a local bare repository.
