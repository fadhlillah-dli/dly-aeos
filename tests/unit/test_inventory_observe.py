"""Reconciler observes what AEOS itself installs and what a human admitted (hermetic: fake runner, tmp data dir).

* OpenBao installed by ``aeos vault install`` lives in ``<data>/bin`` (not on PATH): the installation probe takes the
  settings override, then an executable file in ``<data>/bin``, then PATH, and reports NOT_INSTALLED only when none has
  it; a located binary that will not run is UNKNOWN, never INSTALLED.
* A CANDIDATE with a probe definition is probed once a domain's own admission is TRIAL_APPROVED/APPROVED, never
  before. Admission is per domain: the observation is tagged with the one admitting domain (invisible to the other),
  and a SUSPENDED/REJECTED decision in one domain never stops another domain's probe (PERSONAL/OFFICE separation).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from aeos.inventory import loop, views
from aeos.inventory.probes import RunResult
from aeos.inventory.reconcile import reconcile
from aeos.inventory.seed import run as seed_run
from aeos.models.identity import User
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


def _latest_installation(db, cid: str) -> ComponentObservation:
    db.expire_all()
    return db.scalars(select(ComponentObservation).where(
        ComponentObservation.component_id == cid, ComponentObservation.dimension == "installation")
        .order_by(ComponentObservation.observed_at.desc(), ComponentObservation.id.desc())).first()


def _bao_in_data_bin(settings) -> Path:
    path = settings.data_dir / "bin" / BAO_NAME
    path.parent.mkdir(parents=True)
    path.write_bytes(b"fake-bao-binary")
    path.chmod(0o755)
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


def _admit(db, cid: str, kind: str = "TRIAL", domain: str = "PERSONAL"):
    from aeos.policy import facade as policy

    owner = db.scalar(select(User).where(User.username == "owner")) or create_admin(db)
    p = ChangeProposal(kind="ADD", title="ADD test", submitted_by="owner", domain=domain, status="REVIEWED",
                       candidate_component_ids=[cid], existing_component_ids=[],
                       baseline_hash="sha256:" + "b" * 64, candidate_hash="sha256:" + "c" * 64)
    db.add(p)
    db.flush()
    policy.record_decision(
        db, kind=kind, actor_user_id=owner.id, target_type="change_proposal", target_id=p.id,
        target_hash="sha256:" + "d" * 64, allowed_operations=["record.decision"], domain=domain,
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


# ---------------------------------------------------------------- lookup precedence and executable check

BAO_OLD = "OpenBao v2.6.0 (0000000000000000000000000000000000000000), committed 2026-01-01T00:00:00Z\n"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the execute bit is a POSIX notion")


def _two_bao(runner, data_bin: Path, *, settings_bao: Path | None = None) -> None:
    """bao on PATH prints 2.6.0, the data-bin one 2.7.1, an optional settings override 2.5.0: told apart by argv[0]."""
    runner.add_binary("bao")

    def answer(argv: list[str]) -> RunResult:
        if settings_bao is not None and argv[0] == str(settings_bao):
            return ok("OpenBao v2.5.0\n")
        return ok(BAO_OUT if argv[0] == str(data_bin) else BAO_OLD)

    runner.outputs["bao"] = answer


def test_bao_lookup_precedence_settings_then_data_bin_then_path(seeded, settings, monkeypatch, tmp_path):
    data_bin = _bao_in_data_bin(settings)
    runner = base_runner()
    _two_bao(runner, data_bin)
    reconcile(seeded, settings, runner=runner, component_id="openbao")
    seeded.commit()
    obs = _installation(seeded, "openbao")
    assert obs.observed_version == "2.7.1" and obs.detail["binaries"][0]["lookup"] == "data-dir"
    assert runner.calls[0]["argv"][0] == str(data_bin)
    # the settings override beats both
    override = tmp_path / "custom" / BAO_NAME  # the fake runner names a binary by its file stem
    override.parent.mkdir()
    override.write_bytes(b"x")
    override.chmod(0o755)
    monkeypatch.setattr(settings, "openbao_binary", str(override))
    runner = base_runner()
    _two_bao(runner, data_bin, settings_bao=override)
    reconcile(seeded, settings, runner=runner, component_id="openbao")
    seeded.commit()
    obs = _latest_installation(seeded, "openbao")
    assert obs.observed_version == "2.5.0" and obs.detail["binaries"][0]["lookup"] == "settings"
    assert runner.calls[0]["argv"][0] == str(override)


def test_bao_directory_in_data_bin_is_ignored(seeded, settings):
    (settings.data_dir / "bin" / BAO_NAME).mkdir(parents=True)
    runner = base_runner()
    reconcile(seeded, settings, runner=runner, component_id="openbao")
    seeded.commit()
    assert _installation(seeded, "openbao").status == "NOT_INSTALLED"
    assert runner.calls == []
    # with bao on PATH the directory is skipped and PATH wins
    runner = base_runner()
    runner.add_binary("bao", BAO_OLD)
    reconcile(seeded, settings, runner=runner, component_id="openbao")
    seeded.commit()
    obs = _latest_installation(seeded, "openbao")
    assert obs.detail["binaries"][0]["lookup"] == "PATH" and obs.observed_version == "2.6.0"


@posix_only
def test_bao_without_execute_bit_in_data_bin_is_ignored(seeded, settings):
    _bao_in_data_bin(settings).chmod(0o644)
    runner = base_runner()
    reconcile(seeded, settings, runner=runner, component_id="openbao")
    seeded.commit()
    assert _installation(seeded, "openbao").status == "NOT_INSTALLED"
    assert runner.calls == []


@pytest.mark.parametrize("error", ["TIMEOUT", "OS_ERROR:PermissionError"])
def test_located_binary_that_fails_to_run_is_unknown_not_installed(seeded, settings, error):
    path = _bao_in_data_bin(settings)
    runner = base_runner()
    runner.outputs["bao"] = RunResult(None, "", "", error, 5)
    reconcile(seeded, settings, runner=runner, component_id="openbao")
    seeded.commit()
    obs = _installation(seeded, "openbao")
    assert obs.status == "UNKNOWN" and obs.observed_identity is None and obs.observed_version is None
    assert error in obs.detail["reason"] and obs.detail["failed"] == ["bao"]
    assert runner.calls[0]["argv"][0] == str(path)


# ---------------------------------------------------------------- admission is per domain


def _installs(db, cid: str) -> list[ComponentObservation]:
    return list(db.scalars(select(ComponentObservation).where(
        ComponentObservation.component_id == cid, ComponentObservation.dimension == "installation")
        .order_by(ComponentObservation.observed_at, ComponentObservation.id)))


def _ccusage_runner():
    runner = base_runner()
    runner.add_binary("ccusage", "ccusage 20.0.30\n")
    return runner


def _view(db, domain: str) -> dict:
    return views.component_detail(db, "ccusage", domain=domain)


def personal_row(db, cid: str) -> dict:
    return next(i for i in views.list_components(db, domain="PERSONAL", page_size=200)["items"] if i["component_id"] == cid)


def office_row(db, cid: str) -> dict:
    return next(i for i in views.list_components(db, domain="OFFICE", page_size=200)["items"] if i["component_id"] == cid)


def test_office_only_trial_is_probed_and_tagged_office_and_hidden_from_personal(seeded, settings):
    _admit(seeded, "ccusage", domain="OFFICE")
    run = reconcile(seeded, settings, runner=_ccusage_runner(), component_id="ccusage")
    seeded.commit()
    assert run.component_status["ccusage"]["status"] == "OK"
    (obs,) = _installs(seeded, "ccusage")
    assert obs.scope["domain"] == "OFFICE" and obs.scope["run_id"] == run.id and obs.status == "INSTALLED"
    office = _view(seeded, "OFFICE")
    assert office["lifecycle"]["installation"]["value"] == "INSTALLED"
    personal = _view(seeded, "PERSONAL")
    inst = personal["lifecycle"]["installation"]
    assert inst["value"] == "UNKNOWN" and "admission" in inst["reason"] and "reconcile" not in inst["reason"]
    assert personal["next_action"]["code"] != "RUN_RECONCILE"
    assert "OFFICE" not in str(personal["lifecycle"]["installation"])
    # the OFFICE-tagged probe raises no drift finding in the PERSONAL list either
    assert any(i["component_id"] == "ccusage" for i in views.drift_list(seeded, domain="OFFICE")["items"])
    assert all(i["component_id"] != "ccusage" for i in views.drift_list(seeded, domain="PERSONAL")["items"])
    assert not personal_row(seeded, "ccusage")["drift"] and office_row(seeded, "ccusage")["drift"]
    # coverage in PERSONAL stays self-consistent: the candidate is neither probeable nor never-probed
    totals = views.coverage(seeded, domain="PERSONAL")["totals"]
    assert totals["observed_fresh"] <= totals["probeable"] and totals["never_probed"] <= totals["probeable"]
    cov_office = views.coverage(seeded, domain="OFFICE")["totals"]
    assert cov_office["probeable"] == totals["probeable"] + 1 and cov_office["observed_fresh"] > totals["observed_fresh"]


def test_personal_rejection_does_not_stop_the_office_probe(seeded, settings):
    _admit(seeded, "ccusage", domain="OFFICE")
    _admit(seeded, "ccusage", "REJECTION", domain="PERSONAL")
    run = reconcile(seeded, settings, runner=_ccusage_runner(), component_id="ccusage")
    seeded.commit()
    assert run.component_status["ccusage"]["status"] == "OK"
    (obs,) = _installs(seeded, "ccusage")
    assert obs.scope["domain"] == "OFFICE"
    personal = _view(seeded, "PERSONAL")
    assert personal["lifecycle"]["admission"]["value"] == "REJECTED"
    assert personal["lifecycle"]["installation"]["value"] == "UNKNOWN"
    assert personal["next_action"]["code"] != "RUN_RECONCILE"


def test_suspension_in_one_domain_does_not_block_the_other(seeded, settings):
    from aeos.inventory.decisions import record_component_decision

    owner = _admit(seeded, "ccusage", domain="PERSONAL")
    _admit(seeded, "ccusage", domain="OFFICE")
    record_component_decision(seeded, component_id="ccusage", actor_user_id=owner.id, rationale="finding",
                              admission="SUSPENDED", domain="PERSONAL")
    seeded.commit()
    reconcile(seeded, settings, runner=_ccusage_runner(), component_id="ccusage")
    seeded.commit()
    (obs,) = _installs(seeded, "ccusage")
    assert obs.scope["domain"] == "OFFICE"


def test_trial_in_both_domains_is_untagged_and_visible_to_both(seeded, settings):
    _admit(seeded, "ccusage", domain="PERSONAL")
    _admit(seeded, "ccusage", domain="OFFICE")
    run = reconcile(seeded, settings, runner=_ccusage_runner(), component_id="ccusage")
    seeded.commit()
    (obs,) = _installs(seeded, "ccusage")
    assert obs.scope == {"run_id": run.id}
    for domain in ("PERSONAL", "OFFICE"):
        assert _view(seeded, domain)["lifecycle"]["installation"]["value"] == "INSTALLED"


def test_required_components_keep_the_untagged_scope(seeded, settings):
    run = reconcile(seeded, settings, runner=base_runner(), component_id="git")
    seeded.commit()
    (obs,) = _installs(seeded, "git")
    assert obs.scope == {"run_id": run.id}


# ---------------------------------------------------------------- polling and views for the same authority


def test_poll_loop_includes_an_admitted_candidate_until_probed(seeded, settings):
    from datetime import timedelta

    from aeos.core.timeutil import utcnow

    assert "ccusage" not in loop.due_components(seeded, limit=500)
    _admit(seeded, "ccusage", domain="OFFICE")
    assert "ccusage" in loop.due_components(seeded, limit=500)
    reconcile(seeded, settings, runner=_ccusage_runner(), component_id="ccusage")
    seeded.commit()
    assert "ccusage" not in loop.due_components(seeded, limit=500)
    assert "ccusage" in loop.due_components(seeded, limit=500, now=utcnow() + timedelta(seconds=3601))


def test_views_for_an_unadmitted_candidate_are_unchanged(seeded):
    for domain in ("PERSONAL", "OFFICE"):
        d = _view(seeded, domain)
        inst = d["lifecycle"]["installation"]
        assert inst["value"] == "UNKNOWN" and "admission" in inst["reason"] and "reconcile" not in inst["reason"]
        assert d["next_action"]["code"] == "REVIEW_CANDIDATE"
        totals = views.coverage(seeded, domain=domain)["totals"]
        assert totals["never_probed"] <= totals["probeable"]
    items = {i["component_id"]: i for i in views.list_components(seeded, domain="PERSONAL", page_size=200)["items"]}
    assert items["ccusage"]["next_action"]["code"] == "REVIEW_CANDIDATE"
