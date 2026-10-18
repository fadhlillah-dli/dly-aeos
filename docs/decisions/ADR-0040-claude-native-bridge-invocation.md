# ADR-0040 — ClaudeNativeBridge invocation, output contract and model-policy check

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by the module-group D build (claude_bridge)

## Context

Master §12 requires planning/review through the unmodified official Claude Code binary with the
owner's native Max login — never the Claude API, never `--bare`, never from a worker/repository
directory, with ANTHROPIC_API_KEY and other provider switches removed, and a capability test on
the CLI version actually in use. §42 forbids converting Claude tokens into dollars or into a
"remaining Max quota"; §50 requires that an exhausted Fable/Max quota makes reviews wait.

Observed on the owner's Windows 11 machine on 2026-10-18 (Claude Code 2.1.302, winget install):

* `claude auth status` (scrubbed env) prints JSON
  `{loggedIn, authMethod: "claude.ai", apiProvider: "firstParty", email, orgId, orgName, subscriptionType: "max", ...}`.
* `claude --help` lists `--restricted`, `--tools`, `--strict-mcp-config`, `--permission-prompts host|none`,
  `--no-session-persistence`, `--json-schema`, `--effort`, `--fallback-model`, `--bare` (bypasses OAuth/keychain).
* Three real tiny calls were made (budget: at most four) with
  `-p --output-format json --json-schema {"ok": boolean} --model <m> --restricted --tools ... --strict-mcp-config --permission-prompts none --no-session-persistence`,
  prompt on stdin, from a fresh temp directory, with an allow-listed environment:
  1. `--model opus` → `type=result`, `subtype=success`, `is_error=false`, `api_error_status=null`,
     `result="{\"ok\":true}"`, `structured_output={"ok": true}`, `session_id`, `duration_ms`,
     `usage{input_tokens, output_tokens, cache_*}`, `modelUsage{"claude-opus-5-5": {inputTokens, outputTokens,
     contextWindow: 1000000, canonicalModel, provider: "firstParty", costBasis: "list", costUSD}}`,
     `total_cost_usd`, `terminal_reason=completed`, `safety_stops=0`, `permission_denials=[]`.
  2. `--model fable` → success, but `modelUsage` had two entries: `claude-fable-5-1` with **0 output tokens**
     and `claude-opus-4-8` with 221 output tokens; `safety_stops=2`. The answer was produced by a model
     outside the requested family.
  3. The live capability test (`tests/integration/test_claude_bridge_live.py`, opt-in) through the
     implemented bridge → VERIFIED, resolved `claude-opus-5-*`.

## Decision

1. **Invocation** (`aeos.claude_bridge.bridge`): the exact argv above, `--tools` from a read-only
   allow-list (Read/Grep/Glob, or none), optional `--effort`; prompt on stdin (Windows command-line
   limit); schema compact JSON as one argument (≤16k chars). `assert_safe_argv` refuses `--bare`,
   `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`, `--fallback-model`,
   `--settings`, `--mcp-config`, `--permission-mode`, `--add-dir`, resume/continue, cloud/remote flags,
   and requires `-p --restricted --strict-mcp-config --no-session-persistence --permission-prompts none
   --output-format json`. On Windows only a real `claude.exe` is accepted (npm `.cmd` shims re-parse
   arguments through cmd.exe).
2. **Environment**: explicit allow-list (PATH/SYSTEMROOT/WINDIR/COMSPEC/USERPROFILE/HOME/APPDATA/
   LOCALAPPDATA/TEMP/TMP/LANG/LC_*/TERM + macOS/Linux equivalents, `CLAUDE_CONFIG_DIR`, CA paths,
   credential-free proxies), then a deny check for `ANTHROPIC_*`, `CLAUDE_CODE_*`, `CLAUDE_AGENT_SDK_*`,
   `CLAUDECODE`, `AWS_*`, `GOOGLE_APPLICATION_CREDENTIALS`, `OPENROUTER_*`, `OPENAI_*`, `AEOS_*`, vault and
   GitHub tokens. The controller itself may run inside a Claude Code session; nothing leaks.
3. **Workspace**: a fresh `<data>/supervisor/<request-id>` per request; refused when it overlaps the
   platform repo, a domain/project/task-data directory or any git repository. Context files are copied
   read-only and removed after the run; a redacted `trace.json` (prompt hash, argv with schema hash,
   output without cost fields) stays and is returned as `trace_ref`. Prompts/context containing a
   registered secret or a token-shaped credential are never sent (`SECRET_IN_PROMPT`).
4. **Auth gate**: `loggedIn=true`, `authMethod=claude.ai`, `apiProvider=firstParty`, else BLOCKED
   (`BRIDGE_BLOCKED`, callers write the manual package). `subscriptionType != max` → DEGRADED.
5. **Model policy**: the resolved model is the `modelUsage` entry that produced the most output
   tokens. Alias requests must resolve inside the family; pinned ids must match exactly; any other
   output-producing model outside the family fails the check (small auxiliary `haiku` output is
   tolerated). Missing `modelUsage` → UNKNOWN → fail. Result: `ok=False`, `MODEL_POLICY_MISMATCH`.
   The observed fable→opus-4-8 answer is therefore rejected, not accepted as a Fable review.
6. **Quota**: usage-limit/429/"limit reached"/"resets" signals → `quota_exhausted=True`,
   `QUOTA_EXHAUSTED`, a `QuotaState(provider=claude-native, scope=<family>, EXHAUSTED)` observation and
   an event; no retry, no other model. Otherwise native quota is shown as **UNKNOWN**.
7. **Cost**: usage is recorded through `aeos.cost.facade.record_usage(category=NATIVE_CLAUDE,
   cost_usd=None, is_estimate=False)`. `total_cost_usd`/`costUSD` (list-price notionals,
   `costBasis=list`) are dropped everywhere.
8. **Resolution observation**: a policy-conforming result updates the SUPERVISOR route's
   `resolved_version`; a later different resolution is a new configuration (ADR-0041).
9. **Manual fallback** (`aeos.claude_bridge.manual`): PROMPT.md, compact schema.json, read-only
   context/, INSTRUCTIONS.md with Windows PowerShell 5.1 (quote-escaping + UTF-8 `$OutputEncoding`),
   PowerShell 7 and bash commands plus the interactive alternative; `result.json` accepted as raw
   client JSON (full policy check) or a bare object (identity UNVERIFIED); prompt/schema hashes detect
   tampering; BOM/UTF-16 results from PowerShell redirection are decoded.
10. Live tests that spend Max quota are opt-in (`DLY_AEOS_LIVE_CLAUDE=1`); the binary/auth probe runs
    whenever the binary exists (no model call).

## Consequences

* Fable reviews can currently fail with MODEL_POLICY_MISMATCH when the client answers through
  another model (one sample; generalisation NOT-PROVEN). Switching the supervisor needs an approved
  policy change — never a silent downgrade.
* Disabling paid "extra usage" is an account setting the CLI does not report: shown as UNKNOWN with a
  manual instruction. Whether `--restricted` also suppresses user-level CLAUDE.md memory is not
  documented in `--help` and is NOT-PROVEN.
* A Claude Code update changes the binary fingerprint/version: the last capability test turns STALE
  and `doctor.invalidate("binary:claude")` is requested.
