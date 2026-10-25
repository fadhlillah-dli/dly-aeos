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
