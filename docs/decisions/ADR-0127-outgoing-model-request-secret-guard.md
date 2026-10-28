# ADR-0127 — Outgoing model-request guard: no credential leaves the host, whatever the route (§25, §26, §50)

* Status: PROPOSED
* Date: 2026-10-28
* Deciders: owner (Fadhlillah) request of 2026-10-28; design by the delegating session. The owner can accept, reject
  or supersede it as an `ADR` decision (see the register README).
* References: [ADR-0041](ADR-0041-model-routing-eligibility-and-identity.md) (routing eligibility, decision C: a free
  OpenRouter WORKER route serves PUBLIC tasks only; the OpenCode Go amendment of 2026-10-26),
  [ADR-0111](ADR-0111-office-code-delivery-and-route-feasibility.md) (feasibility probe),
  [ADR-0121](ADR-0121-code-host-identity-and-no-ai-attribution.md) (outbound text guard, the same refuse-never-strip
  rule). ADR-0041 itself is not edited.

## Context

* The owner, 2026-10-28 (verbatim): "saya juga ingin menggunakan DeepSeek, terkait dengan data training kita bisa
  antisipasi kan, sehingan data training yang akan di olah itu memang tidak ada data sensitive, jadi jangan sampai
  model terbaik di list dan bisa kita gunakan malah kita tidak gunakan, akibatnya pekerjaan jadi tidak maksimal."
  Scope of this ADR: the anticipation, i.e. nothing sensitive may leave the host in a model request, so that the best
  listed routes (DeepSeek, OpenRouter `:free` models whose providers may train on prompts, OpenCode Go) can be used.
* Routing already keeps OFFICE and non-PUBLIC tasks away from such routes (ADR-0041 decision C, invariant 9). That is
  a rule about the *task's* data class. It does not stop a PUBLIC task's worker from reading a key file, a token in a
  log or a private key in a fixture into its conversation; the next model call would carry it to the provider.
* Master §26 and AGENTS.md invariant 5: no secret in model prompts. `aeos.core.redaction` is the one owner of secret
  detection (LESSONS L10/L17); the broker already redacts its own logs and errors with it.

## Decision

1. **Where.** The broker scans every request before it is forwarded upstream: every route and pool (WORKER, the
   evaluation-only ARENA pool, any provider: OpenRouter, OpenCode Zen/Go, Gemini, LOCAL), worker traffic through
   `/broker/v1/chat/completions` (streaming or not) and the owner-triggered feasibility probe alike. The scan comes
   before the grant's request count and before an OpenRouter free-model count, so a refused request costs nothing
   and is never counted as one the provider saw.
2. **What is scanned.** Every string of the forwarded payload, at any depth, keys and values: the contents of
   system, user, assistant and tool messages (also content parts), tool calls and their `arguments` (decoded as JSON
   too, so a JSON escape does not hide a token), tool definitions, stop sequences, response schemas.
3. **What is detected.** `aeos.core.redaction.find_high_confidence_secrets`: the token-shaped patterns (OpenRouter,
   Anthropic and OpenAI-style keys, GitHub tokens, AWS access key ids, Google keys and tokens, Vault/OpenBao tokens and
   labelled unseal/root material, Slack tokens, PEM private key blocks) and every secret value AEOS registered at run
   time (`register_secret`: provider keys it fetched, capability tokens it issued, code-host tokens). It returns the
   detector kind and the span; the patterns are not copied anywhere.
4. **What happens.** Nothing is sent. The caller gets HTTP 403 in the proxy's OpenAI-compatible error shape with
   code `SENSITIVE_CONTENT_BLOCKED` and a message naming the detector kinds only. The broker records a broker request
   with outcome `SENSITIVE_BLOCKED` (route key, no upstream model), an audit record
   `broker.model_proxy.sensitive_blocked` (DENIED; details: route key, kinds, up to 20 findings as detector kind plus a
   SHA-256 of the matched span, the total, `sent: false`), a run event `run.model_call_blocked` shown in the run's
   timeline, `usage_summary.sensitive_blocked` and the run's current action. The worker runtime stops the run at its
   next heartbeat, or at the outcome phase if the worker already exited, as FAILED with outcome code
   `SENSITIVE_CONTENT_BLOCKED` and a readable reason (fail closed; never REVIEW, never DONE). A probe is refused with
   `PolicyDenied` code `SENSITIVE_CONTENT_BLOCKED` and its own DENIED audit record, before its free-model reservation.
5. **No off switch.** No setting, route flag, data class, pool or ZDR statement turns the guard off or narrows it,
   for training-capable or any other route. A later extension (for example to personal data) may only add checks; a
   setting that could narrow the scan is refused by a test.
6. **Linear and bounded.** The scan is linear in the payload (bodies are capped at 8 MB): literal pre-checks before
   patterns that the regex engine cannot search by prefix, the PEM block pattern bounded by the last END marker, and
   the labelled unseal-key pattern written without nested whitespace quantifiers. A 2 MB request is scanned in well
   under 200 ms on the owner's Mac (measured in the test), and adversarial 2 MB inputs that backtracked
   quadratically before (minutes) take well under a second.
7. **Fewer false positives in the owner module.** The short Vault token form (`s.` / `b.` / `hvs.` followed by 24+
   letters and digits) now needs a non-alphanumeric character before it, so code such as
   `this.handleSubmitButtonClickedEvent` or `models.CustomerSubscriptionHistoryRecord` is not a token. The binary scan
   (`UNBOUNDED_TOKEN_PATTERNS`) keeps the unbounded form.

## What is not blocked (residual risk, stated)

* **Personal data** (e-mail addresses, names, phone numbers): code and commits are full of them and blocking would
  stop legitimate work. Stored traces, audit and events are still minimised by `aeos.core.pii`.
* **Generic `key=value` forms** (`password = hunter2`, `Authorization: Bearer <opaque>`): too noisy for code; only
  token shapes and registered values block. A low-entropy secret that AEOS never registered passes.
* **Shapes the owner module does not define yet**: for example an `ASIA` temporary AWS key id or a PEM header without
  its END marker. Adding a shape to `aeos.core.redaction` extends this guard automatically.
* **Encoded or split secrets** (base64, hex, a key spread over two messages, text inside an image): not decoded.
* **The span hash** of a low-entropy registered value could confirm a guess by someone who can read the local audit
  trail; token shapes are high entropy. The hash never leaves the host.
* **Prompt data that is not a secret but is still private** (business data in a PUBLIC task): the task's data class
  and routing eligibility (ADR-0041) remain the control for that.

## Consequences

* Training-capable and non-ZDR routes can serve PUBLIC tasks without the risk that a credential the worker happened to
  read reaches the provider. A run that tries is stopped, and the owner sees why in the run and the audit trail.
* A false positive (a fixture with a real-looking key, a test PEM block) fails the run; the fix is to keep the
  credential-shaped sample out of what the worker reads (assemble it from parts, as this repository's tests do).
* Evidence: E2, unit tests on macOS arm64 (every credential kind refused on a `:free` route, a Go route and a probe,
  in each message role and tool calls; near misses forwarded unchanged; the run stops). NOT-PROVEN on Windows and
  macOS Intel until CI runs, and NOT-PROVEN against live providers (upstreams are mocked).
