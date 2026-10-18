"""Cost admission guards added in the module-group-H completeness review (master §42, §50 "Budget
DB/gateway tidak sehat -> Admission ditolak bila enforcement tidak dapat dipercaya", "Dashboard
Personal mencoba ... Office -> Backend menolak sesuai scope", AGENTS.md invariant 1 domain-first)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from aeos.core.errors import Blocked, Conflict, ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.cost import facade, service
from aeos.models.cost import BudgetReservation
from aeos.models.portfolio import Project
from aeos.models.routing import ModelRoute
from aeos.models.workforce import WorkerRun
from tests.unit.test_cost_fakes import install_fake_policy


@pytest.fixture
def policy(monkeypatch):
    return install_fake_policy(monkeypatch)


def _req(**kw):
    base = {"run_id": "r1", "project_id": None, "domain": "PERSONAL", "route_key": None, "est_usd": 0.0,
            "est_tokens": 100, "est_requests": 1}
    return facade.ReservationRequest(**(base | kw))


def test_zero_estimate_without_route_is_refused_under_hard_stop(db):
    """A "$0" request with no registered route cannot be proven free: refused, not admitted."""
    with pytest.raises(Blocked) as e:
        facade.reserve(db, _req())
    assert e.value.code == "BUDGET_STATE_UNTRUSTED"
    assert any("no route_key" in r for r in e.value.details["reasons"])
    db.rollback()
    assert db.scalar(select(BudgetReservation)) is None


def test_zero_estimate_on_registered_free_route_is_admitted(db):
    db.add(ModelRoute(route_key="free-flash", pool="WORKER", provider="opencode-zen", model_id="flash", is_free="YES"))
    db.commit()
    r = facade.reserve(db, _req(route_key="free-flash"))
    db.commit()
    assert r.status == "RESERVED" and r.pricing_basis == "FREE_ROUTE"


def test_reservation_domain_is_required_valid_and_matches_project_and_run(db):
    db.add(ModelRoute(route_key="free-flash", pool="WORKER", provider="opencode-zen", model_id="flash", is_free="YES"))
    db.add(Project(id="po", project_key="office-app", name="Office App", domain="OFFICE"))
    db.add(WorkerRun(id="run-office", task_id="t1", project_id="po", domain="OFFICE"))
    db.commit()
    for bad in ({"domain": None}, {"domain": "personal-ish"}):
        with pytest.raises(ValidationFailed):
            facade.reserve(db, _req(route_key="free-flash", **bad))
    with pytest.raises(ValidationFailed) as e:  # domain never rewritten: an OFFICE project stays OFFICE
        facade.reserve(db, _req(route_key="free-flash", project_id="po"))
    assert e.value.code == "DOMAIN_MISMATCH"
    with pytest.raises(ValidationFailed) as e2:
        facade.reserve(db, _req(route_key="free-flash", run_id="run-office"))
    assert e2.value.code == "DOMAIN_MISMATCH"
    ok = facade.reserve(db, _req(route_key="free-flash", project_id="po", run_id="run-office", domain="OFFICE"))
    db.commit()
    assert ok.domain == "OFFICE"


def test_usage_domain_must_match_project(db):
    db.add(Project(id="po", project_key="office-app", name="Office App", domain="OFFICE"))
    db.commit()
    with pytest.raises(ValidationFailed):
        facade.record_usage(db, provider="openrouter", category="WORKER_API", source="provider", cost_usd=0.1,
                            project_id="po", domain="PERSONAL")


def test_usage_cannot_commit_another_scope_reservation(db, policy):
    db.add(ModelRoute(route_key="paid-pro", pool="WORKER", provider="openrouter", model_id="pro", is_free="NO",
                      pricing={"input_per_mtok": 1.0}, pricing_checked_at=utcnow()))
    db.commit()
    service.set_budget(db, actor_user_id="owner", scope_type="GLOBAL", scope_id="*", period="MONTH", limit_usd=5,
                       rationale="pilot budget")
    db.commit()
    r = facade.reserve(db, _req(route_key="paid-pro", est_usd=0.5, project_id="p1"))
    db.commit()
    for kw in ({"project_id": "p2"}, {"domain": "OFFICE"}, {"route_key": "other-route"}):
        with pytest.raises(Conflict):
            facade.record_usage(db, provider="openrouter", category="WORKER_API", source="provider", cost_usd=0.1,
                                reservation_id=r.id, **kw)
        db.rollback()
    assert db.get(BudgetReservation, r.id).status == "RESERVED"


def test_ledger_hides_domainless_entries_of_other_domain_runs(db):
    """An entry without a domain but tied to an OFFICE run never shows in a PERSONAL ledger/summary."""
    db.add(WorkerRun(id="run-office", task_id="t1", project_id="po", domain="OFFICE"))
    db.commit()
    facade.record_usage(db, provider="openrouter", category="WORKER_API", source="provider", cost_usd=7.0,
                        run_id="run-office")
    facade.record_usage(db, provider="openrouter", category="WORKER_API", source="provider", cost_usd=1.0,
                        domain="PERSONAL")
    db.commit()
    assert [e["cost_usd"] for e in service.list_ledger(db, domain="PERSONAL", allowed_project_ids=None)] == [1.0]
    s = service.summary(db, domain="PERSONAL", allowed_project_ids=None, cross_domain_visible=False)
    assert s["totals"]["actual_usd"] == pytest.approx(1.0)
    office = service.list_ledger(db, domain="OFFICE", allowed_project_ids=None)
    assert [e["cost_usd"] for e in office] == [7.0]


def test_mock_usage_can_never_commit_a_live_reservation(db, policy):
    """§50 'Mock ... costs -> tidak menambah usage actual': a MOCK entry is excluded from budgets, so
    letting it commit a LIVE hold would make real spend disappear from the budget."""
    db.add(ModelRoute(route_key="paid-pro", pool="WORKER", provider="openrouter", model_id="pro", is_free="NO",
                      pricing={"input_per_mtok": 1.0}, pricing_checked_at=utcnow()))
    db.commit()
    service.set_budget(db, actor_user_id="owner", scope_type="GLOBAL", scope_id="*", period="MONTH", limit_usd=1,
                       rationale="pilot budget")
    db.commit()
    r = facade.reserve(db, _req(route_key="paid-pro", est_usd=0.6, project_id="p1"))
    db.commit()
    with pytest.raises(ValidationFailed) as e:
        facade.record_usage(db, provider="openrouter", category="WORKER_API", source="fixture", cost_usd=0.6,
                            reservation_id=r.id, mode="MOCK")
    assert e.value.code == "NON_LIVE_RESERVATION_COMMIT"
    db.rollback()
    assert db.get(BudgetReservation, r.id).status == "RESERVED"  # the hold still counts
    with pytest.raises(Blocked) as e2:  # 0.6 held + 0.6 > 1.0
        facade.reserve(db, _req(run_id="r2", route_key="paid-pro", est_usd=0.6, project_id="p2"))
    assert e2.value.code == "BUDGET_EXCEEDED"
    db.rollback()


def test_usage_without_domain_takes_the_recorded_project_or_run_domain(db):
    """Domain-less events reach every session (§35/§50 counts/events): an Office usage entry and its
    dashboard event carry the Office domain recorded on the project/run, never None."""
    from aeos.models.audit import Event

    db.add(Project(id="po", project_key="office-app", name="Office App", domain="OFFICE"))
    db.add(WorkerRun(id="run-office", task_id="t1", project_id="po", domain="OFFICE"))
    db.commit()
    e1 = facade.record_usage(db, provider="openrouter", category="WORKER_API", source="provider", cost_usd=2.0,
                             project_id="po")
    e2 = facade.record_usage(db, provider="openrouter", category="WORKER_API", source="provider", cost_usd=3.0,
                             run_id="run-office")
    db.commit()
    assert e1.domain == "OFFICE" and e2.domain == "OFFICE"
    events = list(db.scalars(select(Event).where(Event.kind == "cost.usage.recorded")))
    assert len(events) == 2 and {ev.domain for ev in events} == {"OFFICE"}
    platform = service.record_usage(db, provider="ci", category="CI", source="collector", quantity=3, unit="minutes")
    db.commit()
    assert platform.domain is None  # genuinely platform-level usage stays platform-level


def test_project_and_run_of_different_domains_are_refused(db):
    db.add(Project(id="pp", project_key="personal-app", name="Personal App", domain="PERSONAL"))
    db.add(WorkerRun(id="run-office", task_id="t1", project_id="po", domain="OFFICE"))
    db.commit()
    with pytest.raises(ValidationFailed) as e:
        facade.record_usage(db, provider="openrouter", category="WORKER_API", source="provider", cost_usd=0.1,
                            project_id="pp", run_id="run-office")
    assert e.value.code == "DOMAIN_MISMATCH"


def test_ledger_view_never_labels_fixture_rows_as_actual(db):
    facade.record_usage(db, provider="openrouter", category="WORKER_API", source="fixture", cost_usd=4.0,
                        domain="PERSONAL", mode="DEMO")
    facade.record_usage(db, provider="openrouter", category="WORKER_API", source="provider", cost_usd=1.0,
                        domain="PERSONAL")
    db.commit()
    kinds = {row["mode"]: row["cost_kind"] for row in service.list_ledger(db, domain="PERSONAL", allowed_project_ids=None)}
    assert kinds == {"DEMO": "DEMO", "LIVE": "ACTUAL"}


def test_admission_lock_held_elsewhere_fails_closed(db):
    """§50 'Budget DB/gateway tidak sehat': when the admission lock cannot be taken (another process
    holds the write lock past busy_timeout) the request is refused, never admitted unchecked."""
    import sqlite3

    from sqlalchemy import text

    db.add(ModelRoute(route_key="free-flash", pool="WORKER", provider="opencode-zen", model_id="flash", is_free="YES"))
    db.commit()
    path = db.get_bind().url.database
    other = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")  # holds the SQLite write lock
        db.execute(text("PRAGMA busy_timeout=200"))
        with pytest.raises(Blocked) as e:
            facade.reserve(db, _req(route_key="free-flash"))
        assert e.value.code == "BUDGET_LOCK_UNAVAILABLE"
        db.rollback()
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert db.scalar(select(BudgetReservation)) is None
    r = facade.reserve(db, _req(route_key="free-flash"))  # lock free again -> admitted normally
    db.commit()
    assert r.status == "RESERVED"


def test_unit_usage_facade_keeps_ci_compute_storage_human_time_separate(db):
    """§42 'Satuan yang dipantau dipisah': non-token units are recorded in their own unit through the
    facade, never converted into dollars, and never summed with worker API cost."""
    facade.record_unit_usage(db, provider="github-actions", category="CI", source="provider-ci", quantity=12,
                             unit="minutes", domain="PERSONAL")
    facade.record_unit_usage(db, provider="local", category="HUMAN_TIME", source="human", quantity=30,
                             unit="minutes", domain="PERSONAL")
    facade.record_unit_usage(db, provider="local", category="STORAGE", source="collector", quantity=2048,
                             unit="bytes", domain="PERSONAL")
    db.commit()
    s = service.summary(db, domain="PERSONAL", allowed_project_ids=None, cross_domain_visible=True)
    cats = {c["category"]: c for c in s["by_category"]}
    assert cats["CI"]["quantity"] == {"minutes": 12.0} and cats["HUMAN_TIME"]["quantity"] == {"minutes": 30.0}
    assert cats["STORAGE"]["quantity"] == {"bytes": 2048.0}
    assert cats["CI"]["unknown_count"] == 1 and s["totals"]["actual_usd"] == 0  # no price -> UNKNOWN, not $0
    for bad in ({"quantity": -1, "unit": "minutes"}, {"quantity": 5, "unit": ""}):
        with pytest.raises(ValidationFailed):
            facade.record_unit_usage(db, provider="ci", category="CI", source="provider-ci", domain="PERSONAL", **bad)
