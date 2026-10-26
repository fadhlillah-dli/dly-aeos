"""Reconciler observes what AEOS itself installs (hermetic: fake runner, tmp data dir).

* OpenBao installed by ``aeos vault install`` lives in ``<data>/bin`` (not on PATH): the installation probe takes the
  settings override, then an executable regular file in ``<data>/bin``, then PATH, and reports NOT_INSTALLED only when
  none has it (one shared rule, ``aeos.core.binaries``).
* A located binary whose run did not complete normally (timeout, OS error, no status, killed by a signal, Windows
  NTSTATUS crash) is UNKNOWN with evidence and no observed identity or version, never INSTALLED.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from aeos.inventory.probes import RunResult
from aeos.inventory.reconcile import reconcile
from aeos.inventory.seed import run as seed_run
from aeos.models.inventory import ComponentObservation
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


def _unknown_assertions(seeded, path, name="bao"):
    obs = _installation(seeded, "openbao")
    assert obs.status == "UNKNOWN" and obs.observed_identity is None and obs.observed_version is None
    assert obs.detail["failed"] == [name] and obs.detail["binaries"][0]["digest"] is None
    return obs


@pytest.mark.parametrize("error", ["TIMEOUT", "OS_ERROR:PermissionError"])
def test_located_binary_that_fails_to_run_is_unknown_not_installed(seeded, settings, error):
    path = _bao_in_data_bin(settings)
    runner = base_runner()
    runner.outputs["bao"] = RunResult(None, "", "", error, 5)
    reconcile(seeded, settings, runner=runner, component_id="openbao")
    seeded.commit()
    obs = _unknown_assertions(seeded, path)
    assert error in obs.detail["reason"]
    assert runner.calls[0]["argv"][0] == str(path)


@pytest.mark.parametrize("rc", [-9, -6])
def test_binary_killed_by_a_signal_is_unknown_not_installed(seeded, settings, rc):
    path = _bao_in_data_bin(settings)
    runner = base_runner()
    runner.outputs["bao"] = RunResult(rc, "", "", None, 5)
    reconcile(seeded, settings, runner=runner, component_id="openbao")
    seeded.commit()
    obs = _unknown_assertions(seeded, path)
    assert f"exit {rc}" in obs.detail["reason"]


def test_windows_ntstatus_crash_is_unknown_not_installed(seeded, settings, monkeypatch):
    from aeos.inventory import reconcile as reconcile_mod

    monkeypatch.setattr(reconcile_mod.sys, "platform", "win32")
    path = settings.data_dir / "bin" / "bao.exe"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x")
    runner = base_runner()
    runner.add_binary("bao")
    runner.binaries["bao"] = str(path)  # the fake runner names the binary by its path
    runner.outputs["bao"] = RunResult(0xC0000135, "", "", None, 5)  # STATUS_DLL_NOT_FOUND
    reconcile(seeded, settings, runner=runner, component_id="openbao")
    seeded.commit()
    _unknown_assertions(seeded, path)


def test_nonzero_normal_exit_is_not_a_crash(seeded, settings):
    """A plain non-zero exit (no signal, no error) keeps the previous behaviour: INSTALLED without a version."""
    _bao_in_data_bin(settings)
    runner = base_runner()
    runner.outputs["bao"] = RunResult(1, "", "usage", None, 5)
    reconcile(seeded, settings, runner=runner, component_id="openbao")
    seeded.commit()
    obs = _installation(seeded, "openbao")
    assert obs.status == "INSTALLED" and obs.observed_version is None


@pytest.mark.parametrize("cid,name", [("claude-code", "claude"), ("git", "git"), ("opencode", "opencode")])
@pytest.mark.parametrize("result", [RunResult(None, "", "", "TIMEOUT", 5), RunResult(None, "", "", "OS_ERROR:X", 5),
                                    RunResult(-9, "", "", None, 5)])
def test_required_binary_that_does_not_run_is_unknown_too(seeded, settings, cid, name, result):
    runner = base_runner()
    runner.add_binary(name)
    runner.outputs[name] = result
    reconcile(seeded, settings, runner=runner, component_id=cid)
    seeded.commit()
    obs = _installation(seeded, cid)
    assert obs.status == "UNKNOWN" and obs.observed_identity is None and obs.observed_version is None
