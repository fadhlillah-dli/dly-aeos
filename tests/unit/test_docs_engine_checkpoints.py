"""§28 "unified narrative/diagrams updated at an approved checkpoint" and §41 "delta pada PR/checkpoint, finalisasi
pada accepted milestone/release; pada checkpoint yang benar-benar diterima Tech Writer/Diagram Author menyusun
narasi & visual terpadu; Claude memeriksa paket perubahan material".

* A checkpoint is recorded only when really accepted: an ArchitectureSnapshot with a human APPROVAL bound to its
  hash (platform project), or a milestone whose every TaskSpec has a valid ACCEPTED acceptance.
* The immutable package names the changed documents AND sections (hash diff against the previous checkpoint), the
  architecture delta between the approved snapshots, and carries a Tech Writer / Diagram-Visual Author TaskSpec
  DRAFT (never executable before the owner approves it into a plan version).
* Claude's review of a material package is advisory; a non-material package is not sent.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aeos.claude_bridge import bridge
from aeos.claude_bridge.facade import ClaudeResult
from aeos.core.errors import Conflict, ValidationFailed
from aeos.core.hashing import content_hash
from aeos.docs_engine import checkpoints
from aeos.models.decisions import ArchitectureSnapshot, Decision
from aeos.models.publishing import DocumentRecord, DocumentRevision
from tests.unit.test_publisher_support import install_fakes, make_plan, make_project
from tests.unit.test_publisher_support import schema_sample_content as sample

TASKS = [{"task_key": "T1", "feature_key": "F1", "title": "API tugas", "acceptance_criteria": ["T1 works"]},
         {"task_key": "T2", "feature_key": "F2", "title": "Login", "acceptance_criteria": ["T2 works"]}]


@pytest.fixture
def world(settings, db, monkeypatch):
    install_fakes(monkeypatch)
    project = make_project(db)
    _plan, version = make_plan(db, project, sample(), tasks=TASKS)
    accepted: set[str] = set()
    monkeypatch.setattr("aeos.verification.facade.acceptance_is_valid",
                        lambda session, task_id, **kw: task_id in accepted)
    return {"settings": settings, "db": db, "project": project, "version": version, "accepted": accepted}


def _tasks(db, version):
    from aeos.models.planning import TaskSpec

    return {t.task_key: t for t in db.query(TaskSpec).filter_by(plan_version_id=version.id)}


def test_a_milestone_checkpoint_needs_every_task_accepted_and_finalises_its_documentation(world):
    db, settings, v = world["db"], world["settings"], world["version"]
    tasks = _tasks(db, v)
    with pytest.raises(Conflict) as exc:
        checkpoints.record_checkpoint(db, settings, v, kind="MILESTONE", ref="M1", actor_user_id="owner")
    assert exc.value.code == "MILESTONE_NOT_ACCEPTED" and set(exc.value.details["tasks"]) == {"T1", "T2"}
    world["accepted"].add(tasks["T1"].id)
    with pytest.raises(Conflict):
        checkpoints.record_checkpoint(db, settings, v, kind="MILESTONE", ref="M1", actor_user_id="owner")
    world["accepted"].add(tasks["T2"].id)
    out = checkpoints.record_checkpoint(db, settings, v, kind="MILESTONE", ref="M1", actor_user_id="owner")
    db.commit()
    assert out["created"] and out["kind"] == "MILESTONE" and out["finalised"] is True and out["material"] is True
    changed = {c["logical_doc_id"]: c for c in out["changed_documents"]}
    assert "03-architecture-overview" in changed and changed["03-architecture-overview"]["change"] == "ADDED"
    assert changed["03-architecture-overview"]["sections"], "the changed-section list is never empty for a new doc"
    task = out["writer_task"]
    assert task["role_key"] == "technical-writer" and "diagram-visual-author" in task["supporting_roles"]
    assert task["status"] == "DRAFT_FOR_NEXT_PLAN_VERSION" and task["changed_sections"]
    rev = db.get(DocumentRevision, out["id"])
    assert rev.kind == checkpoints.CHECKPOINT_KIND and rev.logical_doc_id == "03-architecture-overview"
    narrative = db.query(DocumentRecord).filter_by(project_id=world["project"].id,
                                                   logical_doc_id="03-architecture-overview").one()
    assert narrative.links["checkpoint"]["revision_id"] == rev.id
    again = checkpoints.record_checkpoint(db, settings, v, kind="MILESTONE", ref="M1", actor_user_id="owner")
    assert again["created"] is False and again["id"] == rev.id, "same package -> same record"
    with pytest.raises(Exception) as missing:
        checkpoints.record_checkpoint(db, settings, v, kind="MILESTONE", ref="M9", actor_user_id="owner")
    assert "M9" in str(missing.value)


def test_section_hashes_name_only_the_sections_that_changed():
    body_a = ("## A\n<!-- aeos:section=context anchor=a content_type=AI -->\nsatu\n"
              "## B\n<!-- aeos:section=decisions anchor=b content_type=AI -->\ndua\n")
    body_b = body_a.replace("dua", "dua (diubah)")
    a, b = checkpoints.section_hashes(body_a), checkpoints.section_hashes(body_b)
    assert set(a) == {"context", "decisions"}
    assert a["context"] == b["context"] and a["decisions"] != b["decisions"]


def _snapshot(db, project, nodes, *, approved=True) -> ArchitectureSnapshot:
    refs = {"scope": {"domain": project.domain}, "nodes": [{"id": n} for n in nodes], "edges": [],
            "manifest": {"content_hash": content_hash(nodes)}}
    snap = ArchitectureSnapshot(label="cp", refs=refs, snapshot_hash=content_hash(refs), created_by="owner")
    db.add(snap)
    db.flush()
    if approved:
        db.add(Decision(kind="APPROVAL", actor_user_id="owner", actor_kind="HUMAN", target_type="architecture_snapshot",
                        target_id=snap.id, target_hash=snap.snapshot_hash, status="ACTIVE", domain=project.domain,
                        rationale="checkpoint diterima"))
        db.flush()
    return snap


def test_an_approved_inventory_checkpoint_updates_the_platform_narrative_with_its_delta(world):
    db, settings, v, project = world["db"], world["settings"], world["version"], world["project"]
    first = _snapshot(db, project, ["control-plane", "sqlite"])
    with pytest.raises(ValidationFailed) as exc:
        checkpoints.record_checkpoint(db, settings, v, kind="ARCHITECTURE_SNAPSHOT", ref=first.id,
                                      actor_user_id="owner")
    assert exc.value.code == "CHECKPOINT_PLATFORM_ONLY"
    project.is_platform = True
    db.flush()
    unapproved = _snapshot(db, project, ["x"], approved=False)
    with pytest.raises(Conflict) as exc2:
        checkpoints.record_checkpoint(db, settings, v, kind="ARCHITECTURE_SNAPSHOT", ref=unapproved.id,
                                      actor_user_id="owner")
    assert exc2.value.code == "SNAPSHOT_NOT_APPROVED"
    one = checkpoints.record_checkpoint(db, settings, v, kind="ARCHITECTURE_SNAPSHOT", ref=first.id,
                                        actor_user_id="owner")
    assert one["created"] and one["architecture_delta"] is None  # the first checkpoint has nothing to diff
    second = _snapshot(db, project, ["control-plane", "sqlite", "openbao"])
    two = checkpoints.record_checkpoint(db, settings, v, kind="ARCHITECTURE_SNAPSHOT", ref=second.id,
                                        actor_user_id="owner")
    assert two["created"] and two["previous_checkpoint_id"] == one["id"]
    assert two["architecture_delta"]["nodes"]["added"] == ["openbao"] and two["material"] is True
    assert two["changed_documents"] == [], "documents unchanged since the previous checkpoint"
    assert [c["id"] for c in checkpoints.list_checkpoints(db, project.id)] == [two["id"], one["id"]]


ANSWER = {"assessment": "Narasi perlu menyebut openbao.", "concerns": ["diagram belum diperbarui"],
          "questions_for_owner": []}


def test_claude_reviews_only_material_packages_advisory(personal_client, db, monkeypatch):
    from aeos.models.portfolio import Project

    seen: list = []

    def fake(request, settings=None):
        seen.append(request)
        return ClaudeResult(ok=True, structured=ANSWER, text="", model_requested="opus",
                            model_resolved="claude-opus-5-5", engine_version="2.1.302", session_id=None, usage={},
                            duration_ms=1, mode="NATIVE")

    monkeypatch.setattr(bridge, "run_structured", fake)
    project = Project(project_key="cp", name="CP", domain="PERSONAL")
    db.add(project)
    db.flush()
    narrative = DocumentRecord(project_id=project.id, domain="PERSONAL", logical_doc_id="03-architecture-overview",
                               node="03", title="Architecture Overview")
    db.add(narrative)
    db.flush()

    def rev(material: bool, domain: str = "PERSONAL") -> DocumentRevision:
        r = DocumentRevision(document_id=narrative.id, project_id=project.id, domain=domain,
                             logical_doc_id="03-architecture-overview", kind=checkpoints.CHECKPOINT_KIND,
                             status="RECORDED", content_hash=f"sha256:{material}{domain}",
                             details={"checkpoint": {"kind": "MILESTONE", "milestone": "M1"}, "material": material,
                                      "changed_documents": [{"logical_doc_id": "03-architecture-overview",
                                                             "sections": ["Context"]}],
                                      "writer_task": {"role_key": "technical-writer"}})
        db.add(r)
        db.flush()
        return r

    material, plain, office = rev(True), rev(False), rev(True, "OFFICE")
    db.commit()
    res = personal_client.post("/api/claude/review-docs-checkpoint", json={"revision_id": material.id})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "NARRATION" and body["package_unchanged"] and "bukan keputusan" in body["label"]
    assert seen[-1].purpose == "review.docs_checkpoint" and seen[-1].allowed_tools == []
    assert "03-architecture-overview" in seen[-1].prompt
    assert personal_client.post("/api/claude/review-docs-checkpoint",
                                json={"revision_id": plain.id}).json()["error"]["code"] == "PACKAGE_NOT_MATERIAL"
    assert personal_client.post("/api/claude/review-docs-checkpoint",
                                json={"revision_id": office.id}).status_code == 404
    assert len(seen) == 1


# ------------------------------------------------------------------- CLI, offers and the writer-draft hand-off


def test_checkpoint_cli_records_only_an_accepted_milestone(world):
    """W6-093 / W6-111: `aeos docs checkpoint <plan_version_id> --kind MILESTONE --ref M1` (ASCII output)."""
    from typer.testing import CliRunner

    from aeos.cli import app

    db, v = world["db"], world["version"]
    runner = CliRunner()
    refused = runner.invoke(app, ["docs", "checkpoint", v.id, "--kind", "MILESTONE", "--ref", "M1"])
    assert refused.exit_code == 1 and "MILESTONE_NOT_ACCEPTED" in refused.output and "T1" in refused.output
    assert refused.output.isascii()
    pid = world["project"].id
    listed = runner.invoke(app, ["docs", "checkpoints", pid])
    assert listed.exit_code == 0 and "milestone M1: 0/2 tasks ACCEPTED" in listed.output and "Offers: none" in listed.output
    world["accepted"].update(t.id for t in _tasks(db, v).values())
    offered = runner.invoke(app, ["docs", "checkpoints", pid])
    assert offered.exit_code == 0 and "MILESTONE M1 on plan version" in offered.output and offered.output.isascii()
    ok = runner.invoke(app, ["docs", "checkpoint", v.id, "--kind", "MILESTONE", "--ref", "M1"])
    assert ok.exit_code == 0, ok.output
    assert "finalised: yes" in ok.output and "DRAFT_FOR_NEXT_PLAN_VERSION" in ok.output and ok.output.isascii()
    db.expire_all()
    assert len(checkpoints.list_checkpoints(db, pid)) == 1
    after = runner.invoke(app, ["docs", "checkpoints", pid])
    assert "Offers: none" in after.output and "Recorded checkpoints: 1" in after.output
    assert "writer task not yet in a plan version" in after.output
    assert runner.invoke(app, ["docs", "checkpoints", "nope"]).exit_code == 2
    again = runner.invoke(app, ["docs", "checkpoint", v.id, "--kind", "MILESTONE", "--ref", "M1"])
    assert again.exit_code == 0 and "(already recorded)" in again.output
    assert runner.invoke(app, ["docs", "checkpoint", "nope", "--kind", "MILESTONE", "--ref", "M1"]).exit_code == 2


def test_an_accepted_milestone_is_offered_until_its_checkpoint_is_recorded(world):
    """W6-111 "nothing offers finalisation when the last task of a milestone is accepted": the candidates view
    counts accepted TaskSpecs per milestone and offers the checkpoint; recording it removes the offer."""
    db, settings, v, project = world["db"], world["settings"], world["version"], world["project"]
    tasks = _tasks(db, v)
    out = checkpoints.checkpoint_candidates(db, project)
    assert out["plan_version"]["id"] == v.id
    (m1,) = out["milestones"]
    assert (m1["key"], m1["tasks_total"], m1["tasks_accepted"], m1["accepted"]) == ("M1", 2, 0, False)
    assert out["offers"] == []
    world["accepted"].add(tasks["T1"].id)
    assert checkpoints.checkpoint_candidates(db, project)["milestones"][0]["tasks_accepted"] == 1
    world["accepted"].add(tasks["T2"].id)
    offers = checkpoints.checkpoint_candidates(db, project)["offers"]
    assert [(o["kind"], o["ref"]) for o in offers] == [("MILESTONE", "M1")] and "ACCEPTED" in offers[0]["reason"]
    rec = checkpoints.record_checkpoint(db, settings, v, kind="MILESTONE", ref="M1", actor_user_id="owner")
    after = checkpoints.checkpoint_candidates(db, project)
    assert after["offers"] == [] and after["milestones"][0]["checkpoint_id"] == rec["id"]


def _real_plan(db, project, content):
    """A plan whose version hash is the real content hash (the planning service checks it)."""
    from aeos.models.planning import Plan, PlanVersion

    plan = Plan(project_id=project.id, domain=project.domain, title="Rencana", state="APPROVED")
    db.add(plan)
    db.flush()
    v = PlanVersion(plan_id=plan.id, project_id=project.id, domain=project.domain, version=1, content=content,
                    manifest_hash=content_hash(content), source="SYNTHESIS")
    db.add(v)
    db.flush()
    plan.current_version_id = v.id
    db.commit()
    return plan, v


def _checkpoint_row(db, project, version, *, material=True):
    narrative = db.query(DocumentRecord).filter_by(project_id=project.id,
                                                   logical_doc_id="03-architecture-overview").one_or_none()
    if narrative is None:
        narrative = DocumentRecord(project_id=project.id, domain=project.domain,
                                   logical_doc_id="03-architecture-overview", node="03", title="Architecture Overview")
        db.add(narrative)
        db.flush()
    label = f"documentation_checkpoint:{project.id}:MILESTONE:M1"
    rev = DocumentRevision(
        document_id=narrative.id, project_id=project.id, domain=project.domain, logical_doc_id=narrative.logical_doc_id,
        kind=checkpoints.CHECKPOINT_KIND, status="RECORDED", plan_version_id=version.id,
        content_hash=f"sha256:pkg-{material}",
        details={"checkpoint": {"kind": "MILESTONE", "milestone": "M1"}, "material": material, "finalised": True,
                 "changed_documents": [{"logical_doc_id": "03-architecture-overview", "change": "CHANGED",
                                        "sections": ["Context"]}], "diagram_delta": ["context"],
                 "writer_task": {"status": "DRAFT_FOR_NEXT_PLAN_VERSION", "key": "DOC-MILE-M1",
                                 "title": "Perbarui narasi & visual terpadu setelah checkpoint milestone M1",
                                 "role_key": "technical-writer", "supporting_roles": ["diagram-visual-author"],
                                 "goal": "Susun narasi arsitektur terpadu", "input_artifacts": [label],
                                 "expected_outputs": ["delta narasi per dokumen"],
                                 "changed_sections": [{"document": "03-architecture-overview",
                                                       "sections": ["Context"]}],
                                 "diagrams": ["context"], "acceptance_criteria": ["Setiap bagian menunjuk artefak"]}})
    db.add(rev)
    db.commit()
    return rev, label


def test_the_owner_adds_the_writer_draft_to_a_new_plan_version_that_still_needs_approval(settings, db):
    """W6-093 / W6-115: the Tech Writer draft becomes a TASK of a NEW human-edit plan version (READY_FOR_HUMAN_REVIEW,
    no write paths: the owner sets them before approving). The compiler resolves its
    ``documentation_checkpoint:<project>:<kind>:<ref>`` input to the recorded, hash-bound package."""
    from aeos.core.errors import NotFound
    from aeos.models.planning import Plan, PlanVersion
    from aeos.planning import dag

    project = make_project(db)
    plan, v1 = _real_plan(db, project, sample())
    rev, label = _checkpoint_row(db, project, v1)
    out = checkpoints.plan_writer_task(db, rev.id, project, actor_user_id="owner", rationale="Narasi checkpoint M1")
    db.commit()
    v2 = db.get(PlanVersion, out["plan_version_id"])
    assert v2.version == 2 and v2.source == "HUMAN_EDIT" and v2.parent_version_id == v1.id
    assert db.get(Plan, plan.id).state == "READY_FOR_HUMAN_REVIEW", "a new hash needs a new owner decision"
    task = next(t for t in v2.content["tasks"] if t["key"] == "DOC-MILE-M1")
    exact = f"doc_checkpoint:{rev.id}"
    assert task["role_key"] == "technical-writer" and task["allowed_paths"] == [] and task["input_artifacts"] == [exact]
    resolved = dag.resolve_input_artifacts([exact, label], dag.artifact_index(db, v2))
    assert all(x["resolved"] and x["sha256"] == rev.content_hash for x in resolved)
    assert resolved[0]["content"]["changed_documents"][0]["sections"] == ["Context"]
    with pytest.raises(Conflict) as again:
        checkpoints.plan_writer_task(db, rev.id, project, actor_user_id="owner", rationale="lagi dong")
    assert again.value.code == "WRITER_TASK_ALREADY_PLANNED"
    listed = checkpoints.list_checkpoints(db, project.id, with_planning=True)
    assert listed[0]["writer_task_planned"]["plan_version_id"] == v2.id
    plain, _ = _checkpoint_row(db, project, v1, material=False)
    plain.created_at = rev.created_at + timedelta(seconds=5)
    db.commit()
    # a newer package of the same milestone: the generic label follows it, the approved task keeps ITS package
    newer = dag.resolve_input_artifacts([exact, label], dag.artifact_index(db, v2))
    assert newer[0]["sha256"] == rev.content_hash and newer[1]["sha256"] == plain.content_hash
    with pytest.raises(Conflict) as not_material:
        checkpoints.plan_writer_task(db, plain.id, project, actor_user_id="owner", rationale="tidak material")
    assert not_material.value.code == "PACKAGE_NOT_MATERIAL"
    with pytest.raises(NotFound):
        checkpoints.plan_writer_task(db, rev.id, make_project(db, key="lain"), actor_user_id="owner", rationale="xx")


def test_the_accepted_writer_change_re_renders_the_unified_narrative(settings, db, monkeypatch):
    """W6-093: once the owner-approved Tech Writer TaskSpec (its ``documentation_checkpoint`` input resolved to the
    hash-bound package) is ACCEPTED, its change record reaches 03-architecture-overview: the narrative's input
    changes, so a render produces a new body naming the change, PR, merge commit and package. A PENDING acceptance
    or an unresolved label proves nothing and leaves the narrative as it was."""
    from aeos.docs_engine import service
    from aeos.models.delivery_ext import PullRequestRecord
    from aeos.models.evidence import AcceptanceRecord
    from aeos.models.planning import TaskSpec

    install_fakes(monkeypatch)
    project = make_project(db)
    _plan, version = make_plan(db, project, sample(), tasks=TASKS)

    def narrative():
        result, _d, _s = service.render_version(db, settings, version, persist=False, render_visuals=False)
        return result.by_id()["03-architecture-overview"]

    before = narrative()
    package = {"ref": "doc_checkpoint:rev-1", "kind": "documentation_checkpoint", "sha256": "sha256:pkg",
               "label": f"documentation_checkpoint:{project.id}:MILESTONE:M1", "resolved": True}
    unresolved = {"ref": None, "kind": "label", "label": f"documentation_checkpoint:{project.id}:MILESTONE:M9",
                  "resolved": False}
    writer = TaskSpec(plan_version_id=version.id, plan_version_hash=version.manifest_hash, project_id=project.id,
                      domain=project.domain, task_key="DOC-MILE-M1", title="Narasi M1", goal="Narasi terpadu",
                      acceptance_criteria=[], depends_on=[], input_artifacts=[package], spec_hash="sha256:w")
    other = TaskSpec(plan_version_id=version.id, plan_version_hash=version.manifest_hash, project_id=project.id,
                     domain=project.domain, task_key="DOC-MILE-M9", title="Narasi M9", goal="Narasi terpadu",
                     acceptance_criteria=[], depends_on=[], input_artifacts=[unresolved], spec_hash="sha256:o")
    db.add_all([writer, other])
    db.flush()
    accs = {}
    for task, number in ((writer, 11), (other, 12)):
        acc = AcceptanceRecord(task_id=task.id, run_id=f"run-{number}", project_id=project.id, domain=project.domain,
                               artifact_hash="sha256:x", head_commit="c" * 40, acceptance_status="PENDING")
        db.add(acc)
        db.flush()
        db.add(PullRequestRecord(project_id=project.id, domain=project.domain, task_id=task.id, run_id=f"run-{number}",
                                 acceptance_id=acc.id, repo_full_name="owner/contoh", number=number,
                                 url=f"https://github.com/owner/contoh/pull/{number}", head_ref=f"aeos/{number}",
                                 base_ref="main", accepted_head_sha="c" * 40, bundle_hash="sha256:b",
                                 merge_commit_sha="e" * 40, merged=True, state="MERGED"))
        accs[task.task_key] = acc
    db.commit()
    infos = {c.task_key: c for c in service.change_infos(db, project)}
    assert infos["DOC-MILE-M1"].checkpoint_refs == ("doc_checkpoint:rev-1 (sha256:pkg)",)
    assert infos["DOC-MILE-M9"].checkpoint_refs == (), "an unresolved label is not a recorded package"
    pending = narrative()
    assert pending.input_hash == before.input_hash and "doc_checkpoint:rev-1" not in (pending.body_markdown or "")
    for acc in accs.values():
        acc.acceptance_status = "ACCEPTED"
    db.commit()
    after = narrative()
    body = after.body_markdown or ""
    assert after.input_hash != before.input_hash, "the accepted writer output re-renders the narrative"
    assert "Narasi terpadu dari checkpoint dokumentasi" in body and "`doc_checkpoint:rev-1 (sha256:pkg)`" in body
    assert "10-changes/change-doc-mile-m1" in body and "PR #11" in body and ("e" * 12) in body
    assert "DOC-MILE-M9" not in body
    assert after.links["change_records"] == ["10-changes/change-doc-mile-m1"]
    assert after.links["merge_commit"] == ["e" * 40]


def test_checkpoint_routes_list_candidates(personal_client, db):
    project = make_project(db)
    res = personal_client.get(f"/api/projects/{project.id}/documents/checkpoints/candidates")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["plan_version"] is None and body["offers"] == [] and body["source"] and body["checked_at"]
    listed = personal_client.get(f"/api/projects/{project.id}/documents/checkpoints").json()
    assert listed["items"] == [] and listed["source"]
    missing = personal_client.post(f"/api/projects/{project.id}/documents/checkpoints/nope/writer-task",
                                   json={"rationale": "coba"})
    assert missing.status_code == 404


def test_a_viewer_cannot_hand_the_writer_draft_to_planning(client, db):
    from aeos.core.enums import Domain
    from tests.conftest import VIEWER_PW, create_admin, create_viewer

    create_admin(db)
    create_viewer(db)
    project = make_project(db)
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    res = client.post(f"/api/projects/{project.id}/documents/checkpoints/x/writer-task", json={"rationale": "coba"})
    assert res.status_code == 403 and res.json()["error"]["code"] == "READ_ONLY_ROLE"
