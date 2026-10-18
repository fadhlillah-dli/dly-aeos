"""Parsing of the official client's JSON (fixtures recorded from Claude Code 2.1.302 on 2026-10-18,
trimmed; see ADR-0040). §12 'tolak penerimaan hasil bila model yang digunakan tidak sesuai policy',
§50 'Fable Max quota habis', 'Model alias/provider route berubah'."""

from __future__ import annotations

import copy
import json

from aeos.claude_bridge.binary import parse_auth_status
from aeos.claude_bridge.parsing import detect_quota, family_of, parse_output, resolve_model

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}

# Real `claude -p --output-format json --json-schema ... --model opus` result (2.1.302).
OPUS_OK = {
    "duration_api_ms": 1772, "stop_reason": "tool_use", "session_id": "35eb5b61-a5bd-47a2-a682-6bcb8be1c995",
    "total_cost_usd": 0.040376,
    "usage": {"input_tokens": 2, "cache_creation_input_tokens": 4916, "cache_read_input_tokens": 0, "output_tokens": 52,
              "service_tier": "standard", "inference_geo": "global", "speed": "standard", "fallback_credit": None},
    "modelUsage": {"claude-opus-5-5": {
        "inputTokens": 2, "outputTokens": 52, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 4916,
        "webSearchRequests": 0, "costUSD": 0.040376, "contextWindow": 1000000, "maxOutputTokens": 128000,
        "thinkingTokens": 0, "canonicalModel": "claude-opus-5-5", "provider": "firstParty", "costBasis": "list"}},
    "permission_denials": [], "terminal_reason": "completed", "safety_stops": 0, "is_error": False, "num_turns": 2,
    "subtype": "success", "api_error_status": None, "result": "{\"ok\":true}", "structured_output": {"ok": True},
    "type": "result", "duration_ms": 1910, "uuid": "c71929f2-2f4b-40b8-b9e3-aa3b9054faa4",
}

# Real `--model fable` result: fable produced 0 output tokens, the answer came from claude-opus-4-8.
FABLE_FALLBACK = {
    "duration_api_ms": 4318, "session_id": "3859830d-d936-453a-a0d1-8a78526829a6", "total_cost_usd": 0.346365,
    "usage": {"input_tokens": 4, "cache_creation_input_tokens": 13620, "cache_read_input_tokens": 0, "output_tokens": 221},
    "modelUsage": {
        "claude-fable-5-1": {"inputTokens": 4, "outputTokens": 0, "cacheCreationInputTokens": 13617,
                             "costUSD": 0.27238, "contextWindow": 1000000, "canonicalModel": "claude-fable-5-1",
                             "provider": "firstParty", "costBasis": "list"},
        "claude-opus-4-8": {"inputTokens": 2, "outputTokens": 221, "cacheCreationInputTokens": 6845,
                            "costUSD": 0.073985, "thinkingTokens": 168, "canonicalModel": "claude-opus-4-8",
                            "provider": "firstParty", "costBasis": "list"},
    },
    "permission_denials": [], "terminal_reason": "completed", "safety_stops": 2, "is_error": False, "num_turns": 3,
    "subtype": "success", "api_error_status": None, "result": "{\"ok\":true}", "structured_output": {"ok": True},
    "type": "result", "duration_ms": 5291,
}

# Real `claude auth status` (identifying fields replaced).
AUTH_MAX = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "analyticsDisabled": False,
            "email": "owner@example.com", "orgId": "org-redacted", "orgName": "redacted", "subscriptionType": "max"}


def _parse(data: dict, model: str = "opus", rc: int = 0, stderr: str = ""):
    return parse_output(json.dumps(data), stderr, returncode=rc, schema=SCHEMA, model_requested=model)


def test_opus_result_parsed_with_resolved_model_and_no_dollars():
    p = _parse(OPUS_OK)
    assert p.ok and p.structured == {"ok": True}
    assert p.model_resolved == "claude-opus-5-5"
    assert p.session_id == OPUS_OK["session_id"] and p.duration_ms == 1910
    assert p.usage["input_tokens"] == 2 and p.usage["output_tokens"] == 52  # numbers not masked by redaction
    assert "costUSD" not in json.dumps(p.usage) and "total_cost_usd" not in json.dumps(p.usage)


def test_fable_request_answered_by_other_model_is_policy_mismatch():
    p = _parse(FABLE_FALLBACK, model="fable")
    assert not p.ok and p.error_code == "MODEL_POLICY_MISMATCH"
    assert p.structured is None  # §12: an off-policy answer is rejected, not handed to the caller
    assert p.model_resolved == "claude-opus-4-8"
    assert "claude-opus-4-8" in p.error
    assert p.usage["safety_stops"] == 2


def test_pinned_id_requires_exact_model():
    assert _parse(OPUS_OK, model="claude-opus-5-5").ok
    p = _parse(OPUS_OK, model="claude-opus-5-6")
    assert not p.ok and p.error_code == "MODEL_POLICY_MISMATCH"


def test_family_mismatch_and_unknown_identity():
    p = _parse(OPUS_OK, model="sonnet")
    assert p.error_code == "MODEL_POLICY_MISMATCH"
    no_usage = copy.deepcopy(OPUS_OK)
    no_usage.pop("modelUsage")
    p = _parse(no_usage)
    assert not p.ok and p.model_resolved == "UNKNOWN" and p.error_code == "MODEL_POLICY_MISMATCH"
    assert p.structured is None  # identity UNKNOWN is never accepted as a pass


def test_auxiliary_haiku_tolerated_but_foreign_output_model_rejected():
    mu = {"claude-opus-5-5": {"outputTokens": 500}, "claude-haiku-5-5": {"outputTokens": 12}}
    r = resolve_model("opus", mu)
    assert r.policy_ok and r.resolved == "claude-opus-5-5" and r.auxiliary_models == ["claude-haiku-5-5"]
    r = resolve_model("opus", {"claude-opus-5-5": {"outputTokens": 500}, "claude-sonnet-5-5": {"outputTokens": 40}})
    assert not r.policy_ok


def test_schema_validation_is_rechecked():
    bad = copy.deepcopy(OPUS_OK)
    bad["structured_output"] = {"ok": "yes", "extra": 1}
    p = _parse(bad)
    assert not p.ok and p.error_code == "SCHEMA_INVALID"
    missing = copy.deepcopy(OPUS_OK)
    missing.pop("structured_output")
    missing["result"] = "not json"
    assert _parse(missing).error_code == "SCHEMA_INVALID"
    from_text = copy.deepcopy(OPUS_OK)
    from_text.pop("structured_output")
    assert _parse(from_text).structured == {"ok": True}  # falls back to result text JSON


def test_quota_exhaustion_detected_from_error_result():
    limit = copy.deepcopy(OPUS_OK)
    limit.update({"is_error": True, "subtype": "error_during_execution", "result": "Claude AI usage limit reached|1792300000",
                  "structured_output": None})
    p = _parse(limit, rc=1)
    assert not p.ok and p.quota_exhausted and p.error_code == "QUOTA_EXHAUSTED"
    assert p.quota_resets_at is not None and p.quota_resets_at.year == 2026
    weekly = copy.deepcopy(limit)
    weekly["result"] = "You've hit your weekly limit - resets Oct 20, 9am"
    p = _parse(weekly, rc=1)
    assert p.quota_exhausted and p.quota_reset_text and "Oct 20" in p.quota_reset_text
    api429 = copy.deepcopy(limit)
    api429.update({"result": "API Error", "api_error_status": 429})
    assert _parse(api429, rc=1).quota_exhausted


def test_quota_detected_from_unparseable_stderr_and_other_errors_not_quota():
    p = parse_output("", "Error: 5-hour limit reached - resets 3pm", returncode=1, schema=SCHEMA, model_requested="opus")
    assert p.quota_exhausted and p.error_code == "QUOTA_EXHAUSTED"
    p = parse_output("garbage", "segfault", returncode=1, schema=SCHEMA, model_requested="opus")
    assert not p.quota_exhausted and p.error_code == "OUTPUT_UNPARSEABLE"
    assert detect_quota("everything fine")[0] is False


def test_auth_status_parsing():
    ok = parse_auth_status(0, json.dumps(AUTH_MAX))
    assert ok.status == "VERIFIED" and ok.subscription_type == "max" and ok.email == "owner@example.com"
    pro = parse_auth_status(0, json.dumps({**AUTH_MAX, "subscriptionType": "pro"}))
    assert pro.status == "DEGRADED"
    for bad in ({**AUTH_MAX, "authMethod": "apiKey"}, {**AUTH_MAX, "authMethod": "console"},
                {**AUTH_MAX, "apiProvider": "bedrock"}, {**AUTH_MAX, "apiProvider": "vertex"},
                {**AUTH_MAX, "loggedIn": False}, {"loggedIn": False}):
        assert parse_auth_status(0, json.dumps(bad)).status == "BLOCKED", bad
    assert parse_auth_status(1, "not json").status == "UNKNOWN"
    assert "orgId" not in parse_auth_status(0, json.dumps(AUTH_MAX)).summary()


def test_family_of():
    assert family_of("claude-opus-4-8") == "opus" and family_of("fable") == "fable"
    assert family_of("claude-fable-5-1[1m]") == "fable" and family_of("gpt-6") is None
