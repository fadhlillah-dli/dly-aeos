"""What a task's worker may receive after a reclassification (ADR-0133, review H1 and M1).

H1: after INTERNAL -> PUBLIC, a PUBLIC task's context pack and prompt carry nothing above PUBLIC: memory, attachments
and project records keep the class they had when they were created, items above the task's effective class are left
out (listed in ``omitted`` with a classification reason), the local MCI source never enters a PUBLIC task's pack, and
the executor refuses to start a run whose pack class the route may not carry.
M1: a raise stops a running run's next model request on a route that may not carry the new class (any route).
"""

from __future__ import annotations

import importlib
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from aeos.api.routers import MODULE_ROUTERS
from aeos.core.db import session_factory
from aeos.core.hashing import content_hash, sha256_bytes
from aeos.core.timeutil import utcnow
from aeos.models.audit import AuditEvent
from aeos.models.planning import Plan, PlanningContext, PlanVersion
from aeos.models.portfolio import Attachment
from aeos.models.research import MemoryRecord
from aeos.models.workforce import WorkerRun
from tests.conftest import create_admin, make_app
from tests.unit.test_workforce_support import make_plan, make_project, make_route

# the real routers (and what they import at module level) load before FakeModules replaces implementation modules
for _mod in MODULE_ROUTERS:
    importlib.import_module(_mod)

INTERNAL_MEMORY = "INTERNAL-MEMORY-NOTE customer list lives in the ops sheet"
PUBLIC_MEMORY = "PUBLIC-MEMORY-NOTE the README describes the public install"
INTERNAL_ATTACHMENT = "INTERNAL-ATTACHMENT-TEXT budget figures for the quarter"
PUBLIC_ATTACHMENT = "PUBLIC-ATTACHMENT-TEXT the public changelog draft"
INTERNAL_PLAN = "INTERNAL-PLAN-OVERVIEW approved while the project was INTERNAL"
PUBLIC_PLAN = "PUBLIC-PLAN-OVERVIEW approved after the project became PUBLIC"
MCI_TEXT = "MCI-SOURCE-TEXT owner-local canonical policy"
REASON = "owner decision (test): the repository is public"


@pytest.fixture
def verified_mci(tmp_path, monkeypatch):
    """A VERIFIED owner-local MCI source on this machine (never the real one)."""
    from aeos.policy import facade as policy

    source = tmp_path / "MCI_V2_SOURCE_OF_TRUTH.md"
    source.write_text(MCI_TEXT + "\n", encoding="utf-8")
    ref = {"status": "VERIFIED", "declared_status": "VERIFIED", "verification_code": "HASH_MATCH", "version": "2",
           "content_hash": "sha256:" + sha256_bytes(source.read_bytes()), "compliance": "ALIGNED",
           "source_location": "<data dir>/policy/canonical"}
    monkeypatch.setattr(policy, "mci_reference", lambda *a, **kw: dict(ref))
    monkeypatch.setattr(policy, "mci_source_path", lambda *a, **kw: source)
    return source


def _attachment(db, project, *, name: str, text: str, klass: str) -> tuple[Attachment, dict[str, Any]]:
    sha = "sha256:" + sha256_bytes(text.encode())
    att = Attachment(project_id=project.id, domain=project.domain, filename=name, stored_name=name, size_bytes=len(text),
                     sha256=sha, classification=klass, scan_status="CLEAN", parse_status="PARSED")
    db.add(att)
    db.flush()
    return att, {"id": att.id, "sha256": sha, "status": "READ", "text": text, "classification": klass,
                 "filename": name}


def _memory(db, project, content: str, klass: str, **kw: Any) -> None:
    db.add(MemoryRecord(project_id=project.id, domain=project.domain, scope="PROJECT", content=content,
                        classification=klass, status="ACCEPTED", **kw))
    db.flush()


def _approved_plan(db, project, overview: str, created_at: Any = None) -> None:
    """An APPROVED plan version (project_doc context); plan versions are append-only, so it is inserted whole."""
    plan = Plan(project_id=project.id, domain=project.domain, title=overview[:20], state="APPROVED")
    db.add(plan)
    db.flush()
    content = {"overview": overview}
    pv = PlanVersion(plan_id=plan.id, project_id=project.id, domain=project.domain, version=1, content=content,
                     manifest_hash=content_hash(content), **({"created_at": created_at} if created_at else {}))
    db.add(pv)
    db.flush()
    plan.approved_version_id = pv.id
    db.flush()


def _prompt_for(task_id: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """The prompt a worker run of this task would receive: delivered artifacts, a fresh task pack, the prompt."""
    from aeos.knowledge import facade as knowledge
    from aeos.models.planning import TaskSpec
    from aeos.worker_runtime import executor

    with session_factory()() as s:
        task = s.get(TaskSpec, task_id)
        artifacts = executor.resolve_artifacts(s, task)
        info = knowledge.build_task_pack(s, task.id, base_commit=None, artifacts=artifacts)
        s.commit()
        pack, _pack_info = executor._context_pack(s, task, "base")
        s.commit()
        prompt = executor.build_prompt(task, findings=[], protected=(), branch="aeos/test", context_pack=pack,
                                       artifacts=artifacts)
        return prompt, info, pack or {}


def test_after_a_lowering_a_public_task_receives_nothing_above_public(db, verified_mci):
    from aeos.portfolio.reclassify import reclassify_project

    admin = create_admin(db)
    project = make_project(db, key="ctx-lowered")
    earlier = utcnow() - timedelta(hours=1)
    # recorded while the project was INTERNAL
    _memory(db, project, INTERNAL_MEMORY, "INTERNAL")
    internal_att, internal_src = _attachment(db, project, name="budget.md", text=INTERNAL_ATTACHMENT, klass="INTERNAL")
    _approved_plan(db, project, INTERNAL_PLAN, created_at=earlier)
    db.commit()
    reclassify_project(db, project=project, domain="PERSONAL", to="PUBLIC", reason=REASON, actor_user_id=admin.id)
    db.commit()
    # recorded after the project became PUBLIC (near misses: they are PUBLIC and must reach the worker)
    _memory(db, project, PUBLIC_MEMORY, "PUBLIC")
    public_att, public_src = _attachment(db, project, name="changelog.md", text=PUBLIC_ATTACHMENT, klass="PUBLIC")
    _approved_plan(db, project, PUBLIC_PLAN)
    plan, _pv, [task] = make_plan(db, project, [{
        "task_key": "public-task", "data_classification": "PUBLIC", "role_key": "planner",
        "input_artifacts": [{"kind": "attachment", "ref": f"attachment:{a.id}", "sha256": a.sha256, "name": a.filename}
                            for a in (internal_att, public_att)]}])
    ctx = PlanningContext(project_id=project.id, domain=project.domain, prompt="p", inputs=[internal_src, public_src])
    db.add(ctx)
    db.flush()
    plan.context_id = ctx.id
    db.commit()

    prompt, info, pack = _prompt_for(task.id)
    for secret in (INTERNAL_MEMORY, INTERNAL_ATTACHMENT, INTERNAL_PLAN, MCI_TEXT):
        assert secret not in prompt, f"{secret[:30]} reached a PUBLIC task's prompt"
    for public in (PUBLIC_MEMORY, PUBLIC_ATTACHMENT, PUBLIC_PLAN):
        assert public in prompt, f"{public[:30]} is PUBLIC and must reach the worker"
    assert pack["classification"] == "PUBLIC"
    assert {i["classification"] for i in pack["items"]} == {"PUBLIC"}
    withheld = {o["ref"]: o for o in info["omitted"] if str(o.get("reason", "")).startswith("classification")}
    assert any(r.startswith("memory:") for r in withheld) and any(r.startswith("task-artifact:") for r in withheld)
    assert all(o["reason"] == f"classification {o['classification']} above the task's class PUBLIC"
               for o in withheld.values())
    assert any("never goes into a pack for a PUBLIC task" in w for w in info["warnings"])
    assert "withheld (data classification above this task's class)" in prompt


def test_an_internal_task_still_receives_internal_context_and_the_mci_source(db, verified_mci):
    """Near miss: the ceiling is the task's class - an INTERNAL task keeps INTERNAL memory and the VERIFIED MCI source."""
    project = make_project(db, key="ctx-internal")
    _memory(db, project, INTERNAL_MEMORY, "INTERNAL")
    _plan, _pv, [task] = make_plan(db, project, [{"task_key": "internal-task", "data_classification": "INTERNAL",
                                                  "role_key": "planner"}])
    db.commit()
    prompt, info, pack = _prompt_for(task.id)
    assert INTERNAL_MEMORY in prompt and MCI_TEXT in prompt
    assert pack["classification"] == "INTERNAL"
    assert not [o for o in info["omitted"] if str(o.get("reason", "")).startswith("classification")]


def test_an_attachment_of_unknown_class_takes_the_projects_highest_class(db):
    """An input known only to the planning context (no attachment row, no class) is unknown-origin project data: it
    takes the highest class the project ever had, so after INTERNAL -> PUBLIC it is withheld from a PUBLIC task. Near
    miss: in an INTERNAL project the same input reaches an INTERNAL task (test_planning_governance)."""
    from aeos.portfolio.reclassify import reclassify_project
    from aeos.worker_runtime import executor

    admin = create_admin(db)
    project = make_project(db, key="ctx-unknown-att")
    reclassify_project(db, project=project, domain="PERSONAL", to="PUBLIC", reason=REASON, actor_user_id=admin.id)
    plan, _pv, [task] = make_plan(db, project, [{
        "task_key": "ghost", "data_classification": "PUBLIC",
        "input_artifacts": [{"kind": "attachment", "ref": "attachment:ghost", "sha256": "sha256:g", "name": "g.md"}]}])
    ctx = PlanningContext(project_id=project.id, domain=project.domain, prompt="p",
                          inputs=[{"id": "ghost", "sha256": "sha256:g", "status": "READ", "text": INTERNAL_ATTACHMENT}])
    db.add(ctx)
    db.flush()
    plan.context_id = ctx.id
    db.commit()
    [art] = executor.resolve_artifacts(db, task)
    assert art["classification"] == "INTERNAL" and art["withheld"] == "classification"
    assert INTERNAL_ATTACHMENT not in art["content"] and "above this task's class PUBLIC" in art["content"]


def test_the_executor_refuses_a_pack_the_route_may_not_carry(db):
    from aeos.worker_runtime.executor import context_class_problem

    public_only = make_route(db, key="worker.public-only", data_classes=["PUBLIC"])
    problem = context_class_problem({"id": "pack-1", "classification": "INTERNAL"}, public_only)
    assert problem is not None and "context pack pack-1 is INTERNAL" in problem
    assert "is not allowed by route worker.public-only" in problem
    assert context_class_problem({"id": "pack-2", "classification": "PUBLIC"}, public_only) is None  # near miss
    assert "is RESTRICTED" in (context_class_problem({"id": "pack-3", "classification": "odd"}, public_only) or "")


# ---------------------------------------------------------------- M1: a raise reaches a running run's next request


UPSTREAM = "https://openrouter.ai/api/v1/chat/completions"
KEY = "sk-or-v1-" + "c" * 48


@pytest.fixture
def running(settings, db, secrets_memory, monkeypatch):
    """A RUNNING run of a PUBLIC task on a route that is not decision C (it allows PUBLIC and INTERNAL)."""
    from aeos.broker import facade as broker
    from tests.unit.test_workforce_support import FakeModules

    FakeModules().install(monkeypatch, skip=("aeos.routing.service",))
    secrets_memory.put("workers/openrouter", KEY, actor="test")
    project = make_project(db, classification="PUBLIC")
    make_route(db, key="worker.class-a", model_id="vendor/model-a", data_classes=["PUBLIC", "INTERNAL"])
    go = make_route(db, key="opencode-go/glm-test", provider="opencode-go", model_id="glm-test", is_free="NO",
                    data_classes=["PUBLIC", "INTERNAL"])
    _plan, _pv, [task] = make_plan(db, project, [{"task_key": "public-run", "data_classification": "PUBLIC"}])
    run = WorkerRun(project_id=project.id, domain="PERSONAL", execution_status="RUNNING",
                    model_route_key="worker.class-a", task_id=task.id)
    db.add(run)
    db.flush()
    raw, grant = broker.issue_grant(db, run_id=run.id, project_id=project.id, domain="PERSONAL", kind="MODEL_PROXY",
                                    scope={"models": {"model-a": "worker.class-a"}}, ttl_seconds=3600, max_requests=20)
    db.commit()
    with TestClient(make_app(settings), base_url="http://testserver") as c:
        yield {"client": c, "token": raw, "grant": grant, "project": project, "go": go}


def _post(env: dict[str, Any]):
    return env["client"].post("/broker/v1/chat/completions",
                              json={"model": "model-a", "messages": [{"role": "user", "content": "hi"}]},
                              headers={"Authorization": f"Bearer {env['token']}"})


def _ok():
    return {"id": "x", "object": "chat.completion", "model": "vendor/model-a",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1}}


def test_a_raise_refuses_the_running_runs_next_request_on_any_route(running, db):
    from aeos.broker import model_proxy
    from aeos.portfolio.reclassify import reclassify_project

    admin = create_admin(db)
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(UPSTREAM).mock(return_value=httpx.Response(200, json=_ok()))
        assert _post(running).status_code == 200  # near miss: the PUBLIC task's request goes through
        project = db.get(type(running["project"]), running["project"].id)
        reclassify_project(db, project=project, domain="PERSONAL", to="CONFIDENTIAL", reason=REASON,
                           actor_user_id=admin.id)
        db.commit()
        r = _post(running)
        assert r.status_code == 403 and r.json()["error"]["code"] == "TASK_CLASS_NOT_ALLOWED", r.text
        assert "the run's task is CONFIDENTIAL" in r.json()["error"]["message"]
        assert len(mock.calls) == 1, "nothing is forwarded after the raise"
    # the same check on a Go (subscription) route: refused after the raise; near miss: a PUBLIC project's run is not
    assert "the run's task is CONFIDENTIAL" in (
        model_proxy.task_class_refusal(db, running["grant"], "opencode-go/glm-test") or "")
    project.data_classification = "PUBLIC"
    db.commit()
    assert model_proxy.task_class_refusal(db, running["grant"], "opencode-go/glm-test") is None
    denied = db.scalars(select(AuditEvent).where(AuditEvent.action == "broker.model_proxy.deny")).all()
    assert any("TASK_CLASS_NOT_ALLOWED" in (a.reason or "") for a in denied)
