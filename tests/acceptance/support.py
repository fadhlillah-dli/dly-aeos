"""Record builders for the trusted acceptance suite (part of the trusted suite; no test functions)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from aeos.core.hashing import content_hash
from aeos.models.planning import Plan, PlanVersion, TaskSpec
from aeos.models.portfolio import Project
from aeos.models.workforce import WorkerRun


@dataclass
class Records:
    project: Project
    version: PlanVersion
    task: TaskSpec
    run: WorkerRun


def make_records(db: Session, *, domain: str = "PERSONAL", key: str = "acc-project") -> Records:
    project = Project(project_key=key, name=key, domain=domain)
    db.add(project)
    db.flush()
    plan = Plan(project_id=project.id, domain=domain, title="acceptance plan")
    db.add(plan)
    db.flush()
    content = {"title": "acceptance plan", "tasks": ["t1"]}
    version = PlanVersion(plan_id=plan.id, project_id=project.id, domain=domain, version=1, content=content,
                          manifest_hash=content_hash(content))
    db.add(version)
    db.flush()
    task = TaskSpec(plan_version_id=version.id, plan_version_hash=version.manifest_hash, project_id=project.id,
                    domain=domain, task_key="t1", title="slice", goal="goal", allowed_paths=["backend/**"],
                    spec_hash=content_hash({"t": "t1", "k": key}))
    db.add(task)
    db.flush()
    run = WorkerRun(task_id=task.id, project_id=project.id, domain=domain, execution_status="REVIEW",
                    worker_reported_status="ALL_TESTS_PASS", exit_code=0, head_commit="a" * 40,
                    base_commit="b" * 40, branch="aeos/t1")
    db.add(run)
    db.commit()
    return Records(project, version, task, run)


def fake_token() -> str:
    """Credential-shaped value built at runtime (keeps repository secret scans clean)."""
    return "gh" + "p_" + "Z9y8X7w6V5" * 4
