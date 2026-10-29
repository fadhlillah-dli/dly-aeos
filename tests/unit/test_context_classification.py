"""What a task's worker may receive after a reclassification (ADR-0133, reviews H1, M1, F1, F3).

A lowering is refused while the project has content (decision 17), so the H1/F1/F3 cases below can only arise for a
project lowered before that rule or by a direct database edit: they are defence in depth, built with
``_legacy_lowering`` (the audit record of a lowering plus the class written directly).

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


def _legacy_lowering(db, project, *, to: str = "PUBLIC") -> None:
    """A lowering recorded before decision 17 (or a direct database edit): the reclassification audit record that the
    class history reads, and the new class written directly - the content stays."""
    from aeos.audit import service as audit

    audit.record(db, actor_type="user", actor_id="legacy", action="portfolio.project.reclassify", target_type="project",
                 target_id=project.id, project_id=project.id, domain=project.domain, reason="legacy lowering (test)",
                 details={"from": project.data_classification, "to": to, "direction": "LOWER"})
    project.data_classification = to
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
    project = make_project(db, key="ctx-lowered")
    earlier = utcnow() - timedelta(hours=1)
    # recorded while the project was INTERNAL
    _memory(db, project, INTERNAL_MEMORY, "INTERNAL")
    internal_att, internal_src = _attachment(db, project, name="budget.md", text=INTERNAL_ATTACHMENT, klass="INTERNAL")
    _approved_plan(db, project, INTERNAL_PLAN, created_at=earlier)
    db.commit()
    _legacy_lowering(db, project)
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


# ---------------------------------------------------------------- F3 and F1 (review round 3, defence in depth)


def test_a_pack_above_the_tasks_class_is_never_given_to_the_worker(db):
    """F3: the newest task pack is checked against the task's effective class when it is used, not only when the
    runtime decides whether to rebuild it - a pack built while the project was INTERNAL is never given to a PUBLIC task,
    also when no rebuild happened (here: none was attempted)."""
    from aeos.knowledge import facade as knowledge
    from aeos.models.planning import TaskSpec
    from aeos.worker_runtime import executor

    project = make_project(db, key="ctx-old-pack")
    _memory(db, project, INTERNAL_MEMORY, "INTERNAL")
    _plan, _pv, [task] = make_plan(db, project, [{"task_key": "pub", "data_classification": "PUBLIC",
                                                  "role_key": "planner"}])
    db.commit()
    with session_factory()() as s:
        info = knowledge.build_task_pack(s, task.id, base_commit=None, artifacts=[])
        s.commit()
    _legacy_lowering(db, db.get(type(project), project.id))
    db.commit()
    with session_factory()() as s:
        pack, used = executor._context_pack(s, s.get(TaskSpec, task.id), "base")
        s.commit()
    assert pack is None and used["used"] is False
    assert f"context pack {info['context_pack_id']} is INTERNAL, above this task's class PUBLIC" in used["reason"]


def test_compiled_artifacts_keep_their_recorded_class_and_unknown_ones_the_highest(db):
    """F1: a compiled copy (a PRD section from an INTERNAL-era plan version, a documentation checkpoint) carries the
    class recorded when it was compiled; one without a recorded class takes the project's highest class; either is
    withheld above the task's class, in the prompt and in the pack. Near misses: a PUBLIC artifact and a plain label
    reach the worker."""
    from aeos.models.planning import TaskSpec
    from aeos.worker_runtime import executor

    project = make_project(db, key="ctx-copies")
    _legacy_lowering(db, project)
    artifacts = [
        {"kind": "plan_section", "ref": "plan:requirements/F1", "name": "PRD F1", "classification": "INTERNAL",
         "content": {"text": "INTERNAL-PRD-SECTION"}},
        {"kind": "documentation_checkpoint", "ref": "doc_checkpoint:r1", "name": "checkpoint",
         "content": {"text": "UNRECORDED-CHECKPOINT"}},
        {"kind": "plan_section", "ref": "plan:requirements/F2", "name": "PRD F2", "classification": "PUBLIC",
         "content": {"text": "PUBLIC-PRD-SECTION"}},
        {"ref": None, "kind": "label", "label": "papan tulis", "resolved": False}]
    # the copies live only in the compiled TaskSpec (as after a compile); its plan version holds no artifact content
    plan = Plan(project_id=project.id, domain=project.domain, title="copies", state="APPROVED")
    db.add(plan)
    db.flush()
    content = {"tasks": [{"task_key": "copies"}]}
    v = PlanVersion(plan_id=plan.id, project_id=project.id, domain=project.domain, version=1, content=content,
                    manifest_hash=content_hash(content))
    db.add(v)
    db.flush()
    task = TaskSpec(plan_version_id=v.id, plan_version_hash=v.manifest_hash, project_id=project.id,
                    domain=project.domain, task_key="copies", title="copies", goal="g", role_key="planner",
                    data_classification="PUBLIC", input_artifacts=artifacts, allowed_paths=["src/**"],
                    spec_hash=content_hash(artifacts))
    db.add(task)
    db.commit()
    arts = executor.resolve_artifacts(db, db.get(TaskSpec, task.id))
    withheld = {a["ref"]: a for a in arts if a.get("withheld")}
    assert set(withheld) == {"plan:requirements/F1", "doc_checkpoint:r1"}
    assert withheld["doc_checkpoint:r1"]["classification"] == "INTERNAL"  # none recorded -> the highest class
    prompt, info, _pack = _prompt_for(task.id)
    for secret in ("INTERNAL-PRD-SECTION", "UNRECORDED-CHECKPOINT"):
        assert secret not in prompt
    assert "PUBLIC-PRD-SECTION" in prompt and "papan tulis" in prompt


def test_the_compiler_records_the_class_of_an_internal_era_plan_section(db):
    """F1 at the source: a plan version created while the project was INTERNAL, compiled after a (legacy) lowering,
    gives its PRD section artifact the class INTERNAL although the task itself is PUBLIC."""
    from aeos.models.planning import TaskSpec
    from aeos.planning import dag
    from tests.unit.test_planning_support import sample_content
    from tests.unit.test_planning_support import task as plan_task

    project = make_project(db, key="ctx-compile")
    content = sample_content(tasks=[plan_task("T1", input_artifacts=["PRD F1"]),
                                    plan_task("T2", deps=["T1"], feature="F2")])
    for t in content["tasks"]:
        del t["data_classification"]  # unstated: the task takes the project's (PUBLIC) class
    plan = Plan(project_id=project.id, domain=project.domain, title="old", state="APPROVED")
    db.add(plan)
    db.flush()
    v = PlanVersion(plan_id=plan.id, project_id=project.id, domain=project.domain, version=1, content=content,
                    manifest_hash=content_hash(content), created_at=utcnow() - timedelta(hours=1))
    db.add(v)
    db.flush()
    plan.current_version_id = plan.approved_version_id = v.id
    _legacy_lowering(db, project)
    db.commit()
    dag.compile_version(db, v, actor="tester")
    db.commit()
    spec = db.scalar(select(TaskSpec).where(TaskSpec.plan_version_id == v.id, TaskSpec.task_key == "T1"))
    assert spec.data_classification == "PUBLIC"
    [prd] = [a for a in spec.input_artifacts if a.get("kind") == "plan_section"]
    assert prd["classification"] == "INTERNAL"


# ---------------------------------------------------------------- decision 19: platform probes never reach a worker

PROBE_CLAIM = "PROBE-CLAIM-MERGE-GATE branch protection of the code host"


def test_platform_probe_evidence_never_reaches_a_task_pack_or_prompt(db):
    """The exempt probe (PROVIDER_CAPABILITY, no run, no task) is not selected into a task's pack, even for an INTERNAL
    task whose ceiling would allow its class - it is never selected, not merely withheld. Near miss: the task's own
    trusted evidence is selected, so the runtime collector did run."""
    from aeos.models.evidence import EvidenceArtifact

    project = make_project(db, key="ctx-probe")
    _plan, _pv, [task] = make_plan(db, project, [{"task_key": "impl", "data_classification": "INTERNAL",
                                                  "role_key": "feature-implementer"}])
    probe = EvidenceArtifact(project_id=project.id, domain=project.domain, kind="PROVIDER_CAPABILITY",
                             origin="collector", collector_identity="github-capability-probe",
                             content_hash="sha256:probe", claim=PROBE_CLAIM, trusted=True)
    own = EvidenceArtifact(project_id=project.id, domain=project.domain, kind="TEST_REPORT", origin="verifier",
                           collector_identity="verifier", content_hash="sha256:own", claim="OWN-TASK-TEST-REPORT",
                           trusted=True, task_id=task.id)
    db.add_all([probe, own])
    db.commit()
    prompt, info, pack = _prompt_for(task.id)
    refs = [i["ref"] for i in pack["items"]] + [o["ref"] for o in info["omitted"]]
    assert f"evidence:{probe.id}" not in refs and PROBE_CLAIM not in prompt
    assert f"evidence:{own.id}" in refs and "OWN-TASK-TEST-REPORT" in prompt


def _exempt_kind_readers(files: list[Any]) -> list[str]:
    from aeos.portfolio.reclassify import EXEMPT_PROBE_KINDS

    return [f"{p}:{i}" for p in files for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if any(k in line for k in EXEMPT_PROBE_KINDS)]


def test_no_pack_or_prompt_builder_selects_an_exempt_probe_kind():
    """Ratchet for decision 19: the exempt probe kinds stay out of every builder of worker or planner input - the
    context packs, the worker runtime, the documentation engine, planning and the Claude bridge - and the task-pack
    runtime collector keeps selecting only the task's own evidence. Exempting a kind that one of them reads would let
    a lowered project's probe reach a lower-class task."""
    import inspect

    from aeos.core.paths import repo_root
    from aeos.docs_engine.service import SIGNAL_EVIDENCE_KINDS
    from aeos.knowledge import packs
    from aeos.portfolio.reclassify import EXEMPT_PROBE_KINDS

    root = repo_root() / "backend" / "aeos"
    files = [p for d in ("knowledge", "worker_runtime", "docs_engine", "planning", "claude_bridge")
             for p in sorted((root / d).glob("*.py"))]
    assert len(files) > 30
    assert _exempt_kind_readers(files) == []
    assert not set(SIGNAL_EVIDENCE_KINDS) & EXEMPT_PROBE_KINDS
    assert "EvidenceArtifact.task_id == task.id" in inspect.getsource(packs._collect_runtime)


def test_bite_the_probe_ratchet_sees_a_builder_that_names_an_exempt_kind(tmp_path):
    bad = tmp_path / "packs.py"
    bad.write_text('q = q.where(EvidenceArtifact.kind == "PROVIDER_CAPABILITY")\n', encoding="utf-8")
    ok = tmp_path / "executor.py"  # near miss: other evidence kinds
    ok.write_text('q = q.where(EvidenceArtifact.kind == "TEST_REPORT")\n', encoding="utf-8")
    assert _exempt_kind_readers([bad, ok]) == [f"{bad}:1"]
