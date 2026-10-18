"""Task DAG validation/compile/projection (§14 consistency rules, §21 TaskSpec, §35 Task DAG,
§50 "Worker A dan B menulis shared schema yang sama", "Jira/GitHub issue Done tanpa evidence")."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from aeos.core.errors import ValidationFailed
from aeos.core.hashing import content_hash
from aeos.core.timeutil import utcnow
from aeos.models.planning import Feature, Milestone, Plan, PlanVersion, TaskDependency, TaskSpec
from aeos.models.workforce import Lease, TaskExecution
from aeos.planning import dag
from tests.unit.test_planning_support import make_project, sample_content, task


def _version(db, project, content):
    plan = Plan(project_id=project.id, domain=project.domain, title="t", state="APPROVED")
    db.add(plan)
    db.flush()
    v = PlanVersion(plan_id=plan.id, project_id=project.id, domain=project.domain, version=1, content=content,
                    manifest_hash=content_hash(content))
    db.add(v)
    db.flush()
    plan.current_version_id = plan.approved_version_id = v.id
    db.commit()
    return plan, v


def _codes(content):
    return {e["code"] for e in dag.validate_graph(content)}


def test_rejects_unknown_self_and_cycle():
    assert _codes(sample_content(tasks=[task("T1", deps=["T9"])])) == {"UNKNOWN_DEPENDENCY"}
    assert _codes(sample_content(tasks=[task("T1", deps=["T1"])])) == {"SELF_DEPENDENCY"}
    errors = dag.validate_graph(sample_content(tasks=[task("A", deps=["C"]), task("B", deps=["A"]), task("C", deps=["B"])]))
    cyc = [e for e in errors if e["code"] == "CYCLE"]
    assert cyc and cyc[0]["path"][0] == cyc[0]["path"][-1] and set(cyc[0]["path"]) == {"A", "B", "C"}
    assert _codes(sample_content(tasks=[task("T1"), task("T1")])) == {"DUPLICATE_KEY"}
    assert _codes(sample_content(tasks=[task("T1", feature="F9")])) == {"UNKNOWN_FEATURE"}
    c = sample_content()
    c["features"][1]["depends_on"] = ["F2"]
    assert "SELF_DEPENDENCY" in _codes(c)
    c = sample_content()
    c["features"][0]["milestone"] = "M9"
    assert "UNKNOWN_MILESTONE" in _codes(c)
    assert _codes(sample_content()) == set()


def test_compile_rejects_invalid_graph(db):
    project = make_project(db)
    _, v = _version(db, project, sample_content(tasks=[task("T1", deps=["T2"]), task("T2", deps=["T1"])]))
    with pytest.raises(ValidationFailed) as exc:
        dag.compile_version(db, v, actor="tester")
    assert exc.value.code == "DAG_INVALID" and exc.value.details["errors"][0]["code"] == "CYCLE"
    assert db.scalar(select(TaskSpec).where(TaskSpec.plan_version_id == v.id)) is None


def test_compile_fills_every_section21_field_and_is_idempotent(db):
    project = make_project(db, classification="CONFIDENTIAL")
    t1 = task("T1", limits={"max_attempts": 5, "timeout_seconds": 0, "max_output_tokens": 999999},
              data_classification="PUBLIC", egress_allowed=[])
    _, v = _version(db, project, sample_content(tasks=[t1, task("T2", deps=["T1"], feature="F2")]))
    res = dag.compile_version(db, v, actor="tester")
    db.commit()
    assert res == {"created": True, "plan_version_id": v.id, "tasks": 2}
    spec = db.scalar(select(TaskSpec).where(TaskSpec.plan_version_id == v.id, TaskSpec.task_key == "T1"))
    assert spec.project_id == project.id and spec.domain == "PERSONAL"
    assert spec.plan_version_hash == v.manifest_hash and spec.feature_key == "F1"
    assert spec.limits == {"max_attempts": 2, "timeout_seconds": 3600, "max_output_tokens": 128000}
    assert spec.egress_allowed == []  # default deny
    assert spec.tools_allowed == dag.DEFAULT_TOOLS
    assert spec.data_classification == "CONFIDENTIAL"  # never below the project's classification
    assert spec.reviewer_policy["required"] is True and spec.reviewer_policy["self_review_forbidden"] is True
    assert spec.verifier_policy["independent"] is True and spec.verifier_policy["suite_read_only"] is True
    assert spec.model_route_policy["paid_fallback"] is False and spec.model_route_policy["supervisor_downgrade"] is False
    assert set(dag.DEFAULT_STOP_CONDITIONS) <= set(spec.stop_conditions)
    assert set(dag.DEFAULT_EVIDENCE) <= set(spec.evidence_requirements)
    assert spec.capability_ttl_seconds == 3600 and spec.capability_scope["branch_prefix"] == "aeos/"
    assert spec.risk_class == "R1" and spec.artifact_schema_version == "1" and spec.base_commit is None
    assert spec.acceptance_criteria and spec.expected_outputs and spec.input_artifacts and spec.allowed_paths
    fields = {c.name: getattr(spec, c.name) for c in TaskSpec.__table__.columns
              if c.name not in ("id", "spec_hash", "created_at")}
    assert spec.spec_hash == content_hash(fields)
    deps = list(db.scalars(select(TaskDependency).where(TaskDependency.plan_version_id == v.id)))
    t2 = db.scalar(select(TaskSpec).where(TaskSpec.plan_version_id == v.id, TaskSpec.task_key == "T2"))
    assert [(d.task_id, d.depends_on_task_id) for d in deps] == [(t2.id, spec.id)]
    assert db.scalar(select(Feature).where(Feature.plan_version_id == v.id, Feature.feature_key == "F2")).depends_on == ["F1"]
    assert db.scalar(select(Milestone).where(Milestone.plan_version_id == v.id)).milestone_key == "M1"
    assert dag.compile_version(db, v, actor="tester")["created"] is False


def test_platform_project_forbids_protected_paths_and_quarantines(db):
    project = make_project(db, is_platform=True, key="dly-aeos")
    tasks = [task("T1", paths=["frontend/src/pages/plans/**"]),
             task("T2", paths=["backend/aeos/policy/interlock.py"], risk_class="R1")]
    _, v = _version(db, project, sample_content(tasks=tasks))
    dag.compile_version(db, v, actor="tester")
    db.commit()
    protected = dag.protected_paths()
    assert "policy/**" in protected and "tests/acceptance/**" in protected and ".github/workflows/**" in protected
    t1 = db.scalar(select(TaskSpec).where(TaskSpec.task_key == "T1", TaskSpec.plan_version_id == v.id))
    assert set(protected) <= set(t1.forbidden_paths)
    t2 = db.scalar(select(TaskSpec).where(TaskSpec.task_key == "T2", TaskSpec.plan_version_id == v.id))
    assert "backend/aeos/policy/**" not in t2.forbidden_paths
    assert t2.reviewer_policy["quarantine_special_review"] is True and t2.risk_class == "R3"


def test_non_platform_project_has_no_platform_protected_defaults(db):
    project = make_project(db)
    _, v = _version(db, project, sample_content())
    dag.compile_version(db, v, actor="tester")
    spec = db.scalars(select(TaskSpec).where(TaskSpec.plan_version_id == v.id)).first()
    assert "policy/**" not in spec.forbidden_paths


@pytest.mark.parametrize(("a", "b", "overlap"), [
    ("src/a/**", "src/a/x.py", True), ("src/a/**", "src/b/**", False), ("src/**", "src/b/c.py", True),
    ("**", "anything", True), ("docs/x.md", "docs/x.md", True), ("./src\\a", "src/a/b", True),
    ("backend/aeos/policy/**", "backend/aeos/planning/**", False),
])
def test_paths_overlap(a, b, overlap):
    assert dag.paths_overlap(a, b) is overlap


def test_dag_view_runtime_conflicts_blockers_and_acceptance(db):
    project = make_project(db)
    tasks = [task("T1", paths=["db/schema.sql"]), task("T2", deps=["T1"], paths=["src/api/**"]),
             task("T3", paths=["db/schema.sql"]), task("T4", deps=["T2"], paths=["src/ui/**"])]
    plan, v = _version(db, project, sample_content(tasks=tasks))
    preview = dag.dag_view(db, plan, v)
    assert preview["compiled"] is False and preview["mode"] == "PREVIEW"
    assert all(n["runtime"]["state"] == "NOT_SCHEDULED" for n in preview["nodes"])
    dag.compile_version(db, v, actor="tester")
    specs = {s.task_key: s for s in db.scalars(select(TaskSpec).where(TaskSpec.plan_version_id == v.id))}
    db.add(TaskExecution(task_id=specs["T1"].id, project_id=project.id, domain="PERSONAL", state="BLOCKED",
                         reason="NEEDS_DECISION: attempts exhausted"))
    db.add(TaskExecution(task_id=specs["T3"].id, project_id=project.id, domain="PERSONAL", state="RUNNING"))
    now = utcnow()
    db.add(Lease(task_id=specs["T3"].id, run_id="run-3", owner="controller-A", acquired_at=now,
                 expires_at=now + timedelta(minutes=5), state="ACTIVE"))
    db.commit()
    view = dag.dag_view(db, plan, v)
    nodes = {n["key"]: n for n in view["nodes"]}
    assert view["compiled"] is True and {e["id"] for e in view["edges"]} == {"T1->T2", "T2->T4"}
    assert nodes["T1"]["runtime"]["state"] == "BLOCKED" and nodes["T3"]["lease"]["owner"] == "controller-A"
    assert nodes["T4"]["runtime"]["state"] == "NOT_SCHEDULED"
    blocker = view["critical_blockers"][0]
    assert blocker["task_key"] == "T1" and blocker["blocks"] == ["T2", "T4"]
    conflict = next(c for c in view["shared_write_conflicts"] if c["tasks"] == ["T1", "T3"])
    assert conflict["concurrent_risk"] is True and nodes["T1"]["conflicts_with"] == ["T3"]
    # acceptance is never inferred from status; verification module absent -> UNKNOWN
    assert nodes["T1"]["acceptance"]["status"] == "UNKNOWN"
    assert nodes["T2"]["dependencies_ready"] is False
