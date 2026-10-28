# ADR-0127 — Outgoing model-request guard: no credential leaves the host, whatever the route (§25, §26, §50)

* Status: ACCEPTED (owner request R32, 2026-10-28)
* Date: 2026-10-28
* Deciders: owner (Fadhlillah) request of 2026-10-28, accepted as owner request R32 the same day; design by the
  delegating session. The owner can still supersede it as an `ADR` decision (see the register README).
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
   too, so a JSON escape does not hide a token), tool definitions, stop sequences, response schemas. An escape
   sequence in a string (a JSON escape such as `\n`, `\t`, `\"` or `\u00e9`, also JSON encoded twice, a terminal
   colour code such as `ESC[31m`, raw, JSON-escaped or written as text (`\x1b[31m`, `\033[31m`, `\e[31m`), the
   OSC-8 hyperlink terminator ESC + backslash in the same three forms, and any URL-encoded byte such as `%3D`, `%2F`,
   `%40` or `%5C`) is a boundary before or after a token (amendment, decision B).
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
   `SENSITIVE_CONTENT_BLOCKED` and a readable reason (fail closed; never REVIEW, never DONE). The refusal decides the
   outcome even when another stop came later (amendment, decision D); an investigation run blocks its task instead of
   moving to the next hypothesis (amendment, decision C). A probe is refused with
   `PolicyDenied` code `SENSITIVE_CONTENT_BLOCKED` and its own DENIED audit record, before its free-model reservation.
5. **No off switch.** No setting, route flag, data class, pool or ZDR statement turns the guard off or narrows it,
   for training-capable or any other route. A later extension (for example to personal data) may only add checks; a
   setting that could narrow the scan is refused by a test.
6. **Linear and bounded.** The scan is linear in the payload (bodies are capped at 8 MB): literal pre-checks before
   patterns that the regex engine cannot search by prefix, the PEM block pattern bounded by the last END marker, and
   the labelled unseal-key pattern written without nested whitespace quantifiers. A 2 MB request is scanned in well
   under 200 ms on the owner's Mac (measured in the test), and adversarial 2 MB inputs that backtracked
   quadratically before (minutes) take well under a second.
7. **Fewer false positives in the guard, never weaker redaction.** The patterns of `aeos.core.redaction` are the
   ones log and error redaction, the repository secret scan and the Claude prompt guard use; this ADR does not narrow
   them (amendment, decision A). Only the high-confidence scan (this guard) skips, after review of the first build:
   * a short Vault/OpenBao form `s.` / `b.` / `r.` whose body reads as an identifier, i.e. splits into camelCase or
     snake_case words of mean length 3.4 or more (Go receivers and single-letter variables: `s.`,
     `b.` or `r.` followed by `registerServiceWithHealthCheck`, `ReportAllocsAndResetTimerNow`,
     `findAllByOrganizationIdentifier`, and a JavaScript `this` or `lib` followed by a member name). All
     3,909 long member names measured in the frontend and Python dependencies read as identifiers; a random token body
     falls apart into one- and two-character pieces, glued to a word or not. `hvs.` keeps matching whatever follows;
   * a Google refresh token `1//...` with an identifier body (`total1//itemsPerPage...`, floor division);
   * a Google API key or refresh token glued to a base64 character before or after it: inside an image data URI or a
     lockfile `sha512-` integrity hash, `AIza` and `1//` occur by chance (a 1 MB image gave several findings).

   "Glued" and "after a letter" are judged on the guard's boundary view (decision B), in which every escape sequence
   is blanked with as many spaces: a JSON escape, a URL-encoded byte, a terminal colour code (raw, JSON-escaped, or
   written as text as `\x1b[31m`, `\033[31m` or `\e[31m`) and the OSC-8 terminator ESC + backslash (raw, JSON-escaped
   or as text). The terminator's backslashes are blanked, never the letter after them, so the `b` of a batch token
   `b.` or the `r` of a recovery token `r.` right after a hyperlink is no longer read as the JSON escape `\b` or `\r`
   and blanked (review of 2026-10-28). A token after any of these escapes is therefore neither glued to base64 nor
   behind a word; the code near misses (Go receivers and member names after a textual colour code or inside an OSC-8
   link) still read as identifiers and pass.

   Redaction of logs and errors keeps masking the wider shapes; only the decision to refuse a request is narrowed.

## Amendment 2026-10-28: differential review before merge

A blind differential review (origin/main `aeos/core/redaction.py` against the first build, 28 contexts) found that
the first build had weakened log redaction and left gaps in the guard. Decided and built before merge:

A. **Redaction is never weakened for the guard.** The first build added a look-behind to the short Vault pattern
   `(?:hvs|s|b)\.`, so `redact_text`, `contains_secret`, the repository secret scan and the Claude prompt guard stopped
   catching a token glued to a letter or digit, for example a Vault token after a JSON-escaped newline in a worker log
   (143 cases origin/main caught). The pattern is restored exactly. The identifier exception lives only in the
   guard's high-confidence scan, only for `s.`, `b.` and `r.` with an identifier-shaped body, never for `hvs.`.
   `tests/unit/test_redaction_never_weaker.py` (LESSONS L25) is the permanent differential: a corpus of every pattern
   origin/main had, in plain, glued, JSON-escaped, URL-encoded, colour-coded, data URI, JSON string and multi-line
   contexts. Each sample is assembled at run time from every character its pattern's class allows, with long bodies
   (L26: the file holds no literal secret shape). A pattern change that drops a case, or masks only part of a secret
   (any 8-character piece of any part survives, as when a character class is narrowed), is red with the pattern and
   the context named, never the value. A secret of several parts is checked part by part: every key of an
   `operator init` JSON list (three keys, so the repeated group of that pattern is exercised twice) and every line of a
   PEM body; bite tests narrow only the repeated group of the list pattern (its class loses `+`, or the repeat is
   bounded to one) and mask only the first PEM body line, and each is red while the first key or line stays masked.
B. **Escapes are boundaries for the guard.** A token after `\n`, `\t`, `\"`, `\u00e9` (also JSON encoded twice),
   after a terminal colour code (raw, JSON-escaped, or written as text: `\x1b[31m`, `\033[31m`, `\e[31m`), after the
   OSC-8 hyperlink terminator ESC + backslash (raw, JSON-escaped or as text; its backslashes only, never the token's
   first letter) or after any URL-encoded byte (`%3D`, `%2F`, `%40`, `%2B`, `%5C`, also encoded twice) is found: the guard scans a same-length view in which each escape sequence is replaced
   by spaces, so the span still names the token in the request. Before, `AIza` and `1//` after an escaped newline or
   `ESC[31m` read as glued to the base64 character `n` or `m`. A backslash run is matched from its first backslash
   only and possessively (the terminator's backslashes in pairs, also possessively), so the view stays linear (a
   20,000-backslash run took 1.3 s in the first build; 2 MB runs of textual escapes and terminators are measured in the
   test). Code identifiers, base64
   image data (also URL-encoded) and lockfile `sha512-` hashes stay unflagged, also JSON-encoded (the near misses of
   the test).
C. **Investigation runs.** `SENSITIVE_CONTENT_BLOCKED` is a recognised stop reason of an investigation run: the run
   keeps that outcome code (not `EXIT_NONZERO`), the hypothesis is not counted as investigated, and the next
   hypothesis is not queued (the same content would be refused again). The task goes BLOCKED with reason code
   `SENSITIVE_CONTENT_BLOCKED` and a readable reason; only an owner retry clears it, never a tick.
D. **Precedence.** A refused request decides the run's outcome even when a later stop (quota exhausted, controller
   shutdown, lost lease, time limit) was set by the monitor: the run fails with `SENSITIVE_CONTENT_BLOCKED` and its
   attempt is never refunded as a quota stop's would be. When the lease was lost and the reconciler already moved the
   task (BLOCKED, `LEASE_LOST_PROCESS_ALIVE`), the task keeps that state and the run still records the code (the
   first build raised a conflict on every retry and left the run RUNNING with an unknown outcome).

Evidence: E2, unit tests on macOS arm64 (the differential corpus, the escape contexts through the broker's
`sensitive_content_findings`, the investigation and precedence tests with red fixtures on the first build).
NOT-PROVEN on Windows and macOS Intel until CI runs.

## What is not blocked (residual risk, stated)

* **Personal data** (e-mail addresses, names, phone numbers): code and commits are full of them and blocking would
  stop legitimate work. Stored traces, audit and events are still minimised by `aeos.core.pii`.
* **Generic `key=value` forms** (`password = hunter2`, `Authorization: Bearer <opaque>`): too noisy for code; only
  token shapes and registered values block. A low-entropy secret that AEOS never registered passes.
* **Shapes the owner module does not define yet**: for example an `ASIA` temporary AWS key id or a PEM header without
  its END marker. Adding a shape to `aeos.core.redaction` extends this guard automatically.
* **Encoded or split secrets** (base64, hex, a key spread over two messages, text inside an image): not decoded.
* **Token lookalike rules** (decision 7): about 0.5% of random 24-character `s.`/`b.`/`r.` token bodies (0.015% at 40
  characters, none at 100; measured on 200,000, 400,000 and 100,000 random bodies) read as an identifier and pass, and
  a Google key or refresh token written directly against a base64 character (for example inside a URL path after a
  literal `/`; an encoded `%2F`, a colour code in any of its forms or an OSC-8 terminator before it is a boundary)
  passes. A token AEOS loaded itself is still
  caught as a registered value whatever its shape.
* **Log redaction of glued tokens (unchanged from before this ADR)**: `redact_text` misses a look-behind shape (`sk-`,
  `hv[sbr].`, `r.`, the unseal-key label) written directly after a letter, digit or escape, and masks a glued `hvs.`
  or `hvb.` token only up to its first `_` or `-` (the short pattern), leaving the rest readable in a log. The
  never-weaker test records these as origin/main's misses; the outgoing guard finds them after an escape.
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
