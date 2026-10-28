"""The broker's outgoing-content guard (ADR-0127, owner request 2026-10-28): nothing sensitive leaves the host in a
model request, whatever the route - an OpenRouter ``:free`` route (its provider may train on prompts), an OpenCode Go
route, a feasibility probe. Detection is aeos.core.redaction (the one owner of secret detection, L10/L17).

Bite tests: a GitHub token, an OpenRouter key, an AWS access key id and a PEM private key are refused with
SENSITIVE_CONTENT_BLOCKED and never reach the (mocked) upstream; near misses (an ordinary code diff, a sha256 hex
digest, a UUID, an e-mail address in code, a long JavaScript member name, Go receivers ``s.``/``b.``/``r.``
with long member names, Python floor division, an image data URI) are forwarded unchanged.

Every credential sample is assembled from parts at run time so the repository secret scan does not flag this file."""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
import random
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
    # built from parts: the canonical short Vault pattern (no look-behind, so logs stay masked) matches these
    "js_member": "this" + ".handleSubmitButtonClickedEventNow(); lib" + ".someLongFunctionNameForTheParser();",
    # Go receivers and single-letter variables: s, b and r followed by a long member name (review of 7b2ff87).
    # Built from parts: the raw canonical pattern matches these, and the repository scan of tracked files flags it.
    "go_receivers": ("func (s *Server) Start(ctx context.Context) error {\n\treturn s" + ".registerServiceWithHealthCheck"
                     "(ctx)\n}\nfunc BenchmarkX(b *testing.B) { b" + ".ReportAllocsAndResetTimerNow() }\n"
                     "rows, err := r" + ".findAllByOrganizationIdentifier(id)\nconst d = s" + ".getOwnPropertyDescriptors\n"),
    "python_floor_division": "pages = total1/" + "/itemsPerPageForThisListing + (n - 1/" + "/items_per_page_for_listing)",
}


def _has(findings: list[dict], kind: str, secret: str) -> bool:
    """A finding of *kind* whose length is the secret's (findings carry no text and no hash)."""
    return any(f["kind"] == kind and f["length"] == len(secret) for f in findings)


def _located(payload: dict, findings: list[dict], kind: str) -> str | None:
    """The text a finding of *kind* points at in the scanned payload (tests only: the guard never returns text)."""
    text = outgoing_text(payload)
    for f in findings:
        if f["kind"] == kind:
            return text[f["offset"]:f["offset"] + f["length"]]
    return None


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
def test_each_credential_kind_is_found_with_its_kind_and_span(kind):
    secret = SECRETS[kind]
    payload = {"messages": [{"role": "user", "content": f"x {secret} y"}]}
    findings = sensitive_content_findings(payload)
    assert _located(payload, findings, kind) == secret, findings
    assert secret not in json.dumps(findings)  # never the text
    assert set(findings[0]) == {"kind", "offset", "length"}  # nothing derived from the secret (no hash)


def test_a_json_escaped_token_in_tool_call_arguments_is_found():
    escaped = "\\u00" + format(ord(GITHUB_TOKEN[0]), "x") + GITHUB_TOKEN[1:]
    arguments = '{"text": "' + escaped + '"}'
    assert GITHUB_TOKEN not in arguments
    found = sensitive_content_findings({"messages": [{"role": "assistant", "tool_calls": [
        {"function": {"name": "w", "arguments": arguments}}]}]})
    assert any(f["kind"] == "github_token" for f in found), found


def test_tool_definitions_and_stop_sequences_are_scanned_too():
    tools = [{"type": "function", "function": {"name": "f", "description": f"uses {AWS_KEY_ID}", "parameters": {}}}]
    assert [f["kind"] for f in sensitive_content_findings({"tools": tools})] == ["aws_access_key_id"]
    assert [f["kind"] for f in sensitive_content_findings({"stop": [AWS_KEY_ID]})] == ["aws_access_key_id"]


def test_a_registered_runtime_secret_of_unknown_shape_is_found():
    value = "Opaque-Runtime-Value-" + "7c" * 6  # no known token shape
    assert sensitive_content_findings({"messages": [{"content": value}]}) == []
    register_secret(value)
    found = sensitive_content_findings({"messages": [{"content": f"key is {value}"}]})
    assert [(f["kind"], f["length"]) for f in found] == [(REGISTERED_SECRET, len(value))]


@pytest.mark.parametrize("name", sorted(NEAR_MISSES))
def test_near_misses_are_not_findings(name):
    assert sensitive_content_findings({"messages": [{"role": "user", "content": NEAR_MISSES[name]}]}) == []


def test_a_real_short_vault_token_is_found_after_a_separator_and_glued_to_a_word():
    token = "s" + "." + "a1B2c3D4e5F6g7H8i9J0k1L2"
    assert [f.kind for f in find_high_confidence_secrets(f"VAULT_TOKEN={token}")] == ["vault_token_short"]
    # glued to a letter (differential review 2026-10-28): a random body is a token, not a member name
    assert [f.kind for f in find_high_confidence_secrets(f"this{token[1:]}")] == ["vault_token_short"]
    assert find_high_confidence_secrets("this" + ".handleSubmitButtonClickedEventNow") == []  # a member name


def test_a_token_after_a_json_escape_or_an_encoded_separator_is_found():
    """Differential review 2026-10-28: a tool result is often JSON text (``\\n`` before the token) or a URL
    (``%3D`` before it); the escape is a boundary, never part of a word."""
    vault = "hv" + "s." + "CAESIJlWh1Ad0nFpQ8ZqXyTb7cWo3kLmN4rS5tUvWxYz"
    api_key = "AI" + "za" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"
    refresh = "1/" + "/0g" + "Xk2Lm9Qp_7Rt-4Vw8Yz1Bc3De5Fg6Hj8Kl0Nm2Pq4St6Uv8Wx0Za"
    anthropic = "sk-" + "ant-" + "api03-Zq7Lm2Np4Rs6Tv8Xb1Cd3Fg5Hj9Kw0Ya2Ue4"
    for secret, kind in ((vault, "vault_token"), (api_key, "google_api_key"), (refresh, "google_refresh_token"),
                         (anthropic, "anthropic_key")):
        for content in (json.dumps({"log": "vault login ok\n" + secret}), json.dumps({"o": "a\t" + secret}),
                        json.dumps({"o": 'said "' + secret + '"'}), json.dumps({"o": "é" + secret}),
                        json.dumps(json.dumps({"o": "x\n" + secret})), "https://h/p?a%3Ftoken%3D" + secret,
                        "https://h/p?a=1%26" + secret):
            found = sensitive_content_findings({"messages": [{"role": "tool", "content": content}]})
            assert _has(found, kind, secret), (kind, content.replace(secret, "<secret>"))


def _image_data_uri() -> str:
    return "data:image/png;base64," + base64.b64encode(random.Random(2).randbytes(750_000)).decode()  # 4 x 1// before


def _lockfile(entries: int) -> str:
    lines = (f'    "node_modules/p{i}": {{"integrity": "sha512-'
             + base64.b64encode(hashlib.sha512(str(i).encode()).digest()).decode() + '"},' for i in range(entries))
    return "{\n" + "\n".join(lines) + "\n}"


def test_base64_data_is_not_a_google_key_or_refresh_token():
    """``AIza`` and ``1//`` occur by chance inside base64: a 1 MB image data URI and a 20,000-entry lockfile gave
    google_api_key and google_refresh_token findings before (review of 7b2ff87)."""
    image = {"type": "image_url", "image_url": {"url": _image_data_uri()}}
    assert sensitive_content_findings({"messages": [{"role": "user", "content": [image]}]}) == []
    assert find_high_confidence_secrets(_lockfile(20_000)) == []


def test_real_google_and_short_token_shapes_are_still_found():
    """The near miss on the other side: random-bodied tokens after a separator are still credentials."""
    api_key = "AI" + "za" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"
    refresh = "1/" + "/0g" + "Xk2Lm9Qp_7Rt-4Vw8Yz1Bc3De5Fg6Hj8Kl0Nm2Pq4St6Uv8Wx0Za" * 2
    recovery = "r" + "." + "Zx9Qw2Er4Ty6Ui8Op0As3Df5"
    text = f'key={api_key}\n"refresh_token": "{refresh}"\nOPENBAO_RECOVERY={recovery}\n'
    assert sorted(f.kind for f in find_high_confidence_secrets(text)) == [
        "google_api_key", "google_refresh_token", "openbao_recovery_token"]
    assert find_high_confidence_secrets("x" + api_key + "y") == []  # glued into a base64-like run


def test_no_setting_can_turn_the_guard_off():
    """ADR-0127: the guard takes the payload only, and no setting names it; an extension may only ADD checks (a new
    setting here needs a test that it cannot narrow the scan)."""
    assert list(inspect.signature(sensitive_content_findings).parameters) == ["payload"]
    named = [f for f in Settings.model_fields if re.search(r"sensitive|content_guard|outgoing|secret_scan", f)]
    assert named == [], f"a setting that could switch the outgoing-content guard: {named}"


# ------------------------------------------------------------------------------------- performance (linear, bounded)
# CPU time of this process, not wall time: under a loaded full-suite run (load average 12-40, xdist workers) the
# wall clock also counts the time the process waits for a core, which made these tests fail with correct code
# (review of 2026-10-28, LESSONS L15: a flaky test is a timing assumption to fix, never a re-run).
def _best_of(n: int, fn) -> float:
    best = float("inf")
    for _ in range(n):
        t0 = time.process_time()
        fn()
        best = min(best, time.process_time() - t0)
    return best


def _load() -> str:
    try:
        return "load average {:.1f} {:.1f} {:.1f}".format(*os.getloadavg())
    except OSError:  # pragma: no cover - Windows has no load average
        return "load average n/a"


def _grows_linearly(small: float, took: float) -> bool:
    """Four times the input may cost at most about four times the CPU: bound 8x for noise (a quadratic scan costs
    16x), and a quarter-size run under 20 ms is read as 20 ms, since a shorter time is mostly noise."""
    return took <= 8 * max(small, 0.02)


def _request(messages: int) -> dict:
    return {"messages": [{"role": "user" if i % 2 else "assistant", "content": CODE_DIFF * 4} for i in range(messages)]}


def test_a_2mb_request_is_scanned_in_linear_time():
    """No absolute bound on a realistic request: the same code took 0.13 s of CPU on the owner's Mac (arm64) and
    0.54 s on the Intel macOS runner of CI (2026-10-28), so a fixed bound measures the machine, not the scan."""
    quarter, full = _request(325), _request(1300)
    size = len(outgoing_text(full))
    assert size >= 2_000_000
    small = _best_of(3, lambda: sensitive_content_findings(quarter))
    took = _best_of(2, lambda: sensitive_content_findings(full))
    assert took < 3.0, f"{size} chars took {took:.3f}s of CPU ({_load()})"
    assert _grows_linearly(small, took), (f"not linear - a quarter of the request {small:.3f}s, {size} chars "
                                          f"{took:.3f}s of CPU ({_load()})")


def _adversarial(shape: str, k: float) -> str:
    """The shape at scale *k* (k=1 is about 2 MB); every run length scales with k."""
    def n(x: int) -> int:
        return max(1, int(x * k))

    return {"unseal_label_then_spaces": lambda: "unseal key" + " " * n(2_000_000),
            "pem_headers_without_end": lambda: "-----BEGIN " + "PRIVATE KEY-----\n" * n(75_000),
            "token_prefix_runs": lambda: ("sk-" + "s." + "r." + "hvs.") * n(200_000),
            "backslash_run": lambda: "\\" * n(2_000_000) + "x",
            "colour_codes_without_final_byte": lambda: ("\x1b[" + "1;" * 50 + "\\u001b[" + "2;" * 50) * n(9_000),
            "percent_escape_runs": lambda: "%25" * n(600_000) + "%" + "%2" * n(100_000),
            "textual_escape_runs": lambda: ("\\e" + "\\" * 51 + "\\033[" + "1;" * 50 + "\\x1b" + "\\" * 49) * n(9_500),
            "string_terminator_runs": lambda: ("\x1b" + "\\" * 999 + "\\u001b" + "\\" * 1_001) * n(1_000),
            # escapes whose tail could begin a token, so the cut view differs from the greedy one at every escape
            "cut_view_tails": lambda: ("\\e[s" + "\\\\b" + "\\01" + "%1B%5Bu" + "\x1b]1" + "\\x1b[1;2;3A") * n(70_000),
            "encoded_escape_runs": lambda: ("%251B%255B" + "%253B" * 20 + "%255C" * 20) * n(10_000)}[shape]()


@pytest.mark.parametrize("shape", ["unseal_label_then_spaces", "pem_headers_without_end", "token_prefix_runs",
                                   "backslash_run", "colour_codes_without_final_byte", "percent_escape_runs",
                                   "textual_escape_runs", "string_terminator_runs", "cut_view_tails",
                                   "encoded_escape_runs"])
def test_adversarial_2mb_inputs_stay_linear(shape):
    """Inputs that made the patterns backtrack quadratically before ADR-0127 (minutes at 2 MB); a run of backslashes
    made the escape-boundary view quadratic (1.3 s at 20,000, review of 2026-10-28); the two boundary views (greedy
    and cut) stay linear when every escape has a tail that could begin a token.

    Linearity is measured, not assumed from a wall-clock bound (`_grows_linearly`), plus a generous absolute CPU cap
    that only a pathological regression reaches."""
    quarter, full = _adversarial(shape, 0.25), _adversarial(shape, 1.0)
    small = _best_of(2, lambda: find_high_confidence_secrets(quarter))
    took = _best_of(1, lambda: find_high_confidence_secrets(full))
    assert took < 3.0, f"{shape}: {len(full)} chars took {took:.3f}s of CPU ({_load()})"
    assert _grows_linearly(small, took), (f"{shape}: not linear - {len(quarter)} chars {small:.3f}s, {len(full)} "
                                          f"chars {took:.3f}s of CPU ({_load()})")


def test_bite_the_linearity_check_catches_a_quadratic_scan():
    """`_grows_linearly` is a real check: a backtracking regex search (quadratic, the failure ADR-0127 removed) fails
    it, and one linear pass passes. The quadratic input is sized so the quarter run costs about 20 ms on the owner's
    Mac and more on slower machines: with a run of 0.3 ms (the first version of this test, 2026-10-28) noise decided
    the ratio and the quadratic grew only 7.3x on a CI runner (L15)."""
    backtracking = re.compile(r"a+b")

    def quadratic(s: str) -> object:
        return backtracking.search(s)

    def linear(s: str) -> int:
        return s.count("\x00")

    quarter, full = "a" * 8_000, "a" * 32_000
    small, took = _best_of(3, lambda: quadratic(quarter)), _best_of(1, lambda: quadratic(full))
    assert not _grows_linearly(small, took), (f"a quadratic scan passed: {small:.3f}s for {len(quarter)} chars, "
                                              f"{took:.3f}s for {len(full)} ({_load()})")
    assert _grows_linearly(_best_of(3, lambda: linear(quarter)), _best_of(1, lambda: linear(full)))


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
    assert _has(rec.details["findings"], kind, secret)
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


def test_an_image_content_part_is_forwarded_unchanged_to_a_free_route(env, db):  # noqa: F811 (fixture)
    content = [{"type": "text", "text": "match this screenshot"},
               {"type": "image_url", "image_url": {"url": _image_data_uri()}}]
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        up = mock.post(UPSTREAM).mock(return_value=httpx.Response(200, json=_ok_json()))
        r = _post(env, body={"model": "model-a", "messages": [{"role": "user", "content": content}]})
        assert r.status_code == 200, r.text
        assert json.loads(up.calls[0].request.content)["messages"][0]["content"] == content


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
    assert _has(rec.details["findings"], kind, SECRETS[kind])
