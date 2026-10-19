"""Seam group W3: Readiness Doctor <-> every module, invalidation and placeholder detection (§08, §38, §50).

Everything runs against the REAL modules (routing, policy, secrets, worker runtime, bridge records, delivery
provider, cost, workforce, operations); only external things are faked: probe runner (no real binaries),
Docker absence, OpenCode status, GitHub (respx) and the secret store (in-memory provider).

§50 rows covered:
* "Config/model/provider berubah setelah doctor" -> dependent checks STALE, capability not READY until recheck.
* "Doctor hanya berhasil connect" / bridge NOT-PROVEN -> DEGRADED (manual fallback), never READY by narration.
* "Binary/model route/config berganti dari approved pin" -> recheck before admission.
* "Budget DB/gateway tidak sehat" -> CAN_RUN_WORKER BLOCKED.
* "Tool opsional atau Office belum ready" -> OFFICE gates BLOCKED by default through the policy module.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from aeos.core.enums import Capability, Domain
from aeos.core.timeutil import utcnow
from aeos.doctor import facade
from aeos.doctor.checks import REGISTRY, load_all
from aeos.doctor.checks import host as host_mod
from aeos.doctor.runner import latest_results, make_context, run_check, run_doctor
from aeos.inventory.probes import file_is_placeholder, set_runner_override, source_is_placeholder
from aeos.models.audit import Event
from aeos.models.doctor_ext import DoctorRun
from aeos.models.routing import ModelRoute
from tests.conftest import create_admin
from tests.unit.test_inventory_fakes import base_runner, ok

BACKEND = Path(__file__).resolve().parents[2] / "backend" / "aeos"
ROUTE = "local-test/coder-free"
GITHUB = "https://api.github.com"


# ------------------------------------------------------------------------------------------- fixtures


@pytest.fixture
def fake_host(monkeypatch):
    r = base_runner()
    set_runner_override(r)
    monkeypatch.setattr(host_mod, "tcp_in_use", lambda h, p, timeout=1.0: False)
    monkeypatch.setattr(host_mod, "can_bind", lambda h, p: True)
    yield r
    set_runner_override(None)


@pytest.fixture
def no_docker(monkeypatch):
    """Docker absent (this build host): only the WEAK process sandbox exists."""
    from aeos.worker_runtime import sandbox

    monkeypatch.setattr(sandbox, "_docker_probe", lambda timeout=10.0: {
        "available": False, "evidence": "docker CLI not found on PATH", "cli": None, "server_version": None})
    monkeypatch.setitem(sandbox._cache, "value", None)


@pytest.fixture
def opencode_installed(monkeypatch):
    from aeos.worker_runtime import opencode

    status = {"status": "INSTALLED", "binary": "/opt/fake/bin/opencode", "version": opencode.PINNED_VERSION,
              "pinned_version": opencode.PINNED_VERSION, "drift": False, "functional": "NOT_PROVEN",
              "checked_at": utcnow().isoformat(), "evidence": "`opencode --version` exit 0"}
    monkeypatch.setattr(opencode, "opencode_status", lambda configured=None, *, refresh=False: dict(status))
    return status


def _admin(db):
    from aeos.models.identity import User

    u = db.scalar(select(User).where(User.username == "owner"))
    return u if u is not None else create_admin(db)


def _worker_route(db, *, provider: str = "local-test", endpoint: str = "http://127.0.0.1:11434/v1") -> ModelRoute:
    from aeos.routing.service import compute_config_hash

    now = utcnow()
    r = ModelRoute(route_key=ROUTE, pool="WORKER", provider=provider, endpoint=endpoint, model_id="coder-free",
                   display_name="Local test coder", harness="opencode", is_free="YES", pricing={"input_per_mtok": 0},
                   pricing_checked_at=now, terms_checked_at=now, data_terms={"may_use_inputs_for_training": False},
                   eligible_domains=["PERSONAL"], allowed_data_classes=["PUBLIC", "INTERNAL"], context_window=65536,
                   supports_tools="YES", status="CANDIDATE", mode="LIVE", origin="OWNER", availability="AVAILABLE")
    r.config_hash = compute_config_hash(r)
    db.add(r)
    db.flush()
    return r


def _activate(db, admin) -> None:
    from aeos.routing import service

    r = service.get_route(db, ROUTE)
    service.activate_route(db, ROUTE, actor_user_id=admin.id, mode="TRIAL", rationale="pilot slice for self-build",
                           expected_config_hash=r.config_hash, terms_reviewed=True)


def _process_exception(db, admin):
    from aeos.policy import facade as policy

    return policy.record_decision(
        db, kind="EXCEPTION", actor_user_id=admin.id, target_type="worker_sandbox", target_id="process",
        domain="PERSONAL", rationale="no Docker on this host yet; accept the WEAK process sandbox for PERSONAL",
        expires_at=utcnow() + timedelta(days=14), details={"allow_process_sandbox": True},
        auth_scope={"domains": ["PERSONAL"]})


def _enable_agent(db) -> None:
    from aeos.models.workforce import AgentDefinition
    from aeos.workforce import seed as wf_seed

    wf_seed.run(db)
    a = db.scalar(select(AgentDefinition).where(AgentDefinition.agent_key == "opencode-implementer"))
    a.enabled = True
    a.enabled_reason = "enabled by owner: test"
    db.flush()


def _run(settings, db, runner, **kw):
    report = run_doctor(settings, db, runner=runner, **kw)
    db.commit()
    return {r.check_id: r for r in report.results}


def _stored(db, cid: str):
    db.expire_all()
    return latest_results(db, [cid])[cid]


@pytest.fixture
def worker_ready(settings, db, secrets_memory, fake_host, no_docker, opencode_installed):
    """Every CAN_RUN_WORKER prerequisite satisfied through the real modules on a Docker-less host."""
    admin = _admin(db)
    _worker_route(db)
    _activate(db, admin)
    _process_exception(db, admin)
    _enable_agent(db)
    db.commit()
    _run(settings, db, fake_host)
    return admin


# --------------------------------------------------------------------------- 3. placeholder detection


def test_no_real_module_is_reported_as_placeholder():
    flagged = [p.relative_to(BACKEND).as_posix() for p in BACKEND.rglob("*.py") if file_is_placeholder(p)]
    assert flagged == []  # every module of the integrated tree is real (incl. secrets/provider.py)


@pytest.mark.parametrize("source,expected", [
    ('"""SecretProvider implementations. PLACEHOLDER — owned and completed by the secrets module agent.\n"""', True),
    ('"""Bridge. PLACEHOLDER - owned by the bridge agent."""\nx = 1\n', True),
    ("# comment\n'''Verifier. PLACEHOLDER: completed later.'''\n", True),
    ('"""Detects modules whose docstring carries the PLACEHOLDER marker."""\n', False),
    ('"""Real module.\n\nThe foundation shipped a PLACEHOLDER — replaced by this file.\n"""\n', False),
    ('"""Real module."""\nSTATUS = "PLACEHOLDER - not a docstring"\n', False),
    ("x = 1\n", False),
])
def test_placeholder_marker_rule(source, expected):
    assert source_is_placeholder(source) is expected


def test_remaining_placeholder_files_are_still_detected(tmp_path, monkeypatch):
    from aeos.doctor.checks._common import module_present
    from aeos.inventory.reconcile import _module_is_placeholder

    pkg = tmp_path / "w3fakepkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "skeleton.py").write_text('"""Interlock. PLACEHOLDER — owned by the policy agent."""\n', encoding="utf-8")
    (pkg / "real.py").write_text('"""Real interlock; mentions PLACEHOLDER detection only."""\n', encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert module_present("w3fakepkg.skeleton") == (True, True)
    assert module_present("w3fakepkg.real") == (True, False)
    assert module_present("w3fakepkg.absent") == (False, False)
    import importlib.util

    assert _module_is_placeholder(importlib.util.find_spec("w3fakepkg.skeleton")) is True
    assert _module_is_placeholder(importlib.util.find_spec("aeos.secrets.provider")) is False


def test_checks_use_real_modules_not_not_implemented_paths(settings, db, secrets_memory, fake_host, no_docker,
                                                          opencode_installed):
    create_admin(db)
    by_id = _run(settings, db, fake_host)
    not_impl = {cid: r.error_code for cid, r in by_id.items() if (r.error_code or "").endswith("NOT_IMPLEMENTED")}
    assert not_impl == {}
    # the interlock is exercised by a self-test (unbound write intent refused), not inferred from module presence
    assert by_id["auth.interlock"].status == "VERIFIED"
    assert by_id["auth.interlock"].observed_evidence["self_test"]["allowed"] is False
    assert by_id["evidence.verifier"].status == "CONFIGURED"
    assert by_id["secret.status"].status == "CONFIGURED"  # memory provider = a reachable store
    assert by_id["worker.opencode"].status == "VERIFIED"


# --------------------------------------------------------------------- 1. CAN_RUN_WORKER, real modules


def test_can_run_worker_ready_only_with_all_real_prerequisites(settings, db, worker_ready):
    st = facade.capability_state(db, Capability.CAN_RUN_WORKER, domain="PERSONAL")
    assert st.status == "READY", st.reasons
    live = next(c for c in st.checks if c["check_id"] == "worker_runtime.sandbox_readiness")
    assert live["ok"] and live["kind"] == "process"
    assert live["effective_status"] == "CONFIGURED"  # WEAK + exception: satisfies policy, never VERIFIED
    # WEAK process sandbox: allowed by the owner's exception, but its residual risks are always surfaced
    assert any("egress" in r for r in st.residual_risks)
    assert any("WEAK" in r for r in st.reasons)
    sandbox = _stored(db, "containment.sandbox")
    assert sandbox.status == "CONFIGURED" and sandbox.observed_evidence["kind"] == "process"
    assert _stored(db, "route.registry").observed_evidence["eligible"] == [ROUTE]
    assert _stored(db, "route.worker_key").status == "NOT_APPLICABLE"  # loopback route, no upstream key
    # OFFICE never: the worker runtime blocks OFFICE sandboxes and the office policy denies by default
    office = facade.capability_state(db, Capability.CAN_RUN_WORKER, domain="OFFICE")
    assert office.status == "BLOCKED"


def test_sandbox_requirement_is_live_and_surfaces_reasons_and_remedies(settings, db, worker_ready):
    from aeos.policy import decisions as registry

    exc = registry.find_active_decision(db, kind="EXCEPTION", target_type="worker_sandbox", target_id="process")
    registry.revoke_decision(db, exc.id, actor_user_id=worker_ready.id, reason="Docker will be installed now")
    db.commit()
    st = facade.capability_state(db, Capability.CAN_RUN_WORKER, domain="PERSONAL")
    assert st.status == "BLOCKED"
    live = next(r for r in st.reasons if r.startswith("worker_runtime.sandbox_readiness: BLOCKED"))
    assert "container sandbox not available" in live and "EXCEPTION" in live
    assert any(r["check_id"] == "worker_runtime.sandbox_readiness" for r in st.remedies)
    assert any("egress" in r for r in st.residual_risks)  # the risks the exception would have to accept
    # the stored sandbox check is STALE (config:policy hook) until rechecked
    assert _stored(db, "containment.sandbox").status == "STALE"


def test_agent_definition_gate_and_enable_endpoint_without_deadlock(settings, db, secrets_memory, fake_host,
                                                                  no_docker, opencode_installed, personal_client):
    from aeos.models.workforce import AgentDefinition
    from aeos.workforce import seed as wf_seed

    admin = _admin(db)
    _worker_route(db)
    _activate(db, admin)
    _process_exception(db, admin)
    wf_seed.run(db)
    db.commit()
    _run(settings, db, fake_host)
    st = facade.capability_state(db, Capability.CAN_RUN_WORKER, domain="PERSONAL")
    assert st.status == "BLOCKED" and any(r.startswith("worker.agent_definition: MISSING") for r in st.reasons)
    # enabling asks CAN_RUN_WORKER without the agent itself (no chicken-and-egg)
    r = personal_client.post("/api/workforce/agents/opencode-implementer/enable", json={"reason": "pilot"})
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.scalar(select(AgentDefinition).where(AgentDefinition.agent_key == "opencode-implementer")).enabled
    stale = facade.capability_state(db, Capability.CAN_RUN_WORKER, domain="PERSONAL")
    assert stale.status != "READY"  # the stored agent check no longer matches: recheck first
    assert any("worker.agent_definition: STALE" in x for x in stale.reasons)
    _run(settings, db, fake_host)
    assert facade.capability_state(db, Capability.CAN_RUN_WORKER, domain="PERSONAL").status == "READY"


def test_untrusted_budget_blocks_worker_admission(settings, db, worker_ready, fake_host):
    from aeos.cost import service as cost
    from aeos.models.cost import Budget

    b = cost.ensure_default_budget(db)
    db.commit()
    db.execute(Budget.__table__.update().where(Budget.id == b.id).values(limit_usd=500))  # tampered, no decision
    db.commit()
    db.expire_all()  # the session keeps loaded rows (expire_on_commit=False); a Doctor run reads fresh state
    by_id = _run(settings, db, fake_host)
    assert by_id["route.budget_admission"].status == "FAILED"
    assert by_id["route.budget_admission"].error_code == "BUDGET_STATE_UNTRUSTED"
    st = facade.capability_state(db, Capability.CAN_RUN_WORKER, domain="PERSONAL")
    assert st.status == "BLOCKED" and any(r.startswith("route.budget_admission: FAILED") for r in st.reasons)


def test_missing_opencode_and_weak_containment_report(settings, db, worker_ready, fake_host, monkeypatch):
    from aeos.worker_runtime import containment, opencode

    monkeypatch.setattr(opencode, "opencode_status", lambda configured=None, *, refresh=False: {
        "status": "MISSING", "binary": None, "version": None, "pinned_version": opencode.PINNED_VERSION})
    weak = {"kind": "process", "overall": "WEAK", "evidence_level": "E2", "summary": {"PASS": 2, "NOT_ENFORCED": 1},
            "results": [{"check": "env_secrets", "status": "PASS"}, {"check": "cancel_kills_tree", "status": "PASS"},
                        {"check": "egress_non_broker", "status": "NOT_ENFORCED"}]}
    monkeypatch.setattr(containment, "containment_report", lambda kind="process": dict(weak))
    by_id = _run(settings, db, fake_host, functional=True, groups=["WORKER", "CONTAINMENT"])
    assert by_id["worker.opencode"].status == "MISSING"
    assert "opencode-ai@" in by_id["worker.opencode"].remediation["command"]
    assert (by_id["containment.report"].status, by_id["containment.report"].error_code) == ("CONFIGURED",
                                                                                            "CONTAINMENT_WEAK")
    st = facade.capability_state(db, Capability.CAN_RUN_WORKER, domain="PERSONAL")
    assert st.status == "BLOCKED" and any(r.startswith("worker.opencode: MISSING") for r in st.reasons)
    assert any("containment egress_non_broker: NOT_ENFORCED" in r for r in st.residual_risks)
    weak["overall"], weak["results"][0]["status"] = "FAIL", "FAIL"  # a real failure blocks (guard tier)
    _run(settings, db, fake_host, functional=True, groups=["CONTAINMENT"])
    st = facade.capability_state(db, Capability.CAN_RUN_WORKER, domain="PERSONAL")
    assert any(r.startswith("containment.report: FAILED") for r in st.reasons)


# ------------------------------------------------------------------------------- 2. invalidation hooks


def test_route_status_change_marks_checks_stale_until_recheck(settings, db, worker_ready, fake_host):
    from aeos.routing import service

    service.disable_route(db, ROUTE, reason="provider terms changed")
    db.commit()
    for cid in ("route.registry", "route.worker_key"):
        row = _stored(db, cid)
        assert row.status == "STALE" and "config:routes" in row.observed_evidence["stale_reason"]
    # a WORKER route (and its revoked TRIAL decision) is not a supervisor route: the bridge proof stays fresh
    assert _stored(db, "claude.capability_test").status != "STALE"
    st = facade.capability_state(db, Capability.CAN_RUN_WORKER, domain="PERSONAL")
    assert st.status == "UNKNOWN" and any("route.registry: STALE" in r for r in st.reasons)
    assert db.scalar(select(Event).where(Event.kind == "doctor.check.stale")) is not None
    by_id = _run(settings, db, fake_host)
    assert by_id["route.registry"].error_code == "NO_APPROVED_WORKER_ROUTE"  # DISABLED is not a decided route
    assert facade.capability_state(db, Capability.CAN_RUN_WORKER, domain="PERSONAL").status == "BLOCKED"


def test_route_config_and_pin_changes_invalidate_model_route(settings, db, worker_ready, fake_host):
    from aeos.models.claude_bridge_ext import ClaudeCapabilityTest
    from aeos.routing import seed as routing_seed
    from aeos.routing import service

    routing_seed.run(db)
    db.add(ClaudeCapabilityTest(status="VERIFIED", engine_version="2.1.302", model_requested="opus", mode="LIVE"))
    db.commit()
    _run(settings, db, fake_host)
    # eligible_domains change of a WORKER route -> new configuration identity -> config:routes + model:route
    r = service.get_route(db, ROUTE)
    r.eligible_domains = ["PERSONAL", "OFFICE"]
    assert service.refresh_config_hash(db, r, reason="eligible_domains edited")
    db.commit()
    assert _stored(db, "route.registry").status == "STALE"
    assert "model:route" in _stored(db, "worker.contract").observed_evidence["stale_reason"]
    # ... but the Claude bridge proof is about SUPERVISOR routes only: reviews keep their gate
    assert _stored(db, "claude.capability_test").status != "STALE"
    # supervisor: the FIRST observed resolution is not a pin change (nothing was bound to an earlier identity);
    # another alias' first call (fable after an opus test) must not push reviews to the manual fallback
    for alias in ("opus", "fable"):
        service.observe_supervisor_resolution(db, route_key=f"claude-native/{alias}", requested=alias,
                                              resolved=f"claude-{alias}-5-5")
    db.commit()
    assert _stored(db, "claude.capability_test").status != "STALE"
    # a real re-resolution of the alias (pin drift) -> model:route:SUPERVISOR -> bridge proof STALE
    service.observe_supervisor_resolution(db, route_key="claude-native/opus", requested="opus",
                                          resolved="claude-opus-5-6")
    db.commit()
    row = _stored(db, "claude.capability_test")
    assert row.status == "STALE" and "model:route:SUPERVISOR" in row.observed_evidence["stale_reason"]


def test_secret_put_and_delete_invalidate_dependent_checks(settings, db, secrets_memory, fake_host):
    from aeos.secrets import facade as secrets
    from aeos.secrets.provider import provider_delete

    create_admin(db)
    _run(settings, db, fake_host, groups=["SECRET", "MODEL_ROUTE", "PROJECT_ADAPTERS"])
    assert _stored(db, "adapter.github_token").status == "MISSING"
    secrets.put_secret("personal/github", "ghp_" + "x" * 36, actor="test")
    gh = _stored(db, "adapter.github_token")
    assert gh.status == "STALE" and "secret-ref personal/github" in gh.observed_evidence["stale_reason"]
    # the store's own status does not depend on single references (a token write/refresh keeps it fresh)
    assert _stored(db, "secret.status").status != "STALE"
    assert _stored(db, "route.worker_key").status != "STALE"  # depends on other refs only
    ev = db.scalar(select(Event).where(Event.kind == "doctor.check.stale", Event.entity_id == "adapter.github_token"))
    assert ev.payload["invalidator_kind"] == "secret" and ev.payload["invalidator_target"] == "personal/github"
    by_id = _run(settings, db, fake_host, groups=["SECRET", "MODEL_ROUTE", "PROJECT_ADAPTERS"])
    assert _stored(db, "adapter.github_token").status == "CONFIGURED"
    # the stored invalidation reasons are not mistaken for a persisted credential
    assert by_id["secret.no_raw_exposure"].status == "VERIFIED"
    provider_delete("personal/github", actor="test")
    assert _stored(db, "adapter.github_token").status == "STALE"
    st = facade.capability_state(db, Capability.CAN_PREPARE_PR, domain="PERSONAL")
    assert st.status != "READY" and any("adapter.github_token: STALE" in r for r in st.reasons)


def test_policy_and_office_decisions_invalidate_policy_checks(settings, db, fake_host, no_docker, personal_client):
    admin = _admin(db)
    from aeos.policy import seed as policy_seed

    policy_seed.run(db)
    db.commit()
    _run(settings, db, fake_host, groups=["CANONICAL_POLICY", "CONTAINMENT"])
    assert _stored(db, "policy.snapshot").status == "CONFIGURED"
    from aeos.policy import facade as policy

    policy.record_decision(db, kind="POLICY", actor_user_id=admin.id, target_type="policy_rule", target_id="r1",
                           domain="PERSONAL", rationale="owner policy for the readiness test", auth_scope={"x": 1})
    db.commit()
    for cid in ("policy.snapshot", "policy.mci", "containment.sandbox"):
        assert _stored(db, cid).status == "STALE", cid
    assert _stored(db, "policy.office_defaults").status != "STALE"
    personal_client.select_domain(Domain.OFFICE)
    r = personal_client.post("/api/decisions", json={
        "kind": "POLICY", "office_authorization": True, "company_reference": "CHG-2026-001 signed by IT",
        "allowed_providers": ["jira"], "auth_scope": {"providers": ["jira"]},
        "rationale": "company authorisation for Jira read access (test)"})
    assert r.status_code == 201, r.text
    row = _stored(db, "policy.office_defaults")
    assert row.status == "STALE" and "config:office_policy" in row.observed_evidence["stale_reason"]
    # platform-level rows and domain-less events are visible to PERSONAL: the OFFICE decision is not quoted
    office_id = r.json()["id"]
    platform = [x for x in latest_results(db).values() if not (x.scope or {}).get("domain")]
    assert platform and not any(office_id[:8] in json.dumps(x.observed_evidence or {}) for x in platform)
    shared = db.scalars(select(Event).where(Event.kind == "doctor.check.stale", Event.domain.is_(None))).all()
    assert shared and not any(office_id[:8] in json.dumps(e.payload) for e in shared)
    # the platform policy snapshot row does not count OFFICE decisions either (counts are metadata, §50)
    from sqlalchemy import func, or_

    from aeos.models.decisions import Decision

    by_id = _run(settings, db, fake_host, groups=["CANONICAL_POLICY"])
    active = Decision.status == "ACTIVE"
    office = db.scalar(select(func.count()).select_from(Decision).where(active, Decision.domain == "OFFICE"))
    others = db.scalar(select(func.count()).select_from(Decision).where(
        active, or_(Decision.domain.is_(None), Decision.domain != "OFFICE")))
    assert office >= 1 and by_id["policy.snapshot"].observed_evidence["active_decisions"] == others


# ---------------------------------------------------------------- CAN_PREPARE_PR / delivery provider


def test_prepare_pr_consumes_provider_capability_evidence(settings, db, secrets_memory, fake_host):
    import respx
    from httpx import Response

    from aeos.delivery import provider
    from aeos.delivery.provider import NOT_ENFORCEABLE_MESSAGE
    from aeos.portfolio import facade as portfolio
    from aeos.portfolio import seed as portfolio_seed

    create_admin(db)
    portfolio_seed.run(db)
    db.commit()
    slug = settings.platform_repo_slug
    by_id = _run(settings, db, fake_host, groups=["DELIVERY"])
    assert by_id["delivery.branch_protection"].error_code == "PROVIDER_CAPABILITY_NOT_CHECKED"
    assert "delivery capabilities --record" in by_id["delivery.branch_protection"].remediation["command"]
    with respx.mock(assert_all_called=False) as gh:
        gh.get(f"{GITHUB}/repos/{slug}").mock(return_value=Response(200, json={
            "private": True, "visibility": "private", "default_branch": "main",
            "owner": {"type": "User", "login": slug.split("/")[0]}, "permissions": {"push": True}}))
        gh.get(f"{GITHUB}/user").mock(return_value=Response(200, json={"login": slug.split("/")[0],
                                                                          "plan": {"name": "free"}}))
        upgrade = {"message": "Upgrade to GitHub Pro or make this repository public to enable this feature."}
        gh.get(f"{GITHUB}/repos/{slug}/branches/main/protection").mock(return_value=Response(403, json=upgrade))
        gh.get(f"{GITHUB}/repos/{slug}/rulesets").mock(return_value=Response(403, json=upgrade))
        gh.get(f"{GITHUB}/repos/{slug}/rules/branches/main").mock(return_value=Response(200, json=[]))
        gh.get(f"{GITHUB}/repos/{slug}/environments").mock(
            return_value=Response(200, json={"total_count": 0, "environments": []}))
        caps = provider.check_capabilities(slug, token="t" * 40)
    provider.record_capability_evidence(db, portfolio.platform_project(db), caps)
    db.commit()
    by_id = _run(settings, db, fake_host, groups=["DELIVERY"])
    bp = by_id["delivery.branch_protection"]
    assert (bp.status, bp.error_code) == ("MISSING", "BRANCH_PROTECTION_NOT_ENFORCEABLE")
    assert bp.observed_evidence["enforceable"] is False and bp.observed_evidence["evidence_id"]
    st = facade.capability_state(db, Capability.CAN_PREPARE_PR, domain="PERSONAL")
    assert NOT_ENFORCEABLE_MESSAGE in st.residual_risks  # residual risk, never blocker-free enforcement
    note = next(r for r in st.reasons if "delivery.branch_protection" in r)
    assert note.startswith("catatan:") and "MISSING" in note
    assert not any(c["check_id"] == "delivery.branch_protection" and c["ok"] for c in st.checks)
    # residual risks quote PERSONAL-scoped evidence: shown to a PERSONAL session, not to one without a domain
    from aeos.doctor.views import capabilities_view

    def pr_item(domain):
        return next(i for i in capabilities_view(db, domain=domain)["items"] if i["capability"] == "CAN_PREPARE_PR")

    assert NOT_ENFORCEABLE_MESSAGE in pr_item("PERSONAL")["residual_risks"]
    assert NOT_ENFORCEABLE_MESSAGE not in pr_item(None)["residual_risks"]


# ------------------------------------------------------------ CAN_REVIEW_WITH_MAX / CAN_PLAN_PERSONAL


NATIVE = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"}


@pytest.fixture
def claude_host(fake_host):
    fake_host.add_binary("claude")
    fake_host.outputs[("claude", ("--version",))] = ok("2.1.302 (Claude Code)\n")
    fake_host.outputs[("claude", ("auth", "status", "--json"))] = ok(json.dumps(NATIVE))
    return fake_host


def _bridge_test(db, status: str, *, version: str = "2.1.302", age: timedelta | None = None, **extra) -> None:
    from aeos.claude_bridge.bridge import persist_capability_test

    out = {"status": status, "steps": [{"id": "structured_call", "status": status}], "model_requested": "opus",
           "model_resolved": "claude-opus-5-5" if status == "VERIFIED" else None, "engine_version": version,
           "binary": {"fingerprint": "sha256:x"}, "auth": {"logged_in": True}, "duration_ms": 1200,
           "error_code": None if status == "VERIFIED" else "CAPABILITY_TEST_FAILED", "trace_ref": None, **extra}
    row = persist_capability_test(db, out, actor="test")
    if age is not None:
        row.created_at = utcnow() - age
    db.commit()


def test_review_with_max_uses_the_bridge_record_never_narration(settings, db, claude_host):
    create_admin(db)
    by_id = _run(settings, db, claude_host)
    assert by_id["claude.capability_test"].error_code == "CAPABILITY_TEST_NOT_RUN"
    st = facade.capability_state(db, Capability.CAN_REVIEW_WITH_MAX, domain="PERSONAL")
    assert st.status == "DEGRADED"  # manual fallback package, NOT-PROVEN automation
    assert any("paket manual" in r for r in st.reasons)
    _bridge_test(db, "VERIFIED")
    by_id = _run(settings, db, claude_host)
    assert by_id["claude.capability_test"].status == "VERIFIED"
    assert facade.capability_state(db, Capability.CAN_REVIEW_WITH_MAX, domain="PERSONAL").status == "READY"
    _bridge_test(db, "FAILED")
    _run(settings, db, claude_host)
    assert facade.capability_state(db, Capability.CAN_REVIEW_WITH_MAX, domain="PERSONAL").status == "BLOCKED"
    _bridge_test(db, "BLOCKED")
    _run(settings, db, claude_host)
    assert facade.capability_state(db, Capability.CAN_REVIEW_WITH_MAX, domain="PERSONAL").status == "DEGRADED"
    _bridge_test(db, "VERIFIED", version="2.0.0")  # proof made with another CLI version
    by_id = _run(settings, db, claude_host)
    assert by_id["claude.capability_test"].error_code == "CAPABILITY_TEST_STALE"
    from aeos.models.claude_bridge_ext import ClaudeCapabilityTest

    db.query(ClaudeCapabilityTest).delete()
    db.commit()
    _bridge_test(db, "VERIFIED", age=timedelta(days=8))  # the only proof is a week old
    by_id = _run(settings, db, claude_host)
    assert by_id["claude.capability_test"].error_code == "CAPABILITY_TEST_STALE"
    assert facade.capability_state(db, Capability.CAN_REVIEW_WITH_MAX, domain="PERSONAL").status == "DEGRADED"
    plan = facade.capability_state(db, Capability.CAN_PLAN_PERSONAL, domain="PERSONAL")
    assert plan.status in ("DEGRADED", "UNKNOWN", "BLOCKED") and plan.status != "READY"


def test_ready_max_gates_still_carry_the_extra_billing_risk(settings, db, claude_host):
    create_admin(db)
    _bridge_test(db, "VERIFIED")
    _run(settings, db, claude_host)
    st = facade.capability_state(db, Capability.CAN_REVIEW_WITH_MAX, domain="PERSONAL")
    assert st.status == "READY"
    assert any("Extra usage berbayar" in r for r in st.residual_risks)  # UNKNOWN is never a silent pass
    assert any(r.startswith("catatan: claude.extra_billing: UNKNOWN") for r in st.reasons)


def test_cli_header_names_the_claude_call_only_in_functional_mode(settings, db, claude_host, monkeypatch):
    from typer.testing import CliRunner

    import aeos.claude_bridge.facade as bridge
    from aeos.cli import app

    monkeypatch.setattr(bridge, "capability_test", lambda: {"status": "VERIFIED", "mode": "LIVE",
                                                            "engine_version": "2.1.302"})
    plain = CliRunner().invoke(app, ["doctor", "--group", "CLAUDE_NATIVE"]).output
    assert "(no server, no LLM)" in plain
    functional = CliRunner().invoke(app, ["doctor", "--group", "CLAUDE_NATIVE", "--functional"]).output
    assert "no LLM" not in functional and "ONE small Claude Code call" in functional
    assert functional.isascii()


def test_functional_capability_test_calls_the_bridge_once(settings, db, claude_host, monkeypatch):
    import aeos.claude_bridge.facade as bridge

    calls = []

    def fake_test():
        calls.append(1)
        return {"status": "VERIFIED", "mode": "LIVE", "engine_version": "2.1.302", "model_resolved": "claude-opus-5-5",
                "model_requested": "opus", "checked_at": utcnow().isoformat(), "steps": []}

    monkeypatch.setattr(bridge, "capability_test", fake_test)
    create_admin(db)
    by_id = _run(settings, db, claude_host, functional=True, groups=["CLAUDE_NATIVE"])
    assert calls == [1] and by_id["claude.capability_test"].status == "VERIFIED"
    assert by_id["claude.capability_test"].observed_evidence["source"].startswith("uji baru")


# ----------------------------------------------------------------------------- CAN_PUBLISH_PERSONAL


def test_publish_personal_needs_tokens_and_write_enablement(settings, db, secrets_memory, fake_host, personal_client):
    from aeos.inventory.probes import HttpResult
    from aeos.secrets import facade as secrets

    _admin(db)
    by_id = _run(settings, db, fake_host)
    assert by_id["adapter.google_oauth"].error_code == "GOOGLE_OAUTH_MISSING"
    assert "publish google-connect" in by_id["adapter.google_oauth"].remediation["command"]
    assert by_id["adapter.write_enablement"].error_code == "WRITE_NOT_ENABLED"
    secrets.put_secret("personal/google-oauth-client", '{"installed": {}}', actor="test")
    secrets.put_secret("personal/google-oauth-token", '{"token": "x"}', actor="test")
    secrets.put_secret("personal/github", "ghp_" + "y" * 36, actor="test")
    # canonical PERSONAL publication providers (aeos.portfolio.providers): Google Docs writes are google-drive
    for p in ("google-drive", "github-issues"):
        r = personal_client.post("/api/decisions", json={"kind": "WRITE_ENABLEMENT", "provider": p, "enabled": True,
                                                         "auth_scope": {"provider": p},
                                                         "rationale": f"owner enables {p} writes for publication"})
        assert r.status_code == 201, r.text
    slug = settings.platform_repo_slug
    fake_host.http[f"{GITHUB}/repos/{slug}"] = HttpResult(
        200, {"private": True, "default_branch": "main", "permissions": {"push": True}, "id": 1})
    _run(settings, db, fake_host)  # secrets/enablement changed -> dependent checks were STALE: recheck
    _run(settings, db, fake_host, functional=True, groups=["PROJECT_ADAPTERS"])  # read-only GitHub probe
    st = facade.capability_state(db, Capability.CAN_PUBLISH_PERSONAL, domain="PERSONAL")
    assert st.status == "READY", st.reasons
    # revoking one enablement makes the gate stale (config:write_enablement) and then BLOCKED
    from aeos.policy import decisions as registry

    d = registry.find_active_decision(db, kind="WRITE_ENABLEMENT", target_type="provider_write", target_id="google-drive")
    registry.revoke_decision(db, d.id, actor_user_id=_admin(db).id, reason="pause Google Drive/Docs writes")
    db.commit()
    assert _stored(db, "adapter.write_enablement").status == "STALE"
    assert facade.capability_state(db, Capability.CAN_PUBLISH_PERSONAL, domain="PERSONAL").status != "READY"
    _run(settings, db, fake_host)
    st = facade.capability_state(db, Capability.CAN_PUBLISH_PERSONAL, domain="PERSONAL")
    assert st.status == "BLOCKED" and any("google-drive" in r for r in st.reasons)


# ---------------------------------------------------------------------------------- OFFICE + staging


def test_office_and_staging_gates_through_the_policy_module(settings, db, fake_host):
    from aeos.doctor.office import office_gate, read_office_policy

    create_admin(db)
    _run(settings, db, fake_host)
    pol, source = read_office_policy(db)
    assert source == "policy-module" and pol["integrations_allowed"] is False
    for cap in (Capability.CAN_PLAN_OFFICE, Capability.CAN_USE_OFFICE_INTEGRATIONS):
        st = facade.capability_state(db, cap, domain="OFFICE")
        assert st.status == "BLOCKED" and st.reasons[0].startswith("OFFICE default deny"), (cap, st.reasons)
    staging = facade.capability_state(db, Capability.CAN_VALIDATE_STAGING, domain="PERSONAL")
    assert staging.status == "BLOCKED" and any("delivery.staging_target: MISSING" in r for r in staging.reasons)
    # a recorded company authorisation (policy module) is the only thing that opens the integration flag
    from aeos.policy import facade as policy

    admin = _admin(db)
    policy.record_decision(db, kind="POLICY", actor_user_id=admin.id, target_type="office_policy", target_id="OFFICE",
                           domain="OFFICE", rationale="company authorisation for Jira (test)",
                           details={"office_authorization": True, "company_reference": "CHG-1 signed",
                                    "allowed_providers": ["jira"]}, auth_scope={"providers": ["jira"]})
    db.commit()
    assert office_gate("CAN_USE_OFFICE_INTEGRATIONS", db)[0] is True
    assert office_gate("CAN_PLAN_OFFICE", db)[0] is False  # Claude Max on Office data still not authorised
    assert office_gate("CAN_RUN_WORKER", db)[0] is False


# ------------------------------------------------------------------------------- remedies per OS


def _all_remedies(settings, db, runner) -> list[dict]:
    load_all()
    ctx = make_context(settings, db, runner=runner)
    return [run_check(spec, ctx).remediation for spec in REGISTRY.values() if not spec.functional]


@pytest.mark.parametrize("plat", ["win32", "darwin"])
def test_every_remedy_command_is_valid_for_the_os(settings, db, fake_host, no_docker, monkeypatch, plat):
    from aeos.worker_runtime import opencode

    monkeypatch.setattr(opencode, "opencode_status", lambda configured=None, *, refresh=False: {
        "status": "MISSING", "binary": None, "version": None, "pinned_version": opencode.PINNED_VERSION})
    import aeos.doctor.checks.claude as claude_mod
    import aeos.doctor.checks.host as host_check
    import aeos.doctor.model as model

    for mod in (model, claude_mod, host_check):
        monkeypatch.setattr(mod.sys, "platform", plat)
    fake_host._env = {"ANTHROPIC_API_KEY": "x" * 30}
    monkeypatch.setattr(host_mod, "tcp_in_use", lambda h, p, timeout=1.0: True)
    commands = [r["command"] for r in _all_remedies(settings, db, fake_host) if r and r.get("command")]
    assert len(commands) >= 8
    for cmd in commands:
        assert " # " not in cmd and not cmd.startswith("#"), cmd  # zsh: '#' is not a comment interactively
        if plat == "win32":
            assert "&&" not in cmd, cmd  # Windows PowerShell 5.1
            assert not cmd.startswith("./aeos") and " npm " not in f" {cmd}", cmd
            assert "unset " not in cmd and "chmod" not in cmd, cmd
        else:
            assert ".\\aeos.ps1" not in cmd and "npm.cmd" not in cmd and "$null" not in cmd, cmd
            assert "[Environment]" not in cmd and "Get-NetTCPConnection" not in cmd, cmd
            assert "winget" not in cmd, cmd


def test_openbao_found_in_data_dir_and_vault_remedies(settings, db, fake_host):
    load_all()
    ctx = make_context(settings, db, runner=fake_host)
    missing = run_check(REGISTRY["secret.openbao_binary"], ctx)
    # the repository manifest has no approved OpenBao pin yet: the remedy names the exact-version form the CLI
    # accepts (plain `vault install` is refused without a pin, §07)
    assert missing.status == "MISSING" and missing.remediation["command"].endswith("vault install --version <X.Y.Z>")
    assert "approved_pin" in missing.remediation["operation"]
    health = run_check(REGISTRY["secret.openbao_health"], ctx)
    assert health.status == "MISSING" and health.remediation["command"].endswith("vault install --version <X.Y.Z>")
    local = settings.data_dir / "bin" / ("bao.exe" if sys.platform == "win32" else "bao")
    fake_host.stats[str(local)] = {"size": 10, "mtime_ns": 1}
    fake_host.binaries["bao"] = str(local)  # FakeRunner maps the path back to the name for the version call
    fake_host.outputs[("bao", ("version",))] = ok("OpenBao v2.7.1\n")
    found = run_check(REGISTRY["secret.openbao_binary"], make_context(settings, db, runner=fake_host))
    assert found.status == "VERIFIED" and found.observed_evidence["lookup"] == "data-dir"
    health = run_check(REGISTRY["secret.openbao_health"], make_context(settings, db, runner=fake_host))
    assert health.status == "FAILED" and health.remediation["command"].endswith("vault start")
    store = run_check(REGISTRY["secret.status"], make_context(settings, db, runner=fake_host))
    assert store.status == "MISSING" and "interim-keyring" in store.remediation["operation"]


def test_ops_backups_are_recognised(settings, db, fake_host):
    from aeos.operations import backup

    load_all()
    res = run_check(REGISTRY["recovery.backups"], make_context(settings, db, runner=fake_host))
    assert res.status == "MISSING" and res.remediation["command"].endswith("ops backup create")
    created = backup.create_backup(settings, label="w3", actor="test")
    res = run_check(REGISTRY["recovery.backups"], make_context(settings, db, runner=fake_host))
    assert res.status == "CONFIGURED" and res.observed_evidence["ops_backups"] == 1
    restore = run_check(REGISTRY["recovery.restore_test"], make_context(settings, db, runner=fake_host))
    assert restore.status == "MISSING" and created["backup_id"] in restore.remediation["command"]


# ------------------------------------------------------------------------- 5. startup doctor (lifespan)


def test_startup_doctor_runs_under_lifespan_without_blocking(settings, db, fake_host, no_docker, monkeypatch):
    from fastapi.testclient import TestClient

    import aeos.api.app as app_mod
    from aeos.api.app import create_app
    from aeos.doctor import runner, worker
    from aeos.doctor.views import checks_view

    monkeypatch.setattr(app_mod, "LIFECYCLE_HOOKS", ["aeos.doctor.worker"])
    monkeypatch.setattr(worker, "STARTUP_DELAY_SECONDS", 0.0)
    real = runner.run_doctor
    gate = threading.Event()

    def held(*a, **kw):
        gate.wait(20)  # the Doctor is held until requests were served: startup must not wait for it
        return real(*a, **kw)

    monkeypatch.setattr(runner, "run_doctor", held)
    app = create_app(settings, testing=False, init_db=False)
    started = time.monotonic()
    with TestClient(app, base_url="http://127.0.0.1:8787") as c:
        entered = time.monotonic() - started
        assert c.get("/api/health").json()["status"] == "ok"
        assert worker._thread is not None and worker._thread.is_alive()  # still running in the background
        gate.set()
        deadline = time.monotonic() + 60
        while worker._thread is not None and worker._thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.1)
    assert entered < 15  # a blocking startup would have waited for the 20 s gate (no tight timing)
    db.expire_all()
    run = db.scalar(select(DoctorRun).where(DoctorRun.trigger == "startup"))
    assert run is not None and run.functional is False
    ev = db.scalar(select(Event).where(Event.kind == "doctor.run.completed", Event.entity_id == run.id))
    assert ev is not None
    view = checks_view(db, domain="PERSONAL")
    assert view["last_run"]["trigger"] == "startup" and view["last_run"]["id"] == run.id


# ---------------------------------------------------------------------------- CLI output redaction


def test_cli_output_uses_logical_data_dir(settings, db, fake_host):
    from typer.testing import CliRunner

    from aeos.cli import app

    result = CliRunner().invoke(app, ["doctor", "--group", "HOST"])
    assert str(settings.data_dir) not in result.output and str(Path.home()) not in result.output
    assert "(AEOS_DATA_DIR)" in result.output
    payload = json.loads(CliRunner().invoke(app, ["doctor", "--group", "HOST", "--json"]).output)
    assert payload["data_dir"].endswith("(AEOS_DATA_DIR)") and str(Path.home()) not in payload["data_dir"]


# ------------------------------------------------------------------------- review (adversarial) fixes


def test_credential_writes_keep_the_store_check_and_worker_gate_fresh(settings, db, worker_ready):
    """A Google token refresh (publisher, no session) must not stop worker admission: the store's own status does
    not depend on single references. A change of the store itself (secret_store decision) still invalidates it."""
    from aeos.secrets import facade as secrets

    assert facade.capability_state(db, Capability.CAN_RUN_WORKER, domain="PERSONAL").status == "READY"
    db.commit()
    secrets.put_secret("personal/google-oauth-token", '{"token": "w3-rotated-access-Q7z"}', actor="publisher")
    assert _stored(db, "adapter.google_oauth").status == "STALE"  # the Google check itself is rechecked
    assert _stored(db, "secret.status").status != "STALE"
    assert facade.capability_state(db, Capability.CAN_RUN_WORKER, domain="PERSONAL").status == "READY"
    facade.invalidate(db, invalidator="secret:*", reason="secret backend changed (interim keyring exception)")
    db.commit()
    assert _stored(db, "secret.status").status == "STALE"
    assert facade.capability_state(db, Capability.CAN_RUN_WORKER, domain="PERSONAL").status == "UNKNOWN"


def test_office_credential_refs_never_reach_platform_rows_or_events(settings, db, secrets_memory, fake_host):
    from aeos.secrets import facade as secrets

    create_admin(db)
    _run(settings, db, fake_host)
    # distinctive value: put registers it for redaction process-wide (a run of "x" would redact other tests)
    secrets.put_secret("office/atlassian", "w3-office-atlassian-cred-Q7z", actor="test")
    jira = _stored(db, "adapter.office_jira")  # OFFICE-scoped row: the reference may be named there
    assert jira.status == "STALE" and "office/atlassian" in jira.observed_evidence["stale_reason"]
    platform = [r for r in latest_results(db).values() if not (r.scope or {}).get("domain")]
    assert platform and not any("office/atlassian" in json.dumps(r.observed_evidence or {}) for r in platform)
    stale = db.scalars(select(Event).where(Event.kind == "doctor.check.stale")).all()
    assert stale and all(e.domain == "OFFICE" for e in stale if "office/atlassian" in json.dumps(e.payload))
    # defence in depth: a platform-level result that depends on an office/* reference is marked STALE without
    # quoting the reference, even when the caller's reason names it
    row = latest_results(db)["secret.status"]
    row.invalidators = ["secret:office/*"]
    db.commit()
    assert facade.invalidate(db, invalidator="secret:office/bitbucket", reason="stored office/bitbucket") >= 1
    db.commit()
    row = _stored(db, "secret.status")
    assert row.status == "STALE" and "office/bitbucket" not in json.dumps(row.observed_evidence)
    ev = db.scalars(select(Event).where(Event.kind == "doctor.check.stale", Event.entity_id == "secret.status")
                    .order_by(Event.seq.desc())).first()
    assert ev.domain is None and ev.payload["invalidator_target"] is None
    assert "office/bitbucket" not in json.dumps(ev.payload)
    bb = _stored(db, "adapter.office_bitbucket")
    assert bb.status == "STALE" and "office/bitbucket" in bb.observed_evidence["stale_reason"]


def test_opencode_pin_drift_stops_worker_admission(settings, db, worker_ready, fake_host, monkeypatch):
    """§38/§50: binary version changed from the approved pin -> admission stops until reconciled."""
    from aeos.models.inventory import ComponentObservation
    from aeos.worker_runtime import opencode

    assert facade.capability_state(db, Capability.CAN_RUN_WORKER, domain="PERSONAL").status == "READY"
    monkeypatch.setattr(opencode, "opencode_status", lambda configured=None, *, refresh=False: {
        "status": "INSTALLED", "binary": "/opt/fake/bin/opencode", "version": "1.20.0",
        "pinned_version": opencode.PINNED_VERSION, "drift": True, "functional": "NOT_PROVEN"})
    oc = _run(settings, db, fake_host, groups=["WORKER"])["worker.opencode"]
    assert (oc.status, oc.error_code) == ("FAILED", "OPENCODE_PIN_DRIFT")
    assert f"opencode-ai@{opencode.PINNED_VERSION}" in oc.remediation["command"]
    st = facade.capability_state(db, Capability.CAN_RUN_WORKER, domain="PERSONAL")
    assert st.status == "BLOCKED" and any(r.startswith("worker.opencode: FAILED") for r in st.reasons)
    # still truthfully INSTALLED (with the observed version) in the shared inventory
    obs = db.scalars(select(ComponentObservation).where(ComponentObservation.component_id == "opencode",
                                                        ComponentObservation.dimension == "installation")
                     .order_by(ComponentObservation.observed_at.desc())).first()
    assert (obs.status, obs.observed_version) == ("INSTALLED", "1.20.0")


def test_capability_result_without_live_mode_is_not_proof():
    from aeos.doctor.checks.claude import _bridge_result_outcome

    for result in ({"status": "VERIFIED", "engine_version": "2.1.302"}, {"status": "VERIFIED", "mode": "DEMO"}):
        out = _bridge_result_outcome(result, fresh=True)
        assert (out.status, out.error_code) == ("UNKNOWN", "CAPABILITY_TEST_NOT_LIVE")
    assert _bridge_result_outcome({"status": "VERIFIED", "mode": "LIVE"}, fresh=True).status == "VERIFIED"


def test_startup_failure_is_recorded_without_paths_and_shown_until_the_next_run(settings, db, fake_host,
                                                                               monkeypatch):
    from aeos.doctor import runner, worker
    from aeos.doctor.views import checks_view

    create_admin(db)
    db.commit()
    real = runner.run_doctor

    def broken(*a, **kw):
        raise RuntimeError(f"database is locked: {settings.layout.db_path} (home {Path.home()})")

    monkeypatch.setattr(runner, "run_doctor", broken)
    monkeypatch.setattr(worker, "STARTUP_DELAY_SECONDS", 0.0)
    worker._stop.clear()
    worker._run(settings)
    db.expire_all()
    ev = db.scalar(select(Event).where(Event.kind == "doctor.run.failed"))
    assert ev is not None and ev.domain is None and ev.payload["error"] == "RuntimeError"
    message = ev.payload["message"]
    assert "<data>" in message and str(Path.home()) not in message and Path.home().as_posix() not in message
    failure = checks_view(db, domain="PERSONAL")["last_failure"]
    assert failure["trigger"] == "startup" and failure["error"] == "RuntimeError"
    monkeypatch.setattr(runner, "run_doctor", real)
    _run(settings, db, fake_host, groups=["HOST"])
    assert checks_view(db, domain="PERSONAL")["last_failure"] is None  # a newer run supersedes the failure
