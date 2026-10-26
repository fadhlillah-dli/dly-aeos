"""One OpenBao lookup rule for the reconciler, the Doctor and the vault runtime (``aeos.core.binaries``): hermetic."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from aeos.core.binaries import is_executable_file, locate_binary
from aeos.core.timeutil import utcnow
from aeos.doctor.checks._common import resolve_openbao
from aeos.doctor.model import CheckContext
from aeos.secrets import server
from tests.unit.test_inventory_fakes import base_runner

BAO_NAME = "bao.exe" if sys.platform == "win32" else "bao"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the execute bit is a POSIX notion")


def _data_bin_dir(settings) -> Path:
    d = settings.data_dir / "bin"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_exec(path: Path, mode: int = 0o755) -> Path:
    path.write_bytes(b"fake")
    path.chmod(mode)
    return path


def test_predicate_rejects_directory_and_missing(tmp_path):
    (tmp_path / "d").mkdir()
    assert not is_executable_file(tmp_path / "d") and not is_executable_file(tmp_path / "missing")
    assert is_executable_file(_make_exec(tmp_path / "f"))


@posix_only
def test_predicate_rejects_file_without_execute_bit(tmp_path):
    assert not is_executable_file(_make_exec(tmp_path / "f", 0o644))


def test_precedence_settings_then_data_bin_then_path(tmp_path):
    data = tmp_path / "data"
    (data / "bin").mkdir(parents=True)
    local = _make_exec(data / "bin" / BAO_NAME)
    override = _make_exec(tmp_path / "custom-bao")
    which = lambda n: "/usr/bin/bao"  # noqa: E731
    assert locate_binary("bao", configured=str(override), data_dir=data, which=which) == (str(override), "settings")
    assert locate_binary("bao", data_dir=data, which=which) == (str(local), "data-dir")
    assert locate_binary("bao", data_dir=None, which=which) == ("/usr/bin/bao", "PATH")
    # a settings override that is unusable is authoritative: no silent fall-through to another binary
    assert locate_binary("bao", configured=str(tmp_path / "nope"), data_dir=data, which=which) == (None, "settings")


def test_settings_override_expands_user(tmp_path, monkeypatch):
    _make_exec(tmp_path / "mybao")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert locate_binary("bao", configured="~/mybao", which=lambda n: None) == (os.path.join(str(tmp_path), "mybao"),
                                                                                "settings")


def test_doctor_skips_a_directory_named_bao_in_data_bin(settings, db):
    (_data_bin_dir(settings) / BAO_NAME).mkdir()
    ctx = CheckContext(settings=settings, session=db, runner=base_runner(), repo=settings.data_dir, now=utcnow())
    assert resolve_openbao(ctx) == (None, "PATH")
    ctx.runner.add_binary("bao")
    assert resolve_openbao(ctx) == (ctx.runner.binaries["bao"], "PATH")
    (_data_bin_dir(settings) / BAO_NAME).rmdir()
    local = _make_exec(_data_bin_dir(settings) / BAO_NAME)
    assert resolve_openbao(ctx) == (str(local), "data-dir")


def test_vault_runtime_skips_a_directory_named_bao_in_data_bin(settings, monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda name: None)
    (_data_bin_dir(settings) / BAO_NAME).mkdir()
    assert server.bin_path(settings) is None
    (_data_bin_dir(settings) / BAO_NAME).rmdir()
    local = _make_exec(_data_bin_dir(settings) / BAO_NAME)
    assert server.bin_path(settings) == local
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/bao")
    local.unlink()
    assert server.bin_path(settings) == Path("/usr/bin/bao")


@posix_only
def test_doctor_and_vault_skip_a_non_executable_data_bin_entry(settings, db, monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda name: None)
    _make_exec(_data_bin_dir(settings) / BAO_NAME, 0o644)
    ctx = CheckContext(settings=settings, session=db, runner=base_runner(), repo=settings.data_dir, now=utcnow())
    assert resolve_openbao(ctx) == (None, "PATH")
    assert server.bin_path(settings) is None
