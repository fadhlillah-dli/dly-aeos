"""The broker's outgoing-content guard (ADR-0127, owner request 2026-10-28): nothing sensitive leaves the host in a
model request, whatever the route - an OpenRouter ``:free`` route (its provider may train on prompts), an OpenCode Go
route, a feasibility probe. Detection is aeos.core.redaction (the one owner of secret detection, L10/L17).

Bite tests: a GitHub token, an OpenRouter key, an AWS access key id and a PEM private key are refused with
SENSITIVE_CONTENT_BLOCKED and never reach the (mocked) upstream; near misses (an ordinary code diff, a sha256 hex
digest, a UUID, an e-mail address in code, a long JavaScript member name) are forwarded unchanged.

Every credential sample is assembled from parts at run time so the repository secret scan does not flag this file."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import time
import uuid

import httpx
import pytest
import respx
from sqlalchemy import select

from aeos.broker import facade as broker
from aeos.broker import feasibility
from aeos.broker.model_proxy import outgoing_text, sensitive_content_findings
from aeos.core.config import Settings
from aeos.core.errors import PolicyDenied
from aeos.core.redaction import REGISTERED_SECRET, find_high_confidence_secrets, register_secret
from aeos.models.audit import AuditEvent, Event
from aeos.models.broker_ext import BrokerRequest
from aeos.models.routing import CapabilityGrant
from aeos.models.workforce import WorkerRun
from tests.unit.test_broker_model_proxy import UPSTREAM, _ok_json, _post, env  # noqa: F401 (fixture)
from tests.unit.test_w7c1_route_request_shape import (  # noqa: F401 (fixtures)
    GO_CHAT,
    GO_GLM,
    _go_budget,
    _ok,
    _trial,
    go_proxy,
    world,
)

GITHUB_TOKEN = "gh" + "p_" + "Zq7Lm2Np4Rs6Tv8Xb1Cd3Fg5Hj9Kw0Ya2Ue4"
OPENROUTER_KEY = "sk-" + "or-v1-" + "9f8e7d6c5b4a" * 5
AWS_KEY_ID = "AK" + "IA" + "Q7W3E9R1T5Y2U8I4"
PEM_KEY = ("-----BEGIN " + "PRIVATE KEY-----\n" + "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7" * 3
           + "\n-----END " + "PRIVATE KEY-----")
SECRETS = {"github_token": GITHUB_TOKEN, "openrouter_key": OPENROUTER_KEY, "aws_access_key_id": AWS_KEY_ID,
           "private_key_pem": PEM_KEY}

CODE_DIFF = '''diff --git a/src/app.py b/src/app.py
index 3b18e51..a9c4f2d 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,6 +1,9 @@
-def load(path):
-    return open(path).read()
+def load(path: str, *, encoding: str = "utf-8") -> str:
+    """Read the file; the token budget is the caller's (max_tokens=256, api_key_ref="workers/openrouter")."""
+    with open(path, encoding=encoding) as fh:
+        return fh.read()
+# task-scheduler-configuration-value, disk-usage-threshold-percentage
'''
NEAR_MISSES = {
    "code_diff": CODE_DIFF,
    "sha256_hex": hashlib.sha256(b"release artefact").hexdigest(),
    "uuid": str(uuid.UUID("6f1c2a9e-4b7d-4e3a-9c1f-2d8e5b7a0c34")),
    "email_in_code": 'AUTHOR = "Jane Doe <jane.doe@example.com>"  # maintainer',
    "js_member": "this.handleSubmitButtonClickedEventNow(); lib.someLongFunctionNameForTheParser();",
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _placements(secret: str) -> dict[str, list[dict]]:
    """The secret in each place of an OpenAI-compatible request a worker can fill."""
    call = {"id": "c1", "type": "function", "function": {"name": "write", "arguments": json.dumps({"text": secret})}}
    return {
        "user": [{"role": "user", "content": f"please review:\n{secret}\n"}],
        "system": [{"role": "system", "content": f"context {secret}"}, {"role": "user", "content": "hi"}],
        "assistant_tool_call": [{"role": "user", "content": "go"},
                                {"role": "assistant", "content": None, "tool_calls": [call]}],
        "tool_result": [{"role": "user", "content": "go"}, {"role": "assistant", "content": None, "tool_calls": [
            {**call, "function": {"name": "read", "arguments": "{}"}}]},
                        {"role": "tool", "tool_call_id": "c1", "content": [{"type": "text", "text": secret}]}],
    }


# ------------------------------------------------------------------------------------- detection (pure, no DB)
@pytest.mark.parametrize("kind", sorted(SECRETS))
def test_each_credential_kind_is_found_with_its_kind_and_span_hash(kind):
    secret = SECRETS[kind]
    findings = sensitive_content_findings({"messages": [{"role": "user", "content": f"x {secret} y"}]})
    assert {"kind": kind, "span_sha256": _sha(secret)} in findings, findings
    assert secret not in json.dumps(findings)  # never the text


def test_a_json_escaped_token_in_tool_call_arguments_is_found():
    escaped = "\\u00" + format(ord(GITHUB_TOKEN[0]), "x") + GITHUB_TOKEN[1:]
    arguments = '{"text": "' + escaped + '"}'
    assert GITHUB_TOKEN not in arguments
    found = sensitive_content_findings({"messages": [{"role": "assistant", "tool_calls": [
        {"function": {"name": "w", "arguments": arguments}}]}]})
    assert {"kind": "github_token", "span_sha256": _sha(GITHUB_TOKEN)} in found


def test_tool_definitions_and_stop_sequences_are_scanned_too():
    tools = [{"type": "function", "function": {"name": "f", "description": f"uses {AWS_KEY_ID}", "parameters": {}}}]
    assert [f["kind"] for f in sensitive_content_findings({"tools": tools})] == ["aws_access_key_id"]
    assert [f["kind"] for f in sensitive_content_findings({"stop": [AWS_KEY_ID]})] == ["aws_access_key_id"]


def test_a_registered_runtime_secret_of_unknown_shape_is_found():
    value = "Opaque-Runtime-Value-" + "7c" * 6  # no known token shape
    assert sensitive_content_findings({"messages": [{"content": value}]}) == []
    register_secret(value)
    found = sensitive_content_findings({"messages": [{"content": f"key is {value}"}]})
    assert found == [{"kind": REGISTERED_SECRET, "span_sha256": _sha(value)}]


@pytest.mark.parametrize("name", sorted(NEAR_MISSES))
def test_near_misses_are_not_findings(name):
    assert sensitive_content_findings({"messages": [{"role": "user", "content": NEAR_MISSES[name]}]}) == []


def test_a_real_short_vault_token_is_still_found_after_a_separator():
    token = "s" + "." + "a1B2c3D4e5F6g7H8i9J0k1L2"
    assert [f.kind for f in find_high_confidence_secrets(f"VAULT_TOKEN={token}")] == ["vault_token_short"]
    assert find_high_confidence_secrets(f"this{token[1:]}") == []  # glued to a word: a member access, not a token


def test_no_setting_can_turn_the_guard_off():
    """ADR-0127: the guard takes the payload only, and no setting names it; an extension may only ADD checks (a new
    setting here needs a test that it cannot narrow the scan)."""
    assert list(inspect.signature(sensitive_content_findings).parameters) == ["payload"]
    named = [f for f in Settings.model_fields if re.search(r"sensitive|content_guard|outgoing|secret_scan", f)]
    assert named == [], f"a setting that could switch the outgoing-content guard: {named}"


# ------------------------------------------------------------------------------------- performance (linear, bounded)
def _best_of(n: int, fn) -> float:
    best = float("inf")
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


def _load() -> str:
    try:
        return "load average {:.1f} {:.1f} {:.1f}".format(*os.getloadavg())
    except OSError:  # pragma: no cover - Windows has no load average
        return "load average n/a"


def test_a_2mb_request_is_scanned_well_within_200ms():
    messages = [{"role": "user" if i % 2 else "assistant", "content": CODE_DIFF * 4} for i in range(1300)]
    payload = {"messages": messages}
    size = len(outgoing_text(payload))
    assert size >= 2_000_000
    took = _best_of(3, lambda: sensitive_content_findings(payload))
    assert took < 0.2, f"{size} chars took {took:.3f}s ({_load()})"


@pytest.mark.parametrize("shape", ["unseal_label_then_spaces", "pem_headers_without_end", "token_prefix_runs"])
def test_adversarial_2mb_inputs_stay_linear(shape):
    """Inputs that made the patterns backtrack quadratically before ADR-0127 (minutes at 2 MB)."""
    text = {"unseal_label_then_spaces": "unseal key" + " " * 2_000_000,
            "pem_headers_without_end": "-----BEGIN " + "PRIVATE KEY-----\n" * 75_000,
            "token_prefix_runs": ("sk-" + "s." + "r." + "hvs.") * 200_000}[shape]
    took = _best_of(1, lambda: find_high_confidence_secrets(text))
    assert took < 1.0, f"{shape}: {len(text)} chars took {took:.3f}s ({_load()})"


# ------------------------------------------------------------------------------------- the proxy, :free route
@pytest.mark.parametrize("kind", sorted(SECRETS))
@pytest.mark.parametrize("where", ["user", "system", "assistant_tool_call", "tool_result"])
def test_a_credential_is_never_sent_to_a_free_route(env, db, kind, where):  # noqa: F811 (fixture)
    secret = SECRETS[kind]
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        up = mock.post(UPSTREAM).mock(return_value=httpx.Response(200, json=_ok_json()))
        r = _post(env, body={"model": "model-a", "messages": _placements(secret)[where]})
        assert up.call_count == 0 and len(mock.calls) == 0
    assert r.status_code == 403, r.text
    err = r.json()["error"]
    assert err["code"] == "SENSITIVE_CONTENT_BLOCKED" and kind in err["message"] and "not sent" in err["message"]
    assert secret not in r.text
    db.expire_all()
    req = db.scalar(select(BrokerRequest).where(BrokerRequest.outcome == "SENSITIVE_BLOCKED"))
    assert req.route_key == "worker.free-a" and req.status_code == 403 and req.upstream_model is None
    assert db.get(CapabilityGrant, env["grant"].id).used_requests == 0  # nothing sent, nothing counted
    run = db.get(WorkerRun, env["run"].id)
    assert run.usage_summary["sensitive_blocked"] == 1 and "SENSITIVE_CONTENT_BLOCKED" in run.current_action
    rec = db.scalar(select(AuditEvent).where(AuditEvent.action == "broker.model_proxy.sensitive_blocked"))
    assert rec.result == "DENIED" and rec.target_id == "worker.free-a"
    assert rec.details["route_key"] == "worker.free-a" and kind in rec.details["kinds"]
    assert {"kind": kind, "span_sha256": _sha(secret)} in rec.details["findings"]
    ev = db.scalar(select(Event).where(Event.kind == "run.model_call_blocked"))
    assert ev.run_id == env["run"].id and ev.payload["code"] == "SENSITIVE_CONTENT_BLOCKED"
    stored = json.dumps([req.error_summary, run.current_action, rec.reason, rec.details, ev.payload])
    assert all(line not in stored for line in secret.splitlines() if len(line) > 16)


@pytest.mark.parametrize("name", sorted(NEAR_MISSES))
def test_near_misses_are_forwarded_unchanged_to_a_free_route(env, db, name):  # noqa: F811 (fixture)
    content = NEAR_MISSES[name]
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        up = mock.post(UPSTREAM).mock(return_value=httpx.Response(200, json=_ok_json()))
        r = _post(env, body={"model": "model-a", "messages": [{"role": "user", "content": content}]})
        assert r.status_code == 200, r.text
        assert json.loads(up.calls[0].request.content)["messages"][0]["content"] == content
    db.expire_all()
    assert db.scalar(select(BrokerRequest).where(BrokerRequest.outcome == "SENSITIVE_BLOCKED")) is None


# ------------------------------------------------------------------------------------- an OpenCode Go route
@pytest.mark.parametrize("kind", sorted(SECRETS))
def test_a_credential_is_never_sent_to_a_go_route(world, db, go_proxy, kind):  # noqa: F811 (fixtures)
    _trial(db, world, GO_GLM, region="EU", budget_decision_id=_go_budget(db, world).id)
    secret = SECRETS[kind]
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        up = mock.post(GO_CHAT).mock(return_value=httpx.Response(200, json=_ok("glm-5.3-flash")))
        r = go_proxy["client"].post("/broker/v1/chat/completions", headers={
            "Authorization": f"Bearer {go_proxy['token']}"}, json={
            "model": "aeos-go-glm", "messages": [{"role": "user", "content": f"deploy with {secret}"}]})
        assert up.call_count == 0
        assert r.status_code == 403 and r.json()["error"]["code"] == "SENSITIVE_CONTENT_BLOCKED"
        ok = go_proxy["client"].post("/broker/v1/chat/completions", headers={
            "Authorization": f"Bearer {go_proxy['token']}"}, json={
            "model": "aeos-go-glm", "messages": [{"role": "user", "content": CODE_DIFF}]})
        assert ok.status_code == 200 and up.call_count == 1  # the near miss on the same route is served
    db.expire_all()
    req = db.scalar(select(BrokerRequest).where(BrokerRequest.outcome == "SENSITIVE_BLOCKED"))
    assert req.route_key == GO_GLM and req.run_id == go_proxy["run_id"]


# ------------------------------------------------------------------------------------- a feasibility probe
@pytest.mark.parametrize("kind", sorted(SECRETS))
def test_a_credential_is_never_sent_by_a_probe(world, db, monkeypatch, kind):  # noqa: F811 (fixture)
    """The probe prompt is fixed today; the guard covers its request all the same (a future probe shape, e.g. with tool
    definitions, cannot carry a credential out)."""
    _trial(db, world, GO_GLM, region="EU", budget_decision_id=_go_budget(db, world).id)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:  # near miss: the ordinary probe is sent
        up = mock.post(GO_CHAT).mock(return_value=httpx.Response(200, json=_ok("glm-5.3-flash")))
        assert broker.probe_route(db, route_key=GO_GLM, actor="user:owner")["outcome"] == "REACHABLE"
        assert up.call_count == 1
    db.commit()
    monkeypatch.setattr(feasibility, "PROBE_PROMPT", f"{feasibility.PROBE_PROMPT} {SECRETS[kind]}")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        up = mock.post(GO_CHAT).mock(return_value=httpx.Response(200, json=_ok("glm-5.3-flash")))
        with pytest.raises(PolicyDenied) as ei:
            broker.probe_route(db, route_key=GO_GLM, actor="user:owner")
        assert up.call_count == 0
    assert ei.value.code == "SENSITIVE_CONTENT_BLOCKED" and SECRETS[kind] not in ei.value.message
    db.commit()
    rec = db.scalars(select(AuditEvent).where(AuditEvent.action == "broker.route_feasibility.probe")).all()[-1]
    assert rec.result == "DENIED" and rec.details["sent"] is False and kind in rec.details["kinds"]
    assert {"kind": kind, "span_sha256": _sha(SECRETS[kind])} in rec.details["findings"]
