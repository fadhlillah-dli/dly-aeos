"""scripts/component_status.py - bootstrap component list (master §07; §50 "Dependency inti belum
terpasang -> Bootstrap CLI memberi alasan/remedy").

The script never installs anything, prints install hints for missing tools, and reports the Claude
Code login only from non-identifying ``claude auth status`` fields; an unreadable status is UNKNOWN,
never "logged in".
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from aeos.core.paths import repo_root


@pytest.fixture
def cs() -> ModuleType:
    path = Path(repo_root()) / "scripts" / "component_status.py"
    spec = importlib.util.spec_from_file_location("aeos_component_status_under_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop(spec.name, None)


def _fake_run(responses: dict[tuple[str, ...], tuple[int, str]]):
    calls: list[list[str]] = []

    def run(cmd, timeout=15):
        calls.append(list(cmd))
        return responses.get(tuple(cmd), (127, ""))

    return run, calls


def test_claude_auth_summary_drops_identifying_fields(cs, monkeypatch):
    status = {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max", "email": "me@example.com",
              "orgId": "org-123"}
    run, _ = _fake_run({("claude", "auth", "status", "--json"): (0, json.dumps(status))})
    monkeypatch.setattr(cs, "_run", run)
    summary = cs.claude_auth_summary()
    assert summary["loggedIn"] is True and summary["authMethod"] == "claude.ai"
    assert "me@example.com" not in json.dumps(summary) and "org-123" not in json.dumps(summary)


def test_claude_auth_falls_back_to_plain_command(cs, monkeypatch):
    run, calls = _fake_run({("claude", "auth", "status", "--json"): (2, "error: unknown option '--json'"),
                            ("claude", "auth", "status"): (0, '{"loggedIn": false}')})
    monkeypatch.setattr(cs, "_run", run)
    assert cs.claude_auth_summary()["loggedIn"] is False
    assert ["claude", "auth", "status"] in calls


def test_unreadable_auth_status_is_unknown_not_logged_in(cs, monkeypatch):
    run, _ = _fake_run({("claude", "--version"): (0, "2.1.0 (Claude Code)"),
                        ("claude", "auth", "status", "--json"): (0, "Logged in as someone"),
                        ("claude", "auth", "status"): (0, "Logged in as someone")})
    monkeypatch.setattr(cs, "_run", run)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    item = cs._claude()
    assert item["status"] == "OK" and item["auth"]["loggedIn"] is None
    assert any("UNKNOWN" in w for w in item["warnings"])
    assert "someone" not in json.dumps(item)


def test_api_key_in_environment_is_warned(cs, monkeypatch):
    run, _ = _fake_run({("claude", "--version"): (0, "2.1.0"),
                        ("claude", "auth", "status", "--json"): (0, '{"loggedIn": true, "authMethod": "claude.ai"}')})
    monkeypatch.setattr(cs, "_run", run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder-not-a-real-key")
    item = cs._claude()
    assert any("ANTHROPIC_API_KEY" in w for w in item["warnings"])
    assert "placeholder-not-a-real-key" not in json.dumps(item)


def test_missing_tool_reports_official_hint_and_never_installs(cs, monkeypatch):
    run, calls = _fake_run({})
    monkeypatch.setattr(cs, "_run", run)
    item = cs._tool("OpenCode", "opencode", ["--version"], 5, False, "worker")
    assert item["status"] == "MISSING" and f"opencode-ai@{cs.OPENCODE_PIN}" in item["install_hint"]
    assert all(c[0] == "opencode" for c in calls)  # only the probe ran - no installer was invoked
