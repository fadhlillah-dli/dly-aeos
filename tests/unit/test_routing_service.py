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
from tests.unit.test_routing_fakes import human_decision, install_fakes

DS = "opencode-zen/deepseek-v4.1-flash-free"
GLM = "opencode-zen/glm-5.3-flash-free"
GEMINI = "gemini/gemini-3.8-flash-free-tier"
PICKLE = "opencode-zen/big-pickle"
LAGUNA = "openrouter/poolside/laguna-s-2.1:free"
QWEN = "opencode-zen/qwen3.8-flash"


@pytest.fixture
def routes(db):
    seed.run(db)
    db.commit()
    return db


def _fresh(r: ModelRoute) -> None:
    r.pricing_checked_at = r.terms_checked_at = utcnow()


def approve(db, key: str, *, kind: str = "ROUTE_ACTIVATION", details: dict | None = None, expires_at=None,
            actor_kind: str = "HUMAN") -> ModelRoute:
    from aeos.models.decisions import Decision

    r = service.get_route(db, key)
    _fresh(r)
    r.status = "APPROVED" if kind == "ROUTE_ACTIVATION" else "TRIAL"
    r.config_hash = service.compute_config_hash(r)
    for old in db.query(Decision).filter_by(target_type="model_route", target_id=key, kind=kind, status="ACTIVE"):
        old.status = "SUPERSEDED"
    human_decision(db, kind=kind, target_type="model_route", target_id=key, target_hash=r.config_hash,
                   details=details, expires_at=expires_at, actor_kind=actor_kind)
    db.flush()
    return r


def task(domain="PERSONAL", classification="INTERNAL", role="feature-implementer", **policy) -> TaskSpec:
    return TaskSpec(domain=domain, data_classification=classification, role_key=role, tools_allowed=["edit"],
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


def test_office_always_denied_by_default(routes, monkeypatch, absent_modules):
    absent_modules("aeos.policy.office")
    r = approve(routes, DS)
    # even if someone had added OFFICE to the route, policy decides — and it is missing here
    r.eligible_domains = ["PERSONAL", "OFFICE"]
    service.refresh_config_hash(routes, r, reason="test")
    approve(routes, DS)
    d = eligible_routes(routes, domain="OFFICE", data_classification="INTERNAL")
    assert DS in d.rejected and any("office policy unavailable" in x for x in d.rejected[DS])
    reg = install_fakes(monkeypatch)
    d = eligible_routes(routes, domain="OFFICE", data_classification="INTERNAL")
    assert any("external_worker_allowed=false" in x for x in d.rejected[DS])
    assert any("approved_worker_routes" in x for x in d.rejected[DS])
    reg.office.update(external_worker_allowed=True, approved_worker_routes=[DS])
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
    approve(routes, QWEN)
    d = eligible_routes(routes, domain="PERSONAL", data_classification="PUBLIC", needs_tools=False, pool="WATCHLIST")
    assert QWEN in d.rejected and any("paid route" in x for x in d.rejected[QWEN])
    budget = human_decision(routes, kind="BUDGET", target_type="model_route", target_id=QWEN)
    approve(routes, QWEN, details={"budget_decision_id": budget.id})
    d = eligible_routes(routes, domain="PERSONAL", data_classification="PUBLIC", needs_tools=False, pool="WATCHLIST")
    assert d.eligible == [QWEN]
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
    route, decision = service.activate_route(routes, DS, actor_user_id="owner", mode="TRIAL",
                                             rationale="pilot slice for self-build", expected_config_hash=h)
    assert route.status == "TRIAL" and decision.kind == "TRIAL" and decision.target_hash == route.config_hash
    assert decision.expires_at is not None and decision.domain == "PERSONAL"
    assert eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL").eligible == [DS]


def test_activation_guards(routes, monkeypatch):
    install_fakes(monkeypatch)
    r = service.get_route(routes, DS)
    with pytest.raises(Conflict):
        service.activate_route(routes, DS, actor_user_id="o", mode="APPROVED", rationale="long enough rationale",
                               expected_config_hash="sha256:stale")
    q = service.get_route(routes, QWEN)
    with pytest.raises(ValidationFailed) as ei:
        service.activate_route(routes, QWEN, actor_user_id="o", mode="APPROVED", rationale="paid model trial please",
                               expected_config_hash=q.config_hash)
    assert ei.value.code == "BUDGET_DECISION_REQUIRED"
    g = service.get_route(routes, GEMINI)
    with pytest.raises(ValidationFailed):
        service.activate_route(routes, GEMINI, actor_user_id="o", mode="APPROVED", rationale="compare internal docs",
                               expected_config_hash=g.config_hash, allowed_data_classes=["PUBLIC", "INTERNAL"])
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
                               expected_config_hash=r.config_hash)
    assert ei.value.code == "DECISION_REGISTRY_UNAVAILABLE"


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
