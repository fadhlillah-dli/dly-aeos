"""Owner decision R31 of 2026-10-28 (ADR-0125): OpenCode Go routes move WATCHLIST <-> WORKER by an owner action (only an
untouched CANDIDATE with no decision), the R31 Go WORKER candidates are seeded without changing any existing seeded
configuration, and a tool-call probe records PASS/FAIL evidence that makes a TRIAL route tool-capable for its CURRENT
configuration only (``routing.facade.tool_capable``) without touching ``supports_tools``. Real routing, ledger,
evidence collector and audit; the upstream is a respx mock (no network, no real provider call, no real key)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from sqlalchemy import select
from typer.testing import CliRunner

from aeos.broker import facade as broker
from aeos.broker import feasibility
from aeos.cli import app
from aeos.core.errors import PolicyDenied, ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.models.audit import AuditEvent, Event
from aeos.models.decisions import Decision
from aeos.models.evidence import EvidenceArtifact
from aeos.models.routing import ModelRoute
from aeos.routing import facade as routing
from aeos.routing import seed, service
from aeos.routing.seed import GO_OPENAI_BASE, _go_pricing
from aeos.verification import facade as verification

GO = "opencode-go/step-5-preview"
GO_CHAT = f"{GO_OPENAI_BASE}/chat/completions"
GO_KEY = "go-test-" + "t" * 40  # visibly fake
WATCH = "opencode-go/kimi-k3.1-pro"  # seeded WATCHLIST CANDIDATE
runner = CliRunner()

# config hash (sha256 hex, first 20 characters) of every route seeded on main before ADR-0125, computed with the worker
# prompt revision pinned (the prompt revision is a separate invariant): a change here voids an owner's ACTIVE TRIAL
GOLDEN_SEED_HASHES = {
    "claude-native/fable": "ac8c68cea8b5c05592d8",
    "claude-native/opus": "62a5b7880ff53024f10e",
    "gemini/gemini-3.8-flash-free-tier": "ebe163d8bb2a07a948f8",
    "local/qwen3.8-flash-next": "d228713b0ee94947d1a3",
    "opencode-go/deepseek-v4.1-flash": "304f5c0fe0793904d63e",
    "opencode-go/glm-5.2": "d81e363f1cc8ad665ac2",
    "opencode-go/glm-5.3": "abd1ecc9c626fbb9e81f",
    "opencode-go/glm-5.3-flash": "e7e1cd8b203b86f90124",
    "opencode-go/hy3": "99d29d4f17ae68d2cfa9",
    "opencode-go/kimi-k2.7-code": "87cc4dddc3e6b34cf652",
    "opencode-go/kimi-k3.1-pro": "87c88e86472440186b68",
    "opencode-go/longcat-2.0": "6e37ff492142b2874229",
    "opencode-go/mimo-v2.6-flash": "8b314eb15ebf30cedeb1",
    "opencode-go/mimo-v2.6-pro": "7a097c99a1a7b4c1fd83",
    "opencode-go/minimax-m3.1-flash": "2fc78182b8194aa011a6",
    "opencode-zen/beam-free": "70135ed2e8ea820dc84e",
    "opencode-zen/big-pickle": "532b41a7489a721e3ba7",
    "opencode-zen/deepseek-v4.1-flash-free": "aebd561c180baeacc1b5",
    "opencode-zen/exo-free": "9f129be1571c8d9f3aa9",
    "opencode-zen/glm-5.3-flash-free": "04c0b3d6a3a57231c0ae",
    "opencode-zen/gpt-5.3-codex": "139c17596d81640044c8",
    "opencode-zen/grok-build-0.1": "f02b7f077a1cb16d1198",
    "opencode-zen/kimi-k2.7-code": "1b0636fc6285ee2c11d3",
    "opencode-zen/ll-alpha-free": "ced8c2fbf5237b6a4f00",
    "opencode-zen/longcat-2.5-preview-free": "af81bb27843ca6b2167e",
    "opencode-zen/mimo-v2.6-flash-free": "bb0e5b5b156b0def6908",
    "opencode-zen/minimax-m3": "ee8a3916e58a0d065057",
    "opencode-zen/mistral-large-4": "c06690811a132b8e9335",
    "opencode-zen/muse-spark-1.3": "07c6a37cc727c4b0917f",
    "opencode-zen/nemotron-3-ultra-free": "254d3d5047ec9db683bd",
    "opencode-zen/nemotron-3.5-lightning-free": "8a8e077a41ea20360fcc",
    "opencode-zen/nemotron-3.5-super-vl-free": "2c409056670ad3ec5a13",
    "opencode-zen/qwen3.8-flash": "3d6ee3adf20bd51a90e5",
    "opencode-zen/space-bunny-free": "a4415f66950abbd338ab",
    "openrouter/poolside/laguna-s-2.1:free": "c998c7c8db562ed9ec4e",
}
PINNED_PROMPT_REVISION = "PROMPT-REVISION-FIXED"


# ---------------------------------------------------------------------------------------------- seed invariance


def _seed_drift(db) -> list[str]:
    """The seeded routes whose config hash differs from the golden one (pinned prompt revision)."""
    seed.run(db)
    db.flush()
    rows = {r.route_key: r for r in db.scalars(select(ModelRoute))}
    out = []
    for key, prefix in GOLDEN_SEED_HASHES.items():
        row = rows.get(key)
        got = (row.config_hash or "")[len("sha256:"):][:20] if row is not None else "MISSING"
        if got != prefix:
            out.append(f"seeded route {key} changed its config hash ({prefix} -> {got}): its TRIAL would be void")
    return out


@pytest.fixture
def pinned_prompt(monkeypatch):
    monkeypatch.setattr(seed, "_worker_prompt_revision", lambda: PINNED_PROMPT_REVISION)


def test_every_existing_seeded_route_keeps_its_config_hash_and_the_r31_routes_are_added(db, pinned_prompt):
    assert _seed_drift(db) == []
    new = {r.route_key for r in db.scalars(select(ModelRoute))} - set(GOLDEN_SEED_HASHES)
    assert new == {"opencode-go/kimi-k3", "opencode-go/step-5-preview"}
    for key in new:
        r = service.get_route(db, key)
        assert (r.pool, r.status, r.supports_tools, r.mode_params["upstream_api"]) == (
            "WORKER", "CANDIDATE", "UNKNOWN", "openai.chat_completions"), key
        assert r.data_terms["may_use_inputs_for_training"] is False and "0 days" in r.data_terms["retention"], key


def test_bite_a_changed_seeded_configuration_is_red_and_a_descriptive_change_is_green(db, pinned_prompt,
                                                                                      monkeypatch):
    monkeypatch.setattr(seed, "GO_TOOLS_SOURCE", "models.dev (re-read)")  # notes only: not part of the identity
    assert _seed_drift(db) == []
    db.rollback()
    for r in db.scalars(select(ModelRoute)):
        db.delete(r)
    db.flush()
    monkeypatch.setitem(seed.GO_TERMS, "retention", "30 days")  # a data-terms change of the existing Go routes
    drift = _seed_drift(db)
    assert any(d.startswith("seeded route opencode-go/glm-5.3-flash changed its config hash") for d in drift), drift
    assert all("its TRIAL would be void" in d for d in drift)


# ---------------------------------------------------------------------------------------------- owner pool move


def _seeded(db) -> None:
    seed.run(db)
    db.commit()


def test_a_go_watchlist_candidate_without_decisions_moves_to_worker_and_back(db):
    _seeded(db)
    before = service.get_route(db, WATCH).config_hash
    route, change = service.set_route_pool(db, WATCH, pool="WORKER", rationale="owner decision R31: best first")
    db.commit()
    assert (route.pool, route.status, change["needs_new_trial"]) == ("WORKER", "CANDIDATE", False)
    assert route.config_hash != before and change["previous"]["pool"] == "WATCHLIST"
    assert db.scalar(select(Event).where(Event.kind == "route.pool_set", Event.entity_id == WATCH)) is not None
    route, _ = service.set_route_pool(db, WATCH, pool="WATCHLIST", rationale="owner moves it back to the watchlist")
    assert route.pool == "WATCHLIST" and route.config_hash == before


@pytest.mark.parametrize("status", ["INVALIDATED", "ACTIVE"])
def test_bite_a_go_route_with_any_decision_is_refused(db, status):
    _seeded(db)
    db.add(Decision(kind="TRIAL", actor_user_id="owner", actor_kind="HUMAN", target_type="model_route",
                    target_id=WATCH, target_hash="sha256:" + "0" * 64, domain="PERSONAL", rationale="old trial",
                    status=status))
    db.commit()
    with pytest.raises(ValidationFailed) as ei:
        service.set_route_pool(db, WATCH, pool="WORKER", rationale="owner decision R31: best first")
    assert ei.value.code == "ROUTE_POOL_MOVE_HAS_DECISIONS" and "already has 1 decision(s)" in ei.value.message
    assert service.get_route(db, WATCH).pool == "WATCHLIST"


def test_bite_a_budget_listing_the_route_counts_as_a_decision(db):
    _seeded(db)
    db.add(Decision(kind="BUDGET", actor_user_id="owner", actor_kind="HUMAN", target_type="model_budget",
                    target_id="opencode-go", domain="PERSONAL", rationale="plan fee", auth_scope={"routes": [WATCH]}))
    db.commit()
    with pytest.raises(ValidationFailed) as ei:
        service.set_route_pool(db, WATCH, pool="WORKER", rationale="owner decision R31: best first")
    assert ei.value.code == "ROUTE_POOL_MOVE_HAS_DECISIONS"


def test_bite_a_go_route_that_is_not_candidate_is_refused(db):
    _seeded(db)
    service.get_route(db, WATCH).status = "TRIAL"
    db.commit()
    with pytest.raises(ValidationFailed) as ei:
        service.set_route_pool(db, WATCH, pool="WORKER", rationale="owner decision R31: best first")
    assert ei.value.code == "ROUTE_POOL_MOVE_NOT_CANDIDATE" and "is TRIAL" in ei.value.message


@pytest.mark.parametrize("key,pool", [(WATCH, "ARENA"), ("opencode-zen/minimax-m3", "WORKER"),
                                      ("opencode-go/glm-5.3-flash", "LOCAL")])
def test_other_moves_stay_unsupported(db, key, pool):
    _seeded(db)
    with pytest.raises(ValidationFailed) as ei:
        service.set_route_pool(db, key, pool=pool, rationale="trying a move that is not allowed")
    assert ei.value.code == "ROUTE_POOL_MOVE_UNSUPPORTED"


def test_api_moves_a_go_route_and_audits_it(admin_client, db):
    _seeded(db)
    r = admin_client.post(f"/api/routes/{WATCH}/pool", json={"pool": "WORKER", "rationale": "owner decision R31"})
    assert r.status_code == 200, r.text
    assert r.json()["route"]["pool"] == "WORKER"
    r = admin_client.post(f"/api/routes/{WATCH}/pool", json={"pool": "WATCHLIST", "rationale": "owner moves it back"})
    assert r.status_code == 200, r.text
    assert db.query(AuditEvent).filter_by(action="route.set_pool", target_id=WATCH).count() == 2


# ---------------------------------------------------------------------------------------------- tool-call probe


def _go_route(db, key: str = GO, *, status: str = "TRIAL") -> ModelRoute:
    r = ModelRoute(route_key=key, pool="WORKER", provider="opencode-go", model_id=key, status=status, is_free="NO",
                   endpoint=GO_OPENAI_BASE, eligible_domains=["PERSONAL"], allowed_data_classes=["PUBLIC", "INTERNAL"],
                   pricing=_go_pricing(15, {"input": 1.0, "output": 2.7}), pricing_checked_at=utcnow(),
                   mode_params={"credential_ref": "workers/opencode-go", "upstream_model": key.split("/", 1)[1],
                                "upstream_api": "openai.chat_completions", "reasoning": "PROVIDER_DEFAULT"})
    db.add(r)
    db.commit()
    return r


def _activate(db, route: ModelRoute) -> None:
    budget = Decision(kind="BUDGET", actor_user_id="owner", actor_kind="HUMAN", target_type="model_budget",
                      target_id=route.route_key, domain="PERSONAL", rationale="plan fee for the trial",
                      auth_scope={"routes": [route.route_key]}, allowed_operations=["budget.set"])
    db.add(budget)
    db.flush()
    route.config_hash = service.compute_config_hash(route)
    db.add(Decision(kind="TRIAL", actor_user_id="owner", actor_kind="HUMAN", target_type="model_route",
                    target_id=route.route_key, target_hash=route.config_hash, domain="PERSONAL",
                    rationale="owner trial after reading terms", allowed_operations=["route.use"],
                    details={"budget_decision_id": budget.id}))
    db.commit()


@pytest.fixture
def key(secrets_memory):
    secrets_memory.put("workers/opencode-go", GO_KEY, actor="test")


def _answer(message: dict) -> dict:
    return {"model": "step-5-preview", "choices": [{"message": message, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 80, "completion_tokens": 20, "cost": 0,
                      "opencode": {"allowance": 0.00013, "billing_source": "go"}}}


def _call(name: str = feasibility.TOOL_PROBE_FUNCTION, args: str = '{"status": "OK"}', **extra) -> dict:
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": name, "arguments": args},
                            **extra}]}


def _tool_reasons(db, route: ModelRoute) -> list[str]:
    reasons = service.route_rejections(db, route, domain="PERSONAL", data_classification="PUBLIC", needs_tools=True,
                                       min_context=None)
    return [x for x in reasons if "tool use not proven" in x]


def _probe(db, answer: dict) -> tuple[dict, dict]:
    with respx.mock(assert_all_mocked=True) as mock:
        sent = mock.post(GO_CHAT).mock(return_value=httpx.Response(200, json=answer))
        out = broker.probe_route(db, route_key=GO, actor="user:owner", tools=True)
        db.commit()
        assert len(mock.calls) == 1  # one request, no retry, no other route
    return out, json.loads(sent.calls[0].request.content)


def test_tool_probe_pass_makes_the_route_tool_capable_without_touching_its_configuration(db, key):
    route = _go_route(db)
    _activate(db, route)
    stored, computed = route.config_hash, service.compute_config_hash(route)
    assert not routing.tool_capable(db, route) and _tool_reasons(db, route)  # no evidence: not tool-capable
    out, sent = _probe(db, _answer(_call()))
    assert [t["function"]["name"] for t in sent["tools"]] == [feasibility.TOOL_PROBE_FUNCTION]
    assert sent["max_tokens"] == feasibility.TOOL_PROBE_MAX_TOKENS and sent["tool_choice"] == "auto"
    assert sent["messages"] == [{"role": "user", "content": feasibility.TOOL_PROBE_PROMPT}]
    assert out["tool_call"]["verdict"] == "PASS" and out["probe"]["data_class"] == "PUBLIC"
    assert GO_KEY not in json.dumps(out)
    db.expire_all()
    route = service.get_route(db, GO)
    assert route.supports_tools == "UNKNOWN" and route.status == "TRIAL"  # never written by the probe
    assert (route.config_hash, service.compute_config_hash(route)) == (stored, computed)
    assert routing.tool_capable(db, route) and _tool_reasons(db, route) == []
    ev = db.get(EvidenceArtifact, out["evidence_id"])
    assert ev.kind == "PROBE" and ev.trusted and ev.evidence_level == "E3"
    assert (ev.environment["probe"], ev.environment["verdict"], ev.environment["config_hash"]) == (
        "TOOL_CALL", "PASS", computed)
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "broker.route_tool_probe")).result == "SUCCESS"
    assert db.scalar(select(Event).where(Event.kind == "route.tool_probe", Event.entity_id == GO)) is not None


def test_bite_evidence_of_an_older_configuration_is_irrelevant(db, key):
    route = _go_route(db)
    _activate(db, route)
    _probe(db, _answer(_call()))
    assert routing.tool_capable(db, route)
    route.context_window = 1_000_000  # a CONFIG_FIELD: a new configuration hash
    db.commit()
    assert not routing.tool_capable(db, route)
    assert _tool_reasons(db, route) and "probe-route --tools opencode-go/step-5-preview" in _tool_reasons(db, route)[0]


@pytest.mark.parametrize("message,reason", [
    ({"role": "assistant", "content": "OK"}, "no tool call in the answer"),
    (_call(name="other_function"), "names a function that was not defined"),
    (_call(args='{"status": "NO"}'), "arguments are not the JSON object"),
    (_call(args="not json"), "arguments are not the JSON object"),
    ({"role": "assistant", "tool_calls": [{"type": "function", "function": {
        "name": feasibility.TOOL_PROBE_FUNCTION, "arguments": '{"status": "OK"}'}}]}, "malformed tool call"),
])
def test_bite_a_fail_records_evidence_and_never_makes_the_route_tool_capable(db, key, message, reason):
    route = _go_route(db)
    _activate(db, route)
    out, _ = _probe(db, _answer(message))
    assert out["tool_call"]["verdict"] == "FAIL" and reason in out["tool_call"]["reason"]
    ev = db.get(EvidenceArtifact, out["evidence_id"])
    assert ev.environment["verdict"] == "FAIL" and "FAIL" in ev.claim
    assert not routing.tool_capable(db, service.get_route(db, GO))
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "broker.route_tool_probe")).result == "FAILED"


def test_a_rate_limited_tool_probe_is_a_fail(db, key):
    route = _go_route(db)
    _activate(db, route)
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(GO_CHAT).mock(return_value=httpx.Response(429, json={"error": "limit"}))
        out = broker.probe_route(db, route_key=GO, actor="user:owner", tools=True)
        db.commit()
    assert out["outcome"] == "RATE_LIMITED" and out["tool_call"]["verdict"] == "FAIL"
    assert not routing.tool_capable(db, route)


def test_bite_a_later_answered_fail_revokes_an_earlier_pass(db, key):
    """The newest answer the model gave decides (owner review 2026-10-28): a model that stops calling tools after a
    PASS is no longer tool-capable under the same configuration."""
    route = _go_route(db)
    _activate(db, route)
    _probe(db, _answer(_call()))
    assert routing.tool_capable(db, route)
    _probe(db, _answer({"role": "assistant", "content": "OK"}))
    assert not routing.tool_capable(db, route), "a later answered FAIL left the route tool-capable"


def test_a_later_unanswered_probe_keeps_an_earlier_pass(db, key):
    """Near miss: a rate limit says nothing about tool use, so it neither grants nor revokes."""
    route = _go_route(db)
    _activate(db, route)
    _probe(db, _answer(_call()))
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(GO_CHAT).mock(return_value=httpx.Response(429, json={"error": "limit"}))
        out = broker.probe_route(db, route_key=GO, actor="user:owner", tools=True)
        db.commit()
    assert out["outcome"] == "RATE_LIMITED"
    assert routing.tool_capable(db, route), "a rate-limited probe revoked an earlier PASS"


def test_bite_untrusted_evidence_with_pass_marks_never_counts(db, key):
    route = _go_route(db)
    _activate(db, route)
    env = {"probe": "TOOL_CALL", "verdict": "PASS", "config_hash": service.compute_config_hash(route)}
    verification.collect_evidence(db, kind="PROBE", origin="worker", collector_identity=routing.TOOL_PROBE_COLLECTOR,
                                  claim="worker says PASS", evidence_level="E3", subject_type="model_route",
                                  subject_id=GO, domain="PERSONAL", classification="PUBLIC", environment=env)
    db.commit()
    assert not routing.tool_capable(db, route)


def test_supports_tools_yes_is_tool_capable_without_a_probe(db):
    route = _go_route(db)
    route.supports_tools = "YES"
    db.commit()
    assert routing.tool_capable(db, route)


def test_bite_tool_probe_is_refused_for_a_candidate_and_for_a_paid_route(db, key, secrets_memory):
    _go_route(db, status="CANDIDATE")
    with respx.mock(assert_all_mocked=True) as mock:
        with pytest.raises(PolicyDenied) as ei:
            broker.probe_route(db, route_key=GO, actor="user:owner", tools=True)
        assert not mock.calls
    assert ei.value.code == "ROUTE_NOT_APPROVED"
    secrets_memory.put("workers/openrouter", "sk-or-v1-" + "q" * 48, actor="test")
    paid = ModelRoute(route_key="worker.paid-pro", pool="WORKER", provider="openrouter", model_id="vendor/pro",
                      status="TRIAL", is_free="NO", eligible_domains=["PERSONAL"], allowed_data_classes=["PUBLIC"],
                      pricing={"input_per_mtok": 1.0, "output_per_mtok": 2.0}, pricing_checked_at=utcnow(),
                      mode_params={"credential_ref": "workers/openrouter"})
    db.add(paid)
    db.commit()
    _activate(db, paid)
    with respx.mock(assert_all_mocked=True) as mock:
        with pytest.raises(PolicyDenied) as ei:
            broker.probe_route(db, route_key="worker.paid-pro", actor="user:owner", tools=True)
        assert not mock.calls  # a per-token paid route: no budget path for a probe
    assert ei.value.code == "ROUTE_NOT_FREE"
    assert db.scalar(select(EvidenceArtifact).where(EvidenceArtifact.subject_id == "worker.paid-pro")) is None


def test_the_plain_feasibility_probe_never_counts_as_a_tool_probe(db, key):
    route = _go_route(db)
    _activate(db, route)
    with respx.mock(assert_all_mocked=True) as mock:
        sent = mock.post(GO_CHAT).mock(return_value=httpx.Response(200, json=_answer(_call())))
        out = broker.probe_route(db, route_key=GO, actor="user:owner")
        db.commit()
    assert "tools" not in json.loads(sent.calls[0].request.content) and "tool_call" not in out
    assert not routing.tool_capable(db, route)


def test_cli_and_api_tool_probe(admin_client, db, key):
    from aeos.core.enums import Domain

    route = _go_route(db)
    _activate(db, route)
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(GO_CHAT).mock(return_value=httpx.Response(200, json=_answer(_call(name="x"))))
        res = runner.invoke(app, ["broker", "probe-route", "--tools", GO])
        assert res.exit_code == 0, res.output
        assert "tool call: FAIL" in res.output and res.output.isascii() and GO_KEY not in res.output
        mock.post(GO_CHAT).mock(return_value=httpx.Response(200, json=_answer(_call())))
        admin_client.select_domain(Domain.PERSONAL)
        r = admin_client.post(f"/api/broker/routes/{GO}/tool-probe")
        assert r.status_code == 200, r.text
        assert r.json()["tool_call"]["verdict"] == "PASS"
        admin_client.select_domain(Domain.OFFICE)
        r = admin_client.post(f"/api/broker/routes/{GO}/tool-probe")
        assert r.status_code == 403 and r.json()["error"]["code"] == "OFFICE_DEFAULT_DENY"
        assert len(mock.calls) == 2
    db.expire_all()
    assert routing.tool_capable(db, service.get_route(db, GO))
