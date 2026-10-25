"""Documents whose §28 timing is not plan approval: release candidate, implementation/PR, production
evidence, improvements, retirement — gathered read-only by docs_engine.service (§27, §28, §41)."""

from __future__ import annotations

import copy

import pytest
from sqlalchemy import select

from aeos.core.errors import Conflict, NotFound, ValidationFailed
from aeos.core.hashing import content_hash
from aeos.docs_engine import service
from aeos.models.delivery_ext import PullRequestRecord
from aeos.models.evidence import AcceptanceRecord, EvidenceArtifact
from aeos.models.planning import Plan, TaskSpec
from aeos.models.publishing import DocumentRecord, DocumentRevision
from aeos.models.research import ChangeProposal
from tests.unit.test_publisher_support import add_version, approve, install_fakes, make_plan, make_project
from tests.unit.test_publisher_support import schema_sample_content as sample

TASKS = [{"task_key": "T1", "feature_key": "F1", "title": "API tugas", "acceptance_criteria": ["T1 works"]},
         {"task_key": "T2", "feature_key": "F2", "title": "Login", "acceptance_criteria": ["T2 works"]}]


@pytest.fixture
def world(settings, db, monkeypatch):
    install_fakes(monkeypatch)
    project = make_project(db)
    plan, version = make_plan(db, project, sample(), tasks=TASKS)
    return {"settings": settings, "db": db, "project": project, "plan": plan, "version": version}


def _doc(db, lid):
    return db.scalar(select(DocumentRecord).where(DocumentRecord.logical_doc_id == lid))


def test_release_candidate_records_one_consistent_snapshot(world):
    db, settings, v = world["db"], world["settings"], world["version"]
    out = service.record_release_candidate(db, settings, v, release_key="1.0.0", commit="A" * 40, actor_user_id="o")
    db.commit()
    assert out["created"] and out["release_plan"] == "07-release-deployment/release-plan-1-0-0"
    rev = db.get(DocumentRevision, out["id"])
    assert rev.kind == "RELEASE_CANDIDATE" and rev.plan_version_id == v.id
    assert rev.details["commit"] == "a" * 40 and rev.details["plan_manifest_hash"] == v.manifest_hash
    docs = {d[0]: d[1] for d in rev.details["documents"]}
    assert docs["07-release-deployment/release-plan-1-0-0"] == _doc(db, "07-release-deployment/release-plan-1-0-0") \
        .content_hash
    assert "08-runbooks/runbook-restart" in docs  # runbooks per scope are part of the snapshot
    assert rev.content_hash == content_hash({k: rev.details[k] for k in (
        "project_id", "release", "commit", "plan_version_id", "plan_manifest_hash", "documents")})
    again = service.record_release_candidate(db, settings, v, release_key="1.0.0", commit="a" * 40, actor_user_id="o")
    assert not again["created"] and again["id"] == out["id"]


def test_release_candidate_is_refused_when_not_ready_or_not_bound(world):
    db, settings, v = world["db"], world["settings"], world["version"]
    with pytest.raises(ValidationFailed):
        service.record_release_candidate(db, settings, v, release_key="1.0.0", commit="main", actor_user_id="o")
    with pytest.raises(NotFound):
        service.record_release_candidate(db, settings, v, release_key="9.9.9", commit="b" * 40, actor_user_id="o")
    plan = db.get(Plan, world["plan"].id)
    content = sample()
    content["release"]["release_plans"][0]["rollback"] = ""
    v2 = add_version(db, plan, content, tasks=TASKS)
    approve(db, plan, v2)
    db.commit()
    with pytest.raises(Conflict) as exc:
        service.record_release_candidate(db, settings, v2, release_key="1.0.0", commit="b" * 40, actor_user_id="o")
    assert exc.value.code == "RELEASE_DOCS_INCOMPLETE"
    assert exc.value.details["pending"][0]["sections"] == ["Rollback"]
    with pytest.raises(Conflict) as exc:  # v1 is no longer the approved version
        service.record_release_candidate(db, settings, world["version"], release_key="1.0.0", commit="b" * 40,
                                         actor_user_id="o")
    assert exc.value.code == "PLAN_NOT_APPROVED"


def _pr(db, project, task, *, status="PENDING", merge=None):
    acc = AcceptanceRecord(task_id=task.id, run_id="run-1", project_id=project.id, domain=project.domain,
                           artifact_hash="sha256:x", head_commit="c" * 40, acceptance_status=status,
                           verifier_evidence_refs=["ev-verifier-1"])
    db.add(acc)
    db.flush()
    pr = PullRequestRecord(project_id=project.id, domain=project.domain, task_id=task.id, run_id="run-1",
                           acceptance_id=acc.id, repo_full_name="owner/contoh", number=7,
                           url="https://github.com/owner/contoh/pull/7", head_ref="aeos/t1", base_ref="main",
                           accepted_head_sha="c" * 40, bundle_hash="sha256:b", merge_commit_sha=merge,
                           merged=bool(merge), state="MERGED" if merge else "OPEN")
    db.add(pr)
    db.commit()
    return acc, pr


def test_change_record_comes_from_the_pr_and_finalises_on_acceptance(world):
    db, settings, v, project = world["db"], world["settings"], world["version"], world["project"]
    task = db.scalar(select(TaskSpec).where(TaskSpec.plan_version_id == v.id, TaskSpec.task_key == "T1"))
    acc, pr = _pr(db, project, task)
    service.render_version(db, settings, v, persist=True, render_visuals=False)
    db.commit()
    ch = _doc(db, "10-changes/change-t1")
    assert ch.status == "DRAFT" and "PENDING" in ch.status_reason
    assert _doc(db, "10-changes").status == "APPROVED"
    acc.acceptance_status = "ACCEPTED"
    pr.merge_commit_sha, pr.merged = "d" * 40, True
    db.commit()
    service.render_version(db, settings, v, persist=True, render_visuals=False)
    db.commit()
    db.refresh(ch)
    assert ch.status == "APPROVED" and "d" * 40 in ch.body_markdown
    prd = _doc(db, "02-requirements/prd-f1")
    assert prd.links["merge_commit"] == ["d" * 40] and prd.links["accepted_head"] == ["c" * 40]


def test_change_record_rollback_and_contract_deltas_come_from_the_changed_files(world, monkeypatch):
    """Rollback notes are the delivery rules applied to the verifier's changed files; an unknown changed set
    yields none (no "no migration changed" claim without evidence)."""
    from aeos.delivery.pr_bundle import _rollback_notes

    db, v, project = world["db"], world["version"], world["project"]
    task = db.scalar(select(TaskSpec).where(TaskSpec.plan_version_id == v.id, TaskSpec.task_key == "T1"))
    _pr(db, project, task, status="ACCEPTED")
    changed = ["backend/aeos/migrations/versions/0099_x.py", "backend/aeos/x/facade.py", "README.md"]
    monkeypatch.setattr(service, "_changed_files", lambda _s, _acc: list(changed))
    (info,) = service.change_infos(db, project)
    assert info.rollback_notes == tuple(_rollback_notes(changed))
    assert info.rollback_notes[0].startswith("Rollback kode")
    assert any("0099_x.py" in n and "does NOT revert data" in n for n in info.rollback_notes)
    assert info.contract_deltas == ("backend/aeos/x/facade.py",)
    monkeypatch.setattr(service, "_changed_files", lambda _s, _acc: [])
    (info,) = service.change_infos(db, project)
    assert info.rollback_notes == () and info.contract_deltas == ()


def test_change_record_deltas_cover_product_contracts_dependencies_and_owner_decisions(world, monkeypatch):
    """§28 Implementation/PR: contract/dependency deltas use the verifier's categories for any product layout,
    and the ACTIVE human owner decisions bound to the task/plan are recorded as the decision delta."""
    from aeos.models.decisions import Decision

    db, settings, v, project = world["db"], world["settings"], world["version"], world["project"]
    task = db.scalar(select(TaskSpec).where(TaskSpec.plan_version_id == v.id, TaskSpec.task_key == "T1"))
    _pr(db, project, task, status="ACCEPTED")
    changed = ["api/openapi.yaml", "proto/tasks.proto", "schema/graphql/tasks.graphql", "package-lock.json",
               "src/app.ts"]
    monkeypatch.setattr(service, "_changed_files", lambda _s, _acc: list(changed))
    dec = Decision(kind="RISK_ACCEPTANCE", actor_user_id="owner", actor_kind="HUMAN", target_type="task",
                   target_id=task.id, target_hash="sha256:t", project_id=project.id, domain=project.domain,
                   status="ACTIVE", rationale="accepted")
    db.add(dec)
    db.commit()
    (info,) = service.change_infos(db, project)
    assert {"api/openapi.yaml", "proto/tasks.proto", "schema/graphql/tasks.graphql"} <= set(info.contract_deltas)
    assert "src/app.ts" not in info.contract_deltas and info.dependency_deltas == ("package-lock.json",)
    assert f"RISK_ACCEPTANCE:{dec.id}" in info.decision_refs
    assert any(d.startswith("APPROVAL:") for d in info.decision_refs), "the plan approval binds the change too"
    result, _d, _s = service.render_version(db, settings, v, persist=False, render_visuals=False)
    doc = result.by_id()["10-changes/change-t1"]
    assert "proto/tasks.proto" in doc.body_markdown and "package-lock.json" in doc.body_markdown
    assert dec.id in doc.body_markdown and dec.id in doc.links["decision"]


def test_change_record_fails_closed_when_the_delivery_rules_are_unavailable(world, monkeypatch):
    """Without the delivery module there is no silent fallback to empty rollback notes / "no contract delta"."""
    import sys

    db, v, project = world["db"], world["version"], world["project"]
    task = db.scalar(select(TaskSpec).where(TaskSpec.plan_version_id == v.id, TaskSpec.task_key == "T1"))
    _pr(db, project, task, status="ACCEPTED")
    monkeypatch.setitem(sys.modules, "aeos.delivery.pr_bundle", None)
    with pytest.raises(ImportError):
        service.change_infos(db, project)


def test_only_trusted_project_evidence_creates_va_documents(world):
    db, settings, v, project = world["db"], world["settings"], world["version"], world["project"]
    common = dict(project_id=project.id, domain=project.domain, kind="VA_REPORT", content_hash="sha256:e",
                  claim="pip-audit", inline={"scope": "API lokal", "method": "pip-audit"},
                  environment={"commit": "e" * 40})
    db.add(EvidenceArtifact(origin="worker", collector_identity="w", trusted=True, **common))  # worker: never
    db.add(EvidenceArtifact(origin="collector", collector_identity="c", trusted=False, **common))  # untrusted
    db.commit()
    result, _d, _s = service.render_version(db, settings, v, persist=False, render_visuals=False)
    assert result.by_id()["04-vulnerability-assessments"].status == "NOT_STARTED"
    db.add(EvidenceArtifact(origin="collector", collector_identity="aeos-scanner", trusted=True, **common))
    db.commit()
    result, _d, _s = service.render_version(db, settings, v, persist=False, render_visuals=False)
    docs = [d for d in result.documents if d.logical_doc_id.startswith("04-vulnerability-assessments/va-")]
    assert len(docs) == 1 and "API lokal" in docs[0].body_markdown


def test_project_proposals_become_improvement_records(world):
    db, settings, v, project = world["db"], world["settings"], world["version"], world["project"]
    db.add(ChangeProposal(kind="ADD", title="Cache daftar tugas", submitted_by="owner", scope_type="PROJECT",
                          project_id=project.id, domain=project.domain, problem="Daftar lambat",
                          expected_benefit="Lebih cepat"))
    db.add(ChangeProposal(kind="ADD", title="Platform-wide", submitted_by="owner", scope_type="PLATFORM"))
    db.commit()
    result, _d, _s = service.render_version(db, settings, v, persist=False, render_visuals=False)
    imps = [d for d in result.documents if d.logical_doc_id.startswith("11-improvements/")]
    assert len(imps) == 1 and "Lebih cepat" in imps[0].body_markdown


def test_retiring_project_gets_the_closure_plan_from_the_checklist(world):
    db, settings, v, project = world["db"], world["settings"], world["version"], world["project"]
    project.lifecycle = "RETIRING"
    db.commit()
    result, _d, _s = service.render_version(db, settings, v, persist=False, render_visuals=False)
    doc = result.by_id()["12-retirement"]
    assert doc.status == "APPROVED" and "owner/contoh" in doc.body_markdown
    assert "Petakan consumer" in doc.body_markdown


def test_platform_erd_is_checked_against_the_real_schema(settings, db, monkeypatch):
    install_fakes(monkeypatch)
    project = make_project(db, key="platform-x")
    project.is_platform = True
    db.commit()
    content = sample()
    content["architecture"]["diagrams"] = [{"key": "erd", "title": "ERD", "kind": "erd",
                                            "mermaid": "erDiagram\n  plan_versions ||--o{ task_specs : has\n"
                                                       "  PlanVersion ||--o{ imaginary_table : x",
                                            "verification_source": "backend/aeos/models"}]
    _plan, v = make_plan(db, project, copy.deepcopy(content), tasks=TASKS)
    result, _d, _s = service.render_version(db, settings, v, persist=False, render_visuals=False)
    erd = result.visuals[0]
    assert erd.verification_status == "MISMATCH" and "imaginary_table" in erd.verification_notes[0]
    assert "plan_versions" not in erd.verification_notes[0] and "PlanVersion" not in erd.verification_notes[0]
