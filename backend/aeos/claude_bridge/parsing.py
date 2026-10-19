"""Parse ``claude -p --output-format json`` results (shape observed on 2.1.302, see ADR-0040).

Observed fields: ``type=result``, ``subtype``, ``is_error``, ``api_error_status``, ``result`` (text),
``structured_output`` (object validated by the CLI against ``--json-schema``), ``session_id``,
``usage`` (aggregate tokens), ``modelUsage`` (per resolved model id: inputTokens/outputTokens/
cache*/contextWindow/canonicalModel/provider/costBasis), ``num_turns``, ``duration_ms``,
``duration_api_ms``, ``terminal_reason``, ``safety_stops``, ``permission_denials``.

Rules enforced here:
* the resolved model is the model that produced the output tokens; when ``--model fable`` was
  requested, the CLI was observed answering through ``claude-opus-4-8`` (fable produced 0 output
  tokens, ``safety_stops`` 2) — that is a MODEL_POLICY_MISMATCH, not a pass;
* the structured object is re-validated with ``jsonschema`` (the CLI's own validation is not
  trusted as the only check);
* usage-limit/quota/429 messages set ``quota_exhausted`` (callers checkpoint and wait);
* ``total_cost_usd``/``costUSD`` are list-price notionals under a subscription and are dropped —
  they are never shown as dollars billed (§42).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from aeos.core.redaction import redact_text

FAMILIES = ("opus", "fable", "sonnet", "haiku")
ALLOWED_REQUEST_FAMILIES = ("opus", "fable", "sonnet")
AUXILIARY_FAMILIES = ("haiku",)  # small helper calls the client may make; never the answer model
_FAMILY_RE = re.compile(r"claude-(opus|fable|sonnet|haiku)\b")
_FULL_ID_RE = re.compile(r"^claude-(opus|fable|sonnet|haiku)-[0-9][0-9a-z\-]*$")

_QUOTA_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"usage limit", r"limit reached", r"hit your (?:usage |weekly |session )?limit", r"rate[ _-]?limit",
        r"\bquota\b", r"too many requests", r"weekly limit", r"out of (?:extra )?usage", r"limit will reset",
        r"resets? (?:at|in|on)\b",
    )
]
_RESET_EPOCH_RE = re.compile(r"\|(\d{10})\b")
_RESET_TEXT_RE = re.compile(r"resets?\s+(?:at\s+|in\s+|on\s+)?([^\n.]{1,60})", re.IGNORECASE)


def redact_strings(value: Any, *, pii: bool = False) -> Any:
    """Redact string leaves only. ``redact()`` would also mask whole values under keys such as
    ``max_tokens``/``token_budget`` and corrupt structured output and usage numbers."""
    if isinstance(value, str):
        return redact_text(value, pii=pii)
    if isinstance(value, dict):
        return {k: redact_strings(v, pii=pii) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_strings(v, pii=pii) for v in value]
    return value


def family_of(model: str | None) -> str | None:
    if not model:
        return None
    m = model.strip().lower()
    if m in FAMILIES:
        return m
    hit = _FAMILY_RE.search(m)
    return hit.group(1) if hit else None


def _normalise_id(model: str) -> str:
    return model.strip().lower().split("[", 1)[0]


def is_pinned_id(model: str) -> bool:
    return bool(_FULL_ID_RE.match(_normalise_id(model)))


@dataclass(frozen=True)
class ModelResolution:
    resolved: str  # model id or "UNKNOWN"
    output_models: list[str]
    auxiliary_models: list[str]
    all_models: list[str]
    policy_ok: bool
    reason: str = ""


def resolve_model(requested: str, model_usage: Any) -> ModelResolution:
    """Determine which model produced the answer and whether it satisfies the request policy.

    * alias request (``opus``/``fable``/``sonnet``): the answer model must be in that family;
    * pinned request (``claude-opus-5-5``): the answer model must be exactly that id;
    * any other model that produced output must be in-family too, except auxiliary ``haiku``
      helpers producing fewer tokens than the answer model;
    * no ``modelUsage`` -> UNKNOWN -> fail (identity cannot be verified).
    """
    if not isinstance(model_usage, dict) or not model_usage:
        return ModelResolution("UNKNOWN", [], [], [], False, "modelUsage absent: resolved model UNKNOWN")
    rows: list[tuple[str, int]] = []
    for name, stats in model_usage.items():
        out = 0
        if isinstance(stats, dict):
            try:
                out = int(stats.get("outputTokens") or 0)
            except (TypeError, ValueError):
                out = 0
        rows.append((str(name), out))
    producing = sorted([r for r in rows if r[1] > 0], key=lambda r: (-r[1], r[0]))
    all_models = [r[0] for r in rows]
    if not producing:
        # Nothing reported output tokens; fall back to the only model if unambiguous.
        if len(rows) == 1:
            producing = rows
        else:
            return ModelResolution("UNKNOWN", [], [], all_models, False,
                                   "no model reported output tokens: answer model UNKNOWN")
    primary, primary_out = producing[0]
    want_family = family_of(requested)
    pinned = is_pinned_id(requested)
    reasons: list[str] = []
    if pinned:
        if _normalise_id(primary) != _normalise_id(requested):
            reasons.append(f"answer produced by {primary}, pinned request was {requested}")
    elif family_of(primary) != want_family:
        reasons.append(f"answer produced by {primary} (family {family_of(primary)}), requested family {want_family}")
    auxiliary: list[str] = []
    for name, out in producing[1:]:
        fam = family_of(name)
        if fam == want_family and (not pinned or _normalise_id(name) == _normalise_id(requested)):
            continue
        if fam in AUXILIARY_FAMILIES and out < primary_out:
            auxiliary.append(name)
            continue
        reasons.append(f"{name} also produced output ({out} tokens) outside the requested policy")
    return ModelResolution(primary, [p[0] for p in producing], auxiliary, all_models, not reasons, "; ".join(reasons))


@dataclass
class ParsedOutput:
    ok: bool
    structured: dict[str, Any] | None
    text: str
    model_resolved: str | None
    session_id: str | None
    usage: dict[str, Any]
    duration_ms: int | None
    error_code: str | None = None
    error: str = ""
    quota_exhausted: bool = False
    quota_resets_at: datetime | None = None
    quota_reset_text: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def detect_quota(*texts: str, api_error_status: Any = None) -> tuple[bool, datetime | None, str | None]:
    blob = "\n".join(t for t in texts if t)
    hit = api_error_status in (429, "429") or any(p.search(blob) for p in _QUOTA_PATTERNS)
    if not hit:
        return False, None, None
    resets_at = None
    m = _RESET_EPOCH_RE.search(blob)
    if m:
        try:
            resets_at = datetime.fromtimestamp(int(m.group(1)), tz=UTC)
        except (OverflowError, OSError, ValueError):
            resets_at = None
    t = _RESET_TEXT_RE.search(blob)
    return True, resets_at, (t.group(1).strip() if t else None)


def _load_result_json(stdout: str) -> dict[str, Any] | None:
    text = (stdout or "").strip().lstrip("﻿")
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        if isinstance(data, list):  # stream-json captured as array: take the result frame
            for item in reversed(data):
                if isinstance(item, dict) and item.get("type") == "result":
                    return item
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("type") == "result":
                return data
    return None


def validate_schema(schema: dict[str, Any]) -> None:
    Draft202012Validator.check_schema(schema)


def schema_errors(schema: dict[str, Any], instance: Any) -> list[str]:
    validator = Draft202012Validator(schema)
    errs = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    return [f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"[:300] for e in errs[:10]]


def summarise_usage(data: dict[str, Any]) -> dict[str, Any]:
    usage: dict[str, Any] = data["usage"] if isinstance(data.get("usage"), dict) else {}
    models: dict[str, Any] = {}
    mu = data.get("modelUsage")
    if isinstance(mu, dict):
        for name, s in mu.items():
            if not isinstance(s, dict):
                continue
            models[str(name)] = {
                k: s.get(k) for k in ("inputTokens", "outputTokens", "cacheReadInputTokens",
                                      "cacheCreationInputTokens", "thinkingTokens", "contextWindow", "provider")
                if k in s
            }
    out: dict[str, Any] = {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "service_tier": usage.get("service_tier"),
        "models": models,
        "num_turns": data.get("num_turns"),
        "duration_api_ms": data.get("duration_api_ms"),
        "terminal_reason": data.get("terminal_reason"),
        "safety_stops": data.get("safety_stops"),
        "permission_denials": len(data.get("permission_denials") or []),
        "cost_basis": "SUBSCRIPTION (list-price notional dropped; never billed dollars)",
    }
    return redact_strings(out)


def parse_output(
    stdout: str, stderr: str, *, returncode: int | None, schema: dict[str, Any], model_requested: str,
) -> ParsedOutput:
    data = _load_result_json(stdout)
    if data is None:
        quota, resets, reset_text = detect_quota(stdout, stderr)
        msg = redact_text((stderr or stdout or "").strip())[:500]
        return ParsedOutput(
            ok=False, structured=None, text="", model_resolved=None, session_id=None, usage={}, duration_ms=None,
            error_code="QUOTA_EXHAUSTED" if quota else "OUTPUT_UNPARSEABLE",
            error=msg or f"no JSON result (exit {returncode})", quota_exhausted=quota,
            quota_resets_at=resets, quota_reset_text=reset_text,
        )
    text: str = data["result"] if isinstance(data.get("result"), str) else ""
    usage = summarise_usage(data)
    session_id = data.get("session_id") if isinstance(data.get("session_id"), str) else None
    duration = data.get("duration_ms") if isinstance(data.get("duration_ms"), int) else None
    resolution = resolve_model(model_requested, data.get("modelUsage"))
    usage["model_resolution"] = {
        "output_models": resolution.output_models, "auxiliary_models": resolution.auxiliary_models,
        "all_models": resolution.all_models, "policy_ok": resolution.policy_ok, "reason": resolution.reason,
    }
    base: dict[str, Any] = {
        "text": redact_text(text), "model_resolved": resolution.resolved, "session_id": session_id,
        "usage": usage, "duration_ms": duration, "raw": data,
    }
    is_error = bool(data.get("is_error")) or data.get("subtype") not in (None, "success")
    if is_error or (returncode not in (0, None)):
        quota, resets, reset_text = detect_quota(text, stderr, str(data.get("subtype") or ""),
                                                 api_error_status=data.get("api_error_status"))
        code = "QUOTA_EXHAUSTED" if quota else f"CLAUDE_{str(data.get('subtype') or 'error').upper()}"
        return ParsedOutput(ok=False, structured=None, error_code=code[:48],
                            error=redact_text(text or stderr or "claude reported an error")[:500],
                            quota_exhausted=quota, quota_resets_at=resets, quota_reset_text=reset_text, **base)
    structured = data.get("structured_output")
    if structured is None and text:
        try:
            candidate = json.loads(text)
            structured = candidate if isinstance(candidate, dict) else None
        except json.JSONDecodeError:
            structured = None
    if not isinstance(structured, dict):
        return ParsedOutput(ok=False, structured=None, error_code="SCHEMA_INVALID",
                            error="no structured object in the result", **base)
    try:
        errors = schema_errors(schema, structured)
    except SchemaError as exc:  # pragma: no cover - schema validated before the call
        errors = [f"schema invalid: {exc.message}"]
    if errors:
        return ParsedOutput(ok=False, structured=None, error_code="SCHEMA_INVALID",
                            error="; ".join(errors), **base)
    if not resolution.policy_ok:
        # §12 "tolak penerimaan hasil": the answer of an off-policy model is never handed to callers
        # (the redacted raw output stays in the trace for diagnosis).
        return ParsedOutput(ok=False, structured=None, error_code="MODEL_POLICY_MISMATCH",
                            error=resolution.reason, **base)
    return ParsedOutput(ok=True, structured=redact_strings(structured), **base)
