"""Background publication executor: no-op when idle, processes RUNNING publications, crash recovery (§14)."""

from __future__ import annotations

from aeos.publisher import jobs, worker
from tests.unit.test_publisher_support import (
    confirm_of,
    install_fakes,
    make_plan,
    make_project,
    seed_personal_secrets,
    use_fake_providers,
)


def test_worker_start_stop_is_noop_when_idle(settings):
    worker.start(settings)
    try:
        assert worker.running()
        assert worker.run_once() == 0
    finally:
        worker.stop()
    assert not worker.running()


def test_run_once_processes_running_publications(settings, db, secrets_memory, monkeypatch):
    install_fakes(monkeypatch)
    fakes = use_fake_providers(monkeypatch)
    seed_personal_secrets(secrets_memory)
    project = make_project(db)
    plan, _v = make_plan(db, project)
    pub = jobs.create_preview(db, settings, plan_id=plan.id, actor_user_id="u", options={"targets": ["issues"]},
                              render_visuals=False)
    db.commit()
    assert jobs.execute_publication(db, pub.id, actor_user_id="u", confirm=confirm_of(pub)).allowed
    db.commit()
    assert worker.run_once() == 1
    db.expire_all()
    db.refresh(pub)
    assert pub.status == "PUBLISHED" and fakes["github-issues"].calls
    assert worker.run_once() == 0  # nothing pending anymore


def test_start_recovers_in_flight_operations(settings, db, secrets_memory, monkeypatch):
    install_fakes(monkeypatch)
    use_fake_providers(monkeypatch)
    seed_personal_secrets(secrets_memory)
    project = make_project(db)
    plan, _v = make_plan(db, project)
    pub = jobs.create_preview(db, settings, plan_id=plan.id, actor_user_id="u", options={"targets": ["issues"]},
                              render_visuals=False)
    op = jobs._ops_for(db, pub.id)[0]
    op.status = "IN_FLIGHT"
    db.commit()
    worker.start(settings)
    worker.stop()
    db.expire_all()
    db.refresh(op)
    assert op.status == "UNKNOWN"
