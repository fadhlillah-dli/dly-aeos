"""GovernanceHarnessAdapter (§32): versioned contract; NullGovernanceHarness = NOT_CONFIGURED/BLOCKED;
never alters verifier results or writes approvals; rejects artifacts whose hash changed since acceptance."""

from __future__ import annotations

import dataclasses

import pytest

from aeos.core.errors import Blocked
from aeos.delivery import governance
from aeos.models.decisions import Decision
from aeos.models.evidence import EvidenceArtifact
from aeos.verification import acceptance, collector
from tests.unit.test_verification_support import (
    FakeWorkforce,
    make_review,
    make_world,
    record_verification,
)


@pytest.fixture
def accepted(db, monkeypatch):
    FakeWorkforce().install(monkeypatch)
    w = make_world(db)
    report = record_verification(db, w)
    make_review(db, w, report)
    acc = acceptance.evaluate_acceptance(db, w.run.id)
    db.commit()
    return w, acc


def test_null_adapter_is_not_configured_and_blocks_downstream(db, accepted):
    w, acc = accepted
    out = governance.handoff(db, w.task.id, actor="owner")
    db.commit()
    o = out["output"]
    assert o["result"] == "NOT_CONFIGURED" and o["contract_version"] == governance.CONTRACT_VERSION
    assert o["required_downstream_checks"][0]["status"] == "BLOCKED"
    assert "Harness.io" in o["required_downstream_checks"][0]["reason"]
    assert o["is_human_approval"] is False
    ev = db.get(EvidenceArtifact, out["evidence_id"])
    assert ev.kind == "GOVERNANCE_HANDOFF"
    stored = collector.read_json(ev)
    assert stored["input"]["artifact_hash"] == acc.artifact_hash
    assert db.query(Decision).count() == 0, "the adapter never writes approvals"


def test_changed_artifact_hash_is_rejected_before_adapter(db, accepted):
    w, _ = accepted
    calls = []

    class Spy(governance.NullGovernanceHarness):
        def handoff(self, payload):
            calls.append(payload)
            return super().handoff(payload)

    out = governance.handoff(db, w.task.id, actor="owner", adapter=Spy(), artifact_hash="sha256:" + "0" * 64)
    assert out["output"]["result"] == "REJECTED"
    assert any("artifact hash changed" in r for r in out["output"]["reasons"])
    assert not calls


def test_stale_acceptance_is_rejected(db, accepted):
    w, acc = accepted
    w.run.head_commit = "9" * 40
    db.commit()
    out = governance.handoff(db, w.task.id, actor="owner")
    assert out["output"]["result"] == "REJECTED"
    acceptance.invalidate(db, task_id=w.task.id, reason="head", actor="t", head_commit="9" * 40)
    with pytest.raises(Blocked):
        governance.handoff(db, w.task.id, actor="owner")


def test_adapter_cannot_mutate_payload_or_change_contract(db, accepted):
    w, _ = accepted

    class Mutating(governance.NullGovernanceHarness):
        def handoff(self, payload):
            object.__setattr__(payload, "artifact_hash", "sha256:forged")
            return super().handoff(payload)

    out = governance.handoff(db, w.task.id, actor="owner", adapter=Mutating())
    assert out["output"]["result"] == "REJECTED"
    assert any("mutated" in r or "different payload" in r for r in out["output"]["reasons"])

    class OtherContract(governance.NullGovernanceHarness):
        contract_version = "0.9"

    out = governance.handoff(db, w.task.id, actor="owner", adapter=OtherContract())
    assert out["output"]["result"] == "REJECTED"


def test_payload_is_immutable(db, accepted):
    w, _ = accepted
    payload, _acc = governance.build_input(db, w.task.id)
    with pytest.raises(dataclasses.FrozenInstanceError):
        payload.artifact_hash = "x"  # type: ignore[misc]
    with pytest.raises(TypeError):
        payload.reviewer["verdict"] = "PASS-PRODUCTION"  # type: ignore[index]
    assert payload.reviewer["verdict"] == "PASS-CONTROLLED"
    assert payload.verifier_reports[0]["status"] == "PASS"


def test_requires_acceptance(db, monkeypatch):
    FakeWorkforce().install(monkeypatch)
    w = make_world(db)
    with pytest.raises(Blocked):
        governance.handoff(db, w.task.id, actor="owner")
