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
   too, so a JSON escape does not hide a token), tool definitions, stop sequences, response schemas. Every escape
   introducer and escape sequence in a string is a boundary before or after a token, in any of the forms text takes
   on its way into a request: raw, JSON-escaped (once or more), written as C, Python or shell text, or URL-encoded
   (once or more). The rule and its two readings are decision 7; the amendments B and E record how it was reached.
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

   "Glued" and "after a letter" are judged on the guard's boundary views, built by one general rule (amendment E),
   not a list of contexts:
   * **What is an escape.** Every escape introducer and escape sequence, in any form: raw control bytes (ESC, BEL,
     NUL, every other C0 and C1 control character); a backslash escape, the backslash raw or URL-encoded and any run
     of them (JSON `\n`, `\t`, `\r`, `\"`, `\\`, `\/`, `\b`, `\f`, `\uXXXX`, encoded once or several times; C,
     Python and shell `\a`, `\e`, `\v`, `\xNN`, `\NNN` octal, `\0`, `\UXXXXXXXX`, `\u{X}`); an ESC in any form (raw,
     `\u001b`, `\x1b`, `\033`, `\e`, `\u{1b}`, caret `^[`, `%1B`) with the CSI sequence after it (`[` or `%5B`,
     parameter bytes, intermediate bytes, a final byte; with or without parameters; an intermediate raw, URL-encoded
     or backslash-escaped as JSON and source text write a quote, apostrophe or slash: `\"`, `\'`, `\/`, `\x22`), the
     head of an OSC sequence (`]` with its number and `;`) or another escape (`ESC(B`, `ESC7`, `ESC%G`: a `%`
     intermediate, raw or encoded, when no two hex digits follow it); the OSC terminator (BEL, or ESC + backslash) as an
     escape of its own; and any URL-encoded byte (`%XX`, `%25XX`). The OSC text (a hyperlink's URL, a window title) stays
     visible, because a token can sit in it.
   * **Never a token character.** No token pattern holds a backslash, `%` or a control character, so blanking those
     never cuts a token. Only the variable tail of an escape can be ambiguous: a single-letter escape, the digits of
     an octal escape, the parameter, intermediate and final bytes of a CSI sequence, the number of an OSC sequence,
     the byte after an ESC. Read greedily, `\e[` followed by a token takes the token's first letter as the CSI final
     byte (the regression of round 3), `\0` followed by a Google refresh token takes its `1` as an octal digit, and a
     backslash pair followed by a batch token `b.` reads as the JSON escape `\b`; read without the tail, `ESC[31m`
     followed by a Google key leaves the `m` glued to it. (The simpler rule "blank the final byte only when it is not
     alphanumeric" fails the second way: a Google key after `ESC[31m` reads as glued to `m`.)
   * **Two readings, one finding.** The guard scans two views of the text, each the same length as the text, and a
     token found in either is found: the greedy view blanks every escape whole; the cut view blanks each escape only up
     to the first character of its tail that can begin a token (`_TOKEN_START`: the first characters of every
     high-confidence pattern, kept complete by a test over the corpus) after the last encoded byte or backslash of the
     tail (a token holds neither, and a hex digit inside `%3A` never starts one) and keeps the rest. A finding of the cut view
     that a greedy finding overlaps is not reported twice. The cut view is built only when it differs.
   * **Linear.** Each escape is matched from its first character, every alternative that starts at a backslash
     always succeeds, possessively, so no run of escapes is rescanned; a 2 MB text of escapes whose tails all differ
     between the views is scanned in well under a second (measured in the test).
   * **Proof.** `tests/unit/test_redaction_escape_fuzz.py` generates 5,000 escape prefixes from a grammar (the atoms
     above, raw, JSON-escaped once or twice, textual, URL-encoded once or twice over any of those, chained one to
     four, with and without parameters) and places every corpus secret after each, before a generated suffix:
     390,000 cases with a fixed seed, no miss by the guard, and no piece of a secret left by `redact_text` beyond
     origin/main's own misses (the MAIN_MISSES derivation; checked equal to a run of origin/main's patterns over the
     same cases). Four further seeds (1,560,000 cases) were dry too (not committed). Over 239 MB of dependency source
     (22,518 files of the Python and frontend dependencies), the two views report exactly the findings the one greedy
     view of round 3 reported: no new false positive.

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

E. **One general escape rule, generated contexts (review of HEAD 528d93f, 2026-10-28).** Three reviews in a row had
   each found one more escape form that was not a boundary; the third found that round 3's own fix blanked the first
   letter of a token after a bare textual `\e[` (a regression) and that the textual BEL forms (`\a`, `\x07`, `\007`)
   and other `\xNN` / `\NNN` bytes were not boundaries. Decided: no more special cases. Decision 7 now states one rule
   (every escape introducer and sequence, the greedy and the cut reading), and the grammar fuzz is the loop-until-dry
   tool: any future escape form that is not a boundary shows up there as a named miss (kind, variant, grammar of the
   prefix; never a value). The bite tests replay the boundaries of 787626a and 528d93f, the greedy view alone and the
   cut view alone, and each is red on its named contexts. The review also found that no `operator init` sample was
   multi-line, so a list pattern whose separator (or the whitespace after `[`) lost its newline stayed green while keys
   2..n of the real indented output leaked from `redact_text`: the corpus now holds that output indented by 2 and 4,
   with LF and CRLF line ends (both lists, all keys checked), and a bite test narrows each whitespace in memory and is
   red. `redact_text` and its patterns are unchanged (byte-identical to origin/main but for the same-language rewrite
   of the unseal-key label's whitespace, decision 6).

F. **Escaped intermediates and encoded tails (review of HEAD e143cd0, 2026-10-28).** Two forms still left a letter
   glued to a Google key or refresh token that `redact_text` masks: `ESC % G` (ISO 2022, a `%` intermediate the
   grammar excluded because a `%` starts an encoded byte) and a CSI whose quote or slash intermediate is JSON-escaped
   or written as text (`\u001b[0\"q`), whose final byte stayed visible after the backslash escape. And the cut view
   started a URL-encoded CSI tail cut off after its parameters (`%1B%5B1%3A`) at the `A` of `%3A`, so the encoded byte
   stayed glued to the token (a regression of decision 7 against 528d93f). Decided: the intermediate byte is read raw,
   encoded or backslash-escaped, a `%` too unless two hex digits follow it; the cut view starts after the last encoded
   byte or backslash of a tail. The fuzz grammar has the three atoms (`esc_docs`, `csi_escaped_intermediate`,
   `csi_open_params`), and a bite test replays e143cd0's boundary and is red on each, naming the atom. The committed
   seed and four further seeds (1,950,000 cases) have no miss; over 330 MB of dependency source (22,557 files) the
   guard reports exactly what e143cd0 reported. `redact_text` is unchanged.

Evidence: E2, unit tests on macOS arm64 (the differential corpus, the escape contexts through the broker's
`sensitive_content_findings`, the grammar fuzz, the investigation and precedence tests with red fixtures on the first
build).
NOT-PROVEN on Windows and macOS Intel until CI runs.

## What is not blocked (residual risk, stated)

* **Personal data** (e-mail addresses, names, phone numbers): code and commits are full of them and blocking would
  stop legitimate work. Stored traces, audit and events are still minimised by `aeos.core.pii`.
* **Generic `key=value` forms** (`password = hunter2`, `Authorization: Bearer <opaque>`): too noisy for code; only
  token shapes and registered values block. A low-entropy secret that AEOS never registered passes.
* **Shapes the owner module does not define yet**: for example an `ASIA` temporary AWS key id or a PEM header without
  its END marker. Adding a shape to `aeos.core.redaction` extends this guard automatically.
* **Encoded or split secrets**: not decoded or joined. A secret split across two messages, two content parts, two
  strings of a tool call or two requests; a secret broken inside by a line wrap, a zero-width character or an
  inserted escape; a secret encoded as a whole (base64, hex, gzip, an image of text) or character by character (JSON
  `\u0073`-style or HTML `&#115;`-style escapes inside the token, or URL-encoded characters inside its body, such as
  `%2B` in a key that holds `+`). A secret AEOS registered itself is still found when it appears whole.
* **Escape readings neither view resolves** (decision 7), each written in a way the language itself reads
  differently: an octal escape of one or two digits that starts with `1` directly before a Google refresh token
  (`\1` then `1//`: C reads `\11`); a CSI sequence cut off after its parameters, with no final byte of its own,
  whose last parameter is a digit, directly before a token (its first letter is read as the final byte, or it stays
  glued to that digit; a last parameter `;`, `:` or `?`, raw or URL-encoded, is resolved); an `ESC %` sequence whose
  final byte is a hex digit (`ESC % B`) directly before a token that begins with one (`%BA` reads as an encoded
  byte); an OSC text (a title, a URL) with no terminator directly glued to a token (it is text, not an escape).
* **Escape forms outside the grammar**: caret notation other than `^[` (`^G`, `^M`), quoted-printable (`=0A`),
  HTML entities whose last character is a letter or digit glued to the token (a numeric entity ends with `;`, a
  boundary already), language-specific escapes such as `%u001b` or `\N{ESCAPE}`. Adding a form means adding it to the
  grammar and to the fuzz, never a special case.
* **Token lookalike rules** (decision 7): about 0.5% of random 24-character `s.`/`b.`/`r.` token bodies (0.015% at 40
  characters, none at 100; measured on 200,000, 400,000 and 100,000 random bodies) read as an identifier and pass, and
  a Google key or refresh token written directly against a base64 character (for example inside a URL path after a
  literal `/`; an encoded `%2F`, a `\/`, or any escape of decision 7 before it is a boundary) passes, and so does a
  look-behind shape or a label written directly after a letter or digit that is not part of an escape (a word). A token AEOS loaded itself is still
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
