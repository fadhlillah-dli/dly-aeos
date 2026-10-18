"""Live ClaudeNativeBridge checks on the owner's machine (§12 'Bridge wajib memiliki capability test
pada versi CLI yang benar-benar dipakai').

* ``test_live_probe_binary_and_auth`` runs whenever the official binary is on PATH: version + auth
  mode only (``claude --version`` / ``claude auth status``) — no model call, no quota.
* ``test_live_capability_test`` makes ONE tiny structured call on the owner's Max quota, so it is
  opt-in: set ``DLY_AEOS_LIVE_CLAUDE=1``. It never runs in CI and never uses an API key.
"""

from __future__ import annotations

import os
import shutil
import sys

import pytest

from aeos.claude_bridge import binary as binmod

pytestmark = pytest.mark.integration

HAS_CLAUDE = bool(shutil.which("claude.exe" if sys.platform == "win32" else "claude") or shutil.which("claude"))
LIVE = os.environ.get("DLY_AEOS_LIVE_CLAUDE") == "1"


@pytest.mark.skipif(not HAS_CLAUDE, reason="official Claude Code binary not on PATH")
def test_live_probe_binary_and_auth(settings):
    binmod.clear_probe_cache()
    pr = binmod.probe(settings, refresh=True)
    assert pr.binary.found, pr.binary.reasons
    assert pr.binary.version and pr.binary.fingerprint and pr.binary.fingerprint.startswith("sha256:")
    # The owner's machine is expected to be on the native claude.ai login; anything else is BLOCKED.
    assert pr.auth.status in {"VERIFIED", "DEGRADED", "BLOCKED"}
    if pr.auth.status != "BLOCKED":
        assert pr.auth.auth_method == "claude.ai" and pr.auth.api_provider == "firstParty"


@pytest.mark.skipif(not (HAS_CLAUDE and LIVE), reason="set DLY_AEOS_LIVE_CLAUDE=1 to spend one tiny Max call")
def test_live_capability_test(settings):
    from aeos.claude_bridge import bridge

    binmod.clear_probe_cache()
    out = bridge.capability_test("opus", actor="pytest-live", settings=settings)
    assert out["status"] in {"VERIFIED", "DEGRADED"}, out["steps"]
    assert out["model_resolved"] and out["model_resolved"].startswith("claude-opus")
    call = next(s for s in out["steps"] if s["id"] == "structured_call")
    assert call["status"] == "VERIFIED" and out["trace_ref"]
    trace = (settings.data_dir / out["trace_ref"]).read_text(encoding="utf-8")
    assert "--bare" not in trace and "total_cost_usd" not in trace
