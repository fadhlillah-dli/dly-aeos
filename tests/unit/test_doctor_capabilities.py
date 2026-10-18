"""Capability gates, STALE/EXPIRED handling, invalidation and OFFICE default deny (§08, §23, §38).

§50 rows covered:
* "Doctor hanya berhasil connect" / "Probe connectivity berhasil tetapi functional check gagal"
  -> CONNECTED + FAILED is not READY.
* "Config/model/provider berubah setelah doctor" -> related checks STALE and rechecked.
* "Binary/model route/config berganti dari approved pin" -> recheck before admission.
* "Tool opsional atau Office belum ready" -> dashboard capability unaffected; Office BLOCKED by default.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from aeos.core.enums import Capability
from aeos.core.errors import Blocked
from aeos.core.timeutil import utcnow
from aeos.doctor import facade
from aeos.doctor.capabilities import RULES, checks_for_capability, evaluate, invalidate
from aeos.doctor.checks import REGISTRY, load_all
from aeos.doctor.runner import latest_results, run_doctor
from aeos.inventory.probes import HttpResult, set_runner_override
from aeos.models.audit import Event
from aeos.models.inventory import CheckResult, ComponentObservation
from tests.unit.test_inventory_fakes import base_runner, ok


def ok_row(cid: str, *, status: str | None = None, expires_in: int = 3600, fingerprint: str | None = None) -> dict:
    load_all()
    spec = REGISTRY[cid]
    st = status or (sorted(spec.ok_statuses)[0] if spec.ok_statuses else "UNKNOWN")
    now = utcnow()
    return {"check_id": cid, "status": st, "summary": f"{cid} {st}", "expires_at": (now + timedelta(seconds=expires_in)).isoformat(),
            "last_checked_at": now.isoformat(), "fingerprint": fingerprint, "remediation": {"operation": "x"},
            "observed_evidence": {}}


def all_ok(cap: Capability) -> dict[str, dict]:
    return {cid: ok_row(cid) for cid in checks_for_capability(cap)}


def ev(cap, rows, *, domain=None, fps=None, session=None):
    return evaluate(cap, rows, now=utcnow(), fps=fps or {}, domain=domain, session=session)


def test_all_ok_is_ready_and_human_only_is_never_a_capability():
    rows = all_ok(Capability.CAN_OPEN_DASHBOARD)
    assert ev(Capability.CAN_OPEN_DASHBOARD, rows).status == "READY"
    assert not [c for c in Capability if "MERGE" in c.value or "PRODUCTION" in c.value or "DEPLOY" in c.value]


def test_connected_but_functional_failed_is_not_ready():
    rows = all_ok(Capability.CAN_REVIEW_WITH_MAX)
    rows["claude.auth"] = ok_row("claude.auth", status="CONNECTED")
    rows["claude.capability_test"] = ok_row("claude.capability_test", status="FAILED")
    st = ev(Capability.CAN_REVIEW_WITH_MAX, rows)
    assert st.status == "BLOCKED"
    assert any("claude.capability_test: FAILED" in r for r in st.reasons)


def test_unproven_bridge_is_degraded_not_ready():
    rows = all_ok(Capability.CAN_PLAN_PERSONAL)
    rows["claude.capability_test"] = ok_row("claude.capability_test", status="UNKNOWN")
    st = ev(Capability.CAN_PLAN_PERSONAL, rows)
    assert st.status == "DEGRADED"
    assert any("claude.capability_test" in r for r in st.reasons)


def test_never_run_stale_and_expired_critical_checks_are_unknown():
    rows = all_ok(Capability.CAN_OPEN_DASHBOARD)
    del rows["runtime.db"]
    assert ev(Capability.CAN_OPEN_DASHBOARD, rows).status == "UNKNOWN"
    rows = all_ok(Capability.CAN_OPEN_DASHBOARD)
    rows["runtime.db"] = ok_row("runtime.db", expires_in=-10)
    st = ev(Capability.CAN_OPEN_DASHBOARD, rows)
    assert st.status == "UNKNOWN" and any("EXPIRED" in r for r in st.reasons)
    rows = all_ok(Capability.CAN_OPEN_DASHBOARD)
    rows["host.port"] = ok_row("host.port", fingerprint="sha256:old")
    st = ev(Capability.CAN_OPEN_DASHBOARD, rows, fps={"host.port": "sha256:new"})
    assert st.status == "UNKNOWN" and any("STALE" in r for r in st.reasons)
    rows = all_ok(Capability.CAN_OPEN_DASHBOARD)
    rows["auth.bootstrap"] = ok_row("auth.bootstrap", status="MISSING")
    st = ev(Capability.CAN_OPEN_DASHBOARD, rows)
    assert st.status == "BLOCKED" and st.remedies


def test_optional_tools_do_not_block_unrelated_capabilities():
    rows = all_ok(Capability.CAN_OPEN_DASHBOARD)
    rows["containment.docker"] = ok_row("containment.docker", status="MISSING")
    rows["secret.openbao_health"] = ok_row("secret.openbao_health", status="MISSING")
    rows["adapter.office_jira"] = ok_row("adapter.office_jira", status="NOT_APPLICABLE")
    assert ev(Capability.CAN_OPEN_DASHBOARD, rows).status == "READY"
    assert "containment.docker" not in checks_for_capability(Capability.CAN_PLAN_PERSONAL)


def test_office_capabilities_blocked_by_default(db, monkeypatch):
    for cap in (Capability.CAN_PLAN_OFFICE, Capability.CAN_USE_OFFICE_INTEGRATIONS):
        st = ev(cap, all_ok(cap), session=db)
        assert st.status == "BLOCKED"
        assert "OFFICE default deny" in st.reasons[0]
    worker = ev(Capability.CAN_RUN_WORKER, all_ok(Capability.CAN_RUN_WORKER), domain="OFFICE", session=db)
    assert worker.status == "BLOCKED" and "external_worker_allowed" in worker.reasons[0]
    review = ev(Capability.CAN_REVIEW_WITH_MAX, all_ok(Capability.CAN_REVIEW_WITH_MAX), domain="OFFICE", session=db)
    assert review.status == "BLOCKED"
    # PERSONAL is not office-gated; READY additionally needs the live ADR-0051 sandbox requirement
    import aeos.worker_runtime.facade as worker

    monkeypatch.setattr(worker, "sandbox_readiness", lambda s, *, domain: {
        "status": "READY" if domain == "PERSONAL" else "BLOCKED", "kind": "container", "reasons": [],
        "remedies": [], "residual_risks": []})
    assert ev(Capability.CAN_RUN_WORKER, all_ok(Capability.CAN_RUN_WORKER), domain="PERSONAL",
              session=db).status == "READY"
    assert ev(Capability.CAN_RUN_WORKER, all_ok(Capability.CAN_RUN_WORKER), domain="PERSONAL").status == "UNKNOWN"
    assert ev(Capability.CAN_PLAN_PERSONAL, all_ok(Capability.CAN_PLAN_PERSONAL), domain="OFFICE").status == \
        "NOT_APPLICABLE"


def test_office_unblocks_only_with_policy_flag(db, monkeypatch):
    import aeos.doctor.office as office

    monkeypatch.setattr(office, "read_office_policy", lambda s, root=None: (
        {**office.DEFAULT_DENY, "integrations_allowed": True}, "policy-module"))
    st = ev(Capability.CAN_USE_OFFICE_INTEGRATIONS, all_ok(Capability.CAN_USE_OFFICE_INTEGRATIONS), session=db)
    assert st.status == "READY"


def test_facade_require_capability_raises_blocked(db):
    with pytest.raises(Blocked) as ei:
        facade.require_capability(db, Capability.CAN_RUN_WORKER, domain="PERSONAL")
    assert ei.value.code == "CAPABILITY_NOT_READY" and ei.value.details["reasons"]
    st = facade.capability_state(db, Capability.CAN_OPEN_DASHBOARD)
    assert st.status in ("UNKNOWN", "BLOCKED")  # never READY without checks


@pytest.fixture
def runner():
    r = base_runner()
    r.add_binary("claude")
    r.outputs[("claude", ("--version",))] = ok("2.1.302 (Claude Code)\n")
    r.stats[r.binaries["claude"]] = {"size": 100, "mtime_ns": 1}
    set_runner_override(r)
    yield r
    set_runner_override(None)


def test_fingerprint_change_marks_previous_stale_and_requires_recheck(settings, db, runner):
    run_doctor(settings, db, groups=["CLAUDE_NATIVE"], runner=runner)
    db.commit()
    first = latest_results(db, ["claude.binary"])["claude.binary"]
    assert first.status == "VERIFIED" and first.fingerprint
    runner.stats[runner.binaries["claude"]] = {"size": 200, "mtime_ns": 2}  # binary upgraded in place
    st = facade.capability_state(db, Capability.CAN_REVIEW_WITH_MAX)
    assert st.status != "READY" and any("claude.binary: STALE" in r for r in st.reasons)
    run_doctor(settings, db, groups=["CLAUDE_NATIVE"], runner=runner)
    db.commit()
    db.refresh(first)
    assert first.status == "STALE" and first.observed_evidence["status_before_stale"] == "VERIFIED"
    assert db.scalar(select(Event).where(Event.kind == "doctor.check.stale")) is not None
    newest = latest_results(db, ["claude.binary"])["claude.binary"]
    assert newest.id != first.id and newest.status == "VERIFIED"


def test_config_change_invalidation(settings, db, runner):
    runner.http["http://127.0.0.1:8200/v1/sys/health"] = HttpResult(200, {"initialized": True, "sealed": False})
    run_doctor(settings, db, groups=["SECRET"], runner=runner)
    db.commit()
    assert latest_results(db, ["secret.openbao_health"])["secret.openbao_health"].status == "CONNECTED"
    obs_before = db.scalar(select(ComponentObservation).where(ComponentObservation.component_id == "openbao",
                                                              ComponentObservation.dimension == "connectivity"))
    assert obs_before is not None
    n = facade.invalidate(db, invalidator="config:openbao_addr", reason="alamat vault diganti")
    db.commit()
    assert n >= 1
    row = latest_results(db, ["secret.openbao_health"])["secret.openbao_health"]
    assert row.status == "STALE" and "alamat vault diganti" in row.observed_evidence["stale_reason"]
    db.refresh(obs_before)
    assert obs_before.expires_at <= utcnow()  # inventory observation invalidated too
    assert invalidate(db, invalidator="config:openbao_addr") == 0  # idempotent


def test_settings_change_is_detected_as_stale_without_rerun(settings, db, runner, monkeypatch):
    from aeos.doctor.views import checks_view

    run_doctor(settings, db, groups=["HOST"], runner=runner)
    db.commit()
    before = {i["check_id"]: i for i in checks_view(db, domain="PERSONAL")["items"]}
    assert before["host.port"]["effective_status"] != "STALE"
    monkeypatch.setenv("AEOS_PORT", "8799")
    from aeos.core.config import reset_settings_cache

    reset_settings_cache()
    try:
        after = {i["check_id"]: i for i in checks_view(db, domain="PERSONAL")["items"]}
        assert after["host.port"]["effective_status"] == "STALE"
    finally:
        monkeypatch.delenv("AEOS_PORT")
        reset_settings_cache()


def test_wildcard_invalidators_match(settings, db, runner, secrets_memory):
    run_doctor(settings, db, groups=["SECRET", "MODEL_ROUTE"], runner=runner)
    db.commit()
    n = invalidate(db, invalidator="secret:workers/openrouter", reason="kunci dirotasi")
    db.commit()
    stale = {r.check_id for r in db.scalars(select(CheckResult).where(CheckResult.status == "STALE"))}
    assert n >= 2 and {"route.worker_key", "secret.status"} <= stale


def test_every_capability_rule_references_registered_checks():
    load_all()
    for cap in Capability:
        assert cap in RULES
        for cid in checks_for_capability(cap):
            assert cid in REGISTRY, (cap, cid)
