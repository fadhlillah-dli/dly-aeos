"""Admitted catalog components are probed; candidates are not (owner request R28/R29, 2026-10-28).

* A TRIAL_APPROVED/APPROVED component (ChangeProposal decision, PERSONAL or platform) with a real probe is a target
  of the full reconciliation; a DISCOVERED/UNDER_REVIEW candidate is not, and a scoped refresh of one is SKIPPED.
* Admission counts PERSONAL decisions only: an OFFICE proposal never adds a target, and an OFFICE-only component is
  never a target (run summary, scope and events stay free of it, §23).
* ``inventory.admitted_unprobed`` lists admitted components whose probe is ``none`` (CONFIGURED, informational,
  in no capability).
* A TRIAL/PROMOTION on a change_proposal invalidates ``component:<id>`` of its components and no route check; a
  TRIAL on a model_route keeps ``config:routes`` / ``model:route``.
* A binary probe entry with ``version_args: []`` is presence only (serena, goose, promptfoo, toolhive): their
  version call writes state or reaches the network, so the binary is located and never run.
* A candidate nobody admitted is never offered RUN_RECONCILE or REFRESH as its next action.

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


def _inst_obs(db, cid: str) -> ComponentObservation:
    return db.scalar(select(ComponentObservation).where(ComponentObservation.component_id == cid,
                                                        ComponentObservation.dimension == "installation")
                     .order_by(ComponentObservation.observed_at.desc()))


# ---------------------------------------------------------------- reconcile targets


def test_trial_approved_component_with_a_binary_probe_is_a_reconcile_target(seeded, owner, settings, repo):
    _decide(seeded, owner, _proposal(seeded, candidates=["context7"]), "TRIAL")
    seeded.commit()
    runner = _runner()
    runner.add_binary("pi", "0.9.1\n")
    run = reconcile(seeded, settings, runner=runner, repo=repo)
    seeded.commit()
    assert "context7" in run.component_status, "TRIAL_APPROVED context7 with a binary probe is not a reconcile target"
    assert run.component_status["context7"]["status"] == "OK"
    assert run.scope["admitted"] == ["context7"]
    obs = _inst_obs(seeded, "context7")
    assert obs.status == "INSTALLED" and obs.observed_version == "4.3.0"
    assert obs.operation_ref == "context7-mcp --version"
    assert "context7-mcp" in _probed(runner)
    assert "pi" not in _probed(runner)  # a candidate without a decision stays unprobed


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
    _decide(seeded, owner, _proposal(seeded, candidates=["context7"], domain="OFFICE"), "TRIAL", domain="OFFICE")
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
    assert not {"context7-mcp", "jira-cli"} & _probed(runner)
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
    _decide(seeded, owner, _proposal(seeded, candidates=["hermes", "serena"]), "TRIAL")
    seeded.commit()
    res = _admitted_check(settings, seeded)
    assert res.status == "CONFIGURED" and res.severity == "INFO"
    listed = res.observed_evidence["admitted_unprobed"]
    assert [u["component_id"] for u in listed] == ["hermes"]  # serena has a (presence-only) binary probe
    assert listed[0]["reason"].startswith("Tidak diprobe")
    assert "hermes" in res.summary


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


# ---------------------------------------------------------------- views: only what reconcile can cover is a gap

# the catalog candidates whose manifest probe is a real binary probe (reconcile probes them only once admitted)
PROBED_CANDIDATES = {"pi", "serena", "context7", "codebase-memory-mcp", "rtk", "ccusage", "temporal", "copier",
                     "goose", "promptfoo", "toolhive"}


def _coverage(db, domain="PERSONAL"):
    from aeos.inventory import views

    return views.coverage(db, domain=domain, compact=True)


def _installation(db, cid, domain="PERSONAL"):
    from aeos.inventory import views

    return views.component_detail(db, cid, domain=domain)["lifecycle"]["installation"]


def test_manifest_candidates_with_a_probe_are_the_expected_set(seeded):
    from aeos.inventory.reconcile import CORE_REQUIREMENTS, is_probeable

    got = {c.component_id for c in seeded.scalars(select(ComponentReference))
           if c.requirement not in CORE_REQUIREMENTS and is_probeable(c)}
    assert got == PROBED_CANDIDATES


def test_unadmitted_candidates_with_a_probe_are_no_never_probed_gap(seeded, settings, repo):
    reconcile(seeded, settings, runner=_runner(), repo=repo)
    seeded.commit()
    cov = _coverage(seeded)
    codes = {g["code"]: g["message"] for g in cov["gaps"]}
    assert "NEVER_PROBED" not in codes, "candidates no human admitted are reported as never probed: " + str(
        codes.get("NEVER_PROBED"))
    assert cov["totals"]["never_probed"] == 0
    assert cov["totals"]["not_probed_candidates"] >= len(PROBED_CANDIDATES)
    inst = _installation(seeded, "pi")
    assert inst["value"] == "UNKNOWN" and "belum di-admit" in inst["reason"], inst
    assert "jalankan aeos inventory reconcile" not in inst["reason"]


def test_an_admitted_candidate_not_yet_probed_is_a_never_probed_gap(seeded, owner, settings, repo):
    """Near miss: a TRIAL_APPROVED candidate is one reconcile covers, so 'never probed' is honest for it."""
    reconcile(seeded, settings, runner=_runner(), repo=repo)
    _decide(seeded, owner, _proposal(seeded, candidates=["serena"]), "TRIAL")
    seeded.commit()
    cov = _coverage(seeded)
    assert cov["totals"]["never_probed"] == 1
    assert "1 komponen yang dapat diprobe belum pernah diprobe" in {g["code"]: g["message"] for g in cov["gaps"]}[
        "NEVER_PROBED"]
    assert _installation(seeded, "serena")["reason"] == "belum diprobe; jalankan aeos inventory reconcile"


def test_a_rejected_trial_leaves_no_stale_gap_reconcile_cannot_refresh(seeded, owner, settings, repo, monkeypatch):
    from datetime import timedelta

    from aeos.core.timeutil import utcnow
    from aeos.inventory import views

    p = _proposal(seeded, candidates=["serena"])
    _decide(seeded, owner, p, "TRIAL")
    seeded.commit()
    reconcile(seeded, settings, runner=_runner(), repo=repo)
    seeded.commit()
    later = utcnow() + timedelta(days=3)  # past every probe TTL
    monkeypatch.setattr(views, "utcnow", lambda: later)
    admitted_stale = _coverage(seeded)["totals"]["stale_only"]  # near miss: serena is still admitted, so it counts
    _decide(seeded, owner, p, "REJECTION")
    seeded.commit()
    rejected_stale = _coverage(seeded)["totals"]["stale_only"]
    assert rejected_stale == admitted_stale - 1, "a REJECTED candidate still counts as a refreshable stale component"


def test_a_personal_trial_does_not_change_the_office_coverage(seeded, owner, settings, repo):
    reconcile(seeded, settings, runner=_runner(), repo=repo)
    seeded.commit()
    before = (_coverage(seeded, "OFFICE"), _installation(seeded, "serena", "OFFICE"))
    personal_before = _coverage(seeded)["totals"]["never_probed"]
    _decide(seeded, owner, _proposal(seeded, candidates=["serena"]), "TRIAL")
    seeded.commit()
    assert _coverage(seeded)["totals"]["never_probed"] == personal_before + 1  # the PERSONAL view did change
    after = (_coverage(seeded, "OFFICE"), _installation(seeded, "serena", "OFFICE"))
    for view in (before, after):
        view[0].pop("last_reconciled_at", None)
        view[0].pop("last_full_reconciled_at", None)
        view[1].pop("at", None)
    assert before == after, "a PERSONAL TRIAL changed the OFFICE coverage or installation view"


# ---------------------------------------------------------------- presence-only binary probes

# component -> the binary its presence-only probe looks up (the version call writes state or reaches the network)
PRESENCE_ONLY = {"serena": "serena", "goose": "goose", "promptfoo": "promptfoo", "toolhive": "thv"}


@pytest.mark.parametrize("cid", sorted(PRESENCE_ONLY))
def test_presence_only_probe_reports_installed_without_running_the_binary(seeded, owner, settings, repo, cid):
    from aeos.inventory.reconcile import PRESENCE_ONLY_REASON

    binary = PRESENCE_ONLY[cid]
    _decide(seeded, owner, _proposal(seeded, candidates=[cid]), "TRIAL")
    seeded.commit()
    runner = base_runner()
    runner.add_binary(binary, "9.9.9\n")  # a version the probe must never read
    run = reconcile(seeded, settings, component_id=cid, runner=runner, repo=repo)
    seeded.commit()
    assert runner.calls == [], f"presence-only probe of {cid} started a process: {runner.calls}"
    assert binary in runner.which_calls
    assert run.component_status[cid]["status"] == "OK"
    obs = _inst_obs(seeded, cid)
    assert obs.status == "INSTALLED" and obs.observed_version is None, (obs.status, obs.observed_version)
    assert obs.operation_ref == f"which {binary}"
    assert obs.detail["reason"] == PRESENCE_ONLY_REASON
    assert obs.detail["version_status"] == "UNKNOWN"
    assert _installation(seeded, cid)["reason"] == PRESENCE_ONLY_REASON


@pytest.mark.parametrize("cid", sorted(PRESENCE_ONLY))
def test_presence_only_probe_reports_not_installed_when_absent(seeded, owner, settings, repo, cid):
    _decide(seeded, owner, _proposal(seeded, candidates=[cid]), "TRIAL")
    seeded.commit()
    runner = base_runner()
    reconcile(seeded, settings, component_id=cid, runner=runner, repo=repo)
    seeded.commit()
    assert runner.calls == [], f"presence-only probe of {cid} started a process: {runner.calls}"
    obs = _inst_obs(seeded, cid)
    assert obs.status == "NOT_INSTALLED" and obs.observed_version is None
    assert obs.detail["missing"] == [PRESENCE_ONLY[cid]]


def test_presence_only_entry_beside_a_version_entry_runs_only_the_version_call(seeded, owner, settings, repo):
    """Near miss: an entry with version_args still runs (exactly once) and resolves its version."""
    comp = seeded.get(ComponentReference, "temporal")
    comp.probe = {"type": "binary", "binaries": [{"name": "temporal-server", "version_args": ["--version"]},
                                                 {"name": "tdbg", "version_args": []}]}
    _decide(seeded, owner, _proposal(seeded, candidates=["temporal"]), "TRIAL")
    seeded.commit()
    runner = base_runner()
    runner.add_binary("temporal-server", "temporal version 1.25.0\n")
    runner.add_binary("tdbg", "1.25.0\n")
    reconcile(seeded, settings, component_id="temporal", runner=runner, repo=repo)
    seeded.commit()
    assert [Path(c["argv"][0]).name for c in runner.calls] == ["temporal-server"]
    obs = _inst_obs(seeded, "temporal")
    assert obs.status == "INSTALLED" and obs.observed_version == "temporal-server 1.25.0"
    assert obs.detail["version_status"] == "UNKNOWN"  # tdbg's version is not read


def _install_claim(db, cid):
    from aeos.inventory import views

    claims = views.component_detail(db, cid, domain="PERSONAL")["evidence"]["claims"]
    return next(c for c in claims if c["claim"].startswith("Instalasi"))


@pytest.mark.parametrize("cid", sorted(PRESENCE_ONLY))
def test_presence_only_installation_claim_is_static_evidence_not_runtime(seeded, owner, settings, repo, cid):
    """A binary located on PATH and never run is a static lookup fact (E1, master §875), never an E3 runtime claim."""
    _decide(seeded, owner, _proposal(seeded, candidates=[cid]), "TRIAL")
    seeded.commit()
    runner = base_runner()
    runner.add_binary(PRESENCE_ONLY[cid], "9.9.9\n")
    reconcile(seeded, settings, component_id=cid, runner=runner, repo=repo)
    seeded.commit()
    assert runner.calls == []
    claim = _install_claim(seeded, cid)
    assert claim["claim"] == "Instalasi: INSTALLED"
    assert claim["level"] == "E1", f"{cid} was never run, yet its installation claim is {claim['level']}"


def test_executed_binary_installation_claim_stays_runtime_evidence(seeded, owner, settings, repo):
    """Near miss: a binary whose version call ran keeps its E3 installation claim; one presence-only entry beside it
    lowers the whole claim to E1 (the claim is only as strong as its weakest leg)."""
    comp = seeded.get(ComponentReference, "temporal")
    comp.probe = {"type": "binary", "binaries": [{"name": "temporal-server", "version_args": ["--version"]}]}
    _decide(seeded, owner, _proposal(seeded, candidates=["temporal"]), "TRIAL")
    seeded.commit()
    runner = base_runner()
    runner.add_binary("temporal-server", "temporal version 1.25.0\n")
    runner.add_binary("tdbg", "1.25.0\n")
    reconcile(seeded, settings, component_id="temporal", runner=runner, repo=repo)
    seeded.commit()
    assert len(runner.calls) == 1
    assert _install_claim(seeded, "temporal")["level"] == "E3"
    comp = seeded.get(ComponentReference, "temporal")
    comp.probe = {"type": "binary", "binaries": [{"name": "temporal-server", "version_args": ["--version"]},
                                                 {"name": "tdbg", "version_args": []}]}
    seeded.commit()
    reconcile(seeded, settings, component_id="temporal", runner=runner, repo=repo)
    seeded.commit()
    assert _install_claim(seeded, "temporal")["level"] == "E1"


def test_manifest_presence_only_entries_are_exactly_the_tools_with_a_stateful_version_call(seeded):
    got = {c.component_id: [b["name"] for b in (c.probe or {}).get("binaries") or [] if not b.get("version_args")]
           for c in seeded.scalars(select(ComponentReference))}
    assert {cid: names for cid, names in got.items() if names} == {cid: [b] for cid, b in PRESENCE_ONLY.items()}


# ---------------------------------------------------------------- next action: never a reconcile it cannot do


def _next(db, cid, domain="PERSONAL"):
    from aeos.inventory import views

    return views.component_detail(db, cid, domain=domain)["next_action"]["code"]


def _row_next(db, cid, domain="PERSONAL"):
    from aeos.inventory import views

    rows = views.list_components(db, domain=domain, q=cid, page_size=200)["items"]
    return next(r for r in rows if r["component_id"] == cid)["next_action"]["code"]


def test_unadmitted_candidate_with_a_probe_is_reviewed_not_reconciled(seeded):
    for cid in ("serena", "context7"):
        assert _next(seeded, cid) == "REVIEW_CANDIDATE", (
            f"{cid} (no admission) asks for a reconcile that skips it: {_next(seeded, cid)}")
        assert _row_next(seeded, cid) == "REVIEW_CANDIDATE", f"list row of {cid}: {_row_next(seeded, cid)}"


def test_an_admitted_component_not_yet_probed_still_gets_run_reconcile(seeded, owner):
    """Near miss: reconcile covers an admitted candidate, so RUN_RECONCILE is satisfiable for it."""
    _decide(seeded, owner, _proposal(seeded, candidates=["serena"]), "TRIAL")
    seeded.commit()
    assert _next(seeded, "serena") == "RUN_RECONCILE"
    assert _row_next(seeded, "serena") == "RUN_RECONCILE"


def test_a_rejected_candidate_with_a_stale_observation_is_not_offered_refresh(seeded, owner, settings, repo,
                                                                               monkeypatch):
    from datetime import timedelta

    from aeos.core.timeutil import utcnow
    from aeos.inventory import views

    p = _proposal(seeded, candidates=["context7"])
    _decide(seeded, owner, p, "TRIAL")
    seeded.commit()
    reconcile(seeded, settings, runner=_runner(), repo=repo)
    seeded.commit()
    later = utcnow() + timedelta(days=3)  # past every probe TTL
    monkeypatch.setattr(views, "utcnow", lambda: later)
    assert _next(seeded, "context7") == "REFRESH"  # near miss: still admitted, a Refresh probes it
    _decide(seeded, owner, p, "REJECTION")
    seeded.commit()
    code = _next(seeded, "context7")
    assert code not in ("RUN_RECONCILE", "REFRESH"), f"a rejected candidate is offered {code}, which reconcile skips"


def test_a_personal_trial_does_not_change_the_office_next_action(seeded, owner):
    before = _next(seeded, "serena", "OFFICE")
    _decide(seeded, owner, _proposal(seeded, candidates=["serena"]), "TRIAL")
    seeded.commit()
    assert _next(seeded, "serena") == "RUN_RECONCILE"  # the PERSONAL view did change
    assert _next(seeded, "serena", "OFFICE") == before == "REVIEW_CANDIDATE"
