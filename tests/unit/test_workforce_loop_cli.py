"""Scheduler loop lifecycle and `aeos work` CLI (ASCII output, no LLM)."""

from __future__ import annotations

import time

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from aeos.core.db import session_factory
from aeos.models.workforce import WorkerRun
from aeos.workforce import loop, scheduler
from aeos.workforce.cli import app as work_app
from tests.unit.test_workforce_support import (
    FakeModules,
    make_plan,
    make_project,
    make_route,
    process_sandbox_exception,
    seed_and_enable_implementer,
)


@pytest.fixture(name="fakes")
def _fakes_fixture(monkeypatch: pytest.MonkeyPatch) -> FakeModules:
    return FakeModules().install(monkeypatch)


def test_loop_starts_and_stops_with_other_modules_missing(settings):
    loop.start(settings, interval=0.05)
    try:
        time.sleep(0.5)
        cur = loop.current()
        assert loop.is_running() and cur.ticks >= 1 and cur.last_error is None
    finally:
        loop.stop()
    assert not loop.is_running()


def _prepare(fakes):
    with session_factory()() as db:
        p = make_project(db)
        plan, pv, specs = make_plan(db, p, [{"task_key": "a"}])
        fakes.approved[plan.id] = pv.id
        make_route(db)
        fakes.route_key = "worker.test-free"
        seed_and_enable_implementer(db)
        process_sandbox_exception(db)
        scheduler.ensure_executions(db, pv.id, actor="t")
        db.commit()
        return specs[0].id


def test_tick_admits_only_when_enabled_and_not_paused(settings, fakes):
    tid = _prepare(fakes)
    lp = loop.SchedulerLoop(settings)
    info = lp.tick(start_threads=False)
    assert info["leased"] == [] and info["enabled"] is False  # disabled by default
    with session_factory()() as s:
        assert scheduler.get_te(s, tid).state == "READY"
        scheduler.set_scheduler_enabled(s, True, actor="t")
        scheduler.set_scheduling_paused(s, True, actor="t")
        s.commit()
    assert lp.tick(start_threads=False)["leased"] == []
    with session_factory()() as s:
        scheduler.set_scheduling_paused(s, False, actor="t")
        s.commit()
    assert len(lp.tick(start_threads=False)["leased"]) == 1


def test_run_once_executes_leased_runs(settings, fakes):
    _prepare(fakes)
    seen = []
    info = loop.run_once(settings, executor=lambda st, run_id, **kw: seen.append(run_id) or {"run_id": run_id,
                                                                                               "status": "REVIEW"},
                         force_admit=True)
    assert len(info["leased"]) == 1 and seen == info["leased"] and info["results"][0]["status"] == "REVIEW"


def test_cli_status_pause_resume_demo_cancel(settings):
    runner = CliRunner()
    r = runner.invoke(work_app, ["status"])
    assert r.exit_code == 0, r.output
    assert "scheduler enabled=False" in r.output and r.output.isascii()
    assert runner.invoke(work_app, ["pause", "--reason", "x"]).exit_code == 0
    with session_factory()() as s:
        assert scheduler.scheduling_paused(s)
    assert runner.invoke(work_app, ["resume"]).exit_code == 0
    r = runner.invoke(work_app, ["demo-fixtures"])
    assert r.exit_code == 0 and "inserted" in r.output
    with session_factory()() as s:
        demo_ids = [x.id for x in s.scalars(select(WorkerRun).where(WorkerRun.mode == "DEMO"))]
    assert len(demo_ids) == 4
    r = runner.invoke(work_app, ["status"])
    assert "DEMO rows: 4" in r.output and "active runs (LIVE): 0" in r.output
    r = runner.invoke(work_app, ["cancel", demo_ids[0]])
    assert r.exit_code == 0 and "NOT undone" in r.output
    assert runner.invoke(work_app, ["demo-fixtures", "--remove"]).exit_code == 0
    with session_factory()() as s:
        assert s.scalar(select(func.count()).select_from(WorkerRun).where(WorkerRun.mode == "DEMO")) == 0


def test_cli_run_once_respects_pause(settings):
    runner = CliRunner()
    runner.invoke(work_app, ["pause"])
    r = runner.invoke(work_app, ["run-once"])
    assert r.exit_code == 0 and "paused" in r.output.lower()
