"""Proposal admission/privacy gates added in the module-group-H completeness review (master §39
"Public research memakai metadata generik ... Nama/code/prompt/data/trace proyek Office tidak
dikirim", §50 "Nama repo ambigu atau sumber tidak terverifikasi -> UNVERIFIED dan blocked
admission", "Source repo rename/redirect/fork -> tidak mewarisi trust otomatis", "Dashboard
Personal mencoba ... Office -> Backend menolak sesuai scope")."""

from __future__ import annotations

import sys
import types

import httpx
import pytest
import respx
from sqlalchemy import select

from aeos.claude_bridge.facade import ClaudeResult
from aeos.core.errors import Blocked
from aeos.models.research import ResearchEntry
from aeos.proposals import service
from aeos.research import service as research
from tests.unit.test_cost_fakes import install_fake_policy

SCOPE = {"target": "platform:dly-aeos", "environment": "sandbox", "provider": "opencode-zen",
         "data_class": "INTERNAL", "budget_usd": 0}


@pytest.fixture
def policy(monkeypatch):
    return install_fake_policy(monkeypatch)


def _proposal(db, sources, *, domain="PERSONAL", pin="1.0.0"):
    p = service.create_proposal(db, actor_user_id="owner", domain=domain, kind="ADD", title=None, sources=sources,
                                problem="internal codename ZEPHYR", candidate_pin=pin)
    db.commit()
    service.review(db, p, actor_user_id="owner")
    db.commit()
    return p


def _trial(db, p):
    return service.decide(db, p, actor_user_id="owner", action="TRIAL_APPROVE", baseline_hash=p.baseline_hash,
                          candidate_hash=p.candidate_hash, scope=SCOPE, rationale="trial in sandbox")


def test_unverified_name_only_source_blocks_trial_admission(db, policy):
    p = _proposal(db, ["SomeInternalTool"])
    gate = p.review_package["human_decision"]["source_identity_gate"]
    assert gate["status"] == "BLOCKED" and gate["blocking"][0]["identity"] == "name:someinternaltool"
    assert any("diblokir" in a for a in p.review_package["human_decision"]["next_actions"])
    with pytest.raises(Blocked) as e:
        _trial(db, p)
    assert e.value.code == "SOURCE_IDENTITY_UNVERIFIED"
    db.rollback()
    assert p.status == "REVIEWED" and not p.decision_ids


def test_changed_identity_blocks_trial_and_trust_is_not_inherited(db, policy):
    research.seed_registry(db)
    th = db.scalar(select(ResearchEntry).where(ResearchEntry.entry_key == "tool:toolhive"))
    th.identity_status = "CHANGED"
    th.upstream = {"observed_identity": "github:stacklok/toolhive"}  # self-alias: link stays on the same row
    db.commit()
    p = _proposal(db, ["https://github.com/stacklok/toolhive"])
    with pytest.raises(Blocked) as e:
        _trial(db, p)
    assert e.value.code == "SOURCE_IDENTITY_UNVERIFIED"
    assert e.value.details["blocking"][0]["status"] == "CHANGED"
    db.rollback()


@respx.mock(assert_all_called=False)
def test_registry_entry_checked_after_submission_is_linked_on_re_review(respx_mock, db, policy):
    p = _proposal(db, ["https://github.com/acme/widget"])
    assert p.research_entry_ids == []
    with pytest.raises(Blocked):
        _trial(db, p)
    db.rollback()
    entry, _ = research.add_by_url(db, url="https://github.com/acme/widget", name=None, category=None, notes=None,
                                   actor_user_id="owner", domain="PERSONAL")
    respx_mock.get("https://api.github.com/repos/acme/widget").mock(return_value=httpx.Response(
        200, json={"full_name": "acme/widget", "archived": False, "fork": False}))
    respx_mock.get("https://api.github.com/repos/acme/widget/releases/latest").mock(
        return_value=httpx.Response(200, json={"tag_name": "v1.0.0"}))
    research.check_upstream(db, entry, actor_type="user", actor_id="owner", domain="PERSONAL")
    db.commit()
    old_candidate = p.candidate_hash
    service.review(db, p, actor_user_id="owner")
    db.commit()
    assert entry.id in p.research_entry_ids and p.candidate_hash != old_candidate
    assert p.review_package["human_decision"]["source_identity_gate"]["status"] == "OK"
    d = _trial(db, p)
    db.commit()
    assert d.kind == "TRIAL" and p.status == "TRIAL_APPROVED"


def test_claude_metadata_never_contains_free_text_names(db, policy, monkeypatch):
    seen = {}

    def run_structured(req):
        seen["prompt"] = req.prompt
        return ClaudeResult(ok=True, structured={"review": {"summary": "s"}, "verify": {"claims": []},
                                                 "verdict": "NOT-PROVEN", "recommendation": "DEFER", "rationale": "r"},
                            text="", model_requested="fable", model_resolved=None, engine_version=None,
                            session_id=None, usage={}, duration_ms=1)

    fake = types.ModuleType("aeos.claude_bridge.bridge")
    fake.run_structured = run_structured
    monkeypatch.setitem(sys.modules, "aeos.claude_bridge.bridge", fake)
    p = service.create_proposal(db, actor_user_id="owner", domain="PERSONAL", kind="COMPARE", title=None,
                                sources=["https://github.com/oraios/serena", "ProjectZephyrInternalTool"])
    db.commit()
    service.review(db, p, actor_user_id="owner", with_claude=True)
    db.commit()
    assert p.review_package["claude_review"]["status"] == "COMPLETED"
    assert "github:oraios/serena" in seen["prompt"]
    assert "zephyr" not in seen["prompt"].lower() and "ZEPHYR" not in seen["prompt"]


def test_office_decisions_never_write_into_the_shared_registry(db, policy):
    research.seed_registry(db)
    db.commit()
    shared = db.scalar(select(ResearchEntry).where(ResearchEntry.entry_key == "tool:toolhive"))
    before = (shared.lifecycle, list(shared.history or []), shared.owner_decision_id)
    office_entry, _ = research.add_by_url(db, url="https://github.com/corp/office-tool", name=None, category=None,
                                          notes=None, actor_user_id="owner", domain="OFFICE")
    db.commit()
    p = _proposal(db, ["https://github.com/stacklok/toolhive", "https://github.com/corp/office-tool"], domain="OFFICE")
    assert set(p.research_entry_ids) == {shared.id, office_entry.id}
    service.decide(db, p, actor_user_id="owner", action="REJECT", baseline_hash=p.baseline_hash,
                   candidate_hash=p.candidate_hash, scope={}, rationale="not for office")
    db.commit()
    db.refresh(shared)
    assert (shared.lifecycle, list(shared.history or []), shared.owner_decision_id) == before
    db.refresh(office_entry)
    assert office_entry.lifecycle == "REJECTED" and office_entry.history[-1]["proposal_id"] == p.id


def test_trial_approval_supersedes_an_earlier_reject_no_conflicting_active_decisions(db, policy):
    """§39 'jangan membuat beberapa approval yang saling bertentangan': after REJECT -> re-review ->
    TRIAL_APPROVE exactly one decision (the trial) is ACTIVE; the rejection is invalidated, not kept."""
    from aeos.models.decisions import Decision

    research.seed_registry(db)
    db.commit()
    p = _proposal(db, ["https://github.com/stacklok/toolhive"])
    service.decide(db, p, actor_user_id="owner", action="REJECT", baseline_hash=p.baseline_hash,
                   candidate_hash=p.candidate_hash, scope={}, rationale="not now")
    db.commit()
    service.review(db, p, actor_user_id="owner")
    db.commit()
    assert p.status == "REVIEWED"
    trial = _trial(db, p)
    db.commit()
    active = list(db.scalars(select(Decision).where(Decision.target_id == p.id, Decision.status == "ACTIVE")))
    assert [d.id for d in active] == [trial.id] and trial.kind == "TRIAL"
    rejected = next(d for d in db.scalars(select(Decision).where(Decision.target_id == p.id)) if d.kind == "REJECTION")
    assert rejected.status == "INVALIDATED" and "TRIAL_APPROVE" in (rejected.invalidation_reason or "")


def test_claude_quota_exhausted_review_waits_and_is_not_a_verdict(db, policy, monkeypatch):
    """§50 'Fable Max quota habis -> Review menunggu; gate tidak dilewati': the package records
    QUOTA_WAIT (no verdict/recommendation), and the owner decision still needs the bound hashes."""

    def run_structured(req):
        return ClaudeResult(ok=False, structured=None, text="", model_requested="fable", model_resolved=None,
                            engine_version=None, session_id=None, usage={}, duration_ms=1,
                            error_code="QUOTA_EXHAUSTED", quota_exhausted=True)

    fake = types.ModuleType("aeos.claude_bridge.bridge")
    fake.run_structured = run_structured
    monkeypatch.setitem(sys.modules, "aeos.claude_bridge.bridge", fake)
    p = service.create_proposal(db, actor_user_id="owner", domain="PERSONAL", kind="COMPARE", title=None,
                                sources=["https://github.com/oraios/serena"])
    db.commit()
    service.review(db, p, actor_user_id="owner", with_claude=True)
    db.commit()
    cr = p.review_package["claude_review"]
    assert cr["status"] == "QUOTA_WAIT" and "verdict" not in cr and "recommendation" not in cr
    assert p.status == "REVIEWED" and not p.decision_ids  # nothing decided or skipped on the model's behalf
    with pytest.raises(service.StaleBinding):  # the human decision still binds the reviewed hashes
        service.decide(db, p, actor_user_id="owner", action="REFERENCE_ONLY", baseline_hash="sha256:" + "0" * 64,
                       candidate_hash=p.candidate_hash, scope={}, rationale="stale hashes")
    db.rollback()


def test_claude_review_reused_on_same_metadata_hash_and_stale_after_change(db, policy, monkeypatch):
    """§41 'Summary ... dipakai ulang pada scope/hash yang sama, dengan invalidation ketika input penting
    berubah' / §42 savings: an unchanged re-review makes no new Claude call; a changed input marks the
    old recommendation STALE instead of presenting it as current."""
    calls = []

    def run_structured(req):
        calls.append(req.prompt)
        return ClaudeResult(ok=True, structured={"review": {"summary": "s"}, "verify": {"claims": []},
                                                 "verdict": "NOT-PROVEN", "recommendation": "DEFER", "rationale": "r"},
                            text="", model_requested="fable", model_resolved=None, engine_version=None,
                            session_id=None, usage={}, duration_ms=1)

    fake = types.ModuleType("aeos.claude_bridge.bridge")
    fake.run_structured = run_structured
    monkeypatch.setitem(sys.modules, "aeos.claude_bridge.bridge", fake)
    research.seed_registry(db)
    db.commit()
    p = service.create_proposal(db, actor_user_id="owner", domain="PERSONAL", kind="COMPARE", title=None,
                                sources=["https://github.com/oraios/serena"])
    db.commit()
    service.review(db, p, actor_user_id="owner", with_claude=True)
    db.commit()
    first = p.review_package["claude_review"]
    assert first["status"] == "COMPLETED" and first["metadata_hash"] and len(calls) == 1
    service.review(db, p, actor_user_id="owner", with_claude=True)
    db.commit()
    again = p.review_package["claude_review"]
    assert again["status"] == "COMPLETED" and again["reused"] is True and len(calls) == 1
    service.review(db, p, actor_user_id="owner")  # plain re-review keeps the still-valid recommendation
    db.commit()
    assert p.review_package["claude_review"]["reused"] is True
    serena = db.scalar(select(ResearchEntry).where(ResearchEntry.entry_key == "tool:serena"))
    serena.identity_status = "CHECKED"
    serena.upstream = {"latest_tag": "v9.9.9", "license_spdx": "MIT"}
    db.commit()
    service.review(db, p, actor_user_id="owner")
    db.commit()
    stale = p.review_package["claude_review"]
    assert stale["status"] == "STALE" and stale["previous_verdict"] == "NOT-PROVEN"
    assert "verdict" not in stale and "recommendation" not in stale and len(calls) == 1
    service.review(db, p, actor_user_id="owner")  # the STALE marker is kept, not silently dropped
    db.commit()
    assert p.review_package["claude_review"]["status"] == "STALE"
    service.review(db, p, actor_user_id="owner", with_claude=True)
    db.commit()
    assert p.review_package["claude_review"]["status"] == "COMPLETED" and len(calls) == 2
    assert "reused" not in p.review_package["claude_review"]
