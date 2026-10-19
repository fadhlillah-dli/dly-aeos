"""ChangeProposal flow (§39-§41). §50 rows covered: "Pengguna hanya meminta compare/review URL ->
Tidak install, activate, raise budget atau kirim data proyek", "Trial disetujui, promotion belum
disetujui -> Baseline aktif tetap berlaku", "Baseline/candidate/target berubah setelah approval ->
Binding ditolak", "Change proposal ditolak atau rollback dilakukan -> kandidat tidak dinyatakan
sukses", "Source repo rename/redirect -> tidak mewarisi trust", "Replace mengubah caller/schema ->
Impact ... diperlukan"."""

from __future__ import annotations

import sys
import types

import httpx
import pytest
import respx
from sqlalchemy import func, select

from aeos.claude_bridge.facade import ClaudeResult
from aeos.core.errors import Blocked, Conflict, PolicyDenied, ValidationFailed
from aeos.models.cost import Budget
from aeos.models.decisions import Decision
from aeos.models.evidence import EvidenceArtifact
from aeos.models.inventory import ComponentReference, ComponentRelation
from aeos.models.portfolio import Attachment, Project
from aeos.models.research import ChangeProposal, ResearchEntry
from aeos.models.routing import ModelRoute
from aeos.proposals import service
from aeos.research import service as research
from tests.unit.test_cost_fakes import install_fake_policy, remove_policy

SCOPE = {"target": "platform:dly-aeos", "environment": "sandbox", "provider": "opencode-zen",
         "data_class": "INTERNAL", "budget_usd": 0}


@pytest.fixture
def policy(monkeypatch):
    return install_fake_policy(monkeypatch)


def _component(db, cid="opencode", pin="1.3.0"):
    db.add(ComponentReference(component_id=cid, kind="worker-engine", name=cid.title(), approved_pin=pin,
                              access={"egress": ["opencode.ai"], "credential_ref": "workers/openrouter"},
                              operations={"restart": "aeos worker restart"}))
    db.add(ComponentRelation(source_id="workforce", target_id=cid, relation="calls", lens="DESIGN",
                             provenance="master §36"))
    db.add(ComponentRelation(source_id=cid, target_id="evidence-store", relation="writes", lens="DESIGN",
                             data_description="run artifacts"))
    db.commit()
    research.seed_registry(db)  # master-stated identities (e.g. github:anomalyco/opencode) pass the admission gate
    db.commit()


def _evidence(db, trusted=True, domain="PERSONAL", mode="LIVE"):
    ev = EvidenceArtifact(kind="TEST_REPORT", origin="verifier" if trusted else "worker",
                          collector_identity="verifier" if trusted else "worker", trusted=trusted,
                          content_hash="sha256:" + "a" * 64, domain=domain, mode=mode, claim="trial suite passed")
    db.add(ev)
    db.commit()
    return ev


def _create(db, kind="REPLACE", sources=("https://github.com/anomalyco/opencode",), domain="PERSONAL", **kw):
    params = {"existing_component_ids": ["opencode"] if kind in ("REPLACE", "UPGRADE", "REMOVE") else []} | kw
    p = service.create_proposal(db, actor_user_id="owner", domain=domain, kind=kind, title=None, sources=list(sources),
                                problem="Butuh engine yang lebih stabil", expected_benefit="kurangi repair", **params)
    db.commit()
    return p


def _review(db, p, **kw):
    service.review(db, p, actor_user_id="owner", **kw)
    db.commit()
    return p


def _decide(db, p, action, **kw):
    d = service.decide(db, p, actor_user_id="owner", action=action, baseline_hash=kw.pop("bh", p.baseline_hash),
                       candidate_hash=kw.pop("ch", p.candidate_hash), scope=kw.pop("scope", SCOPE),
                       rationale=kw.pop("rationale", f"owner {action}"))
    db.commit()
    return d


# ---------------------------------------------------------------- duplicates / identity


def test_duplicate_url_forms_link_to_existing_proposal(db):
    first = _create(db, kind="ADD", sources=["https://github.com/Owner/Repo"])
    for form in ("git@github.com:owner/repo.git", "https://www.github.com/owner/repo/tree/main/docs", "github.com/OWNER/repo/"):
        dup = _create(db, kind="COMPARE", sources=[form])
        assert dup.duplicate_of == first.id, form
        assert dup.canonical_identities == ["github:owner/repo"]


def test_duplicates_never_merge_office_and_personal(db):
    personal = _create(db, kind="ADD", sources=["https://github.com/acme/tool"])
    office = _create(db, kind="ADD", sources=["https://github.com/acme/tool"], domain="OFFICE")
    assert office.duplicate_of is None and personal.duplicate_of is None
    assert {p["id"] for p in service.list_proposals(db, domain="PERSONAL", allowed_project_ids=None)} == {personal.id}
    with pytest.raises(Exception):  # noqa: B017 - NotFound across domains
        service.get_proposal(db, office.id, domain="PERSONAL")


def test_duplicate_through_recorded_redirect_alias(db):
    db.add(ResearchEntry(name="old", identity="github:oldorg/tool", identity_status="CHANGED",
                         upstream={"observed_identity": "github:neworg/tool"}, lifecycle="DISCOVERED"))
    db.commit()
    first = _create(db, kind="ADD", sources=["https://github.com/neworg/tool"])
    second = _create(db, kind="ADD", sources=["https://github.com/oldorg/tool"])
    assert second.canonical_identities == ["github:neworg/tool"] and second.duplicate_of == first.id


def test_unknown_technical_fields_are_stored_as_unknown(db):
    p = _create(db, kind="ADD", sources=["SomeTool"])
    assert "candidate_pin" in p.unknown_fields and "source.license" in p.unknown_fields
    assert p.limits["cost_usd"] == "UNKNOWN" and p.canonical_identities == ["name:sometool"]
    with pytest.raises(ValidationFailed):
        service.create_proposal(db, actor_user_id="o", domain="PERSONAL", kind="REPLACE", title=None,
                                sources=["https://github.com/a/b"])  # REPLACE needs the existing component
    with pytest.raises(ValidationFailed):
        service.create_proposal(db, actor_user_id="o", domain="PERSONAL", kind="ADD", title=None, sources=[])


# ---------------------------------------------------------------- review package


def test_review_package_has_all_ten_items(db, policy):
    _component(db)
    research.seed_registry(db)
    db.commit()
    p = _review(db, _create(db, kind="REPLACE", candidate_pin="1.4.0"))
    pkg = p.review_package
    for key in ("summary", "source_identity", "baseline", "alternatives", "impact", "experiment_plan", "cost",
                "claude_review", "human_decision", "outcome"):
        assert key in pkg, key
    assert pkg["summary"]["decision_question"].startswith("Komponen mana yang diganti")
    src = pkg["source_identity"][0]
    assert src["identity"] == "github:anomalyco/opencode" and src["status"] == "STATED_BY_MASTER"
    assert src["license"] == "UNKNOWN" and "NOT_CHECKED" in src["flags"]
    base = pkg["baseline"]["components"][0]
    assert base["status"] == "KNOWN" and base["approved_pin"] == "1.3.0" and base["consumers"]
    assert {a["option"] for a in pkg["alternatives"]} == {"KEEP_EXISTING", "SMALL_CONFIG_FIX", "ADD", "REPLACE",
                                                         "TRIAL", "REJECT"}
    impact = pkg["impact"]
    assert set(impact) >= {"contracts", "state_data_migration", "own_auth_cap_enf", "secrets", "egress", "ci", "docs",
                           "operations", "rollback"}
    assert impact["contracts"]["status"] == "KNOWN" and "workforce calls opencode" in impact["contracts"]["items"][0]
    assert impact["secrets"]["status"] == "KNOWN" and "tanpa nilai" in impact["secrets"]["items"][0]
    assert impact["ci"]["status"] == "UNKNOWN"
    plan = pkg["experiment_plan"]
    assert plan["pins"]["candidate_pin"] == "1.4.0" and plan["stop_conditions"] and plan["collector_verifier"]
    assert all(v["basis"] in ("UNKNOWN", "NOT_APPLICABLE") for k, v in pkg["cost"].items()
               if isinstance(v, dict) and "basis" in v)
    assert pkg["claude_review"]["status"] == "NOT_REQUESTED"
    assert pkg["human_decision"]["bind"] == {"baseline_hash": p.baseline_hash, "candidate_hash": p.candidate_hash}
    assert pkg["outcome"]["status"] == "PENDING"
    assert p.status == "REVIEWED" and p.review_hash
    rp = pkg["replacement_plan"]  # §40 replacement & rollback plan
    assert rp["previous_accepted_configuration"][0]["approved_pin"] == "1.3.0"
    assert rp["adapter_consumer_changes"] == ["workforce"] and rp["cutover_order"] == "UNKNOWN"
    assert rp["credential_egress_changes"]["credential_refs_to_revoke_after_cutover"] == ["opencode"]
    roles = {c["role"]: c["performed_by"] for c in pkg["review_contributors"]}
    assert "BELUM DIJALANKAN" in roles["Security / Privacy"] and roles["Owner"].startswith("manusia")


def test_review_without_inventory_reports_unknown_baseline(db):
    p = _review(db, _create(db, kind="UPGRADE"))
    base = p.review_package["baseline"]["components"][0]
    assert base["status"] == "UNKNOWN" and "existing_component:opencode (tidak ada di inventaris)" in p.unknown_fields
    assert p.review_package["impact"]["contracts"]["status"] == "UNKNOWN"


# ---------------------------------------------------------------- COMPARE never installs


def test_compare_never_installs_activates_raises_budget_or_sends_data(db, policy, monkeypatch):
    _component(db)
    research.seed_registry(db)
    db.add(ModelRoute(route_key="r1", pool="WORKER", provider="x", model_id="m", status="CANDIDATE"))
    db.commit()
    called = []
    fake = types.ModuleType("aeos.claude_bridge.bridge")
    fake.run_structured = lambda req: called.append(req)  # must not be called without with_claude
    monkeypatch.setitem(sys.modules, "aeos.claude_bridge.bridge", fake)
    before = (db.scalar(select(func.count()).select_from(Budget)),
              [(c.component_id, c.admission_status, c.enablement, c.approved_pin) for c in db.scalars(select(ComponentReference))])
    with respx.mock(assert_all_called=False) as net:
        p = _create(db, kind="COMPARE", sources=["https://github.com/oraios/serena"], existing_component_ids=["opencode"])
        _review(db, p)
        assert p.review_package["experiment_plan"]["type"] == "REFERENCE_REVIEW"
        assert p.review_package["experiment_plan"]["install"] is False
        with pytest.raises(PolicyDenied):
            _decide(db, p, "TRIAL_APPROVE")
        db.rollback()
        with pytest.raises(PolicyDenied):
            service.record_outcome(db, p, actor_user_id="owner", status="ACCEPTED")
        db.rollback()
        _decide(db, p, "REFERENCE_ONLY", scope={})
        assert not net.calls
    assert called == []
    after = (db.scalar(select(func.count()).select_from(Budget)),
             [(c.component_id, c.admission_status, c.enablement, c.approved_pin) for c in db.scalars(select(ComponentReference))])
    assert before == after
    assert db.scalar(select(ModelRoute)).status == "CANDIDATE"
    serena = db.scalar(select(ResearchEntry).where(ResearchEntry.entry_key == "tool:serena"))
    assert serena.lifecycle == "REFERENCE_ONLY" and serena.owner_decision_id  # owner decision synced, nothing installed
    assert p.status == "REFERENCE_ONLY"


# ---------------------------------------------------------------- trial vs promotion


def test_trial_approval_is_not_promotion(db, policy):
    _component(db)
    p = _review(db, _create(db, kind="REPLACE", candidate_pin="1.4.0"))
    trial = _decide(db, p, "TRIAL_APPROVE")
    assert trial.kind == "TRIAL" and trial.allowed_operations == ["trial.sandbox"]
    assert p.status == "TRIAL_APPROVED" and trial.details["budget_raised"] is False
    comp = db.get(ComponentReference, "opencode")
    assert comp.approved_pin == "1.3.0"  # baseline stays active
    with pytest.raises(Blocked) as e:  # promotion without trusted trial evidence
        _decide(db, p, "PROMOTE")
    assert e.value.code == "NOT_PROVEN"
    db.rollback()
    with pytest.raises(Blocked) as e2:  # trial decision cannot be used as acceptance
        service.record_outcome(db, p, actor_user_id="owner", status="ACCEPTED", evidence_ids=[_evidence(db).id],
                               accepted_configuration={"component_id": "opencode", "approved_pin": "1.4.0"})
    assert e2.value.code == "PROMOTION_DECISION_MISSING"
    db.rollback()
    with pytest.raises(ValidationFailed):  # untrusted worker material cannot support the gate
        service.attach_evidence(db, p, evidence_ids=[_evidence(db, trusted=False).id], actor_user_id="owner")
    db.rollback()
    service.attach_evidence(db, p, evidence_ids=[_evidence(db).id], actor_user_id="owner")
    db.commit()
    promo = _decide(db, p, "PROMOTE")
    assert promo.kind == "PROMOTION" and promo.id != trial.id and promo.target_hash != trial.target_hash
    assert p.status == "PROMOTION_APPROVED"


def test_trial_needs_pin_and_bound_scope(db, policy):
    _component(db)
    p = _review(db, _create(db, kind="UPGRADE"))
    with pytest.raises(Blocked) as e:
        _decide(db, p, "TRIAL_APPROVE")
    assert e.value.code == "CANDIDATE_NOT_PINNED"
    db.rollback()
    _review(db, p, candidate_pin="2.0.0")
    with pytest.raises(ValidationFailed):
        _decide(db, p, "TRIAL_APPROVE", scope={"target": "x"})
    db.rollback()


# ---------------------------------------------------------------- stale binding


def test_submitted_hashes_must_match_reviewed_package(db, policy):
    _component(db)
    p = _review(db, _create(db, kind="REPLACE", candidate_pin="1.4.0"))
    with pytest.raises(service.StaleBinding):
        _decide(db, p, "TRIAL_APPROVE", bh="sha256:" + "0" * 64)
    db.rollback()


def test_baseline_change_after_review_refuses_binding_and_invalidates(db, policy):
    _component(db)
    p = _review(db, _create(db, kind="REPLACE", candidate_pin="1.4.0"))
    trial = _decide(db, p, "TRIAL_APPROVE")
    db.get(ComponentReference, "opencode").approved_pin = "1.3.1"  # baseline changes after approval
    db.commit()
    assert service.verify_binding(db, p, action="TRIAL_APPROVE") is None
    db.commit()
    assert db.get(Decision, trial.id).status == "INVALIDATED"
    service.attach_evidence(db, p, evidence_ids=[_evidence(db).id], actor_user_id="owner")
    db.commit()
    with pytest.raises(service.StaleBinding) as e:
        _decide(db, p, "PROMOTE")
    assert e.value.details["current"]["baseline_hash"] != p.baseline_hash
    db.commit()  # router commits the invalidation before refusing
    assert p.status == "UNDER_REVIEW"


def test_candidate_change_at_re_review_invalidates_decisions(db, policy):
    _component(db)
    p = _review(db, _create(db, kind="REPLACE", candidate_pin="1.4.0"))
    trial = _decide(db, p, "TRIAL_APPROVE")
    old = p.candidate_hash
    _review(db, p, candidate_pin="1.5.0")
    assert p.candidate_hash != old and p.status == "REVIEWED"
    assert db.get(Decision, trial.id).status == "INVALIDATED"
    assert "dibatalkan" in p.review_package["binding_note"]


def test_unchanged_re_review_keeps_trial(db, policy):
    _component(db)
    p = _review(db, _create(db, kind="REPLACE", candidate_pin="1.4.0"))
    trial = _decide(db, p, "TRIAL_APPROVE")
    _review(db, p)
    assert p.status == "TRIAL_APPROVED" and db.get(Decision, trial.id).status == "ACTIVE"
    assert service.verify_binding(db, p, action="TRIAL_APPROVE").id == trial.id


# ---------------------------------------------------------------- outcomes


def _promoted(db):
    _component(db)
    p = _review(db, _create(db, kind="REPLACE", candidate_pin="1.4.0"))
    _decide(db, p, "TRIAL_APPROVE")
    service.attach_evidence(db, p, evidence_ids=[_evidence(db).id], actor_user_id="owner")
    db.commit()
    _decide(db, p, "PROMOTE")
    return p


def test_rollback_outcome_is_never_acceptance(db, policy):
    p = _promoted(db)
    ev = _evidence(db)
    with pytest.raises(ValidationFailed):
        service.record_outcome(db, p, actor_user_id="owner", status="ACCEPTED", evidence_ids=[ev.id],
                               accepted_configuration={"approved_pin": "1.4.0"}, rollback={"code_reverted": True})
    db.rollback()
    service.record_outcome(db, p, actor_user_id="owner", status="ACCEPTED", evidence_ids=[ev.id],
                           accepted_configuration={"component_id": "opencode", "approved_pin": "1.4.0"})
    db.commit()
    assert p.status == "ACCEPTED" and p.outcome["current"]["accepted"] is True
    service.record_outcome(db, p, actor_user_id="owner", status="ROLLED_BACK", evidence_ids=[ev.id],
                           rollback={"code_reverted": True, "data_rollback": "NOT_POSSIBLE"}, notes="regression")
    db.commit()
    cur = p.outcome["current"]
    assert p.status == "ROLLED_BACK" and cur["accepted"] is False and cur["accepted_configuration"] is None
    assert cur["rollback"]["data_rollback"] == "NOT_POSSIBLE" and "tidak menjamin rollback data" in cur["rollback"]["note"]
    assert [h["status"] for h in p.outcome["history"]] == ["ACCEPTED", "ROLLED_BACK"]
    assert all(d.status != "ACTIVE" for d in db.scalars(select(Decision).where(Decision.target_id == p.id)))
    with pytest.raises(Conflict):  # a rolled-back proposal cannot be re-accepted
        service.record_outcome(db, p, actor_user_id="owner", status="ACCEPTED", evidence_ids=[ev.id],
                               accepted_configuration={"x": 1})
    tl = service.timeline(db, p)
    assert tl["before_after"]["after"] is None and tl["before_after"]["status"] == "ROLLED_BACK"
    assert tl["before_after"]["before"]["components"][0]["approved_pin"] == "1.3.0"
    assert [i["action"] for i in tl["items"] if i["type"] == "audit"][0] == "proposal.create"


def test_accepted_outcome_and_timeline_before_after(db, policy):
    from aeos.models.decisions import ArchitectureSnapshot

    p = _promoted(db)
    snap_b = ArchitectureSnapshot(label="before", refs={}, snapshot_hash="sha256:b")
    snap_a = ArchitectureSnapshot(label="after", refs={}, snapshot_hash="sha256:a")
    db.add_all([snap_b, snap_a])
    db.commit()
    with pytest.raises(Blocked):  # acceptance needs trusted evidence
        service.record_outcome(db, p, actor_user_id="owner", status="ACCEPTED",
                               accepted_configuration={"approved_pin": "1.4.0"})
    db.rollback()
    service.record_outcome(db, p, actor_user_id="owner", status="ACCEPTED", evidence_ids=[_evidence(db).id],
                           accepted_configuration={"component_id": "opencode", "approved_pin": "1.4.0"},
                           before_snapshot_id=snap_b.id, after_snapshot_id=snap_a.id)
    db.commit()
    dims = service.proposal_view(db, p, detail=True)["delivery_dimensions"]
    assert dims["APPROVED"]["status"] == "YES" and dims["IMPLEMENTED"]["status"] == "ACCEPTED"
    assert dims["ACTIVE"]["status"] == "UNKNOWN"  # approval/acceptance is not activation (§41)
    assert dims["VERIFIED"]["status"] == "EVIDENCE_ATTACHED" and dims["DOCS_PUBLISHED"]["status"] == "UNKNOWN"
    assert dims["ARCHITECTURE_SOURCE_UPDATED"]["status"] == "YES"
    tl = service.timeline(db, p)
    assert tl["before_after"]["after"]["accepted_configuration"]["approved_pin"] == "1.4.0"
    assert tl["before_after"]["snapshots"]["after"]["snapshot_hash"] == "sha256:a"
    kinds = [i["details"]["kind"] for i in tl["items"] if i["type"] == "decision"]
    assert kinds == ["TRIAL", "PROMOTION"]


def test_partial_outcome_requires_failed_parts(db, policy):
    p = _promoted(db)
    with pytest.raises(ValidationFailed):
        service.record_outcome(db, p, actor_user_id="owner", status="PARTIAL")
    db.rollback()
    service.record_outcome(db, p, actor_user_id="owner", status="PARTIAL", partial_failures=["docs publish CONFLICT"])
    db.commit()
    assert p.status == "PARTIAL" and p.outcome["current"]["accepted"] is False


def test_partial_to_accepted_needs_every_failed_part_reconciled(db, policy):
    """§40: 'selesai' never opens the gate - each failed part needs a resolution with trusted evidence."""
    p = _promoted(db)
    service.record_outcome(db, p, actor_user_id="owner", status="PARTIAL",
                           partial_failures=["docs publish CONFLICT", {"target": "jira"}])
    db.commit()
    ev = _evidence(db)
    cfg = {"component_id": "opencode", "approved_pin": "1.4.0"}
    with pytest.raises(ValidationFailed) as exc:  # nothing reconciled
        service.record_outcome(db, p, actor_user_id="owner", status="ACCEPTED", evidence_ids=[ev.id],
                               accepted_configuration=cfg)
    assert exc.value.code == "PARTIAL_NOT_RECONCILED"
    db.rollback()
    with pytest.raises(Blocked):  # a resolution without trusted evidence is NOT-PROVEN
        service.record_outcome(db, p, actor_user_id="owner", status="ACCEPTED", evidence_ids=[ev.id],
                               accepted_configuration=cfg, reconciled_failures=[
                                   {"part": "docs publish CONFLICT", "resolution": "republished", "evidence_ids": [ev.id]},
                                   {"part": "jira", "resolution": "issue dibuat ulang", "evidence_ids": []}])
    db.rollback()
    service.record_outcome(db, p, actor_user_id="owner", status="ACCEPTED", evidence_ids=[ev.id],
                           accepted_configuration=cfg, reconciled_failures=[
                               {"part": "docs publish CONFLICT", "resolution": "republished", "evidence_ids": [ev.id]},
                               {"part": "jira", "resolution": "issue dibuat ulang", "evidence_ids": [ev.id]}])
    db.commit()
    assert p.status == "ACCEPTED"
    assert [r["part"] for r in p.outcome["current"]["reconciled_failures"]] == ["docs publish CONFLICT", "jira"]


def test_failed_proposal_keeps_baseline_and_reason(db, policy):
    p = _promoted(db)
    service.record_outcome(db, p, actor_user_id="owner", status="FAILED", notes="consumer tests failed")
    db.commit()
    assert p.status == "FAILED" and db.get(ComponentReference, "opencode").approved_pin == "1.3.0"
    assert p.outcome["current"]["notes"] == "consumer tests failed"


# ---------------------------------------------------------------- parent/parts, registry, attachments


def test_parent_proposal_with_explicit_parts_single_approval(db, policy):
    _component(db)
    parent = service.create_proposal(
        db, actor_user_id="owner", domain="PERSONAL", kind="ADD", title="Retrieval stack", sources=[],
        parts=[{"title": "Serena", "sources": ["https://github.com/oraios/serena"], "depends_on": []},
               {"title": "Context7", "sources": ["https://github.com/upstash/context7"], "depends_on": [0]}])
    db.commit()
    children = list(db.scalars(select(ChangeProposal).where(ChangeProposal.parent_id == parent.id)))
    assert len(children) == 2 and all(c.duplicate_of is None for c in children)
    assert parent.parts[1]["depends_on"] == [parent.parts[0]["child_id"]]
    assert set(parent.canonical_identities) == {"github:oraios/serena", "github:upstash/context7"}
    with pytest.raises(Conflict):
        service.review(db, children[0], actor_user_id="owner")
    _review(db, parent)
    assert len(parent.review_package["parts"]) == 2
    with pytest.raises(Conflict):
        service.decide(db, children[0], actor_user_id="owner", action="REJECT", baseline_hash="x" * 10,
                       candidate_hash="y" * 10, scope={}, rationale="no")
    with pytest.raises(ValidationFailed):
        service.create_proposal(db, actor_user_id="o", domain="PERSONAL", kind="ADD", title="cyc", sources=[],
                                parts=[{"sources": ["a"], "depends_on": [1]}, {"sources": ["b"], "depends_on": [0]}])


def test_decisions_fail_closed_without_registry(db, monkeypatch):
    _component(db)
    remove_policy(monkeypatch)
    p = _review(db, _create(db, kind="REPLACE", candidate_pin="1.4.0"))
    with pytest.raises(Blocked) as e:
        _decide(db, p, "TRIAL_APPROVE")
    assert e.value.code == "DECISION_REGISTRY_UNAVAILABLE"
    db.rollback()
    assert db.scalar(select(func.count()).select_from(Decision)) == 0


def test_attachments_must_be_in_same_domain_and_clean(db):
    db.add_all([Project(id="pp", project_key="dly-aeos", name="DLY AEOS", domain="PERSONAL"),
                Project(id="po", project_key="office", name="Office", domain="OFFICE")])
    db.commit()
    db.add_all([Attachment(id="a1", project_id="pp", domain="PERSONAL", filename="idea.md", stored_name="x",
                           size_bytes=10, sha256="s", scan_status="CLEAN"),
                Attachment(id="a2", project_id="po", domain="OFFICE", filename="o.md", stored_name="y", size_bytes=1,
                           sha256="s"),
                Attachment(id="a3", project_id="pp", domain="PERSONAL", filename="bad.md", stored_name="z",
                           size_bytes=1, sha256="s", scan_status="REJECTED")])
    db.commit()
    p = _create(db, kind="ADD", sources=["https://github.com/a/b"], attachment_ids=["a1"])
    assert p.attachment_ids == ["a1"]
    for bad in ("a2", "a3", "missing"):
        with pytest.raises(ValidationFailed):
            _create(db, kind="ADD", sources=["https://github.com/a/c"], attachment_ids=[bad])


def test_research_lifecycle_synced_on_reject_and_promotion(db, policy):
    research.seed_registry(db)
    db.commit()
    p = _review(db, _create(db, kind="ADD", sources=["https://github.com/stacklok/toolhive"]))
    _decide(db, p, "REJECT", scope={})
    th = db.scalar(select(ResearchEntry).where(ResearchEntry.entry_key == "tool:toolhive"))
    assert th.lifecycle == "REJECTED" and th.history[-1]["proposal_id"] == p.id


# ---------------------------------------------------------------- Claude recommendation


def test_claude_review_receives_only_public_metadata_and_is_not_approval(db, policy, monkeypatch):
    seen = {}

    def run_structured(req):
        seen["req"] = req
        return ClaudeResult(ok=True, structured={
            "review": {"summary": "ok"}, "verify": {"claims": [{"claim": "faster", "status": "UNKNOWN"}]},
            "verdict": "NOT-PROVEN", "recommendation": "TRIAL", "rationale": "needs trial"},
            text="", model_requested="fable", model_resolved="claude-fable-5-1", engine_version="2.5",
            session_id=None, usage={}, duration_ms=10)

    fake = types.ModuleType("aeos.claude_bridge.bridge")
    fake.run_structured = run_structured
    monkeypatch.setitem(sys.modules, "aeos.claude_bridge.bridge", fake)
    _component(db)
    p = _create(db, kind="REPLACE", candidate_pin="1.4.0")
    p.problem = "SECRET-PROJECT-CODENAME internal plan"
    db.commit()
    _review(db, p, with_claude=True)
    cr = p.review_package["claude_review"]
    assert cr["status"] == "COMPLETED" and cr["verdict"] == "NOT-PROVEN" and "bukan persetujuan" in cr["label"]
    assert "SECRET-PROJECT-CODENAME" not in seen["req"].prompt and "kurangi repair" not in seen["req"].prompt
    assert seen["req"].allowed_tools == [] and seen["req"].project_id is None
    assert p.status == "REVIEWED"  # a recommendation never decides


def test_claude_review_not_sent_for_office_or_when_bridge_missing(db, policy, monkeypatch):
    p = _create(db, kind="ADD", sources=["https://github.com/a/b"], domain="OFFICE")
    _review(db, p, with_claude=True)
    assert p.review_package["claude_review"]["status"] == "NOT_RUN"
    assert "OFFICE" in p.review_package["claude_review"]["reason"]
    monkeypatch.setitem(sys.modules, "aeos.claude_bridge.bridge", None)
    p2 = _create(db, kind="ADD", sources=["https://github.com/a/c"])
    _review(db, p2, with_claude=True)
    assert p2.review_package["claude_review"]["status"] == "NOT_RUN"


def test_claude_schema_mismatch_is_failed(db, monkeypatch):
    fake = types.ModuleType("aeos.claude_bridge.bridge")
    fake.run_structured = lambda req: ClaudeResult(ok=True, structured={"verdict": "PASS-PRODUCTION"}, text="",
                                                   model_requested="fable", model_resolved=None, engine_version=None,
                                                   session_id=None, usage={}, duration_ms=1)
    monkeypatch.setitem(sys.modules, "aeos.claude_bridge.bridge", fake)
    p = _review(db, _create(db, kind="ADD", sources=["https://github.com/a/d"]), with_claude=True)
    assert p.review_package["claude_review"]["status"] == "FAILED"


def test_no_network_on_create_or_review(db):
    with respx.mock(assert_all_called=False) as net:
        net.route().mock(side_effect=httpx.ConnectError("no network allowed"))
        p = _review(db, _create(db, kind="ADD", sources=["https://github.com/x/y"]))
        assert p.status == "REVIEWED" and not net.calls
