"""SLIs and deduplicated alerts (master §33 monitoring, §35 alerts).

§33: SLI targets are owner decisions (NO_TARGET until recorded, never "within target"); monitoring
covers outcome, queue age, stale lease, retry, acceptance failure, budget drift, sync freshness and
external integration health. §50 "Dashboard Personal mencoba run/evidence Office -> Backend menolak":
Office SLI data and alerts never reach a Personal session.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from aeos.core.enums import Domain
from aeos.core.timeutil import utcnow
from aeos.models.audit import Event
from aeos.models.cost import BudgetReservation, UsageLedgerEntry
from aeos.models.evidence import AcceptanceRecord
from aeos.models.operations_ext import OpsAlert
from aeos.models.planning import Plan, PlanVersion, TaskSpec
from aeos.models.portfolio import Project, ProjectIntegration
from aeos.models.publishing import ExternalReference
from aeos.models.workforce import Lease, TaskExecution, WorkerRun
from aeos.operations import alerts, sli


def make_task(db, domain: Domain, key: str, state: str = "QUEUED", age_seconds: int = 0):
    project = db.scalar(select(Project).where(Project.project_key == key))
    if project is None:
        project = Project(project_key=key, name=key, domain=domain.value)
        db.add(project)
        db.flush()
    plan = Plan(project_id=project.id, domain=domain.value, title=f"plan {key}")
    db.add(plan)
    db.flush()
    pv = PlanVersion(plan_id=plan.id, project_id=project.id, domain=domain.value, version=1, content={},
                     manifest_hash="sha256:x")
    db.add(pv)
    db.flush()
    spec = TaskSpec(plan_version_id=pv.id, plan_version_hash="sha256:x", project_id=project.id, domain=domain.value,
                    task_key=f"t-{state.lower()}-{age_seconds}", title="t", goal="g", spec_hash="sha256:y")
    db.add(spec)
    db.flush()
    te = TaskExecution(task_id=spec.id, project_id=project.id, domain=domain.value, state=state)
    db.add(te)
    db.flush()
    if age_seconds:
        te.updated_at = utcnow() - timedelta(seconds=age_seconds)
    db.commit()
    return project, spec, te


def _by_key(result):
    return {i["key"]: i for i in result["items"]}


def test_sli_values_and_no_target_status(settings, db):
    project, spec, _ = make_task(db, Domain.PERSONAL, "alpha", "QUEUED", age_seconds=600)
    now = utcnow()
    db.add(Lease(task_id=spec.id, run_id="r0", owner="ctl", acquired_at=now - timedelta(hours=2),
                 expires_at=now - timedelta(minutes=5), state="ACTIVE"))
    r1 = WorkerRun(project_id=project.id, domain="PERSONAL", attempt=1, verifier_status="FAILED")
    r2 = WorkerRun(project_id=project.id, domain="PERSONAL", attempt=2)
    db.add_all([r1, r2])
    db.flush()
    db.add(BudgetReservation(budget_id="b", run_id=r1.id, reserved_usd=1.0, status="COMMITTED"))
    db.add(UsageLedgerEntry(run_id=r1.id, domain="PERSONAL", provider="openrouter", category="WORKER_API",
                            source="broker", cost_usd=1.5))
    db.add(UsageLedgerEntry(run_id=r2.id, domain="PERSONAL", provider="openrouter", category="WORKER_API",
                            source="broker", cost_usd=None))
    db.add(AcceptanceRecord(task_id=spec.id, run_id=r1.id, project_id=project.id, artifact_hash="h",
                            acceptance_status="REJECTED"))
    db.add(ExternalReference(project_id=project.id, domain="PERSONAL", provider="google-docs", logical_id="00",
                             external_id="doc", sync_status="CONFLICT", last_synced_at=now - timedelta(hours=1)))
    db.add(ProjectIntegration(project_id=project.id, domain="PERSONAL", provider="github", purpose="code",
                              capability_status={"push": {"status": "FAILED"}}))
    db.commit()
    result = sli.compute(db, domain=Domain.PERSONAL, settings=settings)
    k = _by_key(result)
    assert k["queue_age_seconds"]["value"] >= 590
    assert k["stale_leases"]["value"] == 1
    assert k["retry_rate"]["value"] == 0.5
    assert k["acceptance_failures"]["value"] == 2  # 1 rejected acceptance + 1 verifier failure
    assert k["budget_drift_usd"]["value"] == 0.5
    assert k["unknown_cost_entries"]["value"] == 1
    assert k["sync_problems"]["value"] == 1 and k["sync_freshness_seconds"]["value"] >= 3500
    assert k["integration_failures"]["value"] == 1
    assert k["recovery_point_age_seconds"]["value"] is None
    assert {i["status"] for i in result["items"]} == {"NO_TARGET"}  # no owner targets -> never "within target"
    assert "owner decisions" in result["targets_note"]


def test_office_data_not_counted_in_personal(settings, db):
    make_task(db, Domain.OFFICE, "omega", "QUEUED", age_seconds=9999)
    k = _by_key(sli.compute(db, domain=Domain.PERSONAL, settings=settings))
    assert k["queue_age_seconds"]["value"] == 0 and k["queue_age_seconds"]["detail"]["queued_tasks"] == 0
    k_office = _by_key(sli.compute(db, domain=Domain.OFFICE, settings=settings))
    assert k_office["queue_age_seconds"]["value"] >= 9990


def test_owner_target_breach_and_within(settings, db):
    make_task(db, Domain.PERSONAL, "alpha", "READY", age_seconds=120)
    sli.set_target(db, "queue_age_seconds", maximum=60, minimum=None, actor_type="cli", actor_id="owner")
    db.commit()
    k = _by_key(sli.compute(db, domain=Domain.PERSONAL, settings=settings))
    assert k["queue_age_seconds"]["status"] == "BREACH" and k["queue_age_seconds"]["target"]["max"] == 60
    sli.set_target(db, "queue_age_seconds", maximum=3600, minimum=None, actor_type="cli", actor_id="owner")
    sli.set_target(db, "budget_drift_usd", maximum=1, minimum=None, actor_type="cli", actor_id="owner")
    db.commit()
    k = _by_key(sli.compute(db, domain=Domain.PERSONAL, settings=settings))
    assert k["queue_age_seconds"]["status"] == "WITHIN_TARGET"
    assert k["budget_drift_usd"]["status"] == "UNKNOWN"  # target set but no data -> UNKNOWN, not within


def test_alerts_are_deduplicated_and_resolved(settings, db):
    _, spec, _ = make_task(db, Domain.PERSONAL, "alpha")
    now = utcnow()
    lease = Lease(task_id=spec.id, run_id="r0", owner="ctl", acquired_at=now - timedelta(hours=1),
                  expires_at=now - timedelta(minutes=1), state="ACTIVE")
    db.add(lease)
    db.commit()
    for _ in range(3):
        sli.evaluate(db, domain=Domain.PERSONAL, settings=settings)
        db.commit()
    raised = list(db.scalars(select(Event).where(Event.kind == "alert.raised")))
    keys = sorted(e.payload["dedup_key"] for e in raised)
    assert keys == ["GLOBAL:restore_untested", "PERSONAL:stale_leases"]  # one event each despite 3 passes
    stale = db.scalar(select(OpsAlert).where(OpsAlert.dedup_key == "PERSONAL:stale_leases"))
    assert stale.occurrences == 3 and stale.domain == "PERSONAL"
    lease.state = "RELEASED"
    db.commit()
    out = sli.evaluate(db, domain=Domain.PERSONAL, settings=settings)
    db.commit()
    assert "PERSONAL:stale_leases" in out["alerts"]["domain"]["resolved"]
    assert db.scalar(select(Event).where(Event.kind == "alert.resolved")) is not None
    db.refresh(stale)
    assert stale.status == "RESOLVED"


def test_alert_listing_is_domain_scoped(settings, db):
    alerts.raise_alert(db, dedup_key="OFFICE:x", kind="k", severity="HIGH", title="office secret project", domain="OFFICE")
    alerts.raise_alert(db, dedup_key="PERSONAL:y", kind="k", severity="LOW", title="mine", domain="PERSONAL")
    alerts.raise_alert(db, dedup_key="GLOBAL:z", kind="k", severity="LOW", title="platform", domain=None)
    db.commit()
    personal = {a["dedup_key"] for a in alerts.list_alerts(db, domain="PERSONAL")}
    assert personal == {"PERSONAL:y", "GLOBAL:z"}
    office_event = db.scalar(select(Event).where(Event.kind == "alert.raised", Event.entity_type == "alert",
                                                 Event.domain == "OFFICE"))
    assert office_event is not None  # carries its domain so the SSE filter keeps it out of Personal


def test_sli_api_requires_domain_and_scopes_alerts(admin_client, db, settings):
    r = admin_client.get("/api/ops/sli")
    assert r.status_code == 409 and r.json()["error"]["code"] == "DOMAIN_NOT_SELECTED"
    alerts.raise_alert(db, dedup_key="OFFICE:x", kind="k", severity="HIGH", title="office", domain="OFFICE")
    db.commit()
    admin_client.select_domain(Domain.PERSONAL)
    body = admin_client.get("/api/ops/sli").json()
    assert body["domain"] == "PERSONAL" and len(body["items"]) == len(sli.SLI_DEFS)
    assert all(a["domain"] != "OFFICE" for a in admin_client.get("/api/ops/alerts?status=ALL").json()["items"])
    ev = admin_client.post("/api/ops/sli/evaluate")
    assert ev.status_code == 200 and "alerts" in ev.json()


def test_unknown_sli_target_rejected(db):
    import pytest

    from aeos.core.errors import ValidationFailed

    with pytest.raises(ValidationFailed):
        sli.set_target(db, "availability_percent", maximum=99.9, minimum=None, actor_type="cli", actor_id="x")


def test_loop_run_once_respects_flag(settings, db):
    from aeos.operations import flags, loop

    out = loop.run_once(settings)
    assert set(out) == {"PERSONAL", "OFFICE"}
    flags.set_flag(db, "ops.sli_loop_enabled", False, actor_type="cli", actor_id="owner")
    db.commit()
    assert "skipped" in loop.run_once(settings)
    loop.start(settings, first_delay=3600)
    assert loop.state()["running"] is True
    loop.stop()
    assert loop.state()["running"] is False


def test_loop_failure_in_one_domain_does_not_skip_the_other(settings, db, monkeypatch):
    """Alerts keep their domain scope and one failing domain never silences the other (§33 monitoring);
    the error is visible in the service inventory instead of being swallowed."""
    from aeos.operations import loop, services

    real = sli.evaluate

    def flaky(session, *, domain, settings=None):
        if domain == Domain.PERSONAL:
            raise RuntimeError("personal data unreadable")
        return real(session, domain=domain, settings=settings)

    monkeypatch.setattr(sli, "evaluate", flaky)
    try:
        out = loop.run_once(settings)
        assert out["PERSONAL"] == {"error": "RuntimeError"}
        assert set(out["OFFICE"]) == {"domain", "platform"}  # OFFICE was still evaluated
        assert "PERSONAL: RuntimeError" in loop.state()["last_error"]
        inv = {i["service"]: i for i in services.service_inventory(settings, probe=False)["items"]}
        assert inv["aeos.operations.loop"]["status"] == "DEGRADED"
        monkeypatch.setattr(sli, "evaluate", real)
        loop.run_once(settings)
        assert loop.state()["last_error"] is None
    finally:
        loop._state["last_error"] = None  # module-level state: never leak into other tests
