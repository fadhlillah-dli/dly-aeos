"""ClaudeNativeBridge implementation (§12, §42, §08) — official binary, native Max login only.

Invocation (verified against Claude Code 2.1.302, ADR-0040)::

    claude -p --output-format json --json-schema <schema> --model <model> --restricted
           --tools Read,Grep,Glob --strict-mcp-config --permission-prompts none
           --no-session-persistence [--effort <level>]          (prompt on stdin)

* never ``--bare`` (it bypasses the subscription login and requires an API key/provider),
  never ``--dangerously-skip-permissions``/``--allow-dangerously-skip-permissions``, never
  ``--fallback-model`` (no silent supervisor downgrade), never ``--settings``/``--mcp-config``;
* the child environment is an allow-list (``aeos.claude_bridge.env``);
* every request runs in a fresh supervisor directory (``aeos.claude_bridge.workspace``);
* a failure to prove the bridge returns ``ok=False``; callers fall back to the manual package
  (``aeos.claude_bridge.manual``) — there is no Claude API path anywhere in AEOS.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.claude_bridge import binary as binmod
from aeos.claude_bridge.env import assert_clean, child_env, parent_hygiene
from aeos.claude_bridge.facade import ClaudeRequest, ClaudeResult
from aeos.claude_bridge.parsing import (
    ALLOWED_REQUEST_FAMILIES,
    cli_schema,
    family_of,
    is_pinned_id,
    parse_output,
    redact_strings,
    validate_schema,
)
from aeos.claude_bridge.process import run_process
from aeos.claude_bridge.workspace import (
    WorkspaceError,
    cleanup_context,
    copy_context,
    high_confidence_secret_hits,
    prepare_workspace,
    sanitized_argv,
    write_trace,
)
from aeos.core.config import Settings, get_settings
from aeos.core.db import session_scope
from aeos.core.enums import Domain
from aeos.core.hashing import canonical_json, content_hash, sha256_text
from aeos.core.ids import new_id
from aeos.core.redaction import redact_text
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.models.claude_bridge_ext import ClaudeCapabilityTest
from aeos.models.cost import QuotaState

log = logging.getLogger("aeos.claude_bridge")

READ_ONLY_TOOLS = frozenset({"Read", "Grep", "Glob"})
EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})
FORBIDDEN_FLAGS = frozenset({
    "--bare", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--fallback-model",
    "--settings", "--mcp-config", "--plugin-dir", "--plugin-url", "--resume", "--continue", "-c", "-r",
    "--permission-mode", "--add-dir", "--agents", "--system-prompt", "--cloud", "--remote-control",
})
REQUIRED_FLAGS = ("-p", "--restricted", "--strict-mcp-config", "--no-session-persistence")
# Windows CreateProcess limits the whole command line to 32,767 characters *after* quoting (every '"' of the
# JSON schema becomes '\"'). The planning contracts (plan_content/synthesis, ~19k chars compact, ~23k quoted)
# fit; the limit is checked on the real quoted command line with a margin for the binary path. A schema that
# does not fit is first sent without validation-neutral annotations (description/title/$comment/examples);
# the caller still validates the output against its full schema.
MAX_CMDLINE_CHARS = 32_000
MAX_SCHEMA_ARG_CHARS = MAX_CMDLINE_CHARS  # kept for compatibility; the quoted command line is what counts
_SCHEMA_ANNOTATIONS = frozenset({"description", "title", "$comment", "examples"})
# JSON Schema keywords whose value maps *names* (property names, definition names) to subschemas: the keys are
# data, never annotations - a property called "title" or "description" must survive the stripping.
_SCHEMA_NAME_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"})
_SCHEMA_LISTS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_SCHEMA_SINGLE = frozenset({"items", "additionalProperties", "additionalItems", "not", "if", "then", "else",
                            "contains", "propertyNames", "unevaluatedItems", "unevaluatedProperties"})


def _strip_annotations(schema: Any) -> Any:
    """Drop validation-neutral annotations from *schema* (a JSON Schema), walking only subschema positions.

    Names under ``properties``/``$defs``/... and literal values (``enum``, ``const``, ``default``) are kept
    exactly, so the stripped schema accepts and rejects the same documents as the original."""
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k in _SCHEMA_ANNOTATIONS:
            continue
        if k in _SCHEMA_NAME_MAPS and isinstance(v, dict):
            out[k] = {name: _strip_annotations(sub) for name, sub in v.items()}
        elif k in _SCHEMA_LISTS and isinstance(v, list):
            out[k] = [_strip_annotations(sub) for sub in v]
        elif k in _SCHEMA_SINGLE:
            out[k] = [_strip_annotations(sub) for sub in v] if isinstance(v, list) else _strip_annotations(v)
        else:
            out[k] = v  # keywords with literal values (type, required, enum, const, default, pattern, ...)
    return out


def cmdline_length(argv: list[str]) -> int:
    """Length of the command line as Windows sees it (list2cmdline quoting); a safe bound on POSIX too."""
    import subprocess

    return len(subprocess.list2cmdline(argv))


MAX_COMMAND_LINE_CHARS = MAX_CMDLINE_CHARS


def command_line_chars(argv: list[str]) -> int:
    """Length of the command line as Windows receives it (``subprocess.list2cmdline`` quoting).
    Measured on every OS so the same request is accepted or refused everywhere."""
    import subprocess

    return len(subprocess.list2cmdline(argv))
QUOTA_PROVIDER = "claude-native"
CAPABILITY_TEST_SCHEMA: dict[str, Any] = {
    "type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False,
}
CAPABILITY_TEST_PROMPT = (
    'This is an automated capability probe from DLY AEOS. Do not use any tools. '
    'Reply only with the structured result {"ok": true}.'
)
CAPABILITY_TEST_MAX_AGE = timedelta(days=7)
# Purposes that may run without a domain: the doctor probe sends a fixed prompt and no project data.
DOMAINLESS_PURPOSE_PREFIXES = ("doctor.",)

# DESIGN default for display; the planning module's PlanVersion policy baseline is authoritative.
SUPERVISOR_POLICY: dict[str, Any] = {
    "allowed_families": ["opus", "fable"],
    "stages": {
        "planning.primary": "opus", "planning.challenge": "fable", "planning.synthesis": "opus",
        "planning.final": "fable", "review.checkpoint": "fable",
    },
    "fallback": "NONE - queue waits; switching supervisor needs an approved policy change",
    "mode": "DESIGN",
}


def supervisor_route_key(model: str) -> str | None:
    fam = family_of(model)
    return f"claude-native/{fam}" if fam else None


def build_argv(binary_path: str, *, schema_json: str, model: str, tools: list[str], effort: str | None) -> list[str]:
    argv = [
        binary_path, "-p", "--output-format", "json", "--json-schema", schema_json, "--model", model,
        "--restricted", "--tools", ",".join(tools), "--strict-mcp-config", "--permission-prompts", "none",
        "--no-session-persistence",
    ]
    if effort:
        argv += ["--effort", effort]
    return argv


def assert_safe_argv(argv: list[str]) -> None:
    """Refuse any invocation that could leave the native-login, read-only, no-fallback envelope."""
    flags = [a for a in argv[1:] if a.startswith("-")]
    bad = sorted({a.split("=", 1)[0] for a in flags} & FORBIDDEN_FLAGS)
    if bad:
        raise ValueError(f"forbidden Claude flags: {bad}")
    if any("bypassPermissions" in a for a in argv):
        raise ValueError("bypassPermissions is forbidden")
    missing = [f for f in REQUIRED_FLAGS if f not in argv]
    if missing:
        raise ValueError(f"required Claude flags missing: {missing}")
    try:
        i = argv.index("--permission-prompts")
    except ValueError as exc:
        raise ValueError("--permission-prompts none is required") from exc
    if i + 1 >= len(argv) or argv[i + 1] != "none":
        raise ValueError("--permission-prompts none is required")
    if "--output-format" not in argv or argv[argv.index("--output-format") + 1] != "json":
        raise ValueError("--output-format json is required")


NON_MAX_TARGET = ("claude_subscription", "non-max")


def non_max_subscription_exception() -> bool:
    """True only when an ACTIVE owner EXCEPTION decision accepts a non-Max login as the supervisor."""
    try:
        from aeos.core.db import session_scope
        from aeos.policy import facade as policy

        with session_scope() as s:
            return policy.find_active_decision(s, kind="EXCEPTION", target_type=NON_MAX_TARGET[0],
                                               target_id=NON_MAX_TARGET[1]) is not None
    except Exception:  # no database / policy module: no exception can be proven -> fail closed
        return False


def _fail(request: ClaudeRequest, code: str, error: str, *, engine_version: str | None = None,
          trace_ref: str | None = None, quota: bool = False) -> ClaudeResult:
    return ClaudeResult(
        ok=False, structured=None, text="", model_requested=request.model, model_resolved=None,
        engine_version=engine_version, session_id=None, usage={}, duration_ms=None, error_code=code,
        error=redact_text(error)[:1000], quota_exhausted=quota, trace_ref=trace_ref, mode="NATIVE",
    )


def validate_request(request: ClaudeRequest) -> tuple[str, str] | None:
    """Return (code, message) when the request must not be sent at all."""
    model = (request.model or "").strip()
    fam = family_of(model)
    if fam not in ALLOWED_REQUEST_FAMILIES or not (model.lower() in ALLOWED_REQUEST_FAMILIES or is_pinned_id(model)):
        return "REQUEST_INVALID", f"model {model!r} is not an allowed supervisor alias/pinned id (opus/fable/sonnet)"
    bad_tools = sorted(set(request.allowed_tools) - READ_ONLY_TOOLS)
    if bad_tools:
        return "TOOL_NOT_ALLOWED", f"supervisor runs are read-only; tools not allowed: {bad_tools}"
    if request.effort and request.effort not in EFFORT_LEVELS:
        return "REQUEST_INVALID", f"effort must be one of {sorted(EFFORT_LEVELS)}"
    if request.domain is not None and request.domain not in {d.value for d in Domain}:
        return "REQUEST_INVALID", "domain must be PERSONAL or OFFICE"
    if request.project_id and not request.domain:
        return "DOMAIN_REQUIRED", "project-scoped Claude requests need the explicit human-selected domain (U-05)"
    if not request.domain and not str(request.purpose or "").startswith(DOMAINLESS_PURPOSE_PREFIXES):
        # §50 'Submit prompt tanpa domain': refused before any model call. Only the doctor's
        # capability probe (fixed prompt, no project data) runs without a domain.
        return "DOMAIN_REQUIRED", (f"purpose {request.purpose!r} needs the explicit human-selected domain "
                                   "(PERSONAL/OFFICE); only doctor.* probes run without one (U-05)")
    if not (1 <= int(request.timeout_seconds) <= 3600):
        return "REQUEST_INVALID", "timeout_seconds must be within 1..3600"
    try:
        validate_schema(request.json_schema)
    except Exception as exc:  # jsonschema.SchemaError and malformed input
        return "SCHEMA_INVALID", f"json_schema is not a valid JSON Schema: {getattr(exc, 'message', exc)}"
    if high_confidence_secret_hits(request.prompt):
        return "SECRET_IN_PROMPT", "prompt contains a credential; it is never sent to a model (U-13)"
    return None


def project_domain_problem(request: ClaudeRequest) -> tuple[str, str] | None:
    """U-05 defence in depth: a project-scoped request must carry the project's own immutable domain.

    A caller (or upstream bug) passing ``domain=PERSONAL`` for an OFFICE project would otherwise skip
    the OFFICE policy check below; an unregistered project cannot prove its domain -> refused."""
    if not request.project_id:
        return None
    try:
        from aeos.models.portfolio import Project

        with session_scope() as s:
            project = s.get(Project, request.project_id)
            domain = project.domain if project is not None else None
    except Exception as exc:  # fail closed
        return "DOMAIN_UNVERIFIED", f"project domain could not be read ({type(exc).__name__})"
    if domain is None:
        return "PROJECT_NOT_FOUND", "project is not registered; its domain cannot be verified"
    if domain != request.domain:
        return "DOMAIN_MISMATCH", "request domain differs from the project's immutable domain (U-05)"
    return None


def preflight(request: ClaudeRequest) -> tuple[str, str] | None:
    """Everything that must hold before any prompt leaves the machine (bridge and manual package)."""
    problem = validate_request(request)
    if problem:
        return problem
    if request.domain == Domain.OFFICE.value:
        allowed, why = office_claude_allowed()
        if not allowed:
            return "OFFICE_POLICY_DENIED", why
    return project_domain_problem(request)


def office_claude_allowed() -> tuple[bool, str]:
    """§23/§51: OFFICE data goes to Claude Max only after a recorded company authorisation."""
    try:
        from aeos.policy.facade import office_policy

        with session_scope() as s:
            pol = office_policy(s)
    except (ImportError, ModuleNotFoundError):
        return False, "office policy module unavailable - OFFICE planning/review is denied (fail closed)"
    except Exception as exc:
        return False, f"office policy could not be evaluated ({type(exc).__name__}) - denied"
    if pol.get("claude_max_allowed_for_office_data") is True:
        return True, ""
    return False, "OFFICE policy: Claude Max is not authorised for office data (claude_max_allowed_for_office_data=false)"


def run_structured(request: ClaudeRequest, *, settings: Settings | None = None) -> ClaudeResult:
    settings = settings or get_settings()
    problem = preflight(request)
    if problem:
        return _fail(request, *problem)
    schema_json = json.dumps(cli_schema(request.json_schema), separators=(",", ":"), ensure_ascii=False)
    probe_argv = build_argv("claude", schema_json=schema_json, model=request.model,
                            tools=list(request.allowed_tools), effort=request.effort)
    if cmdline_length(probe_argv) > MAX_CMDLINE_CHARS - 512:  # 512: room for the absolute binary path
        schema_json = json.dumps(_strip_annotations(cli_schema(request.json_schema)), separators=(",", ":"),
                                 ensure_ascii=False)
        probe_argv = build_argv("claude", schema_json=schema_json, model=request.model,
                                tools=list(request.allowed_tools), effort=request.effort)
        if cmdline_length(probe_argv) > MAX_CMDLINE_CHARS - 512:
            return _fail(request, "SCHEMA_TOO_LARGE", f"schema does not fit the command line "
                                                      f"({cmdline_length(probe_argv)} > {MAX_CMDLINE_CHARS - 512})")

    pr = binmod.probe(settings)
    if not pr.usable or pr.binary.path is None:
        return _fail(request, "BRIDGE_BLOCKED",
                     "Claude native bridge is BLOCKED: " + "; ".join(pr.reasons or ["unknown"]) +
                     " - use the manual package (official Claude Code) instead; no API fallback exists",
                     engine_version=pr.binary.version)
    # §12 unmodified official binary: an OS-reported INVALID signature or a signer other than the owner-pinned
    # publisher refuses the native call (fail closed); an unsigned binary is reported DEGRADED in the status.
    gate, gate_reason = binmod.signature_gate(binmod.invocation_signature(pr.binary.path, pr.binary.fingerprint),
                                              binmod.approved_publisher())
    if gate == "BLOCKED":
        return _fail(request, "BINARY_SIGNATURE_BLOCKED",
                     f"Claude native bridge is BLOCKED: {gate_reason} - use the manual package (official Claude "
                     "Code) instead; no API fallback exists", engine_version=pr.binary.version)
    if pr.auth.status == "DEGRADED" and not non_max_subscription_exception():
        # U-06: the supervisor is Claude Max. A Pro/other login is not silently used as the supervisor (the
        # doctor reports SUBSCRIPTION_NOT_MAX too); the manual package stays available.
        return _fail(request, "SUBSCRIPTION_NOT_MAX",
                     f"Claude login subscription is {pr.auth.subscription_type!r}, not Max; the native supervisor "
                     "is not used - run the manual package, or record an owner EXCEPTION (target "
                     f"{NON_MAX_TARGET[0]}/{NON_MAX_TARGET[1]}) to accept it", engine_version=pr.binary.version)
    cmdline = command_line_chars(build_argv(str(pr.binary.path), schema_json=schema_json, model=request.model,
                                            tools=list(request.allowed_tools), effort=request.effort))
    if cmdline > MAX_COMMAND_LINE_CHARS:
        return _fail(request, "SCHEMA_TOO_LARGE",
                     f"the command line would be {cmdline} chars after quoting (> {MAX_COMMAND_LINE_CHARS}; "
                     "Windows limit 32767)", engine_version=pr.binary.version)

    request_id = new_id()
    try:
        ws = prepare_workspace(settings, request_id)
        context_manifest = copy_context(ws, list(request.context_files), layout=settings.layout,
                                        domain=request.domain)
    except WorkspaceError as exc:
        return _fail(request, exc.code, str(exc), engine_version=pr.binary.version)

    argv = build_argv(str(pr.binary.path), schema_json=schema_json, model=request.model,
                      tools=list(request.allowed_tools), effort=request.effort)
    env = child_env()
    try:
        assert_safe_argv(argv)
        assert_clean(env)
    except (ValueError, RuntimeError) as exc:
        cleanup_context(ws)
        return _fail(request, "ARGV_UNSAFE", str(exc), engine_version=pr.binary.version)

    started_at = utcnow()
    trace: dict[str, Any] = {
        "request_id": request_id, "purpose": request.purpose, "role": request.role,
        "project_id": request.project_id, "domain": request.domain, "model_requested": request.model,
        "engine_version": pr.binary.version, "binary_fingerprint": pr.binary.fingerprint,
        "auth": {"status": pr.auth.status, "authMethod": pr.auth.auth_method, "apiProvider": pr.auth.api_provider,
                 "subscriptionType": pr.auth.subscription_type},
        "prompt_sha256": sha256_text(request.prompt), "prompt_chars": len(request.prompt),
        "schema_hash": content_hash(request.json_schema), "argv": sanitized_argv(argv[1:], schema_json),
        "context_files": context_manifest, "workspace": f"supervisor/{request_id}",
        "env_names": sorted(env), "started_at": started_at.isoformat(), "timeout_seconds": request.timeout_seconds,
    }
    try:
        proc = run_process(argv, stdin_text=request.prompt, cwd=ws, env=env, timeout=float(request.timeout_seconds))
    except OSError as exc:
        cleanup_context(ws)
        trace.update({"error": f"exec failed: {type(exc).__name__}"})
        ref = write_trace(ws, settings.layout, trace)
        return _fail(request, "EXEC_FAILED", f"could not start the official binary: {type(exc).__name__}",
                     engine_version=pr.binary.version, trace_ref=ref)
    cleanup_context(ws)

    if proc.timed_out:
        trace.update({"timed_out": True, "killed_pids": proc.killed_pids, "duration_ms": proc.duration_ms,
                      "stderr": redact_text(proc.stderr)[-2000:]})
        ref = write_trace(ws, settings.layout, trace)
        return _fail(request, "TIMEOUT", f"Claude run exceeded {request.timeout_seconds}s; process tree killed",
                     engine_version=pr.binary.version, trace_ref=ref)

    parsed = parse_output(proc.stdout, proc.stderr, returncode=proc.returncode, schema=request.json_schema,
                          model_requested=request.model)
    raw = dict(parsed.raw)
    raw.pop("total_cost_usd", None)
    if isinstance(raw.get("modelUsage"), dict):
        raw["modelUsage"] = {k: {kk: vv for kk, vv in (v or {}).items() if kk != "costUSD"}
                             for k, v in raw["modelUsage"].items() if isinstance(v, dict)}
    trace.update({
        "exit_code": proc.returncode, "duration_ms": proc.duration_ms, "output": raw,
        "stderr": redact_text(proc.stderr)[-2000:], "ok": parsed.ok, "error_code": parsed.error_code,
        "model_resolved": parsed.model_resolved, "quota_exhausted": parsed.quota_exhausted,
        "finished_at": utcnow().isoformat(),
    })
    ref = write_trace(ws, settings.layout, trace)
    result = ClaudeResult(
        ok=parsed.ok, structured=parsed.structured, text=parsed.text, model_requested=request.model,
        model_resolved=parsed.model_resolved, engine_version=pr.binary.version, session_id=parsed.session_id,
        usage=parsed.usage, duration_ms=parsed.duration_ms if parsed.duration_ms is not None else proc.duration_ms,
        error_code=parsed.error_code, error=parsed.error, quota_exhausted=parsed.quota_exhausted, trace_ref=ref,
        mode="NATIVE",
    )
    _record_side_effects(request, result, parsed.quota_resets_at, parsed.quota_reset_text)
    return result


def _record_side_effects(request: ClaudeRequest, result: ClaudeResult, resets_at: Any, reset_text: str | None) -> None:
    """Usage ledger, quota observation and resolved-model observation — best effort.

    Runs in its own short transaction. A failure (e.g. a caller holding a long write lock) is
    logged and flagged on ``result.usage['ledger_recorded']``; it never changes ``ok``.
    """
    recorded = False
    try:
        with session_scope() as s:
            recorded = _record_usage(s, request, result)
            if result.quota_exhausted:
                record_quota_exhausted(s, request.model, resets_at=resets_at, reset_text=reset_text,
                                       message=result.error)
            elif result.ok:
                clear_quota_exhausted(s, request.model)
            if result.ok and result.model_resolved and result.model_resolved != "UNKNOWN":
                _observe_resolution(s, request.model, result.model_resolved)
    except Exception:  # pragma: no cover - logged; never break the caller's flow
        log.exception("claude bridge: recording side effects failed")
    result.usage["ledger_recorded"] = recorded


def _record_usage(session: Session, request: ClaudeRequest, result: ClaudeResult) -> bool:
    try:
        from aeos.cost.facade import record_usage
    except (ImportError, ModuleNotFoundError):
        return False
    usage = result.usage or {}
    try:
        record_usage(
            session, provider=QUOTA_PROVIDER, category="NATIVE_CLAUDE", source="claude-bridge",
            project_id=request.project_id, domain=request.domain, route_key=supervisor_route_key(request.model),
            model=result.model_resolved, input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"), requests=1,
            cost_usd=None,  # subscription: never converted to dollars (§42)
            is_estimate=False, mode="LIVE",
        )
    except (ImportError, ModuleNotFoundError):
        return False
    return True


def _set_quota(session: Session, *, scope: str, state: str, source: str, details: dict[str, Any],
               resets_at: Any) -> QuotaState:
    """Native quota observations go through ``aeos.cost.facade.set_quota_state`` (the one QuotaState
    authority). Only when the cost module is absent is the observation written directly (still recorded,
    so the review queue keeps waiting — fail closed)."""
    try:
        from aeos.cost.facade import set_quota_state

        return set_quota_state(session, provider=QUOTA_PROVIDER, state=state, source=source, scope=scope,
                               details=details, resets_at=resets_at)
    except (ImportError, ModuleNotFoundError):
        pass
    row = session.scalars(select(QuotaState).where(QuotaState.provider == QUOTA_PROVIDER,
                                                   QuotaState.scope == scope)).first()
    if row is None:
        row = QuotaState(provider=QUOTA_PROVIDER, scope=scope)
        session.add(row)
    row.state, row.source, row.observed_at, row.resets_at, row.details = state, source, utcnow(), resets_at, details
    session.flush()
    return row


def record_quota_exhausted(session: Session, model: str, *, resets_at: Any = None, reset_text: str | None = None,
                           message: str = "") -> QuotaState:
    """§42/§50 'Fable Max quota habis': observed limit -> EXHAUSTED; callers checkpoint and wait.

    Recorded through the cost module's quota contract (one QuotaState authority, ADR-0080 §6): an observed
    limit message may set EXHAUSTED, never OK/LOW."""
    scope = family_of(model) or "max"
    details = {"reset_text": reset_text, "message": redact_text(message)[:300],
               "note": "observed from the official client output; remaining quota is not published"}
    row = _set_quota(session, scope=scope, state="EXHAUSTED", source="observed-limit-message", details=details,
                     resets_at=resets_at)
    try:
        from aeos.events.service import emit

        emit(session, source="claude_bridge", kind="claude.quota_exhausted", entity_type="quota",
             entity_id=f"{QUOTA_PROVIDER}:{scope}", payload={"scope": scope, "reset_text": reset_text})
    except Exception:  # pragma: no cover
        log.exception("emit quota event failed")
    return row


def clear_quota_exhausted(session: Session, model: str) -> bool:
    """A later successful call proves the limit has lifted for that scope: EXHAUSTED -> UNKNOWN.

    Without this an exhaustion observed without a reset time would block the queue forever. The
    state goes back to UNKNOWN (remaining Max quota is not published), never to "OK"."""
    scope = family_of(model) or "max"
    row = session.scalars(select(QuotaState).where(QuotaState.provider == QUOTA_PROVIDER,
                                                   QuotaState.scope == scope)).first()
    if row is None or row.state != "EXHAUSTED":
        return False
    _set_quota(session, scope=scope, state="UNKNOWN", source="observed-successful-call", resets_at=None,
               details={"note": "a later call succeeded; remaining quota is not published (UNKNOWN, not OK)"})
    try:
        from aeos.events.service import emit

        emit(session, source="claude_bridge", kind="claude.quota_available", entity_type="quota",
             entity_id=f"{QUOTA_PROVIDER}:{scope}", payload={"scope": scope, "state": "UNKNOWN"})
    except Exception:  # pragma: no cover
        log.exception("emit quota event failed")
    return True


def _observe_resolution(session: Session, model: str, resolved: str) -> None:
    try:
        from aeos.routing.service import observe_supervisor_resolution
    except (ImportError, ModuleNotFoundError):  # pragma: no cover - same owner, always present
        return
    key = supervisor_route_key(model)
    if key:
        observe_supervisor_resolution(session, route_key=key, requested=model, resolved=resolved)


def quota_view(session: Session) -> dict[str, Any]:
    """Native quota is UNKNOWN unless an EXHAUSTED observation is still in effect (§42)."""
    rows = list(session.scalars(select(QuotaState).where(QuotaState.provider == QUOTA_PROVIDER)))
    now = utcnow()
    active = [r for r in rows if r.state == "EXHAUSTED" and (r.resets_at is None or ensure_aware(r.resets_at) > now)]
    if active:
        r = max(active, key=lambda x: ensure_aware(x.observed_at) or now)
        return {"state": "EXHAUSTED", "scope": r.scope, "source": r.source,
                "observed_at": r.observed_at.isoformat() if r.observed_at else None,
                "resets_at": r.resets_at.isoformat() if r.resets_at else None, "details": r.details,
                "note": "Antrean review menunggu; tidak ada fallback ke model/penyedia lain."}
    return {"state": "UNKNOWN", "scope": "max", "source": "UNKNOWN", "observed_at": None, "resets_at": None,
            "note": "Sisa kuota Max tidak tersedia secara resmi; ditampilkan UNKNOWN (bukan 'cukup')."}


# ------------------------------------------------------------------------------ capability test


def capability_test(model: str = "opus", *, actor: str = "system", settings: Settings | None = None) -> dict[str, Any]:
    """Binary + version + auth mode + env hygiene + one tiny structured call (§12 pilot, §08)."""
    settings = settings or get_settings()
    started = utcnow()
    steps: list[dict[str, Any]] = []
    pr = binmod.probe(settings, refresh=True)
    steps.append({
        "id": "binary", "status": "VERIFIED" if pr.binary.found else "MISSING",
        "detail": (f"{pr.binary.logical_path} version {pr.binary.version}" if pr.binary.found
                   else "; ".join(pr.binary.reasons)),
    })
    auth_status = pr.auth.status if pr.binary.found else "UNKNOWN"
    steps.append({"id": "auth", "status": auth_status,
                  "detail": "; ".join(pr.auth.reasons) or "native claude.ai login, firstParty provider"})
    hygiene = parent_hygiene()
    env = child_env()
    try:
        assert_clean(env)
        env_status = "WARN" if hygiene["forbidden_present"] else "VERIFIED"
        env_detail = hygiene["note"]
    except RuntimeError as exc:  # pragma: no cover - defence in depth
        env_status, env_detail = "FAILED", str(exc)
    steps.append({"id": "env", "status": env_status, "detail": env_detail,
                  "forbidden_present": hygiene["forbidden_present"]})

    result: ClaudeResult | None = None
    if pr.usable and env_status != "FAILED":
        result = run_structured(ClaudeRequest(
            purpose="doctor.capability_test", role="doctor", model=model, prompt=CAPABILITY_TEST_PROMPT,
            json_schema=CAPABILITY_TEST_SCHEMA, timeout_seconds=180, allowed_tools=[],
        ), settings=settings)
        call_ok = bool(result.ok and result.structured and result.structured.get("ok") is True)
        steps.append({
            "id": "structured_call",
            "status": "VERIFIED" if call_ok else ("EXHAUSTED" if result.quota_exhausted else "FAILED"),
            "detail": (f"resolved {result.model_resolved}" if call_ok else f"{result.error_code}: {result.error}"),
            "error_code": result.error_code, "duration_ms": result.duration_ms,
            "model_resolved": result.model_resolved,
        })
    else:
        steps.append({"id": "structured_call", "status": "BLOCKED", "detail": "skipped: binary/auth not usable"})

    if not pr.binary.found or auth_status in {"BLOCKED", "UNKNOWN"}:
        overall = "BLOCKED"
    elif steps[-1]["status"] != "VERIFIED":
        overall = "FAILED"
    elif auth_status == "DEGRADED":
        overall = "DEGRADED"
    else:
        overall = "VERIFIED"
    duration_ms = int((utcnow() - started).total_seconds() * 1000)
    out: dict[str, Any] = {
        "status": overall, "checked_at": utcnow().isoformat(), "model_requested": model,
        "model_resolved": result.model_resolved if result else None, "engine_version": pr.binary.version,
        "binary": {"found": pr.binary.found, "path_logical": pr.binary.logical_path,
                   "version": pr.binary.version, "fingerprint": pr.binary.fingerprint},
        "auth": pr.auth.summary(), "steps": steps, "duration_ms": duration_ms,
        "error_code": result.error_code if result else "BRIDGE_BLOCKED",
        "quota_exhausted": bool(result and result.quota_exhausted),
        "trace_ref": result.trace_ref if result else None, "evidence_level": "E2", "mode": "LIVE",
        "evidence_id": None, "id": None,
    }
    try:
        with session_scope() as s:
            row = persist_capability_test(s, out, actor=actor)
            out["id"] = row.id
            out["evidence_id"] = row.evidence_id
    except Exception:  # pragma: no cover - logged; the caller still gets the result
        log.exception("persisting capability test failed")
    return out


def persist_capability_test(session: Session, out: dict[str, Any], *, actor: str) -> ClaudeCapabilityTest:
    row = ClaudeCapabilityTest(
        status=out["status"], steps=redact_strings(out["steps"]), model_requested=out["model_requested"],
        model_resolved=out.get("model_resolved"), engine_version=out.get("engine_version"),
        binary_fingerprint=(out.get("binary") or {}).get("fingerprint"),
        auth_summary={k: v for k, v in (out.get("auth") or {}).items() if k != "email"},
        duration_ms=out.get("duration_ms"), error_code=out.get("error_code"), trace_ref=out.get("trace_ref"),
        actor=actor, mode="LIVE",
    )
    session.add(row)
    session.flush()
    evidence_content = {k: v for k, v in out.items() if k not in {"auth"}}
    evidence_content["auth"] = row.auth_summary
    try:
        from aeos.verification.facade import collect_evidence

        ev = collect_evidence(
            session, kind="PROBE", origin="doctor", collector_identity="aeos.claude_bridge.capability_test",
            claim="Claude Code native bridge: official binary, native Max login, structured call, model policy",
            content=canonical_json(evidence_content), evidence_level="E2", subject_type="component",
            subject_id="claude-code",
            environment={"engine_version": row.engine_version, "binary_fingerprint": row.binary_fingerprint,
                         "platform": sys.platform, "model_requested": row.model_requested},
            classification="INTERNAL", mode="LIVE",
        )
        row.evidence_id = ev.id
    except (ImportError, ModuleNotFoundError):
        row.evidence_id = None  # verification module unavailable: evidence NOT persisted (shown as such)
    except Exception:  # pragma: no cover - evidence failure must not hide the test result
        log.exception("collect_evidence failed for capability test")
        row.evidence_id = None
    try:
        from aeos.inventory.facade import record_observation

        record_observation(
            session, component_id="claude-code", dimension="functional",
            status="VERIFIED" if row.status in {"VERIFIED", "DEGRADED"} else "FAILED", observer="claude_bridge",
            observed_version=row.engine_version, observed_identity=row.model_resolved,
            detail={"capability_test_id": row.id, "status": row.status}, ttl_seconds=int(CAPABILITY_TEST_MAX_AGE.total_seconds()),
            evidence_id=row.evidence_id,
        )
    except (ImportError, ModuleNotFoundError):
        pass
    except Exception:  # pragma: no cover
        log.exception("inventory observation failed for capability test")
    try:
        from aeos.events.service import emit

        emit(session, source="claude_bridge", kind="claude.capability_tested", entity_type="component",
             entity_id="claude-code", payload={"status": row.status, "model_resolved": row.model_resolved,
                                               "test_id": row.id})
    except Exception:  # pragma: no cover
        log.exception("emit capability event failed")
    session.flush()
    return row


def latest_capability_test(session: Session) -> ClaudeCapabilityTest | None:
    return session.scalars(select(ClaudeCapabilityTest).order_by(ClaudeCapabilityTest.created_at.desc())).first()


def capability_test_view(row: ClaudeCapabilityTest | None, *, fingerprint: str | None,
                         version: str | None) -> dict[str, Any] | None:
    if row is None:
        return None
    stale = row.stale_reason
    if not stale and fingerprint and row.binary_fingerprint and fingerprint != row.binary_fingerprint:
        stale = "binary fingerprint changed since the test"
    if not stale and version and row.engine_version and version != row.engine_version:
        stale = f"version changed {row.engine_version} -> {version}"
    created = ensure_aware(row.created_at)
    if not stale and created and utcnow() - created > CAPABILITY_TEST_MAX_AGE:
        stale = "older than 7 days"
    return {
        "id": row.id, "status": "STALE" if stale else row.status, "result_status": row.status,
        "stale_reason": stale, "checked_at": created.isoformat() if created else None,
        "model_requested": row.model_requested, "model_resolved": row.model_resolved,
        "engine_version": row.engine_version, "duration_ms": row.duration_ms, "error_code": row.error_code,
        "steps": row.steps, "evidence_id": row.evidence_id, "trace_ref": row.trace_ref,
        "evidence_level": "E2" if row.status in {"VERIFIED", "DEGRADED"} else "E0",
    }


def mark_stale_if_binary_changed(session: Session, row: ClaudeCapabilityTest | None, *, fingerprint: str | None,
                                 version: str | None) -> bool:
    """§08/§50: a binary/version change makes the related check STALE and asks the Doctor to recheck."""
    if row is None or row.stale_reason:
        return False
    reason = None
    if fingerprint and row.binary_fingerprint and fingerprint != row.binary_fingerprint:
        reason = "binary fingerprint changed since the test"
    elif version and row.engine_version and version != row.engine_version:
        reason = f"version changed {row.engine_version} -> {version}"
    if not reason:
        return False
    row.stale_reason = reason
    row.stale_at = utcnow()
    try:
        from aeos.doctor.facade import invalidate

        invalidate(session, invalidator="binary:claude", reason=reason)
    except (ImportError, ModuleNotFoundError):
        pass
    session.flush()
    return True


def status(session: Session, settings: Settings | None = None, *, refresh: bool = False) -> dict[str, Any]:
    from aeos.models.routing import ModelRoute

    settings = settings or get_settings()
    pr = binmod.probe(settings, refresh=refresh)
    row = latest_capability_test(session)
    changed = mark_stale_if_binary_changed(session, row, fingerprint=pr.binary.fingerprint, version=pr.binary.version)
    routes = list(session.scalars(select(ModelRoute).where(ModelRoute.pool == "SUPERVISOR").order_by(ModelRoute.priority)))
    if not pr.binary.found:
        binary_status = "MISSING"
    else:
        binary_status = "CONFIGURED"
    signature = (binmod.publisher_signature(pr.binary.path, fingerprint_value=pr.binary.fingerprint)
                 if pr.binary.found and refresh else
                 binmod._SIG_CACHE.get((str(pr.binary.path), pr.binary.fingerprint or ""),
                                       {"status": "NOT_CHECKED",
                                        "reason": "refresh the status to run the OS signature check"}))
    pinned = binmod.approved_publisher()
    gate, gate_reason = binmod.signature_gate(signature, pinned) if pr.binary.found else ("UNVERIFIED", "no binary")
    bridge_status = "BLOCKED" if (not pr.usable or gate == "BLOCKED") else (
        "DEGRADED" if gate == "DEGRADED" else "CONFIGURED")
    return {
        "binary": {"found": pr.binary.found, "status": binary_status, "path_logical": pr.binary.logical_path,
                   "version": pr.binary.version, "fingerprint": pr.binary.fingerprint, "reasons": pr.binary.reasons,
                   "signature": signature,
                   "authenticity": {"gate": gate, "reason": gate_reason, "approved_signer": pinned,
                                    "owner_step": None if pinned else
                                    "pin the Anthropic signer (Authenticode subject / TeamIdentifier) in "
                                    "install-manifest claude-code signature.approved_signer"}},
        "auth": pr.auth.summary(),
        "bridge_status": bridge_status,
        "env_hygiene": parent_hygiene(),
        "invocation": {
            "argv_template": ["claude", "-p", "--output-format", "json", "--json-schema", "<schema>", "--model",
                              "<model>", "--restricted", "--tools", "Read,Grep,Glob", "--strict-mcp-config",
                              "--permission-prompts", "none", "--no-session-persistence"],
            "forbidden_flags": sorted(FORBIDDEN_FLAGS), "prompt_via": "stdin",
            "workspace": "<data>/supervisor/<request-id> (fresh, outside any repository)",
        },
        "policy": SUPERVISOR_POLICY,
        "models": [
            {"route_key": r.route_key, "alias": r.alias, "model_id": r.model_id,
             "resolved_version": r.resolved_version or "UNKNOWN",
             "resolved_observed_at": r.resolved_observed_at.isoformat() if r.resolved_observed_at else None,
             "status": r.status, "mode": r.mode}
            for r in routes
        ],
        "last_capability_test": capability_test_view(row, fingerprint=pr.binary.fingerprint, version=pr.binary.version),
        "capability_test_invalidated": changed,
        "quota": quota_view(session),
        "extra_usage": {
            "state": "UNKNOWN",
            "note": "Pembelian/penggunaan tambahan berbayar dimatikan lewat pengaturan akun claude.ai (langkah "
                    "manual pemilik); CLI tidak melaporkan status ini.",
        },
        "manual_fallback": "Bila bridge BLOCKED, paket manual dibuat untuk dijalankan di Claude Code resmi.",
        "checked_at": utcnow().isoformat(),
        "source": "claude --version, claude auth status (env bersih), riwayat uji kemampuan",
        "mode": "LIVE",
    }
