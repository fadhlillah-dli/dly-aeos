"""§23 "route produksi hanya menggunakan konfigurasi yang diizinkan dan telah diuji" and the ModelRoute
identity fields: APPROVED needs recorded test evidence of exactly the configuration, eval results reach
selection only through that owner decision, rollback targets, terms re-read at activation, unknown terms,
region/residency, availability, reviewed tool set, runtime binding (policy/prompt/context strategy/harness),
provider-scoped quota and the planner -> routing seam."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from aeos.core.errors import Blocked, ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.evaluation import facade as evaluation
from aeos.evaluation import service as evaluation_service
from aeos.models.decisions import Decision
from aeos.models.routing import ModelRoute
from aeos.routing import catalog, seed, service
from aeos.routing.facade import eligible_routes, note_rate_limited, select_route
from tests.unit.test_routing_fakes import install_fakes
from tests.unit.test_routing_service import DS, GLM, approve, task

LIMITS = {"max_attempts": 2, "max_minutes": 30, "max_cost_usd": 0}
BEAM = "opencode-zen/beam-free"
NEMO = "opencode-zen/nemotron-3.5-lightning-free"


@pytest.fixture
def world(db, monkeypatch):
    reg = install_fakes(monkeypatch)
    seed.run(db)
    evaluation_service.seed_suites(db)
    db.commit()
    return SimpleNamespace(db=db, reg=reg)


def observed(db, key: str) -> ModelRoute:
    """A catalog refresh observed the route (availability) - not part of the identity."""
    r = service.get_route(db, key)
    r.availability, r.availability_checked_at = "AVAILABLE", utcnow()
    db.flush()
    return r


def trial(db, key: str, **kw) -> ModelRoute:
    r = observed(db, key)
    route, _ = service.activate_route(db, key, actor_user_id="owner", mode="TRIAL", rationale="pilot slice trial run",
                                      expected_config_hash=r.config_hash, terms_reviewed=True, **kw)
    return route


def cfg_for(route: ModelRoute, **kw) -> dict:
    base = {"model_id": route.model_id, "model_version": "UNKNOWN", "provider": route.provider,
            "provider_route": route.route_key, "harness": "opencode", "harness_version": "UNKNOWN",
            "prompt_revision": route.prompt_revision or "UNKNOWN", "tools": list(route.tool_set or []),
            "policy_version": route.policy_version or "UNKNOWN", "context_params": {}}
    return base | kw


def recorded_run(db, route: ModelRoute, *, accepted=9, total=10, gates=None, samples=None, suite="eval.small-bugfix"):
    run = evaluation_service.plan_run(db, suite_key=suite, configuration=cfg_for(route), role_key=None, topology=None,
                                      repetitions=1, limits=LIMITS, actor_user_id="owner")
    rows = samples or [{"sample_id": f"s{i}", "status": "ACCEPTED" if i < accepted else "REJECTED",
                        "first_pass": i < accepted} for i in range(total)]
    evaluation.record_result(db, run.id, samples=rows, result_source="collector",
                             gate_results=gates or {"security": "PASS", "authority": "PASS", "evidence": "PASS"})
    db.flush()
    return run


# ---------------------------------------------------------------------------------- tested configurations


def test_approved_needs_test_evidence_of_exactly_this_configuration(world):
    db = world.db
    glm = trial(db, GLM)
    h = glm.config_hash
    assert glm.status == "TRIAL" and eligible_routes(db, domain="PERSONAL", data_classification="INTERNAL").eligible == [GLM]
    with pytest.raises(ValidationFailed) as ei:  # never tested -> never production
        service.activate_route(db, GLM, actor_user_id="owner", mode="APPROVED", rationale="promote to production",
                               expected_config_hash=h)
    assert ei.value.code == "TEST_EVIDENCE_REQUIRED" and "no EvalRun referenced" in str(ei.value.details["problems"])
    assert glm.status == "TRIAL" and glm.config_hash == h  # refused activation changes nothing on the route
    failed_gate = recorded_run(db, glm, gates={"security": "FAIL", "authority": "PASS", "evidence": "PASS"})
    regressed = recorded_run(db, glm, samples=[{"sample_id": "s1", "status": "ACCEPTED", "critical_regressions": 1}])
    planned = evaluation_service.plan_run(db, suite_key="eval.small-bugfix", configuration=cfg_for(glm), role_key=None,
                                          topology=None, repetitions=1, limits=LIMITS, actor_user_id="owner")
    for bad, why in ((failed_gate, "EXCLUDED_GATE_FAILED"), (regressed, "EXCLUDED_REGRESSION"),
                     (planned, "COMPLETED required")):
        with pytest.raises(ValidationFailed) as ei:
            service.activate_route(db, GLM, actor_user_id="owner", mode="APPROVED", rationale="promote to production",
                                   expected_config_hash=h, eval_run_ids=[bad.id])
        assert why in str(ei.value.details["problems"])
    good = recorded_run(db, glm, accepted=9, total=10)
    route, decision = service.activate_route(db, GLM, actor_user_id="owner", mode="APPROVED",
                                             rationale="promote to production", expected_config_hash=h,
                                             eval_run_ids=[good.id])
    assert route.status == "APPROVED" and decision.kind == "ROUTE_ACTIVATION" and decision.target_hash == h
    assert decision.details["eval_run_ids"] == [good.id]
    assert decision.details["rollback_target"]["action"] == "DISABLE_ROUTE"  # first approved configuration
    assert decision.details["previous_config_hash"] == h and decision.details["config_snapshot"]["route_key"] == GLM
    entry = route.eval_results["feature-implementer"]
    assert entry["config_hash"] == h and entry["decision_id"] == decision.id and entry["sample_size"] == 10
    assert 0 < entry["score"] < 0.9 and entry["score_basis"] == "wilson_lower_95"  # conservative: lower bound


def test_eval_result_drives_selection_only_after_the_decision_and_never_after_a_change(world):
    db = world.db
    approve(db, DS)  # priority 10, no evaluation (legacy/test approval)
    glm = trial(db, GLM)
    assert select_route(db, task=task()).route.route_key == DS  # priority basis
    run = recorded_run(db, glm, accepted=10, total=10)
    assert select_route(db, task=task()).route.route_key == DS  # a recorded result alone never changes routing
    service.activate_route(db, GLM, actor_user_id="owner", mode="APPROVED", rationale="promote to production",
                           expected_config_hash=glm.config_hash, eval_run_ids=[run.id])
    sel = select_route(db, task=task())
    assert sel.route.route_key == GLM and "eval" in sel.reason
    glm.resolved_version = "glm-5.3-flash-1101"  # provider changed the model behind the alias
    assert service.refresh_config_hash(db, glm, reason="resolved version observed")
    assert select_route(db, task=task()).route.route_key == DS  # new configuration: no inherited baseline, no approval
    assert service.eval_score(glm, "feature-implementer") is None
    from aeos.models.audit import Event

    needs = db.query(Event).filter_by(kind="route.needs_evaluation", entity_id=GLM).one()
    assert needs.payload["roles"] == ["feature-implementer"] and "EvalRun" in needs.payload["remedy"]


def test_evidence_from_another_configuration_of_the_route_is_not_inherited(world):
    db = world.db
    glm = trial(db, GLM)
    old_run = recorded_run(db, glm)
    glm.fallback_chain = []  # a reviewed fallback/context change is a new configuration (§41)
    service.refresh_config_hash(db, glm, reason="fallback chain changed")
    r = service.get_route(db, GLM)
    with pytest.raises(ValidationFailed) as ei:
        service.activate_route(db, GLM, actor_user_id="owner", mode="APPROVED", rationale="promote to production",
                               expected_config_hash=r.config_hash, eval_run_ids=[old_run.id])
    assert "route config hash differs" in str(ei.value.details["problems"])


def test_second_approved_configuration_records_the_previous_one_as_rollback_target(world):
    db = world.db
    glm = trial(db, GLM)
    first = recorded_run(db, glm)
    _, d1 = service.activate_route(db, GLM, actor_user_id="owner", mode="APPROVED", rationale="promote to production",
                                   expected_config_hash=glm.config_hash, eval_run_ids=[first.id])
    glm.context_window = 131072  # catalog-observed change -> new configuration
    service.refresh_config_hash(db, glm, reason="context window observed")
    glm = trial(db, GLM)
    second = recorded_run(db, glm)
    _, d2 = service.activate_route(db, GLM, actor_user_id="owner", mode="APPROVED", rationale="promote the new config",
                                   expected_config_hash=glm.config_hash, eval_run_ids=[second.id])
    rb = d2.details["rollback_target"]
    assert rb["action"] == "RESTORE_PREVIOUS_CONFIGURATION" and rb["decision_id"] == d1.id
    assert rb["config_hash"] == d1.target_hash and rb["config_snapshot_in_decision"] is True


# ---------------------------------------------------------------------------------- terms (§23, §45, §51 P2)


def test_seeded_route_needs_an_owner_terms_review_at_activation(world):
    db = world.db
    r = observed(db, DS)
    assert any("pricing/terms never checked" in x for x in service.route_rejections(
        db, r, domain="PERSONAL", data_classification="INTERNAL", needs_tools=True, min_context=None))
    with pytest.raises(ValidationFailed) as ei:
        service.activate_route(db, DS, actor_user_id="owner", mode="TRIAL", rationale="pilot slice trial run",
                               expected_config_hash=r.config_hash)
    assert ei.value.code == "TERMS_REVIEW_REQUIRED"
    route, d = service.activate_route(db, DS, actor_user_id="owner", mode="TRIAL", rationale="pilot slice trial run",
                                      expected_config_hash=r.config_hash, terms_reviewed=True,
                                      terms_note="Zen privacy page re-read: zero retention for free models")
    review = d.details["terms_review"]
    assert review["checked_by"] == "owner" and review["data_classes"] == ["PUBLIC", "INTERNAL"] and "Zen" in review["note"]
    assert route.terms_checked_at is not None and eligible_routes(db, domain="PERSONAL",
                                                                  data_classification="INTERNAL").eligible == [DS]


def test_unknown_or_trial_terms_allow_public_only_until_the_owner_records_them(world):
    db = world.db
    from aeos.routing.catalog import discovered_terms

    terms = discovered_terms("nvidia")  # a Nemotron discovered on OpenRouter keeps the family trial terms
    assert terms["trial_only_no_confidential"] is True and terms["may_use_inputs_for_training"] is None
    for key in (NEMO,):
        r = observed(db, key)
        with pytest.raises(ValidationFailed) as ei:
            service.activate_route(db, key, actor_user_id="owner", mode="TRIAL", rationale="arena trial for nemotron",
                                   expected_config_hash=r.config_hash, terms_reviewed=True,
                                   allowed_data_classes=["PUBLIC", "INTERNAL"])
        assert ei.value.code == "DATA_TERMS_RESTRICTED"
    disc = ModelRoute(route_key="openrouter/nvidia/nemotron-x:free", pool="ARENA", provider="openrouter",
                      model_id="nvidia/nemotron-x:free", harness="opencode", eligible_domains=["PERSONAL"],
                      allowed_data_classes=["PUBLIC"], data_terms=terms, availability="AVAILABLE", is_free="YES",
                      supports_tools="YES", mode="LIVE", origin="CATALOG_DISCOVERED")
    db.add(disc)
    db.flush()
    disc.config_hash = service.compute_config_hash(disc)
    with pytest.raises(ValidationFailed):
        service.activate_route(db, disc.route_key, actor_user_id="owner", mode="TRIAL", rationale="arena trial run",
                               expected_config_hash=disc.config_hash, terms_reviewed=True,
                               allowed_data_classes=["PUBLIC", "INTERNAL", "CONFIDENTIAL"])
    # UNKNOWN training terms (no family flag): restricted until the owner's own reading is recorded
    lag = observed(db, "openrouter/poolside/laguna-s-2.1:free")
    lag.free_until = utcnow() + timedelta(days=30)  # independent of the real end of the free period (2026-10-31)
    lag.config_hash = service.compute_config_hash(lag)  # a CANDIDATE row: nothing bound to its identity yet
    with pytest.raises(ValidationFailed) as ei:
        service.activate_route(db, lag.route_key, actor_user_id="owner", mode="TRIAL", rationale="arena trial run",
                               expected_config_hash=lag.config_hash, terms_reviewed=True,
                               allowed_data_classes=["PUBLIC", "INTERNAL"])
    assert "UNKNOWN" in ei.value.message
    route, _ = service.activate_route(
        db, lag.route_key, actor_user_id="owner", mode="TRIAL", rationale="arena trial run",
        expected_config_hash=lag.config_hash, terms_reviewed=True, allowed_data_classes=["PUBLIC", "INTERNAL"],
        terms_record={"may_use_inputs_for_training": False, "trial_only_no_confidential": False,
                      "note": "OpenRouter provider page: no training on prompts"})
    assert route.allowed_data_classes == ["PUBLIC", "INTERNAL"] and route.data_terms["owner_terms_record"] is True


def test_region_restricted_provider_needs_a_reviewed_region(world):
    db = world.db
    beam = observed(db, BEAM)
    with pytest.raises(ValidationFailed) as ei:
        service.activate_route(db, BEAM, actor_user_id="owner", mode="TRIAL", rationale="arena trial for beam",
                               expected_config_hash=beam.config_hash, terms_reviewed=True)
    assert ei.value.code == "REGION_REVIEW_REQUIRED"
    beam.data_terms = {**beam.data_terms, "unavailable_regions": ["CN"]}
    service.refresh_config_hash(db, beam, reason="terms re-read")
    with pytest.raises(ValidationFailed) as ei:
        service.activate_route(db, BEAM, actor_user_id="owner", mode="TRIAL", rationale="arena trial for beam",
                               expected_config_hash=beam.config_hash, terms_reviewed=True, region="cn")
    assert ei.value.code == "REGION_UNAVAILABLE"
    route, d = service.activate_route(db, BEAM, actor_user_id="owner", mode="TRIAL", rationale="arena trial for beam",
                                      expected_config_hash=beam.config_hash, terms_reviewed=True, region="ID")
    assert route.region == "ID" and d.details["region"] == "ID"
    assert BEAM in eligible_routes(db, domain="PERSONAL", data_classification="PUBLIC", pool="ARENA").eligible
    route.region = None  # e.g. edited away: the hard filter fails closed
    reasons = service.route_rejections(db, route, domain="PERSONAL", data_classification="PUBLIC", needs_tools=True,
                                       min_context=None)
    assert any("region not reviewed" in x for x in reasons)


def test_office_policy_allowed_regions_are_enforced():
    r = ModelRoute(route_key="x", pool="WORKER", provider="openrouter", model_id="m", region="US", data_terms={})
    office = {"external_worker_allowed": True, "approved_worker_routes": ["x"], "allowed_regions": ["ID"]}
    assert service.region_rejections(r, domain="OFFICE", office=office)
    r.region = "ID"
    assert service.region_rejections(r, domain="OFFICE", office=office) == []


# ---------------------------------------------------------------------------------- availability / tools / quota


def test_availability_never_observed_is_not_eligible(world):
    db = world.db
    r = approve(db, DS)
    r.availability = "UNKNOWN"
    d = eligible_routes(db, domain="PERSONAL", data_classification="INTERNAL")
    assert d.eligible == [] and any("availability UNKNOWN" in x for x in d.rejected[DS])
    assert any("run `aeos routes refresh`" in x for x in d.rejected[DS])
    r.availability = "AVAILABLE"
    assert eligible_routes(db, domain="PERSONAL", data_classification="INTERNAL").eligible == [DS]
    gemini = service.get_route(db, "gemini/gemini-3.8-flash-free-tier")  # no catalog source for this provider
    reasons = service.route_rejections(db, gemini, domain="PERSONAL", data_classification="PUBLIC", needs_tools=False,
                                       min_context=None)
    assert any("no catalog source observes provider gemini" in x for x in reasons)


def test_task_tools_must_be_inside_the_reviewed_tool_set(world):
    db = world.db
    trial(db, DS)
    assert select_route(db, task=task(tools=["fs.read", "tests.run"])).route.route_key == DS
    with pytest.raises(Blocked) as ei:
        select_route(db, task=task(tools=["fs.read", "bash:pytest -q"]))
    assert "outside the route's reviewed tool set" in str(ei.value.details["rejected"][DS])
    r = service.get_route(db, DS)
    r.tool_set = ["opencode-default"]
    assert any("tool set not reviewed" in x for x in service.tool_set_rejections(r, ["fs.read"]))
    r.tool_set = ["fs.read", "bash"]
    assert service.tool_set_rejections(r, ["bash:pytest -q", "fs.read"]) == []
    with pytest.raises(ValidationFailed):
        service.activate_route(db, GLM, actor_user_id="owner", mode="TRIAL", rationale="pilot slice trial run",
                               expected_config_hash=observed(db, GLM).config_hash, terms_reviewed=True,
                               tool_set=["sudo"])


def test_exhausted_provider_quota_blocks_every_route_of_that_provider(world):
    """§42 'tidak menggunakan account/key rotation untuk melewati limit': the free-daily quota is keyed by the
    provider, so a second route (another model or credential of the same provider) is blocked as well."""
    db = world.db
    approve(db, DS)
    approve(db, GLM)
    note_rate_limited(db, provider="opencode-zen", scope="free-daily", retry_after_seconds=600)
    d = eligible_routes(db, domain="PERSONAL", data_classification="INTERNAL")
    assert d.eligible == [] and all(any("quota EXHAUSTED" in x for x in d.rejected[k]) for k in (DS, GLM))
    for key in (DS, GLM):
        assert service.quota_exhausted(db, service.get_route(db, key)) is not None


# ---------------------------------------------------------------------------------- runtime binding (§23 fields)


def _installed(monkeypatch, version: str) -> None:
    from aeos.worker_runtime import opencode

    monkeypatch.setattr(opencode, "opencode_status",
                        lambda configured=None, refresh=False: {"status": "INSTALLED", "version": version})


def test_activation_binds_policy_prompt_context_and_harness(world, monkeypatch):
    db = world.db
    _installed(monkeypatch, "1.19.3")
    r = trial(db, DS)
    runtime = service.runtime_context()
    assert r.policy_version == runtime["policy_version"] and r.policy_version.startswith("pol-")
    assert r.prompt_revision == runtime["prompt_revision"] and r.prompt_revision.startswith("wp-")
    assert r.context_strategy == runtime["context_strategy"] and r.harness_version == "1.19.3"
    assert eligible_routes(db, domain="PERSONAL", data_classification="INTERNAL").eligible == [DS]
    r.prompt_revision = None  # activated before the binding existed
    assert any("not bound at activation" in x for x in service.binding_drift(r, service.runtime_context()))


def test_policy_change_makes_an_activated_route_ineligible_until_re_review(world, monkeypatch):
    db = world.db
    trial(db, DS)
    monkeypatch.setattr(service, "current_policy_version", lambda: "pol-changed")
    d = eligible_routes(db, domain="PERSONAL", data_classification="INTERNAL")
    assert d.eligible == [] and any("policy_version changed since activation" in x for x in d.rejected[DS])
    route, _ = service.activate_route(db, DS, actor_user_id="owner", mode="TRIAL", rationale="re-review after policy",
                                      expected_config_hash=service.get_route(db, DS).config_hash)
    assert route.policy_version == "pol-changed"
    assert eligible_routes(db, domain="PERSONAL", data_classification="INTERNAL").eligible == [DS]


def test_worker_prompt_or_context_strategy_change_is_a_runtime_change(world, monkeypatch):
    from aeos.worker_runtime import executor

    db = world.db
    trial(db, DS)
    original = executor.build_prompt
    monkeypatch.setattr(executor, "build_prompt", lambda *a, **k: original(*a, **k) + "\n- a new worker rule\n")
    d = eligible_routes(db, domain="PERSONAL", data_classification="INTERNAL")
    assert any("prompt_revision changed since activation" in x for x in d.rejected[DS])
    monkeypatch.setattr(executor, "build_prompt", original)
    monkeypatch.setattr(executor, "MAX_PROMPT_ATTACH_TOTAL", executor.MAX_PROMPT_ATTACH_TOTAL * 2)
    d = eligible_routes(db, domain="PERSONAL", data_classification="INTERNAL")
    assert any("context_strategy changed since activation" in x for x in d.rejected[DS])


def test_new_harness_version_is_a_new_configuration(world, monkeypatch):
    db = world.db
    _installed(monkeypatch, "1.19.3")
    r = trial(db, DS)
    assert r.harness_version == "1.19.3"
    assert eligible_routes(db, domain="PERSONAL", data_classification="INTERNAL").eligible == [DS]
    _installed(monkeypatch, "1.20.0")
    out = catalog.observe_harness_version(db, "opencode")
    assert out["harness_version"] == "1.20.0" and out["harness_changed"] == [DS]  # candidates bind at activation
    d = eligible_routes(db, domain="PERSONAL", data_classification="INTERNAL")
    assert d.eligible == [] and any("config hash" in x or "TRIAL without" in x for x in d.rejected[DS])
    assert (DS, "route configuration changed: harness opencode 1.19.3 -> 1.20.0") in world.reg.invalidated_decisions
    assert service.get_route(db, GLM).harness_version is None


def test_engine_installed_or_upgraded_after_activation_is_not_the_reviewed_harness(world, monkeypatch):
    """§23 harness version: a route activated while OpenCode was absent has no bound version; once an engine is
    observable the hard filter refuses the route until it is re-activated on that version (fail closed, even
    before a catalog refresh records it as a configuration change)."""
    from aeos.worker_runtime import opencode

    db = world.db
    monkeypatch.setattr(opencode, "opencode_status", lambda configured=None, refresh=False: {"status": "MISSING"})
    r = trial(db, DS)
    assert r.harness_version is None
    assert eligible_routes(db, domain="PERSONAL", data_classification="INTERNAL").eligible == [DS]  # ENGINE_MISSING later
    _installed(monkeypatch, "1.19.3")
    d = eligible_routes(db, domain="PERSONAL", data_classification="INTERNAL")
    assert d.eligible == [] and any("harness_version was not bound at activation" in x for x in d.rejected[DS])
    route, _ = service.activate_route(db, DS, actor_user_id="owner", mode="TRIAL", rationale="re-review on 1.19.3",
                                      expected_config_hash=service.get_route(db, DS).config_hash)
    assert route.harness_version == "1.19.3"
    assert eligible_routes(db, domain="PERSONAL", data_classification="INTERNAL").eligible == [DS]
    _installed(monkeypatch, "1.20.0")  # upgraded, no catalog refresh yet
    d = eligible_routes(db, domain="PERSONAL", data_classification="INTERNAL")
    assert any("harness_version changed since activation (1.19.3 -> 1.20.0)" in x for x in d.rejected[DS])


def test_activation_fails_closed_when_the_runtime_cannot_be_fingerprinted(world, monkeypatch):
    db = world.db
    monkeypatch.setattr(service, "current_policy_version", lambda: None)
    r = observed(db, DS)
    with pytest.raises(Blocked) as ei:
        service.activate_route(db, DS, actor_user_id="owner", mode="TRIAL", rationale="pilot slice trial run",
                               expected_config_hash=r.config_hash, terms_reviewed=True)
    assert ei.value.code == "RUNTIME_BINDING_UNKNOWN"


# ---------------------------------------------------------------------------------- planner -> routing seam


def _compiled(min_context: int, requires_tools: bool = True, **extra):
    from aeos.models.planning import TaskSpec
    from aeos.planning import dag

    project = SimpleNamespace(id="p1", domain="PERSONAL", data_classification="INTERNAL", is_platform=False)
    version = SimpleNamespace(id="pv1", manifest_hash="sha256:pv", mode="LIVE")
    fields = dag.task_spec_fields({"key": "T1", "title": "t", "goal": "g", "allowed_paths": ["src/**"],
                                   "model_route_policy": {"pool": "WORKER", "requires_tools": requires_tools,
                                                          "min_context_tokens": min_context, **extra}},
                                  project=project, version=version, protected=())
    return TaskSpec(**fields, spec_hash="sha256:spec"), fields["model_route_policy"]


def test_compiled_route_policy_reaches_the_context_and_tool_filters(world):
    db = world.db
    trial(db, DS)  # context window UNKNOWN on the seeded route
    spec, _ = _compiled(200_000)
    assert service._task_policy(spec)["min_context"] == 200_000
    with pytest.raises(Blocked) as ei:
        select_route(db, task=spec)
    assert ei.value.code == "NO_ELIGIBLE_ROUTE" and "context window UNKNOWN" in str(ei.value.details["rejected"][DS])
    ds = service.get_route(db, DS)
    ds.context_window = 128_000  # observed by a catalog refresh: a configuration change, re-reviewed below
    service.refresh_config_hash(db, ds, reason="context window observed")
    trial(db, DS)
    with pytest.raises(Blocked) as ei:
        select_route(db, task=spec)
    assert "context window 128000 < required 200000" in str(ei.value.details["rejected"][DS])
    spec_small, _ = _compiled(64_000)
    assert select_route(db, task=spec_small).route.route_key == DS
    no_tools, _ = _compiled(0, requires_tools=False)
    assert service._task_policy(no_tools)["needs_tools"] is False and service._task_policy(no_tools)["min_context"] is None


def test_every_route_policy_key_the_compiler_emits_is_consumed_or_enforced():
    _, policy = _compiled(1000)
    known = service.POLICY_KEYS_CONSUMED | set(service.POLICY_KEYS_ENFORCED) | service.POLICY_KEYS_INFORMATIONAL
    assert set(policy) <= known, set(policy) - known
    for key, enforced in service.POLICY_KEYS_ENFORCED.items():
        assert policy[key] == enforced  # the compiler never asks routing to relax a constraint


def test_route_policy_asking_to_relax_a_constraint_is_refused(world):
    with pytest.raises(Blocked) as ei:
        select_route(world.db, task=task(paid_fallback=True))
    assert ei.value.code == "ROUTE_POLICY_UNSUPPORTED"


# ---------------------------------------------------------------------------------- views


def test_supervisor_alias_change_is_reported_as_needing_evaluation(world):
    from aeos.routing.router import _eval_summary

    db = world.db
    service.observe_supervisor_resolution(db, route_key="claude-native/opus", requested="opus", resolved="claude-opus-5-5")
    opus = service.get_route(db, "claude-native/opus")
    assert _eval_summary(opus)["state"] == "BELUM_DIUJI"
    service.observe_supervisor_resolution(db, route_key="claude-native/opus", requested="opus", resolved="claude-opus-5-6")
    summary = _eval_summary(opus)
    assert summary["state"] == "NEEDS_EVALUATION" and "keputusan pemilik" in summary["note"]


def test_route_view_reports_binding_execution_path_and_restrictions(world):
    from aeos.routing.router import route_view

    db = world.db
    trial(db, DS)
    principal = SimpleNamespace(active_domain=None)
    view = route_view(db, service.get_route(db, DS), principal)
    assert view["runtime_binding"]["status"] == "BOUND" and view["execution_path"]["status"] == "WORKER_RUNTIME"
    arena = route_view(db, service.get_route(db, NEMO), principal)
    assert arena["execution_path"]["status"] == "NOT_WIRED" and arena["terms_restriction"]
    assert route_view(db, service.get_route(db, BEAM), principal)["region_restricted"] is True
    laguna = route_view(db, service.get_route(db, "openrouter/poolside/laguna-s-2.1:free"), principal)
    assert laguna["terms_unknown"] == ["may_use_inputs_for_training"] and "UNKNOWN" in laguna["terms_restriction"]
    assert view["terms_unknown"] == [] and view["runtime_binding"]["current"].keys() >= {"harness_version"}
    assert db.query(Decision).filter_by(target_id=DS, kind="TRIAL").count() == 1
