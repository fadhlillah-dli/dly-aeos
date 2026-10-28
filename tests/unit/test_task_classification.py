"""The class a task gets and the class routing applies to it (ADR-0133).

* DAG compile: a task with no stated class takes its project's class; a stated class can only raise it; an unknown
  label (task or project) becomes the strictest class, RESTRICTED, with its own finding in the compile output.
* Routing: the higher of the task's compiled class and its project's current class, so raising a project stops a
  less restrictive route from taking its open tasks at once; a project that did not change still routes.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from aeos.core.errors import Blocked
from aeos.core.hashing import content_hash
from aeos.models.planning import Plan, PlanVersion, TaskSpec
from aeos.models.workforce import WorkerRun
from aeos.planning import dag
from aeos.routing import seed, service
from aeos.routing.facade import select_route
from tests.conftest import create_admin
from tests.unit.test_planning_support import make_project, sample_content, task

REASON = "owner raised the class after review (test)"


# ---------------------------------------------------------------- DAG compile


@pytest.mark.parametrize(("project_class", "stated", "expected"), [
    ("PUBLIC", None, "PUBLIC"),            # unstated: the project's class, not a fixed INTERNAL
    ("PUBLIC", "", "PUBLIC"),
    ("PUBLIC", "INTERNAL", "INTERNAL"),    # a stated class raises it
    ("INTERNAL", "PUBLIC", "INTERNAL"),    # and never lowers it below the project
    ("INTERNAL", None, "INTERNAL"),
    ("CONFIDENTIAL", "public", "CONFIDENTIAL"),
    ("PUBLIC", "public", "PUBLIC"),        # case does not matter
])
def test_task_class_follows_the_project_and_a_stated_class_only_raises_it(project_class, stated, expected):
    klass, finding = dag.task_classification(project_class, stated)
    assert (klass, finding) == (expected, None)


@pytest.mark.parametrize("stated", ["SECRET", "Top-Secret", 3, ["PUBLIC"], {"class": "PUBLIC"}])
def test_an_unknown_task_class_is_the_strictest_with_its_own_finding(stated):
    klass, finding = dag.task_classification("PUBLIC", stated)
    assert klass == "RESTRICTED" and finding is not None
    assert finding["code"] == "TASK_CLASS_UNKNOWN" and finding["applied"] == "RESTRICTED"
    assert "compiled as RESTRICTED (the strictest class)" in finding["message"]


def test_an_unknown_project_class_is_the_strictest_with_its_own_finding():
    klass, finding = dag.task_classification("WEIRD", None)
    assert klass == "RESTRICTED" and finding is not None and finding["code"] == "PROJECT_CLASS_UNKNOWN"
    assert dag.task_classification("WEIRD", "PUBLIC")[0] == "RESTRICTED"  # a stated class never lowers it


def _compile(db, project, tasks: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, str]]:
    content = sample_content(tasks=tasks)
    plan = Plan(project_id=project.id, domain=project.domain, title="t", state="APPROVED")
    db.add(plan)
    db.flush()
    v = PlanVersion(plan_id=plan.id, project_id=project.id, domain=project.domain, version=1, content=content,
                    manifest_hash=content_hash(content), mode="LIVE")
    db.add(v)
    db.flush()
    plan.current_version_id = plan.approved_version_id = v.id
    db.commit()
    result = dag.compile_version(db, v, actor="tester")
    db.commit()
    classes = {t.task_key: t.data_classification
               for t in db.scalars(select(TaskSpec).where(TaskSpec.plan_version_id == v.id))}
    return result, classes


def _unstated(key: str, **kw: Any) -> dict[str, Any]:
    t = task(key, **kw)
    del t["data_classification"]
    return t


def test_compile_gives_a_public_project_public_tasks_and_reports_an_unknown_class(db):
    project = make_project(db, key="public-dag", classification="PUBLIC")
    result, classes = _compile(db, project, [
        _unstated("T1"),
        task("T2", deps=["T1"], feature="F2", data_classification="INTERNAL"),
        task("T3", feature="F2", data_classification="SECRET")])
    assert classes == {"T1": "PUBLIC", "T2": "INTERNAL", "T3": "RESTRICTED"}
    [finding] = result["classification_findings"]
    assert (finding["code"], finding["task_key"], finding["stated"]) == ("TASK_CLASS_UNKNOWN", "T3", "SECRET")
    assert "compiled as RESTRICTED" in finding["message"]


def test_compile_keeps_the_project_floor_and_reports_nothing_when_every_class_is_known(db):
    project = make_project(db, key="internal-dag", classification="INTERNAL")
    result, classes = _compile(db, project, [task("T1", data_classification="PUBLIC"), _unstated("T2", feature="F2")])
    assert classes == {"T1": "INTERNAL", "T2": "INTERNAL"}
    assert "classification_findings" not in result


# ---------------------------------------------------------------- routing


@pytest.fixture
def free_route(db, worker_keys, monkeypatch):
    """The owner's decision C route: a free OpenRouter WORKER TRIAL that serves PUBLIC data only."""
    from tests.unit.test_routing_fakes import install_fakes
    from tests.unit.test_routing_free_worker import FREE, _worker

    seed.run(db)
    db.commit()
    install_fakes(monkeypatch)
    _worker(db)
    return FREE


def _open_task(db, project, key: str) -> TaskSpec:
    from tests.unit.test_workforce_support import make_plan

    _plan, _pv, [spec] = make_plan(db, project, [{"task_key": key, "data_classification": "PUBLIC"}])
    return spec


def test_a_raise_stops_a_public_only_route_from_taking_the_open_task_at_once(db, free_route):
    from aeos.portfolio.reclassify import reclassify_project

    admin = create_admin(db)
    raised = make_project(db, key="raised-later", classification="PUBLIC")
    steady = make_project(db, key="stays-public", classification="PUBLIC")
    open_task, other = _open_task(db, raised, "open"), _open_task(db, steady, "steady")
    # the open task's run; it does not hold the route here (model_route_key unset), so the selections below are about
    # the class only, never "route busy"; the per-request check is given the route explicitly
    run = WorkerRun(project_id=raised.id, domain="PERSONAL", execution_status="RUNNING", task_id=open_task.id)
    db.add(run)
    db.flush()
    assert select_route(db, task=open_task).route.route_key == free_route
    route = service.get_route(db, free_route)
    assert service.free_worker_problem(db, route, run_id=run.id) is None

    reclassify_project(db, project=raised, domain="PERSONAL", to="INTERNAL", reason=REASON, actor_user_id=admin.id)
    db.flush()
    with pytest.raises(Blocked) as ei:
        select_route(db, task=open_task)
    assert ei.value.code == "NO_ELIGIBLE_ROUTE" and ei.value.details["classification"] == "INTERNAL"
    assert any("not allowed" in why for why in ei.value.details["rejected"][free_route])
    # the per-request check of the broker refuses the running run's next model call as well
    assert "the run's task is INTERNAL: decision C serves PUBLIC data only" in (
        service.free_worker_problem(db, route, run_id=run.id) or "")
    assert open_task.data_classification == "PUBLIC"  # the TaskSpec itself is unchanged (immutable)
    # near miss: a project that did not change still routes its PUBLIC task to the PUBLIC-only route
    assert select_route(db, task=other).route.route_key == free_route


def test_a_lowered_project_never_relaxes_a_compiled_task(db, free_route):
    """The other direction: the task's own class stays the floor (no silent downgrade of in-flight work)."""
    from tests.unit.test_workforce_support import make_plan

    project = make_project(db, key="lowered-later", classification="PUBLIC")
    _plan, _pv, [spec] = make_plan(db, project, [{"task_key": "internal", "data_classification": "INTERNAL"}])
    with pytest.raises(Blocked) as ei:
        select_route(db, task=spec)
    assert ei.value.details["classification"] == "INTERNAL"
