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

from aeos.audit import service as audit
from aeos.claude_bridge.facade import ClaudeResult
from aeos.core.errors import Blocked, Conflict, PolicyDenied, ValidationFailed
from aeos.models.cost import Budget
from aeos.models.decisions import ArchitectureSnapshot, Decision
from aeos.models.inventory import ComponentReference
from aeos.models.portfolio import Attachment, Project
from aeos.models.research import ChangeProposal, ResearchEntry
from aeos.models.routing import ModelRoute
from aeos.proposals import service
from aeos.research import service as research
from tests.unit.proposal_fixtures import (
    SCOPE,
    apply_config,
    checkpoint_pass,
    component,
    create,
    decide,
    evidence,
    owner_plan,
    promoted,
    review,
    task,
)
from tests.unit.test_cost_fakes import install_fake_policy, remove_policy


@pytest.fixture
def policy(monkeypatch):
    return install_fake_policy(monkeypatch)


# ---------------------------------------------------------------- duplicates / identity


def test_duplicate_url_forms_link_to_existing_proposal(db):
    first = create(db, kind="ADD", sources=["https://github.com/Owner/Repo"])
    for form in ("git@github.com:owner/repo.git", "https://www.github.com/owner/repo/tree/main/docs", "github.com/OWNER/repo/"):
        dup = create(db, kind="COMPARE", sources=[form])
        assert dup.duplicate_of == first.id, form
        assert dup.canonical_identities == ["github:owner/repo"]


def test_duplicates_never_merge_office_and_personal(db):
    personal = create(db, kind="ADD", sources=["https://github.com/acme/tool"])
    office = create(db, kind="ADD", sources=["https://github.com/acme/tool"], domain="OFFICE")
    assert office.duplicate_of is None and personal.duplicate_of is None
    assert {p["id"] for p in service.list_proposals(db, domain="PERSONAL", allowed_project_ids=None)} == {personal.id}
    with pytest.raises(Exception):  # noqa: B017 - NotFound across domains
        service.get_proposal(db, office.id, domain="PERSONAL")


def test_duplicate_through_recorded_redirect_alias(db):
    db.add(ResearchEntry(name="old", identity="github:oldorg/tool", identity_status="CHANGED",
                         upstream={"observed_identity": "github:neworg/tool"}, lifecycle="DISCOVERED"))
    db.commit()
    first = create(db, kind="ADD", sources=["https://github.com/neworg/tool"])
    second = create(db, kind="ADD", sources=["https://github.com/oldorg/tool"])
    assert second.canonical_identities == ["github:neworg/tool"] and second.duplicate_of == first.id


def test_unknown_technical_fields_are_stored_as_unknown(db):
    p = create(db, kind="ADD", sources=["SomeTool"])
    assert "candidate_pin" in p.unknown_fields and "source.license" in p.unknown_fields
    assert p.limits["cost_usd"] == "UNKNOWN" and p.canonical_identities == ["name:sometool"]
    with pytest.raises(ValidationFailed):
        service.create_proposal(db, actor_user_id="o", domain="PERSONAL", kind="REPLACE", title=None,
                                sources=["https://github.com/a/b"])  # REPLACE needs the existing component
    with pytest.raises(ValidationFailed):
        service.create_proposal(db, actor_user_id="o", domain="PERSONAL", kind="ADD", title=None, sources=[])


# ---------------------------------------------------------------- review package


def test_review_package_has_all_ten_items(db, policy):
    component(db)
    p = review(db, create(db, kind="REPLACE", candidate_pin="1.4.0"))
    pkg = p.review_package
    for key in ("summary", "source_identity", "baseline", "alternatives", "impact", "experiment_plan", "cost",
                "claude_review", "human_decision", "outcome", "security_privacy", "replacement_plan"):
        assert key in pkg, key
    assert pkg["summary"]["decision_question"].startswith("Komponen mana yang diganti")
    src = pkg["source_identity"][0]
    assert src["identity"] == "github:anomalyco/opencode" and src["status"] == "STATED_BY_MASTER"
    assert src["license"] == "UNKNOWN" and "NOT_CHECKED" in src["flags"] and "ADVISORIES_NOT_CHECKED" in src["flags"]
    base = pkg["baseline"]["components"][0]
    assert base["status"] == "KNOWN" and base["approved_pin"] == "1.3.0" and base["consumers"]
    assert {a["option"] for a in pkg["alternatives"]} == {"KEEP_EXISTING", "SMALL_CONFIG_FIX", "ADD", "REPLACE",
                                                         "TRIAL", "REJECT"}
    impact = pkg["impact"]
    assert set(impact) >= {"contracts", "state_data_migration", "own_auth_cap_enf", "secrets", "egress", "ci", "docs",
                           "operations", "rollback", "schema_api_tools", "hooks_prompts_memory", "identity",
                           "resource_hardware"}
    assert impact["contracts"]["status"] == "KNOWN" and "workforce calls opencode" in impact["contracts"]["items"][0]
    assert impact["secrets"]["status"] == "KNOWN" and "tanpa nilai" in impact["secrets"]["items"][0]
    assert impact["ci"]["status"] == "UNKNOWN" and impact["docs"]["status"] == "UNKNOWN"
    plan = pkg["experiment_plan"]
    assert plan["pins"]["candidate_pin"] == "1.4.0" and plan["stop_conditions"] and plan["collector_verifier"]
    assert plan["acceptance_status"] == "NOT_DECLARED"
    assert all(v["basis"] in ("UNKNOWN", "NOT_APPLICABLE") for k, v in pkg["cost"].items()
               if isinstance(v, dict) and "basis" in v)
    assert pkg["claude_review"]["status"] == "NOT_REQUESTED"
    assert pkg["human_decision"]["bind"] == {"baseline_hash": p.baseline_hash, "candidate_hash": p.candidate_hash}
    assert {"max_minutes", "max_attempts", "stop_conditions", "rollback_scope"} <= set(pkg["human_decision"]["scope_fields"])
    assert any("acceptance terukur" in a for a in pkg["human_decision"]["next_actions"])
    assert pkg["outcome"]["status"] == "PENDING"
    assert p.status == "REVIEWED" and p.review_hash
    rp = pkg["replacement_plan"]  # §40 replacement & rollback plan
    assert rp["previous_accepted_configuration"][0]["approved_pin"] == "1.3.0"
    assert rp["adapter_consumer_changes"] == ["workforce"] and rp["cutover_order"] == "UNKNOWN"
    assert rp["credential_egress_changes"]["credential_refs_to_revoke_after_cutover"] == ["opencode: workers/openrouter"]
    assert rp["operations_owner"] == ["owner"] and rp["data_state_relations"] is True
    roles = {c["role"]: c["performed_by"] for c in pkg["review_contributors"]}
    assert "batas data/akses" in roles["Security / Privacy"] and roles["Owner"].startswith("manusia")


def test_review_without_inventory_reports_unknown_baseline(db):
    p = review(db, create(db, kind="UPGRADE"))
    base = p.review_package["baseline"]["components"][0]
    assert base["status"] == "UNKNOWN" and "existing_component:opencode (tidak ada di inventaris)" in p.unknown_fields
    assert p.review_package["impact"]["contracts"]["status"] == "UNKNOWN"


# ---------------------------------------------------------------- COMPARE never installs


def test_compare_never_installs_activates_raises_budget_or_sends_data(db, policy, monkeypatch):
    component(db)
    db.add(ModelRoute(route_key="r1", pool="WORKER", provider="x", model_id="m", status="CANDIDATE"))
    db.commit()
    called = []
    fake = types.ModuleType("aeos.claude_bridge.bridge")
    fake.run_structured = lambda req: called.append(req)  # must not be called without with_claude
    monkeypatch.setitem(sys.modules, "aeos.claude_bridge.bridge", fake)
    before = (db.scalar(select(func.count()).select_from(Budget)),
              [(c.component_id, c.admission_status, c.enablement, c.approved_pin) for c in db.scalars(select(ComponentReference))])
    with respx.mock(assert_all_called=False) as net:
        p = create(db, kind="COMPARE", sources=["https://github.com/oraios/serena"], existing_component_ids=["opencode"])
        review(db, p)
        assert p.review_package["experiment_plan"]["type"] == "REFERENCE_REVIEW"
        assert p.review_package["experiment_plan"]["install"] is False
        with pytest.raises(PolicyDenied):
            decide(db, p, "TRIAL_APPROVE")
        db.rollback()
        with pytest.raises(PolicyDenied):
            service.record_outcome(db, p, actor_user_id="owner", status="ACCEPTED")
        db.rollback()
        decide(db, p, "REFERENCE_ONLY", scope={})
        assert not net.calls
    assert called == []
    after = (db.scalar(select(func.count()).select_from(Budget)),
             [(c.component_id, c.admission_status, c.enablement, c.approved_pin) for c in db.scalars(select(ComponentReference))])
    assert before == after
    assert db.scalar(select(ModelRoute).where(ModelRoute.route_key == "r1")).status == "CANDIDATE"
    serena = db.scalar(select(ResearchEntry).where(ResearchEntry.entry_key == "tool:serena"))
    assert serena.lifecycle == "REFERENCE_ONLY" and serena.owner_decision_id  # owner decision synced, nothing installed
    assert p.status == "REFERENCE_ONLY"


# ---------------------------------------------------------------- trial vs promotion


def test_trial_approval_is_not_promotion(db, policy):
    component(db)
    p = create(db, kind="UPGRADE", candidate_pin="1.4.0")
    owner_plan(db, p)
    review(db, p)
    trial = decide(db, p, "TRIAL_APPROVE")
    assert trial.kind == "TRIAL" and trial.allowed_operations[0] == "trial.sandbox"
    assert set(trial.allowed_operations) >= {"trial.task.run", "trial.verifier.collect", "trial.stop"}
    assert p.status == "TRIAL_APPROVED" and trial.details["budget_raised"] is False
    assert trial.details["provider_eligibility"]["status"] == "NOT_ACTIVATED"
    comp = db.get(ComponentReference, "opencode")
    assert comp.approved_pin == "1.3.0"  # baseline stays active
    with pytest.raises(Blocked) as e:  # promotion without trusted trial evidence
        decide(db, p, "PROMOTE")
    assert e.value.code == "NOT_PROVEN"
    db.rollback()
    t = task(db, p)
    with pytest.raises(Blocked) as e2:  # trial decision cannot be used as acceptance
        service.record_outcome(db, p, actor_user_id="owner", status="ACCEPTED", evidence_ids=[evidence(db, task_spec=t).id],
                               accepted_configuration={"component_id": "opencode", "approved_pin": "1.4.0"})
    assert e2.value.code == "PROMOTION_DECISION_MISSING"
    db.rollback()
    with pytest.raises(ValidationFailed):  # untrusted worker material cannot support the gate
        service.attach_evidence(db, p, evidence_ids=[evidence(db, task_spec=t, trusted=False).id], actor_user_id="owner")
    db.rollback()
    service.attach_evidence(db, p, evidence_ids=[evidence(db, task_spec=t).id], actor_user_id="owner")
    db.commit()
    with pytest.raises(Blocked) as e3:  # §39/§40 Claude checkpoint over the evidence before PROMOTE
        decide(db, p, "PROMOTE")
    assert e3.value.code == "CHECKPOINT_REVIEW_REQUIRED"
    db.rollback()
    checkpoint_pass(db, p)
    promo = decide(db, p, "PROMOTE")
    assert promo.kind == "PROMOTION" and promo.id != trial.id and promo.target_hash != trial.target_hash
    assert promo.details["cutover"]["authorised_by_this_decision"] is True
    assert p.status == "PROMOTION_APPROVED"


def test_trial_needs_pin_and_bound_scope(db, policy):
    component(db)
    p = create(db, kind="UPGRADE")
    owner_plan(db, p)
    review(db, p)
    with pytest.raises(Blocked) as e:
        decide(db, p, "TRIAL_APPROVE")
    assert e.value.code == "CANDIDATE_NOT_PINNED"
    db.rollback()
    review(db, p, candidate_pin="2.0.0")
    with pytest.raises(ValidationFailed):
        decide(db, p, "TRIAL_APPROVE", scope={"target": "x"})
    db.rollback()
    no_caps = {k: v for k, v in SCOPE.items() if k not in ("max_minutes", "max_attempts")}
    with pytest.raises(ValidationFailed) as e2:  # §40 time and attempt caps are part of the scope
        decide(db, p, "TRIAL_APPROVE", scope=no_caps)
    assert set(e2.value.details["missing"]) == {"max_minutes", "max_attempts"}
    db.rollback()


# ---------------------------------------------------------------- stale binding


def test_submitted_hashes_must_match_reviewed_package(db, policy):
    component(db)
    p = create(db, kind="REPLACE", candidate_pin="1.4.0")
    owner_plan(db, p)
    review(db, p)
    with pytest.raises(service.StaleBinding):
        decide(db, p, "TRIAL_APPROVE", bh="sha256:" + "0" * 64)
    db.rollback()


def test_baseline_change_after_review_refuses_binding_and_invalidates(db, policy):
    component(db)
    p = create(db, kind="UPGRADE", candidate_pin="1.4.0")
    owner_plan(db, p)
    review(db, p)
    trial = decide(db, p, "TRIAL_APPROVE")
    db.get(ComponentReference, "opencode").approved_pin = "1.3.1"  # baseline changes after approval
    db.commit()
    assert service.verify_binding(db, p, action="TRIAL_APPROVE") is None
    db.commit()
    assert db.get(Decision, trial.id).status == "INVALIDATED"
    with pytest.raises(service.StaleBinding) as e:
        decide(db, p, "PROMOTE")
    assert e.value.details["current"]["baseline_hash"] != p.baseline_hash
    db.commit()  # router commits the invalidation before refusing
    assert p.status == "UNDER_REVIEW"


def test_candidate_change_at_re_review_invalidates_decisions(db, policy):
    component(db)
    p = create(db, kind="UPGRADE", candidate_pin="1.4.0")
    owner_plan(db, p)
    review(db, p)
    trial = decide(db, p, "TRIAL_APPROVE")
    old = p.candidate_hash
    review(db, p, candidate_pin="1.5.0")
    assert p.candidate_hash != old and p.status == "REVIEWED"
    assert db.get(Decision, trial.id).status == "INVALIDATED"
    assert "dibatalkan" in p.review_package["binding_note"]


def test_unchanged_re_review_keeps_trial(db, policy):
    component(db)
    p = create(db, kind="UPGRADE", candidate_pin="1.4.0")
    owner_plan(db, p)
    review(db, p)
    trial = decide(db, p, "TRIAL_APPROVE")
    review(db, p)
    assert p.status == "TRIAL_APPROVED" and db.get(Decision, trial.id).status == "ACTIVE"
    assert service.verify_binding(db, p, action="TRIAL_APPROVE").id == trial.id


# ---------------------------------------------------------------- outcomes


def _accept(db, p, ev, **kw):
    params = {"evidence_ids": [ev.id], "accepted_configuration": {"component_id": "opencode", "approved_pin": "1.4.0"},
              "acceptance_results": {"AC1": {"status": "PASS", "evidence_id": ev.id}}} | kw
    service.record_outcome(db, p, actor_user_id="owner", status="ACCEPTED", **params)
    db.commit()


def _rollback_ref(db):
    """The rollback actually ran: the manifest with the previous pin was reloaded (audited)."""
    comp = db.get(ComponentReference, "opencode")
    comp.approved_pin = "1.3.0"
    a = audit.record(db, actor_type="cli", actor_id="owner", action="inventory.manifest.load",
                     target_type="install_manifest", target_id="m-rollback")
    db.commit()
    return a.event_id


def test_rollback_outcome_is_never_acceptance(db, policy):
    p, t, _ev = promoted(db)
    apply_config(db)
    ev = evidence(db, task_spec=t, claim="post-activation probe")
    with pytest.raises(ValidationFailed):
        service.record_outcome(db, p, actor_user_id="owner", status="ACCEPTED", evidence_ids=[ev.id],
                               accepted_configuration={"approved_pin": "1.4.0"}, rollback={"code_reverted": True})
    db.rollback()
    _accept(db, p, ev)
    assert p.status == "ACCEPTED" and p.outcome["current"]["accepted"] is True
    with pytest.raises(ValidationFailed) as e:  # §40 the rollback must have actually run
        service.record_outcome(db, p, actor_user_id="owner", status="ROLLED_BACK", evidence_ids=[ev.id],
                               rollback={"code_reverted": True, "data_rollback": "NOT_POSSIBLE"}, notes="regression")
    assert e.value.code == "ROLLBACK_NOT_REFERENCED"
    db.rollback()
    ref = _rollback_ref(db)
    service.record_outcome(db, p, actor_user_id="owner", status="ROLLED_BACK", evidence_ids=[ev.id],
                           rollback={"code_reverted": True, "data_rollback": "NOT_POSSIBLE",
                                     "reference": {"audit_event_id": ref}}, notes="regression")
    db.commit()
    cur = p.outcome["current"]
    assert p.status == "ROLLED_BACK" and cur["accepted"] is False and cur["accepted_configuration"] is None
    assert cur["rollback"]["data_rollback"] == "NOT_POSSIBLE" and "tidak menjamin rollback data" in cur["rollback"]["note"]
    assert cur["rollback"]["reference"]["action"] == "inventory.manifest.load"
    assert cur["rollback"]["reference"]["restored_pins"] == {"opencode": "1.3.0"}
    assert [h["status"] for h in p.outcome["history"]] == ["ACCEPTED", "ROLLED_BACK"]
    decisions = list(db.scalars(select(Decision).where(Decision.target_id == p.id)))
    rb = [d for d in decisions if d.kind == "ROLLBACK"]  # §10 rollback is a Decision bound to the proposal
    assert len(rb) == 1 and rb[0].status == "ACTIVE" and rb[0].target_hash and rb[0].id == cur["rollback_decision_id"]
    assert rb[0].allowed_operations == ["rollback.candidate", "restore.baseline"]
    assert all(d.status != "ACTIVE" for d in decisions if d.kind != "ROLLBACK")
    with pytest.raises(Conflict):  # a rolled-back proposal cannot be re-accepted
        service.record_outcome(db, p, actor_user_id="owner", status="ACCEPTED", evidence_ids=[ev.id],
                               accepted_configuration={"x": 1})
    tl = service.timeline(db, p)
    assert tl["before_after"]["after"] is None and tl["before_after"]["status"] == "ROLLED_BACK"
    assert tl["before_after"]["before"]["components"][0]["approved_pin"] == "1.3.0"
    assert [i["action"] for i in tl["items"] if i["type"] == "audit"][0] == "proposal.create"


def test_accepted_outcome_and_timeline_before_after(db, policy):
    p, t, _ev = promoted(db)
    snap_b = ArchitectureSnapshot(label="before", refs={"scope": {"domain": "PERSONAL"}}, snapshot_hash="sha256:b")
    snap_a = ArchitectureSnapshot(label="after", refs={"scope": {"domain": "PERSONAL"}}, snapshot_hash="sha256:a")
    db.add_all([snap_b, snap_a])
    db.commit()
    apply_config(db)
    with pytest.raises(Blocked):  # acceptance needs trusted evidence
        service.record_outcome(db, p, actor_user_id="owner", status="ACCEPTED",
                               accepted_configuration={"approved_pin": "1.4.0"})
    db.rollback()
    ev = evidence(db, task_spec=t, claim="post-activation probe")
    _accept(db, p, ev, before_snapshot_id=snap_b.id, after_snapshot_id=snap_a.id)
    view = service.proposal_view(db, p, detail=True)
    dims = view["delivery_dimensions"]
    assert dims["APPROVED"]["status"] == "YES" and dims["IMPLEMENTED"]["status"] == "ACCEPTED"
    assert dims["ACTIVE"]["status"] == "ENABLED_IN_CONFIG"  # from the inventory record, not from approval
    assert dims["VERIFIED"]["status"] == "EVIDENCE_ATTACHED" and dims["DOCS_PUBLISHED"]["status"] == "UNKNOWN"
    assert dims["ARCHITECTURE_SOURCE_UPDATED"]["status"] == "YES"
    cur = p.outcome["current"]
    assert cur["acceptance"]["unmet"] == [] and cur["acceptance"]["criteria"][0]["status"] == "PASS"
    assert cur["finalization"]["inventory"]["status"] in ("DONE", "PARTIAL", "PENDING")
    assert cur["inventory_docs_update"].startswith("PENDING: ") and "ADR" in cur["inventory_docs_update"]
    tl = service.timeline(db, p)
    assert tl["before_after"]["after"]["accepted_configuration"]["approved_pin"] == "1.4.0"
    assert tl["before_after"]["snapshots"]["after"]["snapshot_hash"] == "sha256:a"
    kinds = [i["details"]["kind"] for i in tl["items"] if i["type"] == "decision"]
    assert kinds == ["TRIAL", "PROMOTION"]
    oc = db.scalar(select(ResearchEntry).where(ResearchEntry.entry_key == "tool:opencode"))
    assert oc.lifecycle == "ENABLED" and oc.test_results[-1]["outcome"] == "ACCEPTED"


def test_partial_outcome_requires_failed_parts(db, policy):
    p, _t, _ev = promoted(db)
    with pytest.raises(ValidationFailed):
        service.record_outcome(db, p, actor_user_id="owner", status="PARTIAL")
    db.rollback()
    service.record_outcome(db, p, actor_user_id="owner", status="PARTIAL", partial_failures=["docs publish CONFLICT"])
    db.commit()
    assert p.status == "PARTIAL" and p.outcome["current"]["accepted"] is False
    assert service.delivery_dimensions(db, p)["IMPLEMENTED"]["status"] == "PARTIAL"


def test_failed_proposal_keeps_baseline_and_reason(db, policy):
    p, _t, _ev = promoted(db)
    service.record_outcome(db, p, actor_user_id="owner", status="FAILED", notes="consumer tests failed")
    db.commit()
    assert p.status == "FAILED" and db.get(ComponentReference, "opencode").approved_pin == "1.3.0"
    assert p.outcome["current"]["notes"] == "consumer tests failed"


# ---------------------------------------------------------------- parent/parts, registry, attachments


def test_parent_proposal_with_explicit_parts_single_approval(db, policy):
    component(db)
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
    review(db, parent)
    assert len(parent.review_package["parts"]) == 2
    with pytest.raises(Conflict):
        service.decide(db, children[0], actor_user_id="owner", action="REJECT", baseline_hash="x" * 10,
                       candidate_hash="y" * 10, scope={}, rationale="no")
    with pytest.raises(ValidationFailed):
        service.create_proposal(db, actor_user_id="o", domain="PERSONAL", kind="ADD", title="cyc", sources=[],
                                parts=[{"sources": ["a"], "depends_on": [1]}, {"sources": ["b"], "depends_on": [0]}])


def test_decisions_fail_closed_without_registry(db, monkeypatch):
    component(db)
    remove_policy(monkeypatch)
    p = create(db, kind="REPLACE", candidate_pin="1.4.0")
    owner_plan(db, p)
    review(db, p)
    with pytest.raises(Blocked) as e:
        decide(db, p, "TRIAL_APPROVE")
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
    p = create(db, kind="ADD", sources=["https://github.com/a/b"], attachment_ids=["a1"])
    assert p.attachment_ids == ["a1"]
    for bad in ("a2", "a3", "missing"):
        with pytest.raises(ValidationFailed):
            create(db, kind="ADD", sources=["https://github.com/a/c"], attachment_ids=[bad])


def test_research_lifecycle_synced_on_reject(db, policy):
    research.seed_registry(db)
    db.commit()
    p = review(db, create(db, kind="ADD", sources=["https://github.com/stacklok/toolhive"]))
    decide(db, p, "REJECT", scope={})
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
    component(db)
    p = create(db, kind="REPLACE", candidate_pin="1.4.0")
    p.problem = "SECRET-PROJECT-CODENAME internal plan"
    db.commit()
    review(db, p, with_claude=True)
    cr = p.review_package["claude_review"]
    assert cr["status"] == "COMPLETED" and cr["verdict"] == "NOT-PROVEN" and "bukan persetujuan" in cr["label"]
    assert "SECRET-PROJECT-CODENAME" not in seen["req"].prompt and "kurangi repair" not in seen["req"].prompt
    assert seen["req"].allowed_tools == [] and seen["req"].project_id is None
    assert p.status == "REVIEWED"  # a recommendation never decides


def test_claude_review_not_sent_for_office_or_when_bridge_missing(db, policy, monkeypatch):
    p = create(db, kind="ADD", sources=["https://github.com/a/b"], domain="OFFICE")
    review(db, p, with_claude=True)
    assert p.review_package["claude_review"]["status"] == "NOT_RUN"
    assert "OFFICE" in p.review_package["claude_review"]["reason"]
    monkeypatch.setitem(sys.modules, "aeos.claude_bridge.bridge", None)
    p2 = create(db, kind="ADD", sources=["https://github.com/a/c"])
    review(db, p2, with_claude=True)
    assert p2.review_package["claude_review"]["status"] == "NOT_RUN"


def test_claude_schema_mismatch_is_failed(db, monkeypatch):
    fake = types.ModuleType("aeos.claude_bridge.bridge")
    fake.run_structured = lambda req: ClaudeResult(ok=True, structured={"verdict": "PASS-PRODUCTION"}, text="",
                                                   model_requested="fable", model_resolved=None, engine_version=None,
                                                   session_id=None, usage={}, duration_ms=1)
    monkeypatch.setitem(sys.modules, "aeos.claude_bridge.bridge", fake)
    p = review(db, create(db, kind="ADD", sources=["https://github.com/a/d"]), with_claude=True)
    assert p.review_package["claude_review"]["status"] == "FAILED"


def test_no_network_on_create_or_review(db):
    with respx.mock(assert_all_called=False) as net:
        net.route().mock(side_effect=httpx.ConnectError("no network allowed"))
        p = review(db, create(db, kind="ADD", sources=["https://github.com/x/y"]))
        assert p.status == "REVIEWED" and not net.calls
