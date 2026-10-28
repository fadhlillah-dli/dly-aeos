"""Routing hard filters, selection and fallback (§23, §42, §43; §50 'Free quota habis / 429',
'Model alias/provider route berubah', 'Budget DB/gateway tidak sehat', 'Trial disetujui, promotion
belum disetujui', 'Baseline/candidate/target berubah setelah approval', 'Katalog ... belum diprobe')."""

from __future__ import annotations

from datetime import timedelta

import pytest

from aeos.core.errors import Blocked, Conflict, ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.models.planning import TaskSpec
from aeos.models.routing import ModelRoute
from aeos.routing import seed, service
from aeos.routing.facade import eligible_routes, fallback_route, note_rate_limited, select_route
from tests.unit.test_routing_fakes import (
    OWNER_CLIENT_TERMS,
    accept_client_terms,
    human_decision,
    install_fakes,
)

# routing keeps a route whose worker key is not stored out of eligibility: these tests store fake keys
pytestmark = pytest.mark.usefixtures("worker_keys")

DS = "opencode-zen/deepseek-v4.1-flash-free"
GLM = "opencode-zen/glm-5.3-flash-free"
GEMINI = "gemini/gemini-3.8-flash-free-tier"
PICKLE = "opencode-zen/big-pickle"
LAGUNA = "openrouter/poolside/laguna-s-2.1:free"
QWEN = "opencode-zen/qwen3.8-flash"
KIMI = "opencode-zen/kimi-k2.7-code"  # paid, chat-completions API (QWEN: anthropic.messages, never served)
# OpenCode Go (owner decision A, ADR-0041 amendment 2026-10-26): subscription WORKER candidates, no client restriction
GO_WORKERS = ("glm-5.3-flash", "mimo-v2.6-flash", "minimax-m3.1-flash", "hy3", "kimi-k2.7-code")
GO_WATCH = ("longcat-2.0", "glm-5.2", "glm-5.3", "kimi-k3.1-pro", "mimo-v2.6-pro", "deepseek-v4.1-flash")
GO_GLM = "opencode-go/glm-5.3-flash"
GO_HY3 = "opencode-go/hy3"
GO_DS = "opencode-go/deepseek-v4.1-flash"


@pytest.fixture
def routes(db):
    seed.run(db)
    db.commit()
    return db


def _fresh(r: ModelRoute) -> None:
    """What an owner terms review + a catalog refresh record (none of it is part of the identity)."""
    r.pricing_checked_at = r.terms_checked_at = utcnow()
    r.availability, r.availability_checked_at = "AVAILABLE", utcnow()


def bind_runtime(r: ModelRoute) -> None:
    """What an activation binds: the runtime in force (policy version, worker prompt, context strategy)."""
    runtime = service.runtime_context()
    for f in service.binding_fields(r):
        setattr(r, f, runtime[f])
    if service.runs_on_worker_runtime(r):
        r.harness_version = runtime.get("harness_version") or r.harness_version


def approve(db, key: str, *, kind: str = "ROUTE_ACTIVATION", details: dict | None = None, expires_at=None,
            actor_kind: str = "HUMAN") -> ModelRoute:
    from aeos.models.decisions import Decision

    r = service.get_route(db, key)
    _fresh(r)
    bind_runtime(r)
    accept_client_terms(r)  # the owner's S07 client-terms decision an activation of a free Zen route records
    r.status = "APPROVED" if kind == "ROUTE_ACTIVATION" else "TRIAL"
    r.config_hash = service.compute_config_hash(r)
    for old in db.query(Decision).filter_by(target_type="model_route", target_id=key, kind=kind, status="ACTIVE"):
        old.status = "SUPERSEDED"
    human_decision(db, kind=kind, target_type="model_route", target_id=key, target_hash=r.config_hash,
                   details=details, expires_at=expires_at, actor_kind=actor_kind)
    db.flush()
    return r


def task(domain="PERSONAL", classification="INTERNAL", role="feature-implementer", tools=None, **policy) -> TaskSpec:
    return TaskSpec(domain=domain, data_classification=classification, role_key=role,
                    tools_allowed=list(tools) if tools is not None else ["fs.read", "fs.write:allowed_paths"],
                    model_route_policy=policy, task_key="t1", title="t", goal="g", project_id="p",
                    plan_version_id="pv", plan_version_hash="h", spec_hash="s")


def test_seed_is_design_candidate_personal_only(routes):
    rows = routes.query(ModelRoute).all()
    assert len(rows) >= 20
    assert all(r.status == "CANDIDATE" and r.mode == "DESIGN" and r.origin == "SEED" for r in rows)
    assert all("OFFICE" not in (r.eligible_domains or []) for r in rows)
    assert all(r.resolved_version is None and r.config_hash and r.source_refs for r in rows)
    assert all(r.config_hash == service.compute_config_hash(r) for r in rows)  # stable identity after flush
    pools = {r.pool for r in rows}
    assert pools == {"SUPERVISOR", "WORKER", "COMPARATOR", "ARENA", "WATCHLIST", "LOCAL"}
    by = {r.route_key: r for r in rows}
    assert by["claude-native/fable"].model_id == "claude-fable-5-1" and by["claude-native/fable"].alias == "fable"
    assert by["claude-native/opus"].model_id == "claude-opus-5-5" and by["claude-native/opus"].alias == "opus"
    assert by[DS].is_free == "YES" and by[DS].evidence_level == "E1"
    assert by[GEMINI].allowed_data_classes == ["PUBLIC"]
    assert by[LAGUNA].model_id == "poolside/laguna-s-2.1:free" and "2026-10-31" in by[LAGUNA].expires_note
    for k in (PICKLE, "opencode-zen/space-bunny-free", "opencode-zen/ll-alpha-free", "opencode-zen/exo-free"):
        assert by[k].identity_status == "UNVERIFIED" and by[k].priority >= 900
    assert by[QWEN].pool == "WATCHLIST" and by[QWEN].is_free == "NO"
    assert by["local/qwen3.8-flash-next"].endpoint is None
    assert seed.run(routes)["created"] == 0  # idempotent


SEEDED = {
    "SUPERVISOR": {"claude-native/fable", "claude-native/opus"},
    "WORKER": {DS, GLM, *(f"opencode-go/{m}" for m in GO_WORKERS)},
    "COMPARATOR": {GEMINI},
    "ARENA": {"opencode-zen/longcat-2.5-preview-free", "opencode-zen/mimo-v2.6-flash-free", "opencode-zen/beam-free",
              "opencode-zen/nemotron-3-ultra-free", "opencode-zen/nemotron-3.5-lightning-free",
              "opencode-zen/nemotron-3.5-super-vl-free", LAGUNA, "opencode-zen/space-bunny-free",
              "opencode-zen/ll-alpha-free", "opencode-zen/exo-free", PICKLE},
    "WATCHLIST": {QWEN, *(f"opencode-zen/{m}" for m in ("kimi-k2.7-code", "minimax-m3", "mistral-large-4",
                                                       "gpt-5.3-codex", "grok-build-0.1", "muse-spark-1.3")),
                  *(f"opencode-go/{m}" for m in GO_WATCH)},
    "LOCAL": {"local/qwen3.8-flash-next"},
}


def test_seed_registers_every_section_23_candidate_with_its_terms(routes):
    """§23 'Registry berisi semua kandidat yang relevan': the exact candidate set per pool, and the per-route
    data terms the hard filter relies on (a widened MiMo or a Nemotron without its trial flag fails here)."""
    by = {r.route_key: r for r in routes.query(ModelRoute).all()}
    assert {k: {key for key, r in by.items() if r.pool == k} for k in SEEDED} == SEEDED
    assert len(by) == sum(len(v) for v in SEEDED.values()) == 35
    for key in SEEDED["ARENA"] | SEEDED["WORKER"] | SEEDED["WATCHLIST"] | SEEDED["COMPARATOR"]:
        r = by[key]
        # a research reading of a provider claim is not an owner terms review (§23, §51 P2)
        assert r.terms_checked_at is None and "not an owner terms review" in (r.data_terms or {}).get(
            "read_for_research", "not an owner terms review"), key
    assert by["claude-native/fable"].terms_checked_at is not None  # supervisor: owner's own account terms
    assert by["opencode-zen/longcat-2.5-preview-free"].allowed_data_classes == ["PUBLIC", "INTERNAL"]
    assert by["opencode-zen/longcat-2.5-preview-free"].data_terms["may_use_inputs_for_training"] is False
    mimo = by["opencode-zen/mimo-v2.6-flash-free"]
    assert mimo.allowed_data_classes == ["PUBLIC"] and mimo.data_terms["may_use_inputs_for_training"] is True
    beam = by["opencode-zen/beam-free"]
    assert beam.allowed_data_classes == ["PUBLIC"] and "regions" in beam.data_terms and service.region_restricted(beam)
    for key in ("opencode-zen/nemotron-3-ultra-free", "opencode-zen/nemotron-3.5-lightning-free",
                "opencode-zen/nemotron-3.5-super-vl-free"):
        assert by[key].data_terms["trial_only_no_confidential"] is True and by[key].allowed_data_classes == ["PUBLIC"]
        assert service.terms_restriction(by[key].data_terms, by[key].identity_status)
    assert all(by[k].is_free == "NO" for k in SEEDED["WATCHLIST"])
    # Zen watchlist terms are UNKNOWN (PUBLIC only); Go states no training / 0 days (DeepSeek: ZDR expiring, PUBLIC)
    assert all(by[k].allowed_data_classes == ["PUBLIC"] for k in SEEDED["WATCHLIST"] if k.startswith("opencode-zen/"))
    assert by[GO_DS].allowed_data_classes == ["PUBLIC"] and by["opencode-go/glm-5.2"].allowed_data_classes == [
        "PUBLIC", "INTERNAL"]
    assert by[DS].tool_set == list(service.REVIEWED_WORKER_TOOLS)  # a reviewable candidate tool set, no placeholder
    assert by["local/qwen3.8-flash-next"].mode_params == {"quantization": "UNKNOWN", "inference_backend": "UNKNOWN",
                                                          "hardware": "UNKNOWN"}


def test_seeded_go_routes_are_subscription_candidates_without_a_client_restriction(routes):
    """Owner decision A (ADR-0041 amendment 2026-10-26, E1 Go docs read 2026-10-26): Go WORKER candidates, PERSONAL
    only, a subscription with dollar allowances (never a per-token price), Go allows other clients, the reviewed
    region is required, and the free Zen pilots keep their S07 client restriction."""
    by = {r.route_key: r for r in routes.query(ModelRoute).filter(ModelRoute.provider == "opencode-go")}
    assert set(by) == {f"opencode-go/{m}" for m in GO_WORKERS + GO_WATCH}
    for key, r in by.items():
        assert (r.status, r.mode, r.origin, r.evidence_level) == ("CANDIDATE", "DESIGN", "SEED", "E1"), key
        assert r.eligible_domains == ["PERSONAL"] and r.is_free == "NO" and service.needs_budget(r), key
        assert r.endpoint == seed.GO_OPENAI_BASE and r.model_id == key and r.fallback_chain == [], key
        assert r.mode_params["credential_ref"] == "workers/opencode-go", key
        assert r.mode_params["upstream_model"] == key.split("/", 1)[1], key
        assert r.mode_params["upstream_api"] == "openai.chat_completions", key
        p = r.pricing
        assert (p["billing"], p["plan"], p["monthly_fee_usd"]) == ("SUBSCRIPTION", "OpenCode Go", 10), key
        assert p["allowance_windows"] == {"5h": 0.2, "week": 0.5, "month": 1.0} and p["allowance_usd_month"] > 0, key
        assert "input_per_mtok" not in p and "output_per_mtok" not in p, key  # never read as a metered price
        assert r.pricing_checked_at is not None and r.terms_checked_at is None, key
        t = r.data_terms
        assert t["may_use_inputs_for_training"] is False and "0 days" in t["retention"], key
        assert service.client_restriction(r) is None and service.client_restriction_problem(r) is None, key
        assert "any agent" in t["client_permission"]["statement"], key
        assert service.region_restricted(r) and "Extra Usage" in t["provider_paid_fallback"], key
    glm = by[GO_GLM]
    assert glm.pricing["allowance_usd_month"] == 60 and glm.pricing["allowance_rate_per_mtok"] == {
        "input": 0.15, "output": 0.50, "cached_read": 0.03}
    assert by["opencode-go/minimax-m3.1-flash"].pricing["allowance_usd_month"] == 20
    assert by["opencode-go/kimi-k3.1-pro"].pricing["allowance_usd_month"] == 15
    # tool calling per models.dev (E1); hy3 states none, so it stays UNKNOWN and never serves tool-using tasks
    assert {m: by[f"opencode-go/{m}"].supports_tools for m in GO_WORKERS} == {
        "glm-5.3-flash": "YES", "mimo-v2.6-flash": "YES", "minimax-m3.1-flash": "YES", "hy3": "UNKNOWN",
        "kimi-k2.7-code": "YES"}
    assert all(by[f"opencode-go/{m}"].supports_tools == "UNKNOWN" for m in GO_WATCH)
    # ranked above the client-restricted Zen pilots, glm-5.3-flash first
    zen = {k: service.get_route(routes, k) for k in (DS, GLM)}
    assert min(by[f"opencode-go/{m}"].priority for m in GO_WORKERS) == glm.priority
    assert max(by[f"opencode-go/{m}"].priority for m in GO_WORKERS) < min(r.priority for r in zen.values())
    assert all(service.client_restriction_problem(r) for r in zen.values())  # the Zen pilots stay restricted
    ds = by[GO_DS]
    assert ds.pool == "WATCHLIST" and "2026-10-31" in ds.data_terms["retention"] and "Global" in ds.data_terms["regions"]
    assert "2026-10-31" in ds.expires_note


def test_go_worker_route_needs_budget_region_and_tools(routes):
    """A Go WORKER route is eligible only with its BUDGET decision and reviewed region; it then ranks above an
    approved Zen pilot. hy3 has no documented tool support: never selected for tool-using work."""
    r = service.get_route(routes, GO_GLM)
    approve(routes, GO_GLM)
    d = eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL")
    assert GO_GLM in d.rejected and any("subscription route (OpenCode Go, $10/month plan fee" in x
                                        for x in d.rejected[GO_GLM])
    assert any("region not reviewed" in x for x in d.rejected[GO_GLM])
    assert not any("client restriction" in x for x in d.rejected[GO_GLM])
    budget = human_decision(routes, kind="BUDGET", target_type="model_budget", target_id="opencode-go",
                            auth_scope={"routes": [GO_GLM, GO_HY3]})
    r.region = "EU"
    approve(routes, GO_GLM, details={"budget_decision_id": budget.id})
    approve(routes, DS)
    d = eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL")
    assert d.eligible == [GO_GLM, DS]
    assert select_route(routes, task=task()).route.route_key == GO_GLM
    hy3 = service.get_route(routes, GO_HY3)
    hy3.region = "EU"
    approve(routes, GO_HY3, details={"budget_decision_id": budget.id})
    d = eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL")
    assert GO_HY3 not in d.eligible and any("tool support UNKNOWN" in x for x in d.rejected[GO_HY3])
    assert GO_HY3 in eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL",
                                     needs_tools=False).eligible
    budget.status = "INVALIDATED"
    routes.flush()
    assert eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL").eligible == [DS]


def test_seed_resets_a_seeded_research_date_on_untouched_rows(routes):
    r = service.get_route(routes, DS)
    r.terms_checked_at = seed.RESEARCH_DATE
    r.tool_set = ["opencode-default"]
    r.config_hash = service.compute_config_hash(r)
    routes.flush()
    out = seed.run(routes)
    assert out["seeded_terms_check_reset"] >= 1 and r.terms_checked_at is None
    assert r.tool_set == list(service.REVIEWED_WORKER_TOOLS) and r.config_hash == service.compute_config_hash(r)


def test_nothing_is_eligible_by_default(routes):
    d = eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL")
    assert d.eligible == [] and all(any("not approved" in x for x in v) for v in d.rejected.values())
    with pytest.raises(Blocked) as ei:
        select_route(routes, task=task())
    assert ei.value.code == "NO_ELIGIBLE_ROUTE" and DS in ei.value.details["rejected"]


def test_approved_route_eligible_and_selected(routes):
    approve(routes, DS)
    d = eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL")
    assert d.eligible == [DS]
    sel = select_route(routes, task=task())
    assert sel.route.route_key == DS and "priority" in sel.reason


def test_office_always_denied_by_default(routes, monkeypatch, absent_modules, worker_keys):
    absent_modules("aeos.policy.office")
    r = approve(routes, DS)
    # even if someone had added OFFICE to the route, policy decides — and it is missing here
    r.eligible_domains = ["PERSONAL", "OFFICE"]
    # §26 (integration amendment, ADR-0041): an OFFICE call only ever uses an office/ credential
    r.mode_params = {"credential_ref": "office/opencode-zen"}
    service.refresh_config_hash(routes, r, reason="test")
    approve(routes, DS)
    d = eligible_routes(routes, domain="OFFICE", data_classification="INTERNAL")
    assert DS in d.rejected and any("office policy unavailable" in x for x in d.rejected[DS])
    reg = install_fakes(monkeypatch)
    d = eligible_routes(routes, domain="OFFICE", data_classification="INTERNAL")
    assert any("external_worker_allowed=false" in x for x in d.rejected[DS])
    assert any("approved_worker_routes" in x for x in d.rejected[DS])
    reg.office.update(external_worker_allowed=True, approved_worker_routes=[DS], office_authorization=True)
    # §01 U-03 / §51 P1: the company also has to approve the data class (approved_data_classes, UNDECIDED = deny)
    d = eligible_routes(routes, domain="OFFICE", data_classification="INTERNAL")
    assert d.eligible == [] and any("approved_data_classes" in x for x in d.rejected[DS])
    reg.office["approved_data_classes"] = ["PUBLIC"]
    assert eligible_routes(routes, domain="OFFICE", data_classification="INTERNAL").eligible == []
    reg.office["approved_data_classes"] = ["INTERNAL"]
    d = eligible_routes(routes, domain="OFFICE", data_classification="INTERNAL")  # the office key is not stored yet
    assert d.eligible == [] and any("credential office/opencode-zen not stored" in x for x in d.rejected[DS])
    worker_keys.put("office/opencode-zen", "fake-office-key-value", actor="test")
    assert eligible_routes(routes, domain="OFFICE", data_classification="INTERNAL").eligible == [DS]


def test_seeded_routes_never_office_eligible_even_with_company_policy(routes, monkeypatch):
    reg = install_fakes(monkeypatch)
    reg.office.update(external_worker_allowed=True, approved_worker_routes=[DS])
    approve(routes, DS)
    d = eligible_routes(routes, domain="OFFICE", data_classification="INTERNAL")
    assert any("not in eligible_domains" in x for x in d.rejected[DS])


def test_approval_bound_to_config_hash_and_not_inherited(routes):
    r = approve(routes, DS)
    assert eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL").eligible == [DS]
    r.resolved_version = "deepseek-v4.1-flash-1017"
    assert service.refresh_config_hash(routes, r, reason="resolved version observed")
    d = eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL")
    assert d.eligible == [] and any("config hash" in x for x in d.rejected[DS])


def test_trial_needs_active_trial_decision_and_expires(routes):
    approve(routes, DS, kind="TRIAL", expires_at=utcnow() + timedelta(days=3))
    assert eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL").eligible == [DS]
    approve(routes, GLM, kind="TRIAL", expires_at=utcnow() - timedelta(minutes=1))
    d = eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL")
    assert GLM in d.rejected and any("TRIAL without" in x for x in d.rejected[GLM])


def test_non_human_decision_never_counts(routes):
    approve(routes, DS, actor_kind="AGENT")
    assert eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL").eligible == []


def test_paid_route_requires_budget_decision(routes):
    approve(routes, KIMI)
    d = eligible_routes(routes, domain="PERSONAL", data_classification="PUBLIC", needs_tools=False, pool="WATCHLIST")
    assert KIMI in d.rejected and any("paid route" in x for x in d.rejected[KIMI])
    budget = human_decision(routes, kind="BUDGET", target_type="model_route", target_id=KIMI)
    approve(routes, KIMI, details={"budget_decision_id": budget.id})
    d = eligible_routes(routes, domain="PERSONAL", data_classification="PUBLIC", needs_tools=False, pool="WATCHLIST")
    assert d.eligible == [KIMI]
    # S07 documents the anthropic.messages API for qwen3.8-flash: the broker never speaks it, so never eligible
    q_budget = human_decision(routes, kind="BUDGET", target_type="model_route", target_id=QWEN)
    approve(routes, QWEN, details={"budget_decision_id": q_budget.id})
    d = eligible_routes(routes, domain="PERSONAL", data_classification="PUBLIC", needs_tools=False, pool="WATCHLIST")
    assert any("ROUTE_API_UNSUPPORTED" in x for x in d.rejected[QWEN])
    budget.status = "INVALIDATED"
    routes.flush()
    d = eligible_routes(routes, domain="PERSONAL", data_classification="PUBLIC", needs_tools=False, pool="WATCHLIST")
    assert d.eligible == []


def test_comparator_public_only(routes):
    approve(routes, GEMINI)
    d = eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL", needs_tools=False, pool="COMPARATOR")
    assert any("INTERNAL not allowed" in x for x in d.rejected[GEMINI])
    d = eligible_routes(routes, domain="PERSONAL", data_classification="PUBLIC", needs_tools=False, pool="COMPARATOR")
    assert d.eligible == [GEMINI]


def test_unverified_identity_excluded_for_non_public(routes):
    r = approve(routes, PICKLE)
    r.allowed_data_classes = ["PUBLIC", "INTERNAL"]  # even if terms were widened by mistake
    approve(routes, PICKLE)
    d = eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL", pool="ARENA")
    assert any("identity UNVERIFIED" in x for x in d.rejected[PICKLE])
    assert PICKLE in eligible_routes(routes, domain="PERSONAL", data_classification="PUBLIC", pool="ARENA").eligible


def test_exhausted_quota_excluded_until_reset(routes):
    approve(routes, DS)
    q = note_rate_limited(routes, provider="opencode-zen", scope="free-daily", retry_after_seconds=600)
    d = eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL")
    assert any("quota EXHAUSTED" in x for x in d.rejected[DS])
    from aeos.models.cost import QuotaState

    row = routes.query(QuotaState).filter_by(provider="opencode-zen", scope="free-daily").one()
    assert row.id is not None and q is None
    row.resets_at = utcnow() - timedelta(seconds=1)
    routes.flush()
    assert eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL").eligible == [DS]


def test_context_tools_terms_mock_and_free_expiry_filters(routes):
    r = approve(routes, DS)
    d = eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL", min_context=64000)
    assert any("context window UNKNOWN" in x for x in d.rejected[DS])
    r.pricing_checked_at = utcnow() - timedelta(days=45)
    d = eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL")
    assert any("older than 30 days" in x for x in d.rejected[DS])
    lag = approve(routes, LAGUNA)
    lag.free_until = utcnow() - timedelta(days=1)
    service.refresh_config_hash(routes, lag, reason="free period ended")
    approve(routes, LAGUNA)
    d = eligible_routes(routes, domain="PERSONAL", data_classification="PUBLIC", pool="ARENA")
    assert any("free period expired" in x for x in d.rejected[LAGUNA])
    g = approve(routes, GEMINI)
    d = eligible_routes(routes, domain="PERSONAL", data_classification="PUBLIC", needs_tools=True, pool="COMPARATOR")
    assert any("tool support UNKNOWN" in x for x in d.rejected[GEMINI])
    g.mode = "MOCK"
    d = eligible_routes(routes, domain="PERSONAL", data_classification="PUBLIC", needs_tools=False, pool="COMPARATOR")
    assert any("fixtures are never routable" in x for x in d.rejected[GEMINI])


def test_selection_uses_current_eval_results_then_priority(routes):
    approve(routes, DS)
    glm = approve(routes, GLM)
    assert select_route(routes, task=task()).route.route_key == DS  # priority 10 < 20
    glm.eval_results = {"feature-implementer": {"score": 0.9, "config_hash": glm.config_hash, "runs": 12}}
    sel = select_route(routes, task=task())
    assert sel.route.route_key == GLM and "eval" in sel.reason
    glm.eval_results = {"feature-implementer": {"score": 0.9, "config_hash": "sha256:old", "runs": 12}}
    assert select_route(routes, task=task()).route.route_key == DS  # stale baseline not inherited
    assert select_route(routes, task=task(routes=[GLM])).route.route_key == GLM  # TaskSpec allow-list narrows


def test_supervisor_never_routed_to_workers(routes):
    with pytest.raises(Blocked) as ei:
        select_route(routes, task=task(pool="SUPERVISOR"))
    assert ei.value.code == "SUPERVISOR_NOT_ROUTABLE"


def test_fallback_only_to_eligible_free_same_pool(routes):
    approve(routes, DS)
    with pytest.raises(Blocked) as ei:
        fallback_route(routes, task=task(), failed_route_key=DS)  # GLM in chain but not approved
    assert ei.value.code == "NO_ELIGIBLE_FALLBACK"
    assert "not itself eligible" in str(ei.value.details["rejected"][GLM])
    approve(routes, GLM)
    assert fallback_route(routes, task=task(), failed_route_key=DS).route.route_key == GLM
    # a paid route in the chain is never an automatic fallback
    ds = service.get_route(routes, DS)
    ds.fallback_chain = [QWEN]
    with pytest.raises(Blocked):
        fallback_route(routes, task=task(), failed_route_key=DS)
    with pytest.raises(Blocked) as ei:
        fallback_route(routes, task=task(), failed_route_key="claude-native/fable")
    assert ei.value.code == "NO_SUPERVISOR_DOWNGRADE"


def test_primary_exhausted_follows_chain_only(routes):
    approve(routes, DS)
    approve(routes, GLM)
    note_rate_limited(routes, provider="opencode-zen", scope=DS, retry_after_seconds=600)
    sel = select_route(routes, task=task(primary=DS))
    assert sel.route.route_key == GLM and "fallback" in sel.reason
    note_rate_limited(routes, provider="opencode-zen", scope="free-daily", retry_after_seconds=600)
    with pytest.raises(Blocked) as ei:
        select_route(routes, task=task(primary=DS))
    assert ei.value.code == "NO_ELIGIBLE_ROUTE"


def test_invalid_inputs_rejected(routes):
    with pytest.raises(ValidationFailed):
        eligible_routes(routes, domain="", data_classification="INTERNAL")
    with pytest.raises(ValidationFailed):
        eligible_routes(routes, domain="PERSONAL", data_classification="SECRET")


def test_activation_records_hash_bound_decision(routes, monkeypatch):
    install_fakes(monkeypatch)
    r = service.get_route(routes, DS)
    _fresh(r)
    h = service.compute_config_hash(r)
    r.config_hash = h
    with pytest.raises(ValidationFailed) as ei:  # S07: free-tier Zen models require the OpenCode client
        service.activate_route(routes, DS, actor_user_id="owner", mode="TRIAL", rationale="pilot slice for self-build",
                               expected_config_hash=h)
    assert ei.value.code == "CLIENT_TERMS_DECISION_REQUIRED" and r.status == "CANDIDATE"
    route, decision = service.activate_route(routes, DS, actor_user_id="owner", mode="TRIAL",
                                             rationale="pilot slice for self-build", expected_config_hash=h,
                                             client_terms=OWNER_CLIENT_TERMS)
    assert route.status == "TRIAL" and decision.kind == "TRIAL" and decision.target_hash == route.config_hash
    assert decision.expires_at is not None and decision.domain == "PERSONAL"
    assert decision.details["client_terms"]["decision"] == service.CLIENT_TERMS_ACCEPTED
    assert eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL").eligible == [DS]


def test_activation_guards(routes, monkeypatch):
    install_fakes(monkeypatch)
    r = service.get_route(routes, DS)
    with pytest.raises(Conflict):
        service.activate_route(routes, DS, actor_user_id="o", mode="APPROVED", rationale="long enough rationale",
                               expected_config_hash="sha256:stale")
    q = service.get_route(routes, KIMI)
    with pytest.raises(ValidationFailed) as ei:
        service.activate_route(routes, KIMI, actor_user_id="o", mode="APPROVED", rationale="paid model trial please",
                               expected_config_hash=q.config_hash, terms_reviewed=True)
    assert ei.value.code == "BUDGET_DECISION_REQUIRED"
    q = service.get_route(routes, QWEN)  # S07: anthropic.messages API - the broker could never call it
    with pytest.raises(ValidationFailed) as ei:
        service.activate_route(routes, QWEN, actor_user_id="o", mode="APPROVED", rationale="paid model trial please",
                               expected_config_hash=q.config_hash, terms_reviewed=True)
    assert ei.value.code == "ROUTE_REQUEST_SHAPE_REFUSED"
    g = service.get_route(routes, GEMINI)
    with pytest.raises(ValidationFailed) as ei:
        service.activate_route(routes, GEMINI, actor_user_id="o", mode="APPROVED", rationale="compare internal docs",
                               expected_config_hash=g.config_hash, allowed_data_classes=["PUBLIC", "INTERNAL"],
                               terms_reviewed=True)
    assert ei.value.code == "DATA_TERMS_RESTRICTED"
    with pytest.raises(ValidationFailed):
        service.activate_route(routes, "claude-native/opus", actor_user_id="o", mode="APPROVED",
                               rationale="supervisor activation", expected_config_hash="x" * 20)
    r.eligible_domains = ["PERSONAL", "OFFICE"]
    r.config_hash = service.compute_config_hash(r)
    with pytest.raises(ValidationFailed):
        service.activate_route(routes, DS, actor_user_id="o", mode="APPROVED", rationale="office please now",
                               expected_config_hash=r.config_hash)


def test_activation_fails_closed_without_decision_registry(routes, absent_modules):
    absent_modules("aeos.policy.decisions")
    r = service.get_route(routes, DS)
    with pytest.raises(Blocked) as ei:
        service.activate_route(routes, DS, actor_user_id="o", mode="TRIAL", rationale="no policy module here",
                               expected_config_hash=r.config_hash, terms_reviewed=True, client_terms=OWNER_CLIENT_TERMS)
    assert ei.value.code == "DECISION_REGISTRY_UNAVAILABLE"
    assert r.status == "CANDIDATE" and r.terms_checked_at is None  # nothing applied when the decision cannot be recorded


def test_alias_resolution_change_is_a_new_configuration(routes, monkeypatch):
    reg = install_fakes(monkeypatch)
    assert not service.observe_supervisor_resolution(routes, route_key="claude-native/opus", requested="opus",
                                                     resolved="claude-opus-5-5")
    opus = service.get_route(routes, "claude-native/opus")
    assert opus.resolved_version == "claude-opus-5-5" and opus.identity_status == "VERIFIED"
    changed = service.observe_supervisor_resolution(routes, route_key="claude-native/opus", requested="opus",
                                                    resolved="claude-opus-5-6")
    assert changed and opus.config_changed_at is not None
    assert {"invalidator": "config:routes", "reason": opus.route_key + ": alias opus resolved claude-opus-5-5 -> claude-opus-5-6"} in reg.invalidations
    assert reg.invalidated_decisions and reg.invalidated_decisions[-1][0] == "claude-native/opus"


def test_disable_route_excludes_and_invalidates(routes, monkeypatch):
    reg = install_fakes(monkeypatch)
    approve(routes, DS)
    service.disable_route(routes, DS, reason="terms changed")
    d = eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL")
    assert any("DISABLED" in x for x in d.rejected[DS]) and reg.invalidated_decisions


def test_compiled_min_context_tokens_filters_routes(routes):
    """§19/§21: the task compiler writes model_route_policy.min_context_tokens; route selection honours it
    (the older spelling min_context keeps working)."""
    r = service.get_route(routes, DS)
    r.context_window = 65_536
    service.refresh_config_hash(routes, r, reason="test")
    approve(routes, DS)
    assert select_route(routes, task=task(min_context_tokens=32_000)).route.route_key == DS
    with pytest.raises(Blocked):
        select_route(routes, task=task(min_context_tokens=128_000))
    with pytest.raises(Blocked):
        select_route(routes, task=task(min_context=128_000))
