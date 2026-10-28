"""Admitted catalog components are probed; candidates are not (owner request R28/R29, 2026-10-28).

* A TRIAL_APPROVED/APPROVED component (ChangeProposal decision, PERSONAL or platform) with a real probe is a target
  of the full reconciliation; a DISCOVERED/UNDER_REVIEW candidate is not, and a scoped refresh of one is SKIPPED.
* Admission counts PERSONAL decisions only: an OFFICE proposal never adds a target, and an OFFICE-only component is
  never a target (run summary, scope and events stay free of it, §23).
* ``inventory.admitted_unprobed`` lists admitted components whose probe is ``none`` (CONFIGURED, informational,
  in no capability).
* A TRIAL/PROMOTION on a change_proposal invalidates ``component:<id>`` of its components and no route check; a
  TRIAL on a model_route keeps ``config:routes`` / ``model:route``.

Every probe runs against the fake runner: no test starts a real binary on the host.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from aeos.core.enums import Capability
from aeos.inventory.reconcile import reconcile
from aeos.inventory.seed import run as seed_run
from aeos.models.audit import Event
from aeos.models.decisions import Decision
from aeos.models.inventory import ComponentObservation, ComponentReference
from aeos.models.research import ChangeProposal
from tests.conftest import create_admin
from tests.unit.test_inventory_fakes import base_runner

OFFICE_ONLY = ("bitbucket", "bitbucket-pipelines", "jira", "confluence")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


@pytest.fixture
def seeded(db):
    seed_run(db)
    db.commit()
    return db


@pytest.fixture
def owner(seeded):
    return create_admin(seeded)


def _proposal(db, *, candidates=(), existing=(), domain="PERSONAL", status="REVIEWED") -> ChangeProposal:
    p = ChangeProposal(kind="ADD", title="trial test", submitted_by="owner", domain=domain, status=status,
                       candidate_component_ids=list(candidates), existing_component_ids=list(existing),
                       baseline_hash="sha256:" + "b" * 64, candidate_hash="sha256:" + "c" * 64)
    db.add(p)
    db.flush()
    return p


def _decide(db, owner, p: ChangeProposal, kind: str, *, domain="PERSONAL") -> Decision:
    from aeos.policy import facade as policy

    return policy.record_decision(
        db, kind=kind, actor_user_id=owner.id, target_type="change_proposal", target_id=p.id,
        target_hash="sha256:" + "d" * 64, allowed_operations=["record.decision"], domain=domain,
        rationale="owner decides in a test", details={"baseline_hash": p.baseline_hash,
                                                     "candidate_hash": p.candidate_hash, "action": kind})


def _runner():
    r = base_runner()
    r.add_binary("serena", "Serena 2.0.0\n")
    r.add_binary("context7-mcp", "4.3.0\n")
    return r


def _probed(runner) -> set[str]:
    return {Path(c["argv"][0]).name for c in runner.calls}


# ---------------------------------------------------------------- reconcile targets


def test_trial_approved_component_with_a_binary_probe_is_a_reconcile_target(seeded, owner, settings, repo):
    _decide(seeded, owner, _proposal(seeded, candidates=["serena"]), "TRIAL")
    seeded.commit()
    runner = _runner()
    run = reconcile(seeded, settings, runner=runner, repo=repo)
    seeded.commit()
    assert "serena" in run.component_status, "TRIAL_APPROVED serena with a binary probe is not a reconcile target"
    assert run.component_status["serena"]["status"] == "OK"
    assert run.scope["admitted"] == ["serena"]
    obs = seeded.scalar(select(ComponentObservation).where(ComponentObservation.component_id == "serena",
                                                           ComponentObservation.dimension == "installation"))
    assert obs.status == "INSTALLED" and obs.observed_version == "2.0.0" and obs.operation_ref == "serena --version"
    assert "serena" in _probed(runner)
    assert "context7-mcp" not in _probed(runner)  # a candidate without a decision stays unprobed


def test_promotion_admits_too(seeded, owner, settings, repo):
    p = _proposal(seeded, candidates=["context7"])
    _decide(seeded, owner, p, "TRIAL")
    _decide(seeded, owner, p, "PROMOTION")
    seeded.commit()
    run = reconcile(seeded, settings, runner=_runner(), repo=repo)
    assert run.component_status.get("context7", {}).get("status") == "OK", "APPROVED context7 is not probed"
    assert run.scope["admitted"] == ["context7"]


def test_discovered_and_under_review_candidates_are_not_targets(seeded, owner, settings, repo):
    _proposal(seeded, candidates=["serena"], status="UNDER_REVIEW")
    seeded.commit()
    runner = _runner()
    run = reconcile(seeded, settings, runner=runner, repo=repo)
    assert "serena" not in run.component_status and "context7" not in run.component_status
    assert not {"serena", "context7-mcp"} & _probed(runner)
    assert run.scope.get("admitted", []) == []


def test_scoped_refresh_of_an_unadmitted_candidate_is_skipped(seeded, settings, repo):
    runner = _runner()
    run = reconcile(seeded, settings, component_id="serena", runner=runner, repo=repo)
    assert run.component_status["serena"]["status"] == "SKIPPED"
    assert "belum di-admit" in run.component_status["serena"]["reason"]
    assert runner.calls == []


def test_scoped_refresh_of_an_admitted_candidate_probes_it(seeded, owner, settings, repo):
    _decide(seeded, owner, _proposal(seeded, candidates=["serena"]), "TRIAL")
    seeded.commit()
    run = reconcile(seeded, settings, component_id="serena", runner=_runner(), repo=repo)
    assert run.component_status["serena"]["status"] == "OK"


def test_office_decisions_and_office_only_components_never_become_targets(seeded, owner, settings, repo):
    # OFFICE proposal TRIAL on a platform candidate: not counted for probing (§23)
    _decide(seeded, owner, _proposal(seeded, candidates=["serena"], domain="OFFICE"), "TRIAL", domain="OFFICE")
    # an OFFICE-only component with a probe, admitted by a PERSONAL decision, is still never a target
    jira = seeded.get(ComponentReference, "jira")
    jira.requirement = "CANDIDATE"  # outside the core set: only admission could make it a target
    jira.probe = {"type": "binary", "binaries": [{"name": "jira-cli", "version_args": ["--version"]}]}
    _decide(seeded, owner, _proposal(seeded, candidates=["jira"]), "TRIAL")
    seeded.commit()
    runner = _runner()
    runner.add_binary("jira-cli", "1.0.0\n")
    run = reconcile(seeded, settings, runner=runner, repo=repo)
    seeded.commit()
    assert not {"serena", "jira-cli"} & _probed(runner)
    assert run.scope.get("admitted", []) == []
    blob = json.dumps({"scope": run.scope, "summary": run.summary, "status": run.component_status})
    assert not [c for c in OFFICE_ONLY if f'"{c}"' in blob]
    payloads = json.dumps([e.payload for e in seeded.scalars(select(Event).where(Event.source == "inventory"))])
    assert '"jira"' not in payloads


def test_no_office_only_component_of_the_manifest_is_probeable(seeded):
    """The core set (REQUIRED/OPTIONAL with a probe) has no domain filter: it stays free of OFFICE components only
    while none of them carries a probe."""
    from aeos.inventory.reconcile import is_probeable

    office = [c for c in seeded.scalars(select(ComponentReference)) if (c.scope or {}).get("domains") == ["OFFICE"]]
    assert sorted(c.component_id for c in office) == sorted(OFFICE_ONLY)
    assert not [c.component_id for c in office if is_probeable(c)]


def test_poll_loop_includes_admitted_components(seeded, owner):
    from aeos.inventory.loop import due_components

    assert "serena" not in due_components(seeded, limit=500)
    _decide(seeded, owner, _proposal(seeded, candidates=["serena"]), "TRIAL")
    seeded.commit()
    assert "serena" in due_components(seeded, limit=500)


# ---------------------------------------------------------------- doctor: admitted but unprobed


def _admitted_check(settings, db):
    from aeos.doctor.checks import REGISTRY, load_all
    from aeos.doctor.runner import make_context, run_check

    load_all()
    return run_check(REGISTRY["inventory.admitted_unprobed"], make_context(settings, db, runner=base_runner()))


def test_admitted_component_without_a_probe_is_listed_as_configured(seeded, owner, settings):
    assert _admitted_check(settings, seeded).status == "VERIFIED"
    _decide(seeded, owner, _proposal(seeded, candidates=["goose", "serena"]), "TRIAL")
    seeded.commit()
    res = _admitted_check(settings, seeded)
    assert res.status == "CONFIGURED" and res.severity == "INFO"
    listed = res.observed_evidence["admitted_unprobed"]
    assert [u["component_id"] for u in listed] == ["goose"]  # serena has a binary probe
    assert listed[0]["reason"].startswith("Tidak diprobe")
    assert "goose" in res.summary


def test_admitted_unprobed_check_blocks_no_capability():
    from aeos.doctor.capabilities import checks_for_capability

    assert not [c for c in Capability if "inventory.admitted_unprobed" in checks_for_capability(c)]


# ---------------------------------------------------------------- decision invalidators


def test_change_proposal_trial_invalidates_its_components_not_the_routes(seeded, owner):
    from aeos.policy.decisions import readiness_invalidators

    p = _proposal(seeded, candidates=["serena"], existing=["context7"])
    for kind in ("TRIAL", "PROMOTION"):
        invs = readiness_invalidators(_decide(seeded, owner, p, kind), seeded)
        assert {"component:serena", "component:context7", "inventory:admission"} <= set(invs)
        assert not [i for i in invs if i.startswith(("model:route", "config:routes"))], invs


def test_office_change_proposal_trial_invalidates_nothing_of_the_personal_inventory(seeded, owner):
    from aeos.policy.decisions import readiness_invalidators

    p = _proposal(seeded, candidates=["serena"], domain="OFFICE")
    invs = readiness_invalidators(_decide(seeded, owner, p, "TRIAL", domain="OFFICE"), seeded)
    assert not [i for i in invs if i.startswith(("component:", "inventory:", "model:route", "config:routes"))]


def test_model_route_trial_keeps_the_route_invalidators():
    from aeos.policy.decisions import readiness_invalidators

    invs = readiness_invalidators(Decision(kind="TRIAL", target_type="model_route", target_id="worker-x"))
    assert "config:routes" in invs and "model:route" in invs
    assert not [i for i in invs if i.startswith("component:")]


def test_recorded_proposal_trial_stales_component_checks_not_route_checks(seeded, owner, settings):
    from aeos.doctor.runner import latest_results, run_doctor

    checks = ["claude.binary", "route.registry"]
    run_doctor(settings, seeded, runner=base_runner(), checks=checks, actor="t", trigger="test")
    seeded.commit()
    assert not [c for c, r in latest_results(seeded, checks).items() if r.status == "STALE"]
    _decide(seeded, owner, _proposal(seeded, candidates=["goose"], existing=["claude-code"]), "TRIAL")
    seeded.commit()
    rows = latest_results(seeded, checks)
    route = rows["route.registry"]
    assert route.status != "STALE", "a change_proposal TRIAL made the route check STALE: " + str(
        (route.observed_evidence or {}).get("stale_reason"))
    assert rows["claude.binary"].status == "STALE", "component:claude-code was not invalidated"


def test_recorded_proposal_trial_stales_the_admission_check(seeded, owner, settings):
    from aeos.doctor.runner import latest_results, run_doctor

    run_doctor(settings, seeded, runner=base_runner(), checks=["inventory.admitted_unprobed"], actor="t",
               trigger="test")
    seeded.commit()
    _decide(seeded, owner, _proposal(seeded, candidates=["goose"]), "TRIAL")
    seeded.commit()
    assert latest_results(seeded)["inventory.admitted_unprobed"].status == "STALE"
