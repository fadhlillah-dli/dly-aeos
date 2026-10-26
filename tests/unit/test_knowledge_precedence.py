"""ContextPack contract (master §24 table + §13 ContextPack): missing policy is never reconstructed, approved
project documents are project context (drafts never), sources outrank summaries, packs carry AUTH and a
status, and consumers can only take a CURRENT pack."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from aeos.core.errors import Blocked
from aeos.core.hashing import content_hash
from aeos.core.timeutil import utcnow
from aeos.knowledge import facade as knowledge
from aeos.knowledge import packs
from aeos.models.audit import AuditEvent
from aeos.models.knowledge_ext import ContextPackMeta
from aeos.models.planning import Plan, PlanVersion
from aeos.models.research import MemoryRecord
from tests.conftest import AuthedClient
from tests.unit.test_knowledge_hardening import _build, _task
from tests.unit.test_portfolio_support import create_project, git, make_repo, needs_git


def test_missing_policy_is_reported_never_reconstructed(personal_client: AuthedClient, monkeypatch, tmp_path):
    monkeypatch.setattr(packs, "policy_files", lambda: (tmp_path / "policy", []))
    p = create_project(personal_client, name="No Policy")
    pack = _build(personal_client, p["id"])
    policy = [i for i in pack["items"] if i["kind"] == "policy"]
    assert [i["ref"] for i in policy] == ["policy:missing"]
    assert policy[0]["provenance"]["status"] == "MISSING" and "MISSING" in policy[0]["content"]
    assert any("policy context MISSING" in w for w in pack["warnings"])
    assert "external_worker_allowed" not in str(pack)  # nothing of the real policy files is invented


def _plan(db, pid: str, *, approved: bool, title: str, content: dict) -> PlanVersion:
    plan = Plan(project_id=pid, domain="PERSONAL", title=title)
    db.add(plan)
    db.flush()
    pv = PlanVersion(plan_id=plan.id, project_id=pid, domain="PERSONAL", version=1, content=content,
                     manifest_hash=content_hash(content))
    db.add(pv)
    db.flush()
    plan.current_version_id = pv.id
    if approved:
        plan.approved_version_id = pv.id
        plan.state = "APPROVED"
    db.commit()
    return pv


def test_approved_project_documents_are_context_and_drafts_are_not(personal_client: AuthedClient, db):
    p = create_project(personal_client, name="Docs Ctx")
    approved = _plan(db, p["id"], approved=True, title="Approved plan",
                     content={"prd": {"goal": "Invoice export for accountants"}, "rfc": "Use CSV, then XLSX"})
    _plan(db, p["id"], approved=False, title="Draft plan", content={"prd": {"goal": "DRAFT-ONLY-IDEA"}})
    pack = _build(personal_client, p["id"], role_key="planner")
    docs = [i for i in pack["items"] if i["kind"] == "project_doc"]
    assert {i["ref"] for i in docs} == {f"plan_version:{approved.id}#prd", f"plan_version:{approved.id}#rfc"}
    assert all(i["provenance"]["status"] == "APPROVED" and i["provenance"]["manifest_hash"] for i in docs)
    assert "Invoice export" in str(docs) and "DRAFT-ONLY-IDEA" not in str(pack["items"])


def _document(db, pid: str, pv: PlanVersion, *, logical: str, status: str, body: str) -> None:
    from aeos.models.publishing import DocumentRecord, DocumentRevision

    doc = DocumentRecord(project_id=pid, domain="PERSONAL", logical_doc_id=logical, node="01", title=f"Doc {logical}",
                         status=status, source_plan_version_id=pv.id)
    db.add(doc)
    db.flush()
    db.add(DocumentRevision(document_id=doc.id, project_id=pid, domain="PERSONAL", logical_doc_id=logical,
                            kind="RENDERED", status="CURRENT", plan_version_id=pv.id, content_hash=content_hash(body),
                            body_markdown=body))
    db.commit()


def test_accepted_product_documents_are_project_context_and_drafts_are_not(personal_client: AuthedClient, db):
    """§24 (re-audit): the accepted Product 1 DocumentRecord branch of the project-doc collector had no test."""
    p = create_project(personal_client, name="Docs Product1")
    approved = _plan(db, p["id"], approved=True, title="Approved plan", content={"prd": "Ekspor faktur"})
    draft_pv = _plan(db, p["id"], approved=False, title="Draft plan", content={"prd": "x"})
    _document(db, p["id"], approved, logical="01-prd", status="APPROVED", body="# PRD ACCEPTED-DOC-BODY")
    _document(db, p["id"], approved, logical="02-rfc", status="PUBLISHED", body="# RFC PUBLISHED-DOC-BODY")
    _document(db, p["id"], approved, logical="03-tdd", status="DRAFT", body="# TDD DRAFT-DOC-BODY")
    _document(db, p["id"], draft_pv, logical="04-x", status="APPROVED", body="# UNAPPROVED-VERSION-BODY")
    pack = _build(personal_client, p["id"], role_key="docs-writer")
    docs = {i["ref"]: i for i in pack["items"] if i["kind"] == "project_doc" and i["ref"].startswith("document:")}
    assert set(docs) == {"document:01-prd", "document:02-rfc"}
    assert docs["document:01-prd"]["provenance"]["plan_version_id"] == approved.id
    assert docs["document:02-rfc"]["provenance"]["status"] == "PUBLISHED"
    text = str(pack["items"])
    assert "DRAFT-DOC-BODY" not in text and "UNAPPROVED-VERSION-BODY" not in text


@pytest.mark.parametrize("role", ["default", "planner", "reviewer", "security-reviewer", "docs-writer"])
def test_metadata_context_never_carries_draft_or_revoked_records(personal_client: AuthedClient, db, role):
    """§24 (re-audit): the metadata collector added draft/superseded records (labelled SUBORDINATE) to the packs of
    the default, planner, reviewer, security-reviewer and docs-writer roles. Only accepted records are context."""
    from aeos.models.decisions import Decision

    p = create_project(personal_client, name=f"Meta {role}")
    _plan(db, p["id"], approved=False, title="Draft zebraplan DRAFTMARK", content={"prd": "x"})
    _plan(db, p["id"], approved=True, title="Approved zebraplan OKMARK", content={"prd": "y"})
    for status in ("ACTIVE", "INVALIDATED"):
        db.add(Decision(kind="POLICY", actor_user_id="owner", actor_kind="HUMAN", target_type="project_convention",
                        target_id=f"zebraplan-{status}", project_id=p["id"], domain="PERSONAL", status=status,
                        rationale=f"zebraplan decision {status}"))
    # past its expires_at but the expiry is not written back yet (stored status still ACTIVE): EXPIRED, not context
    db.add(Decision(kind="POLICY", actor_user_id="owner", actor_kind="HUMAN", target_type="project_convention",
                    target_id="zebraplan-LAPSED", project_id=p["id"], domain="PERSONAL", status="ACTIVE",
                    expires_at=utcnow() - timedelta(minutes=5), rationale="zebraplan decision lapsed"))
    db.commit()
    pack = _build(personal_client, p["id"], role_key=role, query=["zebraplan"])
    meta = [i for i in pack["items"] if i["kind"] == "metadata"]
    titles = str([i["title"] for i in meta])
    assert "OKMARK" in titles and "DRAFTMARK" not in str(pack["items"])
    assert "zebraplan-ACTIVE" in str(meta) and "zebraplan-INVALIDATED" not in str(pack["items"])
    assert "zebraplan-LAPSED" not in str(pack["items"])
    assert all(packs.metadata_accepted({"kind": (i["provenance"]["record_kind"]), "status": i["provenance"]["status"]})
               for i in meta)


def test_metadata_judges_an_adr_by_the_owner_decision_not_the_builder_status(personal_client: AuthedClient, db,
                                                                              tmp_path, monkeypatch):
    """§02/§24: the ADR file's Status line is builder-written; the owner's ADR decision (bound to the content hash)
    decides whether it is accepted project context - a file-ACCEPTED ADR the owner REJECTED is not, a file-PROPOSED
    ADR the owner ACCEPTED is (labelled with its status source)."""
    from aeos.models.decisions import ArchitectureDecisionRecord
    from aeos.policy import adrs

    d = tmp_path / "decisions"
    d.mkdir()
    for key, status in (("ADR-0901", "ACCEPTED"), ("ADR-0902", "PROPOSED")):
        (d / f"{key}-zebra.md").write_text(
            f"# {key} \u2014 zebraadr {status.lower()}\n\n* Status: {status}\n\n## Decision\nUse zebraadr.\n",
            encoding="utf-8")
    monkeypatch.setattr(adrs, "decisions_dir", lambda: d)
    adrs.load_adrs(db)
    db.commit()
    p = create_project(personal_client, name="ADR ctx")

    def adr_items() -> dict[str, dict]:
        pack = _build(personal_client, p["id"], role_key="planner", query=["zebraadr"])
        return {i["provenance"]["record_id"]: i for i in pack["items"]
                if i["kind"] == "metadata" and i["provenance"]["record_kind"] == "adr"}

    items = adr_items()
    assert set(items) == {"ADR-0901"} and items["ADR-0901"]["provenance"]["status_source"] == "FILE (builder-written)"
    user_id = personal_client.session["user"]["id"]
    for key, outcome in (("ADR-0901", "REJECTED"), ("ADR-0902", "ACCEPTED")):
        row = db.scalar(select(ArchitectureDecisionRecord).where(ArchitectureDecisionRecord.adr_key == key))
        adrs.record_adr_decision(db, adr_key=key, outcome=outcome, actor_user_id=user_id,
                                 rationale="keputusan pemilik atas ADR ini", content_hash=row.content_hash)
    db.commit()
    items = adr_items()
    assert set(items) == {"ADR-0902"}
    assert items["ADR-0902"]["provenance"]["status"] == "ACCEPTED"
    assert items["ADR-0902"]["provenance"]["status_source"] == "OWNER_DECISION"


@needs_git
def test_source_outranks_memory_summary(personal_client: AuthedClient, db, tmp_path: Path):
    repo_dir = make_repo(tmp_path / "src", {"src/limits.py": "MAX_RETRIES = 5  # retry_limit\n"},
                         origin="https://github.com/octo/limits.git")
    p = create_project(personal_client, name="Precedence")
    r = personal_client.post(f"/api/projects/{p['id']}/repositories", json={
        "provider": "GITHUB", "owner": "octo", "name": "limits", "local_path": str(repo_dir)})
    assert r.status_code == 200, r.text
    db.add(MemoryRecord(project_id=p["id"], domain="PERSONAL", scope="PROJECT", status="ACCEPTED",
                        content="retry_limit: MAX_RETRIES is 3 (old summary)", classification="INTERNAL"))
    db.commit()
    head = git(repo_dir, "rev-parse", "HEAD")
    pack = _build(personal_client, p["id"], query=["retry_limit"])
    kinds = [i["kind"] for i in pack["items"]]
    source_idx = kinds.index("code_match")
    memory_idx = kinds.index("memory")
    assert source_idx < memory_idx  # the source at HEAD comes first and wins the budget
    source = pack["items"][source_idx]
    memory = pack["items"][memory_idx]
    assert "MAX_RETRIES = 5" in source["content"] and source["provenance"]["commit"] == head
    assert memory["provenance"]["precedence"] == "SUBORDINATE"
    assert memory["provenance"]["subordinate_to"] == f"source at {head[:12]}"
    assert "never override a newer source" in pack["precedence_rule"]


def test_pack_carries_auth_reference_and_lifecycle_status(personal_client: AuthedClient, db):
    p = create_project(personal_client, name="Auth Pack")
    task = _task(db, p["id"])
    first = _build(personal_client, p["id"], task_id=task.id)
    assert first["status"] == "ACTIVE"
    assert first["auth_ref"]["basis"] == "TASK_SPEC" and first["auth_ref"]["task_spec_id"] == task.id
    assert first["auth_ref"]["spec_hash"] == task.spec_hash and first["auth_ref"]["principal_user_id"]
    _build(personal_client, p["id"], task_id=task.id)
    db.expire_all()
    assert db.get(ContextPackMeta, first["id"]).status == "SUPERSEDED"
    adhoc = _build(personal_client, p["id"])
    assert adhoc["auth_ref"]["basis"] == "PRINCIPAL_PROJECT_SCOPE"
    meta = db.get(ContextPackMeta, adhoc["id"])
    meta.expires_at = utcnow() - timedelta(minutes=1)
    db.commit()
    got = personal_client.get(f"/api/context-packs/{adhoc['id']}").json()["context_pack"]
    assert got["status"] == "EXPIRED" and got["freshness"] == "STALE"


def test_consumers_only_receive_a_current_pack(personal_client: AuthedClient, db):
    p = create_project(personal_client, name="Use Pack")
    task = _task(db, p["id"])
    pack = _build(personal_client, p["id"], task_id=task.id)
    got = knowledge.use_pack(db, pack["id"], project_id=p["id"], consumer="worker:feature-implementer",
                             task_id=task.id)
    db.commit()
    assert got["pack_hash"] == pack["pack_hash"] and [i["ref"] for i in got["items"]] == [i["ref"] for i in pack["items"]]
    use = db.scalar(select(AuditEvent).where(AuditEvent.action == "knowledge.context_pack.use"))
    assert use is not None and use.details["pack_hash"] == pack["pack_hash"]
    with pytest.raises(Exception):
        knowledge.use_pack(db, pack["id"], project_id=p["id"], consumer="worker", task_id="another-task")
    _build(personal_client, p["id"], task_id=task.id)  # supersedes the first pack
    db.expire_all()
    with pytest.raises(Blocked) as exc:
        knowledge.use_pack(db, pack["id"], project_id=p["id"], consumer="worker:feature-implementer")
    assert exc.value.code == "CONTEXT_STALE" and exc.value.details["status"] == "SUPERSEDED"
