"""Owner decision R34 of 2026-10-28 (ADR-0129): the full OpenCode Go registry, the Console-translation candidates on
chat-completions, the paid OpenRouter DeepSeek 1017 route in the role ranking, and AGENTS.md invariant 6 (no Claude
family model off the native bridge, no meta-router) through one check in seed, catalog, eligibility and ranking.

Bite tests: each check has a violating case that is red with its own message and a near miss that is green. The
upstream is a respx mock (no network, no real provider call, no real key)."""

from __future__ import annotations

import copy
import importlib.util
import json
from types import ModuleType

import httpx
import pytest
import respx
from sqlalchemy import select

from aeos.broker import facade as broker
from aeos.broker import feasibility
from aeos.core.errors import PolicyDenied
from aeos.core.model_policy import invariant6_refusal, model_family_refusal
from aeos.core.paths import repo_root
from aeos.core.timeutil import utcnow
from aeos.models.decisions import Decision
from aeos.models.routing import ModelRoute
from aeos.routing import catalog, seed, service
from aeos.routing.seed import GO_OPENAI_BASE, GO_TRANSLATION_STATEMENT

ROOT = repo_root()
GO_CHAT = f"{GO_OPENAI_BASE}/chat/completions"
GO_KEY = "go-test-" + "t" * 40  # visibly fake
ARGON = "opencode-go/gemini-4-argon"
PAID_1017 = "openrouter/deepseek/deepseek-v4.1-flash-1017"
TRANSLATION = {"gemini-4-argon", "qwen3.8-max", "qwen3.8-flash", "qwen3.7-plus", "minimax-m3"}


def _ranking_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("role_route_ranking", ROOT / "scripts" / "role_route_ranking.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------------------------- the one check
@pytest.mark.parametrize("model,family", [
    ("opencode-go/claude-haiku-5-5", "claude"),          # the Go Claude model
    ("claude-haiku-5-5", "claude"),
    ("anthropic/claude-opus-5.5", "claude"),
    ("openrouter/anthropic/claude-sonnet-5.5:batch", "claude"),
    ("openrouter/~anthropic/claude-sonnet-latest", "claude"),
    ("us.anthropic.claude-opus-5-5", "claude"),
    ("openrouter/openrouter/auto", "meta-router"),
    ("openrouter/auto", "meta-router"),
    ("openrouter/fusion", "meta-router"),
    ("openrouter/pareto-code", "meta-router"),
    ("~deepseek/deepseek-flash-latest", "meta-router"),  # a floating alias: the provider picks the build
    ("typesafe/jev-router", "meta-router"),
])
def test_bite_claude_family_and_meta_router_ids_are_refused(model, family):
    assert model_family_refusal(model) == family
    why = invariant6_refusal(model, provider="openrouter")
    assert why and why.startswith(f"{model} is a ") and "AGENTS.md invariant 6" in why


@pytest.mark.parametrize("model", [
    "opencode-go/gemini-4-argon", "opencode-go/qwen3.8-max", "opencode-go/minimax-m3", "opencode-go/deepseek-v4-pro",
    PAID_1017, "deepseek/deepseek-v4.1-flash", "openrouter/openrouter/stealth-alpha:free",  # a model, not a router
    "openrouter/poolside/laguna-s-2.1:free", "z-ai/autoglm-phone", "local/qwen3.8-flash-next",
])
def test_near_miss_real_models_are_allowed(model):
    assert model_family_refusal(model) is None and invariant6_refusal(model) is None


def test_the_native_bridge_is_the_one_allowed_claude_path():
    assert invariant6_refusal("claude-native/opus", "claude-opus-5-5", provider="claude-native") is None
    why = invariant6_refusal("openrouter/claude-opus-5-5", "claude-opus-5-5", provider="openrouter")
    assert why and "Claude-family model" in why and "native claude binary" in why


# ---------------------------------------------------------------------------------------------- seed
def test_r34_routes_are_seeded_as_worker_candidates_with_their_terms(db):
    seed.run(db)
    db.flush()
    rows = {r.route_key: r for r in db.scalars(select(ModelRoute).where(ModelRoute.provider == "opencode-go"))}
    for model in TRANSLATION:
        r = rows[f"opencode-go/{model}"]
        assert (r.pool, r.status, r.supports_tools, r.endpoint, r.mode_params["upstream_api"],
                r.mode_params["upstream_model"]) == ("WORKER", "CANDIDATE", "UNKNOWN", GO_OPENAI_BASE,
                                                     "openai.chat_completions", model)
        assert r.data_terms["serving"] == GO_TRANSLATION_STATEMENT and GO_TRANSLATION_STATEMENT in r.notes
        assert r.data_terms["may_use_inputs_for_training"] is False and r.allowed_data_classes == ["PUBLIC", "INTERNAL"]
    pro = rows["opencode-go/deepseek-v4-pro"]
    assert pro.allowed_data_classes == ["PUBLIC"] and "Global region" in pro.data_terms["regions"]
    assert "renewed monthly" in pro.data_terms["retention"] and pro.pricing["allowance_usd_month"] == 15
    hy4 = rows["opencode-go/hy4-preview"]
    assert (hy4.pool, hy4.allowed_data_classes, hy4.pricing["allowance_usd_month"]) == ("WORKER", ["PUBLIC", "INTERNAL"],
                                                                                       30)
    workers = sorted((r.priority, r.route_key) for r in db.scalars(select(ModelRoute).where(ModelRoute.pool == "WORKER")))
    assert [k for _p, k in workers[-2:]] == ["opencode-go/mimo-v2.5", "opencode-go/mimo-v2.5-pro"], \
        "the superseded MiMo V2.5 pair is the lowest priority of the WORKER pool (overflow only)"
    assert rows["opencode-go/qwen3.7-plus"].pricing["context_tier_rate_per_mtok"]["from_context_tokens"] == 256000


def test_bite_seeding_a_claude_or_meta_router_route_is_refused(db, monkeypatch):
    real = seed.seed_routes
    bad_go = seed._go("claude-haiku-5-5", "Claude Haiku 5.5", pool="WORKER", priority=99,
                      pricing=seed._go_pricing(15, {"input": 0.1, "output": 0.5}))
    bad_router = {**copy.deepcopy(real()[-1]), "route_key": "openrouter/openrouter/auto", "provider": "openrouter",
                  "model_id": "openrouter/auto"}
    for bad, fragment in ((bad_go, "opencode-go/claude-haiku-5-5 is a Claude-family model"),
                          (bad_router, "openrouter/openrouter/auto is a meta-router")):
        monkeypatch.setattr(seed, "seed_routes", lambda bad=bad: [*real(), bad])
        with pytest.raises(PolicyDenied) as exc:
            seed.run(db)
        assert exc.value.code == "MODEL_FAMILY_REFUSED" and fragment in exc.value.message
        assert db.scalar(select(ModelRoute).where(ModelRoute.route_key == bad["route_key"])) is None
    # near miss: a real Go model seeded the same way
    near = seed._go("minimax-m3", "MiniMax M3", pool="WORKER", priority=99,
                    pricing=seed._go_pricing(60, {"input": 0.3, "output": 1.2}))
    assert seed.invariant6_problems([*real(), near]) == []
    assert seed.invariant6_problems(real()) == []  # the native supervisor routes stay allowed


# ---------------------------------------------------------------------------------------------- catalog discovery
@respx.mock
def test_bite_catalog_discovery_never_registers_a_claude_or_router_free_model(db):
    seed.run(db)
    db.commit()
    data = {"data": [
        {"id": "anthropic/claude-haiku-5.5:free", "name": "synthetic", "context_length": 1000,
         "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"]},
        {"id": "openrouter/auto:free", "name": "synthetic", "context_length": 1000,
         "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"]},
        {"id": "nvidia/nemotron-3.5-lightning:free", "name": "near miss", "context_length": 1000,
         "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"]},
    ]}
    respx.get(catalog.OPENROUTER_MODELS_URL).mock(return_value=httpx.Response(200, json=data))
    with httpx.Client() as c:
        out = catalog.refresh_openrouter_catalog(db, c)
    assert out["refused_model_family"] == ["openrouter/anthropic/claude-haiku-5.5:free", "openrouter/openrouter/auto:free"]
    assert out["discovered"] == ["openrouter/nvidia/nemotron-3.5-lightning:free"]
    keys = {r.route_key for r in db.scalars(select(ModelRoute))}
    assert "openrouter/anthropic/claude-haiku-5.5:free" not in keys and "openrouter/openrouter/auto:free" not in keys


# ---------------------------------------------------------------------------------------------- eligibility
def _route(db, key: str, *, provider: str = "opencode-go", status: str = "TRIAL") -> ModelRoute:
    model = key.split("/", 1)[1]
    r = ModelRoute(route_key=key, pool="WORKER", provider=provider, model_id=key, status=status, is_free="NO",
                   endpoint=GO_OPENAI_BASE, eligible_domains=["PERSONAL"], allowed_data_classes=["PUBLIC", "INTERNAL"],
                   pricing=seed._go_pricing(15, {"input": 1.0, "output": 2.7}), pricing_checked_at=utcnow(),
                   mode_params={"credential_ref": "workers/opencode-go", "upstream_model": model,
                                "upstream_api": "openai.chat_completions", "reasoning": "PROVIDER_DEFAULT"})
    db.add(r)
    db.commit()
    return r


def test_bite_a_claude_registry_row_is_never_eligible(db):
    bad = _route(db, "opencode-go/claude-haiku-5-5")
    near = _route(db, "opencode-go/glm-5.3-flash-x")
    reasons = service.route_rejections(db, bad, domain="PERSONAL", data_classification="PUBLIC", needs_tools=False,
                                       min_context=None)
    assert any(r.startswith("opencode-go/claude-haiku-5-5 is a Claude-family model") for r in reasons), reasons
    near_reasons = service.route_rejections(db, near, domain="PERSONAL", data_classification="PUBLIC",
                                            needs_tools=False, min_context=None)
    assert not [r for r in near_reasons if "invariant 6" in r]


# ---------------------------------------------------------------------------------------------- translation probe
def _activate(db, route: ModelRoute) -> None:
    budget = Decision(kind="BUDGET", actor_user_id="owner", actor_kind="HUMAN", target_type="model_budget",
                      target_id=route.route_key, domain="PERSONAL", rationale="plan fee for the trial",
                      auth_scope={"routes": [route.route_key]}, allowed_operations=["budget.set"])
    db.add(budget)
    db.flush()
    route.status = "TRIAL"
    route.config_hash = service.compute_config_hash(route)
    db.add(Decision(kind="TRIAL", actor_user_id="owner", actor_kind="HUMAN", target_type="model_route",
                    target_id=route.route_key, target_hash=route.config_hash, domain="PERSONAL",
                    rationale="owner trial after reading terms", allowed_operations=["route.use"],
                    details={"budget_decision_id": budget.id}))
    db.commit()


def test_a_translation_candidate_is_probed_on_chat_completions_with_its_bare_id(db, secrets_memory):
    """No new broker code: the seeded Argon route is called like every Go route (the plain and the --tools probe go
    to the chat-completions endpoint with the bare model id); whether Go translates it is what the live probe shows."""
    secrets_memory.put("workers/opencode-go", GO_KEY, actor="test")
    seed.run(db)
    db.commit()
    route = service.get_route(db, ARGON)
    _activate(db, route)
    answer = {"model": "gemini-4-argon", "choices": [{"message": {
        "role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {
            "name": feasibility.TOOL_PROBE_FUNCTION, "arguments": '{"status": "OK"}'}}]}, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 80, "completion_tokens": 20, "cost": 0,
                  "opencode": {"allowance": 0.0004, "billing_source": "go"}}}
    with respx.mock(assert_all_mocked=True) as mock:
        sent = mock.post(GO_CHAT).mock(return_value=httpx.Response(200, json=answer))
        out = broker.probe_route(db, route_key=ARGON, actor="user:owner", tools=True)
        db.commit()
    body = json.loads(sent.calls[0].request.content)
    assert body["model"] == "gemini-4-argon" and out["tool_call"]["verdict"] == "PASS"
    assert GO_KEY not in json.dumps(out)
    assert service.get_route(db, ARGON).supports_tools == "UNKNOWN"  # the probe never writes the claim


# ---------------------------------------------------------------------------------------------- ranking
def test_go_deepseek_is_scored_by_its_served_build_only_where_releases_are_comparable():
    mod = _ranking_script()
    data = json.loads(mod.DATA.read_text(encoding="utf-8"))
    go_ds = "opencode-go/deepseek-v4.1-flash"
    assert data["served_builds"][go_ds]["release"] == "2026-10-14"
    for agent, want in (("coder", 90.2), ("analyst", 79.3), ("reasoner", 86.7), ("researcher", 86.8),
                        ("designer", 79.2)):
        e = next(x for x in data["agents"][agent]["entries"] if x["route_key"] == go_ds)
        assert e["score"] == want and e["source_refs"][0]["release"] == "2026-10-14", agent
    la = next(x for x in data["agents"]["agentic-coder"]["entries"] if x["route_key"] == go_ds)
    assert la["score"] is None and "not comparable with 2026-10-25 in Long Agentic" in la["note"]
    # the 1017 score goes to the paid OpenRouter route, the last tier (escalation and overflow only, never PUBLIC)
    for agent in data["agents"].values():
        last = agent["entries"][-1]
        assert (last["route_key"], last["tier"]) == (PAID_1017, "OPENROUTER_PAID") and "never for PUBLIC" in \
            last["paid_use"]
    assert next(x for x in data["agents"]["coder"]["entries"] if x["route_key"] == PAID_1017)["score"] == 96.0


def test_bite_comparability_is_red_when_a_shared_model_differs_and_green_when_all_agree():
    mod = _ranking_script()
    inputs = json.loads(mod.INPUTS.read_text(encoding="utf-8"))
    assert mod.comparable(inputs, "Coding") == (True, [])
    ok, differ = mod.comparable(inputs, "Long Agentic")
    assert not ok and "kimi-k3.1-pro" in differ
    edited = copy.deepcopy(inputs)
    row = next(r for r in edited["rows"][mod.R1014] if r["model"] == "kimi-k3.1-pro")
    row["scores"]["Coding"] += 1.0
    assert mod.comparable(edited, "Coding") == (False, ["kimi-k3.1-pro"])
    refs, notes = mod._served_builds(edited, "Coding")
    assert refs == {} and "not comparable with 2026-10-25 in Coding" in notes["opencode-go/deepseek-v4.1-flash"]


def test_translation_entries_carry_the_seed_statement_and_superseded_routes_come_last():
    mod = _ranking_script()
    assert mod.TRANSLATION_STATEMENT == GO_TRANSLATION_STATEMENT
    assert set(mod.GO_TRANSLATION) == TRANSLATION
    data = json.loads(mod.DATA.read_text(encoding="utf-8"))
    for agent in data["agents"].values():
        by_key = {e["route_key"]: e for e in agent["entries"]}
        for model in TRANSLATION:
            assert by_key[f"opencode-go/{model}"]["serving"] == GO_TRANSLATION_STATEMENT
        go = [e["route_key"] for e in agent["entries"] if e["tier"] == "OPENCODE_GO"]
        assert go[-2:] == ["opencode-go/mimo-v2.5", "opencode-go/mimo-v2.5-pro"]
        assert "overflow_only" in by_key["opencode-go/mimo-v2.5"]
    # best first: Argon leads Long Agentic (61.2), above the best route the ranking had before (kimi-k3.1-pro 39.1)
    assert data["agents"]["agentic-coder"]["preferred_routes"][0] == ARGON


def test_bite_the_ranking_refuses_a_claude_or_meta_router_route(monkeypatch):
    mod = _ranking_script()
    inputs = json.loads(mod.INPUTS.read_text(encoding="utf-8"))
    monkeypatch.setitem(mod.GO_ALLOWANCE, "claude-haiku-5-5", 15)
    with pytest.raises(ValueError, match=r"route opencode-go/claude-haiku-5-5 is refused: claude is never listed"):
        mod.build_data(inputs, "sha")
    monkeypatch.delitem(mod.GO_ALLOWANCE, "claude-haiku-5-5")
    monkeypatch.setitem(mod.OPENROUTER_PAID, "openrouter/openrouter/auto", "router")
    with pytest.raises(ValueError, match=r"route openrouter/openrouter/auto is refused: meta-router is never listed"):
        mod.build_data(inputs, "sha")
    monkeypatch.delitem(mod.OPENROUTER_PAID, "openrouter/openrouter/auto")
    # near miss: an allowed Go model added to the list is ranked, not refused
    monkeypatch.setitem(mod.GO_ALLOWANCE, "glm-5.4-test", 15)
    prefs = mod.build_data(inputs, "sha")["agents"]["coder"]["preferred_routes"]
    assert "opencode-go/glm-5.4-test" in prefs
