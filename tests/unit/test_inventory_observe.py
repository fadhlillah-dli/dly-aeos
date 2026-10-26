"""Reconciler observes what AEOS itself installs and what a human admitted (hermetic: fake runner, tmp data dir).

* OpenBao installed by ``aeos vault install`` lives in ``<data>/bin`` (not on PATH): the installation probe finds it
  there, then on PATH, and reports NOT_INSTALLED only when neither has it.
* A CANDIDATE with a probe definition is probed once its admission is TRIAL_APPROVED/APPROVED, never before.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from aeos.inventory import views
from aeos.inventory.reconcile import reconcile
from aeos.inventory.seed import run as seed_run
from aeos.models.inventory import ComponentObservation
from aeos.models.research import ChangeProposal
from tests.conftest import create_admin
from tests.unit.test_inventory_fakes import base_runner, ok

BAO_OUT = "OpenBao v2.7.1 (a5db72cef75c24b920ade02065b18dd8eb666bac), committed 2026-10-01T15:32:54Z\n"
BAO_NAME = "bao.exe" if sys.platform == "win32" else "bao"


@pytest.fixture
def seeded(db):
    seed_run(db)
    db.commit()
    return db


def _installation(db, cid: str) -> ComponentObservation | None:
    return db.scalar(select(ComponentObservation).where(ComponentObservation.component_id == cid,
                                                        ComponentObservation.dimension == "installation"))


def _bao_in_data_bin(settings) -> Path:
    path = settings.data_dir / "bin" / BAO_NAME
    path.parent.mkdir(parents=True)
    path.write_bytes(b"fake-bao-binary")
    return path


def test_bao_in_data_bin_is_installed_with_version_and_digest(seeded, settings):
    path = _bao_in_data_bin(settings)
    runner = base_runner()  # bao is NOT on PATH
    runner.outputs[BAO_NAME.removesuffix(".exe")] = ok(BAO_OUT)
    reconcile(seeded, settings, runner=runner, component_id="openbao")
    seeded.commit()
    obs = _installation(seeded, "openbao")
    assert obs.status == "INSTALLED" and obs.observed_version == "2.7.1"
    assert obs.observed_identity and obs.observed_identity.startswith("sha256:")
    assert obs.detail["binaries"][0]["lookup"] == "data-dir"
    assert obs.observed_location == "<data>/bin/" + BAO_NAME
    assert runner.calls[0]["argv"] == [str(path), "version"]


def test_bao_on_path_is_installed(seeded, settings):
    runner = base_runner()
    runner.add_binary("bao", BAO_OUT)
    reconcile(seeded, settings, runner=runner, component_id="openbao")
    seeded.commit()
    obs = _installation(seeded, "openbao")
    assert obs.status == "INSTALLED" and obs.observed_version == "2.7.1"
    assert obs.detail["binaries"][0]["lookup"] == "PATH"


def test_bao_absent_is_not_installed(seeded, settings):
    reconcile(seeded, settings, runner=base_runner(), component_id="openbao")
    seeded.commit()
    obs = _installation(seeded, "openbao")
    assert obs.status == "NOT_INSTALLED" and obs.detail["missing"] == ["bao"]
    assert obs.detail["lookup"] == "settings/data-dir/PATH"


def _admit(db, cid: str, kind: str = "TRIAL"):
    from aeos.policy import facade as policy

    owner = create_admin(db)
    p = ChangeProposal(kind="ADD", title="ADD test", submitted_by="owner", domain="PERSONAL", status="REVIEWED",
                       candidate_component_ids=[cid], existing_component_ids=[],
                       baseline_hash="sha256:" + "b" * 64, candidate_hash="sha256:" + "c" * 64)
    db.add(p)
    db.flush()
    policy.record_decision(
        db, kind=kind, actor_user_id=owner.id, target_type="change_proposal", target_id=p.id,
        target_hash="sha256:" + "d" * 64, allowed_operations=["record.decision"], domain="PERSONAL",
        rationale="owner decides in a test", details={"baseline_hash": p.baseline_hash,
                                                     "candidate_hash": p.candidate_hash, "action": kind})
    db.commit()
    return owner


def test_unadmitted_candidate_is_not_probed(seeded, settings):
    runner = base_runner()
    runner.add_binary("ccusage", "ccusage 20.0.30\n")
    run = reconcile(seeded, settings, runner=runner, component_id="ccusage")
    seeded.commit()
    assert run.component_status["ccusage"]["status"] == "SKIPPED"
    assert "admission" in run.component_status["ccusage"]["reason"]
    assert _installation(seeded, "ccusage") is None
    assert all("ccusage" not in c["argv"][0] for c in runner.calls)
    # the full core pass leaves it out too
    full = reconcile(seeded, settings, runner=runner)
    assert "ccusage" not in full.component_status
    dims = views.component_detail(seeded, "ccusage", domain="PERSONAL")["lifecycle"]
    assert dims["installation"]["value"] == "UNKNOWN" and "admission" in dims["installation"]["reason"]


@pytest.mark.parametrize("kind", ["TRIAL", "PROMOTION"])
def test_admitted_candidate_is_probed_read_only(seeded, settings, kind):
    _admit(seeded, "ccusage", kind)
    runner = base_runner()
    ccusage = runner.add_binary("ccusage", "ccusage 20.0.30\n")
    for scoped in (True, False):
        run = reconcile(seeded, settings, runner=runner, component_id="ccusage" if scoped else None)
        seeded.commit()
        assert run.component_status["ccusage"]["status"] == "OK"
    obs = _installation(seeded, "ccusage")
    assert obs.status == "INSTALLED" and obs.observed_version == "20.0.30"
    assert [c["argv"] for c in runner.calls if c["argv"][0] == ccusage][0] == [ccusage, "--version"]
    dims = views.component_detail(seeded, "ccusage", domain="PERSONAL")["lifecycle"]
    assert dims["installation"]["value"] == "INSTALLED"


def test_admitted_candidate_missing_binary_is_not_installed(seeded, settings):
    _admit(seeded, "rtk")
    reconcile(seeded, settings, runner=base_runner(), component_id="rtk")
    seeded.commit()
    assert _installation(seeded, "rtk").status == "NOT_INSTALLED"


def test_admitted_candidate_without_probe_definition_stays_unknown(seeded, settings):
    _admit(seeded, "context7")
    runner = base_runner()
    run = reconcile(seeded, settings, runner=runner, component_id="context7")
    seeded.commit()
    assert run.component_status["context7"]["status"] == "SKIPPED"
    assert _installation(seeded, "context7") is None
    inst = views.component_detail(seeded, "context7", domain="PERSONAL")["lifecycle"]["installation"]
    assert inst["value"] == "UNKNOWN" and "binary lokal" in inst["reason"]


def test_suspended_candidate_is_not_probed(seeded, settings):
    from aeos.inventory.decisions import record_component_decision

    owner = _admit(seeded, "ccusage")
    record_component_decision(seeded, component_id="ccusage", actor_user_id=owner.id, rationale="security finding",
                              admission="SUSPENDED", domain="PERSONAL")
    seeded.commit()
    runner = base_runner()
    runner.add_binary("ccusage", "ccusage 20.0.30\n")
    run = reconcile(seeded, settings, runner=runner, component_id="ccusage")
    assert run.component_status["ccusage"]["status"] == "SKIPPED"
    assert _installation(seeded, "ccusage") is None
