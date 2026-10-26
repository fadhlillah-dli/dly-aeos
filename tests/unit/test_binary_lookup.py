"""One OpenBao lookup rule for the reconciler, the Doctor and the vault runtime (``aeos.core.binaries``): hermetic."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from aeos.core import binaries
from aeos.core.binaries import data_bin_dir, exe_name, is_executable_file, locate_binary
from aeos.core.timeutil import utcnow
from aeos.doctor.checks._common import resolve_binary, resolve_openbao
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


def _found(**kw):
    r = locate_binary("bao", **kw)
    return r.path, r.lookup


def test_precedence_settings_then_data_bin_then_path(tmp_path):
    data = tmp_path / "data"
    (data / "bin").mkdir(parents=True)
    local = _make_exec(data / "bin" / BAO_NAME)
    override = _make_exec(tmp_path / "custom-bao")
    which = lambda n: "/usr/bin/bao"  # noqa: E731
    assert _found(configured=str(override), data_dir=data, which=which) == (str(override), "settings")
    assert _found(data_dir=data, which=which) == (str(local), "data-dir")
    assert _found(data_dir=None, which=which) == ("/usr/bin/bao", "PATH")
    # a settings override that is unusable is authoritative: no silent fall-through to another binary
    assert _found(configured=str(tmp_path / "nope"), data_dir=data, which=which) == (None, "settings")


def test_a_miss_names_the_step_that_decided_and_the_rejected_path(tmp_path):
    data = tmp_path / "data"
    none = lambda n: None  # noqa: E731
    d = tmp_path / "a-directory"
    d.mkdir()
    r = locate_binary("bao", configured=str(d), data_dir=data, which=lambda n: "/usr/bin/bao")
    assert r == (None, "settings", str(d))  # the override wins even though PATH has a bao
    assert locate_binary("bao", data_dir=data, which=none) == (None, "data-dir/PATH", None)
    assert locate_binary("git", data_dir=None, which=none) == (None, "PATH", None)


def test_settings_override_expands_user(tmp_path, monkeypatch):
    _make_exec(tmp_path / "mybao")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert _found(configured="~/mybao", which=lambda n: None) == (os.path.join(str(tmp_path), "mybao"), "settings")


def test_data_bin_candidate_comes_from_the_installers_own_functions(settings):
    """The place `aeos vault install` writes to and the place the lookup reads cannot drift (one helper)."""
    from aeos.secrets import installer

    target = installer.install_dir(settings) / installer.binary_name()
    assert target == data_bin_dir(settings.data_dir) / exe_name("bao")
    target.parent.mkdir(parents=True, exist_ok=True)
    _make_exec(target)
    assert _found(data_dir=settings.data_dir, which=lambda n: None) == (str(target), "data-dir")


def test_path_step_is_the_same_call_for_every_caller(settings, db, monkeypatch):
    """No caller asks for ``bao.exe`` first: the vault runtime, the Doctor and the reconciler all ask ``which('bao')``."""
    asked: list[str] = []
    monkeypatch.setattr(binaries.shutil, "which", lambda n: asked.append(n))
    assert server.bin_path(settings) is None
    ctx = CheckContext(settings=settings, session=db, runner=base_runner(), repo=settings.data_dir, now=utcnow())
    assert resolve_openbao(ctx) == (None, "data-dir/PATH")
    assert asked == ["bao"] and ctx.runner.which_calls[-1] == "bao"


def test_doctor_skips_a_directory_named_bao_in_data_bin(settings, db):
    (_data_bin_dir(settings) / BAO_NAME).mkdir()
    ctx = CheckContext(settings=settings, session=db, runner=base_runner(), repo=settings.data_dir, now=utcnow())
    assert resolve_openbao(ctx) == (None, "data-dir/PATH")
    ctx.runner.add_binary("bao")
    assert resolve_openbao(ctx) == (ctx.runner.binaries["bao"], "PATH")
    (_data_bin_dir(settings) / BAO_NAME).rmdir()
    local = _make_exec(_data_bin_dir(settings) / BAO_NAME)
    assert resolve_openbao(ctx) == (str(local), "data-dir")


def test_vault_runtime_data_bin_then_path(settings, monkeypatch):
    monkeypatch.setattr(binaries.shutil, "which", lambda name: None)
    assert server.bin_path(settings) is None
    local = _make_exec(_data_bin_dir(settings) / BAO_NAME)
    assert server.bin_path(settings) == local
    monkeypatch.setattr(binaries.shutil, "which", lambda name: "/usr/bin/bao")
    local.unlink()
    assert server.bin_path(settings) == Path("/usr/bin/bao")


@posix_only
def test_vault_runtime_skips_a_non_executable_data_bin_file(settings, monkeypatch):
    """origin/main accepted any ``is_file()`` here and would try to start a file that cannot run."""
    monkeypatch.setattr(binaries.shutil, "which", lambda name: None)
    _make_exec(_data_bin_dir(settings) / BAO_NAME, 0o644)
    assert server.bin_path(settings) is None
    monkeypatch.setattr(binaries.shutil, "which", lambda name: "/usr/bin/bao")
    assert server.bin_path(settings) == Path("/usr/bin/bao")  # falls through to PATH instead


@posix_only
def test_vault_runtime_rejects_a_non_executable_settings_file_and_says_so(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(binaries.shutil, "which", lambda name: "/usr/bin/bao")
    plain = _make_exec(tmp_path / "bao-no-x", 0o644)
    monkeypatch.setattr(settings, "openbao_binary", str(plain))
    assert server.bin_path(settings) is None
    found = server.bin_lookup(settings)
    assert found.lookup == "settings" and found.rejected == str(plain)


@posix_only
def test_doctor_and_vault_skip_a_non_executable_data_bin_entry(settings, db, monkeypatch):
    monkeypatch.setattr(binaries.shutil, "which", lambda name: None)
    _make_exec(_data_bin_dir(settings) / BAO_NAME, 0o644)
    ctx = CheckContext(settings=settings, session=db, runner=base_runner(), repo=settings.data_dir, now=utcnow())
    assert resolve_openbao(ctx) == (None, "data-dir/PATH")
    assert server.bin_path(settings) is None


# ---------------------------------------------------------------- Doctor: every binary uses the same rule


def _ctx(settings, db, runner=None):
    return CheckContext(settings=settings, session=db, runner=runner or base_runner(), repo=settings.data_dir,
                        now=utcnow())


def test_doctor_resolve_binary_expands_user_like_the_inventory(settings, db, monkeypatch, tmp_path):
    """origin/main: stat('~/bin/claude') finds nothing, so the Doctor said MISSING where the inventory said INSTALLED."""
    (tmp_path / "bin").mkdir()
    exe = _make_exec(tmp_path / "bin" / "claude")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(settings, "claude_binary", "~/bin/claude")
    assert resolve_binary(_ctx(settings, db), "claude", "claude_binary") == (str(exe), "settings")


def test_doctor_resolve_binary_rejects_a_directory_and_a_non_executable_file(settings, db, monkeypatch, tmp_path):
    d = tmp_path / "a-dir"
    d.mkdir()
    monkeypatch.setattr(settings, "claude_binary", str(d))
    runner = base_runner()
    runner.add_binary("claude")  # on PATH, but the settings override is authoritative
    assert resolve_binary(_ctx(settings, db, runner), "claude", "claude_binary") == (None, "settings")


@posix_only
def test_doctor_resolve_binary_rejects_a_file_without_execute_bit(settings, db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "opencode_binary", str(_make_exec(tmp_path / "opencode", 0o644)))
    assert resolve_binary(_ctx(settings, db), "opencode", "opencode_binary") == (None, "settings")


def test_doctor_resolve_binary_without_override_uses_path(settings, db):
    runner = base_runner()
    path = runner.add_binary("docker")
    assert resolve_binary(_ctx(settings, db, runner), "docker") == (path, "PATH")
    assert resolve_binary(_ctx(settings, db), "node") == (None, "PATH")


def test_doctor_resolver_falls_back_to_the_real_check_for_a_runner_without_is_executable(settings, db, tmp_path):
    """A third-party runner without ``is_executable`` still gets the shared predicate."""
    class Bare:
        def which(self, name):
            return None

    exe = _make_exec(tmp_path / "x")
    monkey = settings.model_copy(update={"claude_binary": str(exe)})
    ctx = CheckContext(settings=monkey, session=db, runner=Bare(), repo=settings.data_dir, now=utcnow())
    assert resolve_binary(ctx, "claude", "claude_binary") == (str(exe), "settings")
