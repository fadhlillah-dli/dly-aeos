"""OpenRouter/OpenCode catalog refresh and key info (§23, §42; §50 'Upstream release baru terdeteksi',
'Binary/model route/config berganti dari approved pin', 'Free quota habis / 429'). respx only — no
network, no paid API, no real credentials."""

from __future__ import annotations

import copy
import subprocess

import httpx
import pytest
import respx

from aeos.models.cost import QuotaState
from aeos.models.routing import ModelRoute
from aeos.models.routing_ext import CatalogRefresh
from aeos.routing import catalog, seed, service
from tests.unit.test_routing_fakes import human_decision, install_fakes

LAGUNA = "openrouter/poolside/laguna-s-2.1:free"

# Trimmed from the public catalog (GET /api/v1/models, 2026-10-18); the stealth entry is synthetic.
CATALOG = {"data": [
    {"id": "poolside/laguna-s-2.1:free", "name": "Poolside: Laguna S 2.1 (free)", "context_length": 262144,
     "pricing": {"prompt": "0", "completion": "0"}, "expiration_date": "2026-10-31",
     "top_provider": {"context_length": 262144, "max_completion_tokens": 32768, "is_moderated": False},
     "supported_parameters": ["include_reasoning", "max_tokens", "reasoning", "temperature", "tool_choice", "tools"]},
    {"id": "nvidia/nemotron-3.5-lightning:free", "name": "NVIDIA: Nemotron 3.5 Lightning (free)",
     "context_length": 1000000, "pricing": {"prompt": "0", "completion": "0"}, "expiration_date": None,
     "top_provider": {"is_moderated": False}, "supported_parameters": ["max_tokens", "tools", "tool_choice"]},
    {"id": "openrouter/stealth-alpha:free", "name": "Stealth Alpha (free)", "context_length": 128000,
     "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["max_tokens"]},
    {"id": "deepseek/deepseek-v4.1-flash", "name": "DeepSeek: DeepSeek V4.1 Flash", "context_length": 1048576,
     "pricing": {"prompt": "0.00000005", "completion": "0.0000006"}, "supported_parameters": ["tools"]},
]}
KEY_INFO = {"data": {"label": "sk-or-v1-abc...f53", "limit": None, "limit_remaining": None, "usage": 0.0,
                     "usage_daily": 0, "is_free_tier": True, "is_provisioning_key": False,
                     "rate_limit": {"requests": 20, "interval": "10s"}}}


@pytest.fixture
def routes(db):
    seed.run(db)
    db.commit()
    return db


def _vault_down() -> None:
    respx.route(url__startswith="http://127.0.0.1:8200").mock(side_effect=httpx.ConnectError("vault down"))


@respx.mock
def test_catalog_updates_known_and_discovers_free_candidates(routes):
    respx.get(catalog.OPENROUTER_MODELS_URL).mock(return_value=httpx.Response(200, json=CATALOG))
    before = service.get_route(routes, LAGUNA).config_hash
    with httpx.Client() as c:
        out = catalog.refresh_openrouter_catalog(routes, c)
    assert out["status"] == "OK" and LAGUNA in out["updated"] and out["config_changed"] == []
    lag = service.get_route(routes, LAGUNA)
    assert lag.config_hash == before  # unchanged catalog keeps the identity (approvals stay bound)
    assert lag.availability == "AVAILABLE" and lag.pricing_checked_at is not None and lag.context_window == 262144
    assert lag.supports_tools == "YES" and lag.free_until.date().isoformat() == "2026-10-31"
    assert lag.terms_checked_at is None  # the catalog is not a data-terms review
    new = service.get_route(routes, "openrouter/nvidia/nemotron-3.5-lightning:free")
    assert (new.status, new.mode, new.origin, new.evidence_level) == ("CANDIDATE", "LIVE", "CATALOG_DISCOVERED", "E1")
    assert new.eligible_domains == ["PERSONAL"] and new.allowed_data_classes == ["PUBLIC"] and new.is_free == "YES"
    stealth = service.get_route(routes, "openrouter/openrouter/stealth-alpha:free")
    assert stealth.identity_status == "UNVERIFIED" and stealth.supports_tools == "NO"
    assert routes.query(ModelRoute).filter(ModelRoute.status.in_(["APPROVED", "TRIAL"])).count() == 0
    assert "openrouter/deepseek/deepseek-v4.1-flash" not in out["discovered"]  # paid models never auto-registered


@respx.mock
def test_price_change_is_config_change_and_invalidates(routes, monkeypatch):
    reg = install_fakes(monkeypatch)
    lag = service.get_route(routes, LAGUNA)
    lag.status = "APPROVED"
    human_decision(routes, kind="ROUTE_ACTIVATION", target_type="model_route", target_id=LAGUNA,
                   target_hash=lag.config_hash)
    paid = copy.deepcopy(CATALOG)
    paid["data"][0]["pricing"] = {"prompt": "0.00000009", "completion": "0.00000018"}
    respx.get(catalog.OPENROUTER_MODELS_URL).mock(return_value=httpx.Response(200, json=paid))
    with httpx.Client() as c:
        out = catalog.refresh_openrouter_catalog(routes, c)
    assert LAGUNA in out["config_changed"]
    assert lag.is_free == "NO" and lag.config_changed_at is not None
    assert any(i["invalidator"] == "config:routes" for i in reg.invalidations)
    assert reg.invalidated_decisions and reg.invalidated_decisions[0][0] == LAGUNA


@respx.mock
def test_missing_from_catalog_marks_unavailable(routes):
    respx.get(catalog.OPENROUTER_MODELS_URL).mock(return_value=httpx.Response(200, json={"data": CATALOG["data"][1:]}))
    with httpx.Client() as c:
        out = catalog.refresh_openrouter_catalog(routes, c)
    assert LAGUNA in out["missing"]
    lag = service.get_route(routes, LAGUNA)
    reasons = service.route_rejections(routes, lag, domain="PERSONAL", data_classification="PUBLIC", needs_tools=True,
                                       min_context=None)
    assert any("missing from the provider catalog" in r for r in reasons)


@respx.mock
def test_key_info_without_key_is_unknown(routes):
    _vault_down()
    route = respx.get(catalog.OPENROUTER_KEY_URL).mock(return_value=httpx.Response(200, json=KEY_INFO))
    with httpx.Client() as c:
        out = catalog.refresh_openrouter_key_info(routes, c)
    assert out["status"] == "UNKNOWN" and not route.called
    q = routes.query(QuotaState).filter_by(provider="openrouter", scope="credits").one()
    assert q.state == "UNKNOWN" and q.source == "no-key"


@respx.mock
def test_key_info_parsed_label_never_stored(routes, secrets_memory):
    fake_key = "or-test-" + "k" * 24
    secrets_memory.put("workers/openrouter", fake_key, actor="test")
    route = respx.get(catalog.OPENROUTER_KEY_URL).mock(return_value=httpx.Response(200, json=KEY_INFO))
    with httpx.Client() as c:
        out = catalog.refresh_openrouter_key_info(routes, c)
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {fake_key}"
    assert out["status"] == "OK" and out["is_free_tier"] is True and out["documented_free_daily_limit"] == 50
    credits = routes.query(QuotaState).filter_by(provider="openrouter", scope="credits").one()
    daily = routes.query(QuotaState).filter_by(provider="openrouter", scope="free-daily").one()
    assert credits.state == "OK" and credits.source == "official-key-endpoint"
    assert daily.state == "UNKNOWN" and daily.details["documented_daily_limit"] == 50
    blob = str(credits.details) + str(daily.details) + str(out)
    assert "abc...f53" not in blob and fake_key not in blob


@respx.mock
def test_key_info_fallback_ref_rejected_key_and_exhausted_credits(routes, secrets_memory):
    secrets_memory.put("personal/openrouter", "or-personal-" + "p" * 20, actor="test")
    respx.get(catalog.OPENROUTER_KEY_URL).mock(return_value=httpx.Response(401, json={"error": "no"}))
    with httpx.Client() as c:
        out = catalog.refresh_openrouter_key_info(routes, c)
    assert out["status"] == "FAILED" and out["secret_ref"] == "personal/openrouter"
    exhausted = {"data": {**KEY_INFO["data"], "limit": 5.0, "usage": 5.0, "limit_remaining": 0, "is_free_tier": False}}
    respx.get(catalog.OPENROUTER_KEY_URL).mock(return_value=httpx.Response(200, json=exhausted))
    with httpx.Client() as c:
        catalog.refresh_openrouter_key_info(routes, c)
    credits = routes.query(QuotaState).filter_by(provider="openrouter", scope="credits").one()
    assert credits.state == "EXHAUSTED"
    lag = service.get_route(routes, LAGUNA)
    assert service.quota_exhausted(routes, lag) is not None  # excluded from routing


def test_opencode_catalog_unknown_without_binary(routes, settings, monkeypatch):
    monkeypatch.setattr(catalog, "_opencode_binary", lambda s: None)
    out = catalog.refresh_opencode_catalog(routes, settings)
    assert out["status"] == "UNKNOWN"
    assert service.get_route(routes, "opencode-zen/deepseek-v4.1-flash-free").availability == "UNKNOWN"


def test_opencode_catalog_parsed(routes, settings, monkeypatch):
    listing = "opencode/deepseek-v4.1-flash-free\nopencode/glm-5.3-flash-free\nopenrouter/x/y\n"
    monkeypatch.setattr(catalog, "_opencode_binary", lambda s: "opencode")
    monkeypatch.setenv("OPENCODE_API_KEY", "oc-" + "k" * 30)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-" + "k" * 40)
    seen: dict = {}

    def fake_run(argv, **kw):
        seen.update(kw, argv=argv)
        return subprocess.CompletedProcess(argv, 0, stdout=listing, stderr="")

    monkeypatch.setattr(catalog.subprocess, "run", fake_run)
    out = catalog.refresh_opencode_catalog(routes, settings)
    # list args + timeout, scrubbed env (no provider keys), neutral cwd (never the controller's repo)
    import tempfile
    from pathlib import Path

    assert seen["argv"] == ["opencode", "models"] and seen["timeout"] and "shell" not in seen
    assert "OPENCODE_API_KEY" not in seen["env"] and "OPENROUTER_API_KEY" not in seen["env"]
    assert Path(seen["cwd"]).resolve() == Path(tempfile.gettempdir()).resolve()
    assert out["status"] == "OK" and "opencode-zen/deepseek-v4.1-flash-free" in out["available"]
    assert "opencode-zen/beam-free" in out["missing"]
    assert catalog.parse_opencode_models(listing) == {"opencode/deepseek-v4.1-flash-free", "opencode/glm-5.3-flash-free",
                                                      "openrouter/x/y"}


@respx.mock
def test_refresh_all_isolates_failures_and_records_history(routes, settings, monkeypatch):
    monkeypatch.setattr(catalog, "_opencode_binary", lambda s: None)
    _vault_down()
    respx.get(catalog.OPENROUTER_MODELS_URL).mock(side_effect=httpx.ConnectError("offline"))
    with httpx.Client() as c:
        row = catalog.refresh_all(routes, actor="test", settings=settings, client=c)
    assert row.status == "FAILED" or row.status == "PARTIAL"
    assert row.results["openrouter_catalog"]["status"] == "FAILED"
    assert row.results["openrouter_key"]["status"] == "UNKNOWN"
    assert row.results["opencode_go_catalog"]["status"] == "UNKNOWN"  # no Go key: nothing requested
    assert routes.query(CatalogRefresh).count() == 1
    assert service.get_route(routes, LAGUNA).pricing_checked_at.year == 2026  # unchanged seed timestamp


# ------------------------------------------------------------------- OpenCode Go (ADR-0041 amendment 2026-10-26)

GO_GLM = "opencode-go/glm-5.3-flash"
GO_HY3 = "opencode-go/hy3"
GO_KEY = "go-test-" + "c" * 40  # visibly fake
# The documented Go model list (GET /inference/go/v1/models, OpenAI list shape); trimmed and synthetic, not a reading.
GO_LIST = {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "opencode"} for m in (
    "glm-5.3-flash", "mimo-v2.6-flash", "minimax-m3.1-flash", "kimi-k2.7-code", "glm-5.2", "minimax-m3")]}


def _go_rows(db) -> dict[str, ModelRoute]:
    return {r.route_key: r for r in db.query(ModelRoute).filter(ModelRoute.provider == "opencode-go")}


@respx.mock
def test_go_catalog_without_a_key_is_unknown_and_requests_nothing(routes, secrets_memory):
    listing = respx.get(catalog.OPENCODE_GO_MODELS_URL).mock(return_value=httpx.Response(200, json=GO_LIST))
    with httpx.Client() as c:
        out = catalog.refresh_opencode_go_catalog(routes, c)
    assert out["status"] == "UNKNOWN" and not listing.called
    assert out["remedy"] == "aeos secrets set workers/opencode-go" and "workers/opencode-go" in out["reason"]
    rows = _go_rows(routes)
    assert rows and all(r.availability == "UNKNOWN" for r in rows.values())
    reasons = service.route_rejections(routes, rows[GO_GLM], domain="PERSONAL", data_classification="PUBLIC",
                                       needs_tools=False, min_context=None)
    assert any("availability UNKNOWN" in x and "aeos routes refresh" in x for x in reasons)


@respx.mock
def test_go_catalog_marks_listed_routes_available_and_absent_ones_missing(routes, secrets_memory):
    secrets_memory.put("workers/opencode-go", GO_KEY, actor="test")
    before = {k: r.config_hash for k, r in _go_rows(routes).items()}
    listing = respx.get(catalog.OPENCODE_GO_MODELS_URL).mock(return_value=httpx.Response(200, json=GO_LIST))
    with httpx.Client() as c:
        out = catalog.refresh_opencode_go_catalog(routes, c)
    assert listing.calls.last.request.headers["Authorization"] == f"Bearer {GO_KEY}"
    assert out["status"] == "OK" and out["listed"] == 6 and GO_KEY not in str(out)
    rows = _go_rows(routes)
    assert {GO_GLM, "opencode-go/kimi-k2.7-code", "opencode-go/glm-5.2"} <= set(out["available"])
    assert {GO_HY3, "opencode-go/deepseek-v4.1-flash"} <= set(out["missing"])
    assert rows[GO_GLM].availability == "AVAILABLE" and rows[GO_GLM].availability_checked_at is not None
    assert rows[GO_HY3].availability == "MISSING"
    # availability is an observation, not part of the identity; nothing is activated, Zen rows are not touched
    assert {k: r.config_hash for k, r in rows.items()} == before
    assert all(r.status == "CANDIDATE" for r in rows.values())
    assert service.get_route(routes, "opencode-zen/deepseek-v4.1-flash-free").availability == "UNKNOWN"


@respx.mock
@pytest.mark.parametrize("answer", [httpx.Response(401, json={"error": "no"}), httpx.Response(403, json={}),
                                    httpx.Response(500, text="oops"), httpx.Response(200, json={"models": []}),
                                    httpx.Response(200, text="not json"), httpx.ConnectError("offline")])
def test_go_catalog_rejected_key_or_unreachable_list_is_unknown_never_missing(routes, secrets_memory, answer):
    secrets_memory.put("workers/opencode-go", GO_KEY, actor="test")
    glm = _go_rows(routes)[GO_GLM]
    glm.availability = "AVAILABLE"  # an earlier observation does not survive a list that could not be read
    mock = respx.get(catalog.OPENCODE_GO_MODELS_URL)
    if isinstance(answer, Exception):
        mock.mock(side_effect=answer)
    else:
        mock.mock(return_value=answer)
    with httpx.Client() as c:
        out = catalog.refresh_opencode_go_catalog(routes, c)
    assert out["status"] == "UNKNOWN" and "availability UNKNOWN" in out["reason"] and GO_KEY not in str(out)
    assert all(r.availability == "UNKNOWN" for r in _go_rows(routes).values())
