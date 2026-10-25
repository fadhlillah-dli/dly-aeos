"""§41 "Kriteria kesiapan menentukan apakah bagian dokumentasi wajib lengkap sebelum tahap berikutnya": the
PROJECT_CREATION and FEATURE_APPROVAL required sections (product1.REQUIRED_SECTIONS) must be complete before
implementation of a feature starts. Workforce admission WAITS (DOCS_NOT_READY) while they are documentation
pending; only the documents of the task's own feature (PRD, test plan) and the project-creation set count."""

from __future__ import annotations

import pytest
from sqlalchemy import update

from aeos.core.hashing import content_hash
from aeos.docs_engine import facade as docs
from aeos.docs_engine import service as docs_service
from aeos.models.workforce import TaskExecution
from aeos.workforce import integrations as ext
from aeos.workforce import scheduler
from tests.unit.test_publisher_support import schema_sample_content
from tests.unit.test_workforce_support import FakeModules, make_plan, make_project


@pytest.fixture(name="fakes")
def _fakes_fixture(monkeypatch: pytest.MonkeyPatch) -> FakeModules:
    return FakeModules().install(monkeypatch)


def _product_plan(db, content):
    """A Product 1 plan version (immutable: content is set at creation) with one task per feature."""
    from aeos.models.planning import Plan, PlanVersion, TaskSpec

    project = make_project(db)
    plan = Plan(project_id=project.id, domain=project.domain, title="Plan", state="APPROVED")
    db.add(plan)
    db.flush()
    pv = PlanVersion(plan_id=plan.id, project_id=project.id, domain=project.domain, version=1, content=content,
                     manifest_hash=content_hash(content))
    db.add(pv)
    db.flush()
    specs = []
    for key, fk in (("T1", "F1"), ("T2", "F2")):
        ts = TaskSpec(plan_version_id=pv.id, plan_version_hash=pv.manifest_hash, project_id=project.id,
                      domain=project.domain, task_key=key, title=key, goal="do the thing", feature_key=fk,
                      allowed_paths=[f"src/{key}/**"], expected_outputs=["code"], acceptance_criteria=["AC1"],
                      spec_hash=content_hash({"k": key, "v": pv.id}))
        db.add(ts)
        specs.append(ts)
    db.flush()
    plan.approved_version_id = pv.id
    db.flush()
    return plan, pv, specs


def _gaps(db, pv, spec):
    return {(b["stage"], b["logical_doc_id"]) for b in docs.stage_blockers(db, pv, task_id=spec.id,
                                                                            feature_key=spec.feature_key)}


def test_complete_plan_has_no_project_blockers_and_a_prd_gap_blocks_only_its_feature(db):
    _plan, pv, (t1, t2) = _product_plan(db, schema_sample_content())
    assert not [g for g in _gaps(db, pv, t1) | _gaps(db, pv, t2) if g[0] == "PROJECT_CREATION"]
    assert not [g for g in _gaps(db, pv, t1) if g[1] == "02-requirements/prd-f1"]

    broken = schema_sample_content()
    prd = next(x for x in broken["requirements"] if x["feature_key"] == "F1")
    prd["functional_requirements"] = []
    next(x for x in broken["features"] if x["key"] == "F1")["requirement"] = ""
    _plan, pv2, (b1, b2) = _product_plan(db, broken)
    gaps1 = _gaps(db, pv2, b1)
    assert ("FEATURE_APPROVAL", "02-requirements/prd-f1") in gaps1, gaps1
    assert ("FEATURE_APPROVAL", "02-requirements/prd-f1") not in _gaps(db, pv2, b2), "F2 work is not held by F1"
    item = next(b for b in docs.stage_blockers(db, pv2, task_id=b1.id, feature_key="F1")
                if b["logical_doc_id"] == "02-requirements/prd-f1")
    assert "Functional Requirements" in item["sections"]


def test_project_creation_gap_holds_every_task_and_task_only_content_is_untouched(db):
    content = schema_sample_content()
    content["security_privacy"]["overview"]["authn_authz"] = ""
    _plan, pv, (t1, t2) = _product_plan(db, content)
    for t in (t1, t2):
        assert any(g[0] == "PROJECT_CREATION" and "security" in g[1] for g in _gaps(db, pv, t)), _gaps(db, pv, t)
    project = make_project(db)
    _p, task_only, specs = make_plan(db, project, [{"task_key": "a"}])
    assert docs.stage_blockers(db, task_only, task_id=specs[0].id) == [], "task-only DAGs have no Product 1 docs"


def test_readiness_is_cached_per_immutable_version(db, monkeypatch):
    _plan, pv, (t1, _t2) = _product_plan(db, schema_sample_content())
    docs.stage_blockers(db, pv, task_id=t1.id, feature_key="F1")
    monkeypatch.setattr(docs_service.r, "render_document_set", lambda **kw: (_ for _ in ()).throw(AssertionError))
    docs.stage_blockers(db, pv, task_id=t1.id, feature_key="F1")  # no re-render for the same version + hash


def test_admission_waits_with_docs_not_ready(db, fakes):
    content = schema_sample_content()
    content["security_privacy"]["overview"]["authn_authz"] = ""
    plan, pv, (t1, _t2) = _product_plan(db, content)
    fakes.approved[plan.id] = pv.id
    scheduler.ensure_executions(db, pv.id, actor="t")
    db.execute(update(TaskExecution).where(TaskExecution.task_id == t1.id).values(state="READY"))
    adm = scheduler.admission_check(db, t1.id)
    waits = dict(adm.waits)
    assert "DOCS_NOT_READY" in waits and "§41" in waits["DOCS_NOT_READY"] and not adm.admitted


def test_a_failing_readiness_check_is_a_wait_never_a_pass(db, monkeypatch):
    _plan, pv, (t1, _t2) = _product_plan(db, schema_sample_content())

    def boom(*_a, **_k):
        raise RuntimeError("renderer bug")

    monkeypatch.setattr(docs, "stage_blockers", boom)
    assert [c for c, _ in ext.documentation_blockers(db, t1, pv)] == ["DOCS_READINESS_UNVERIFIED"]
