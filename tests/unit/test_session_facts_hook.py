"""SessionStart launcher (scripts/claude_hooks/session_facts.py): the facts describe the project interpreter.

Nothing here runs scripts/preflight.py for real (it calls ``gh auth status``): the runner is a fake (LESSONS L20)."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts" / "claude_hooks" / "session_facts.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("session_facts_under_test", LAUNCHER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launcher = _load()


def _fake_venv(base: Path) -> Path:
    sub = ("Scripts", "python.exe") if sys.platform == "win32" else ("bin", "python")
    py = base / ".venv" / sub[0] / sub[1]
    py.parent.mkdir(parents=True)
    py.write_text("", encoding="utf-8")
    py.chmod(0o755)
    return py


@pytest.fixture(autouse=True)
def _no_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AEOS_GATES_PYTHON", raising=False)


def test_the_checkout_venv_is_preferred_over_the_launching_interpreter(tmp_path: Path) -> None:
    py = _fake_venv(tmp_path)
    assert launcher.interpreter(tmp_path) == str(py)


def test_without_a_venv_the_launching_interpreter_is_the_fallback(tmp_path: Path) -> None:
    assert launcher.interpreter(tmp_path) == sys.executable


def test_the_override_wins_when_it_is_an_executable_file_and_is_ignored_otherwise(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mine = tmp_path / "mine"
    mine.write_text("", encoding="utf-8")
    mine.chmod(0o755)
    monkeypatch.setenv("AEOS_GATES_PYTHON", str(mine))
    if os.name == "posix":
        assert launcher.interpreter(tmp_path) == str(mine)
    monkeypatch.setenv("AEOS_GATES_PYTHON", str(tmp_path / "missing"))
    assert launcher.interpreter(tmp_path) == sys.executable


def test_main_runs_preflight_facts_with_the_venv_python_and_passes_its_output_through(
        tmp_path: Path, capfdbinary: pytest.CaptureFixture[bytes]) -> None:
    py = _fake_venv(tmp_path)
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"project_venv: yes (fake)\n", b"")

    assert launcher.main(fake_run, tmp_path) == 0
    assert calls == [[str(py), str(tmp_path / "scripts" / "preflight.py"), "--facts"]]
    assert capfdbinary.readouterr().out == b"project_venv: yes (fake)\n"


@pytest.mark.parametrize("failure", ["raises", "timeout", "nonzero", "empty"])
def test_every_failure_is_silent_exit_zero(
        failure: str, tmp_path: Path, capfdbinary: pytest.CaptureFixture[bytes]) -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        if failure == "raises":
            raise OSError("no such interpreter")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 1)
        return subprocess.CompletedProcess(argv, 3 if failure == "nonzero" else 0, b"partial" if failure == "nonzero" else b"", b"x")

    assert launcher.main(fake_run, tmp_path) == 0
    captured = capfdbinary.readouterr()
    assert captured.out == b"" and captured.err == b""
