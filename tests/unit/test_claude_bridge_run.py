"""run_structured / capability_test end to end with a fake `claude` child process (§12, §42, §50).

The fake records argv/cwd/env/stdin so the tests prove: prompt via stdin, required flags, no
--bare, scrubbed environment, fresh supervisor cwd, quota exhaustion -> wait (no fallback),
model policy mismatch, usage recorded as NATIVE_CLAUDE with cost UNKNOWN (never dollars)."""

from __future__ import annotations

import json
import sys
import textwrap
import time
from pathlib import Path

import psutil
import pytest

from aeos.claude_bridge import binary as binmod
from aeos.claude_bridge import bridge, process
from aeos.claude_bridge.binary import AuthInfo, BinaryInfo, Probe
from aeos.claude_bridge.facade import ClaudeRequest
from aeos.claude_bridge.process import run_process
from aeos.models.claude_bridge_ext import ClaudeCapabilityTest
from aeos.models.cost import QuotaState
from aeos.models.routing import ModelRoute
from tests.conftest import VIEWER_PW, AuthedClient, create_admin, create_viewer
from tests.unit.test_claude_bridge_parsing import FABLE_FALLBACK, OPUS_OK, SCHEMA
from tests.unit.test_routing_fakes import install_fakes

PROMPT = "Rencanakan fitur kecil. Kembalikan {\"ok\": true}."


def _probe(found: bool = True, auth: str = "VERIFIED", fingerprint: str = "sha256:" + "a" * 64,
           version: str = "2.1.302") -> Probe:
    info = BinaryInfo(found=found, path=Path("claude.exe") if found else None, source="PATH",
                      version=version if found else None, fingerprint=fingerprint if found else None,
                      reasons=[] if found else ["official Claude Code binary not found on PATH"])
    return Probe(binary=info, auth=AuthInfo(status=auth, logged_in=True, auth_method="claude.ai",
                                            api_provider="firstParty", subscription_type="max"), checked_at=0.0)


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Returns a setter: fake(output_dict|None, exit_code=0, stderr="", sleep=0)."""
    record = tmp_path / "record.json"
    state: dict = {}

    def configure(output: dict | None = OPUS_OK, exit_code: int = 0, stderr: str = "", sleep: float = 0.0):
        script = tmp_path / "fake_claude.py"
        script.write_text(textwrap.dedent(f"""
            import json, os, sys, time
            data = {{"argv": sys.argv[1:], "cwd": os.getcwd(), "env": sorted(os.environ), "stdin": sys.stdin.read()}}
            open({str(record)!r}, "w", encoding="utf-8").write(json.dumps(data))
            time.sleep({sleep})
            out = {json.dumps(json.dumps(output)) if output is not None else repr("")}
            sys.stdout.write(out)
            sys.stderr.write({stderr!r})
            sys.exit({exit_code})
        """), encoding="utf-8")
        state["script"] = script

    def fake_run(argv, **kw):
        state["calls"] = state.get("calls", 0) + 1
        return process.run_process([sys.executable, str(state["script"]), *argv[1:]], **kw)

    monkeypatch.setattr(bridge, "run_process", fake_run)
    monkeypatch.setattr(binmod, "probe", lambda settings, refresh=False: _probe())
    configure()
    configure.record = record  # type: ignore[attr-defined]
    configure.state = state  # type: ignore[attr-defined]
    return configure


def _seed_routes(db):
    from aeos.routing import seed

    seed.run(db)
    db.commit()


def test_run_structured_contract(settings, db, fake_claude, monkeypatch, tmp_path):
    """Prompt on stdin; required flags; no --bare; scrubbed env; fresh supervisor cwd; trace redacted."""
    reg = install_fakes(monkeypatch)
    _seed_routes(db)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "q" * 30)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    ctx = tmp_path / "context.md"
    ctx.write_text("konteks proyek", encoding="utf-8")
    res = bridge.run_structured(ClaudeRequest(purpose="planning.primary", role="Primary Planner", model="opus",
                                              prompt=PROMPT, json_schema=SCHEMA, project_id="p1", domain="PERSONAL",
                                              context_files=[ctx]), settings=settings)
    assert res.ok, res.error
    assert res.structured == {"ok": True} and res.model_resolved == "claude-opus-5-5" and res.mode == "NATIVE"
    rec = json.loads(fake_claude.record.read_text(encoding="utf-8"))
    argv = rec["argv"]
    assert rec["stdin"] == PROMPT and PROMPT not in argv
    assert "--bare" not in argv and "--dangerously-skip-permissions" not in argv
    for flag in ("-p", "--restricted", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == SCHEMA
    for forbidden in ("ANTHROPIC_API_KEY", "CLAUDECODE", "CLAUDE_CODE_USE_BEDROCK"):
        assert forbidden not in rec["env"]
    cwd = Path(rec["cwd"]).resolve()
    assert cwd.parent == settings.layout.supervisor_workspace.resolve()
    assert not (cwd / "context").exists()  # copied context removed after the run
    trace = (settings.data_dir / res.trace_ref).read_text(encoding="utf-8")
    assert PROMPT not in trace and json.dumps(SCHEMA, separators=(",", ":")) not in trace
    assert "total_cost_usd" not in trace and "costUSD" not in trace
    # usage ledger: native Claude, never converted to dollars
    assert reg.usage and reg.usage[0]["category"] == "NATIVE_CLAUDE" and reg.usage[0]["cost_usd"] is None
    assert reg.usage[0]["is_estimate"] is False and reg.usage[0]["route_key"] == "claude-native/opus"
    db.expire_all()
    opus = db.query(ModelRoute).filter_by(route_key="claude-native/opus").one()
    assert opus.resolved_version == "claude-opus-5-5" and opus.identity_status == "VERIFIED"


def test_fable_policy_mismatch_rejected_and_not_recorded_as_resolution(settings, db, fake_claude, monkeypatch):
    install_fakes(monkeypatch)
    _seed_routes(db)
    fake_claude(FABLE_FALLBACK)
    res = bridge.run_structured(ClaudeRequest(purpose="review.checkpoint", role="Reviewer", model="fable",
                                              prompt=PROMPT, json_schema=SCHEMA), settings=settings)
    assert not res.ok and res.error_code == "MODEL_POLICY_MISMATCH" and res.model_resolved == "claude-opus-4-8"
    db.expire_all()
    assert db.query(ModelRoute).filter_by(route_key="claude-native/fable").one().resolved_version is None


def test_quota_exhausted_checkpoint_wait_no_fallback(settings, db, fake_claude, monkeypatch):
    """§50 'Fable Max quota habis': ok=False QUOTA_EXHAUSTED, QuotaState EXHAUSTED, single call only."""
    install_fakes(monkeypatch)
    out = dict(OPUS_OK, is_error=True, subtype="error_during_execution", structured_output=None,
               result="Claude AI usage limit reached|1893456000")  # resets 2030-01-01
    fake_claude(out, exit_code=1)
    res = bridge.run_structured(ClaudeRequest(purpose="review.checkpoint", role="Reviewer", model="fable",
                                              prompt=PROMPT, json_schema=SCHEMA), settings=settings)
    assert not res.ok and res.quota_exhausted and res.error_code == "QUOTA_EXHAUSTED"
    assert fake_claude.state["calls"] == 1  # no retry with another model
    q = db.query(QuotaState).filter_by(provider="claude-native", scope="fable").one()
    assert q.state == "EXHAUSTED" and q.resets_at is not None
    assert bridge.quota_view(db)["state"] == "EXHAUSTED"
    q.resets_at = q.resets_at.replace(year=2020)  # reset passed -> back to UNKNOWN (never "OK")
    db.commit()
    assert bridge.quota_view(db)["state"] == "UNKNOWN"


def test_blocked_bridge_never_runs(settings, monkeypatch):
    called = []
    monkeypatch.setattr(bridge, "run_process", lambda *a, **k: called.append(1))
    monkeypatch.setattr(binmod, "probe", lambda settings, refresh=False: _probe(found=False))
    res = bridge.run_structured(ClaudeRequest(purpose="x", role="r", model="opus", prompt=PROMPT, json_schema=SCHEMA),
                                settings=settings)
    assert not res.ok and res.error_code == "BRIDGE_BLOCKED" and "manual package" in res.error and not called
    monkeypatch.setattr(binmod, "probe", lambda settings, refresh=False: _probe(auth="BLOCKED"))
    res = bridge.run_structured(ClaudeRequest(purpose="x", role="r", model="opus", prompt=PROMPT, json_schema=SCHEMA),
                                settings=settings)
    assert res.error_code == "BRIDGE_BLOCKED" and not called


@pytest.mark.parametrize("kwargs, code", [
    ({"model": "gpt-6"}, "REQUEST_INVALID"),
    ({"model": "haiku"}, "REQUEST_INVALID"),
    ({"allowed_tools": ["Read", "Bash"]}, "TOOL_NOT_ALLOWED"),
    ({"allowed_tools": ["Edit"]}, "TOOL_NOT_ALLOWED"),
    ({"project_id": "p1"}, "DOMAIN_REQUIRED"),
    ({"effort": "ultra"}, "REQUEST_INVALID"),
    ({"json_schema": {"type": "nope"}}, "SCHEMA_INVALID"),
    ({"prompt": "use key ghp_" + "c" * 36}, "SECRET_IN_PROMPT"),
])
def test_invalid_requests_never_reach_the_binary(settings, monkeypatch, kwargs, code):
    called = []
    monkeypatch.setattr(bridge, "run_process", lambda *a, **k: called.append(1))
    monkeypatch.setattr(binmod, "probe", lambda settings, refresh=False: _probe())
    base = {"purpose": "x", "role": "r", "model": "opus", "prompt": PROMPT, "json_schema": SCHEMA}
    res = bridge.run_structured(ClaudeRequest(**{**base, **kwargs}), settings=settings)
    assert not res.ok and res.error_code == code and not called


def test_office_denied_by_default_and_when_policy_missing(settings, monkeypatch, fake_claude):
    """§23/§51: Office policy applies from the first planning call; missing policy fails closed."""
    req = ClaudeRequest(purpose="planning.primary", role="Planner", model="opus", prompt=PROMPT, json_schema=SCHEMA,
                        project_id="p-office", domain="OFFICE")
    res = bridge.run_structured(req, settings=settings)  # policy module not installed
    assert res.error_code == "OFFICE_POLICY_DENIED"
    reg = install_fakes(monkeypatch)
    assert bridge.run_structured(req, settings=settings).error_code == "OFFICE_POLICY_DENIED"
    assert "calls" not in fake_claude.state
    reg.office["claude_max_allowed_for_office_data"] = True
    assert bridge.run_structured(req, settings=settings).ok


def test_timeout_kills_the_whole_process_tree(tmp_path: Path):
    pidfile = tmp_path / "grandchild.pid"
    script = tmp_path / "hang.py"
    script.write_text(textwrap.dedent(f"""
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        open({str(pidfile)!r}, "w").write(str(child.pid))
        sys.stdin.read()
        time.sleep(120)
    """), encoding="utf-8")
    res = run_process([sys.executable, str(script)], stdin_text="x", cwd=tmp_path,
                      env={k: v for k, v in __import__("os").environ.items()}, timeout=3)
    assert res.timed_out
    deadline = time.time() + 10
    while not pidfile.exists() and time.time() < deadline:
        time.sleep(0.1)
    grandchild = int(pidfile.read_text())
    assert grandchild in res.killed_pids
    time.sleep(0.5)
    assert not psutil.pid_exists(grandchild) or psutil.Process(grandchild).status() == psutil.STATUS_ZOMBIE


def test_run_structured_timeout_reports_and_traces(settings, fake_claude):
    fake_claude(OPUS_OK, sleep=30)
    res = bridge.run_structured(ClaudeRequest(purpose="x", role="r", model="opus", prompt=PROMPT, json_schema=SCHEMA,
                                              timeout_seconds=2), settings=settings)
    assert not res.ok and res.error_code == "TIMEOUT" and res.trace_ref
    trace = json.loads((settings.data_dir / res.trace_ref).read_text(encoding="utf-8"))
    assert trace["timed_out"] is True


def test_capability_test_persists_evidence_and_goes_stale_on_binary_change(settings, db, fake_claude, monkeypatch):
    reg = install_fakes(monkeypatch)
    _seed_routes(db)
    out = bridge.capability_test("opus", actor="tester", settings=settings)
    assert out["status"] == "VERIFIED", out
    assert [s["id"] for s in out["steps"]] == ["binary", "auth", "env", "structured_call"]
    assert out["model_resolved"] == "claude-opus-5-5" and out["evidence_id"] == "evidence-1"
    ev = reg.evidence[0]
    assert ev["origin"] == "doctor" and ev["evidence_level"] == "E2" and ev["subject_id"] == "claude-code"
    assert "owner@" not in json.dumps(ev)  # e-mail not copied into evidence
    assert reg.observations and reg.observations[0]["component_id"] == "claude-code"
    row = db.query(ClaudeCapabilityTest).one()
    assert row.status == "VERIFIED" and row.binary_fingerprint
    view = bridge.status(db, settings)
    assert view["last_capability_test"]["status"] == "VERIFIED"
    assert view["quota"]["state"] == "UNKNOWN"  # never "enough": no official remaining-quota source
    # binary replaced -> STALE + Doctor invalidation (§08, §50 'Config/model/provider berubah setelah doctor')
    monkeypatch.setattr(binmod, "probe", lambda settings, refresh=False: _probe(fingerprint="sha256:" + "b" * 64))
    view = bridge.status(db, settings)
    assert view["last_capability_test"]["status"] == "STALE"
    assert reg.invalidations[-1]["invalidator"] == "binary:claude"


def test_capability_test_blocked_without_binary(settings, monkeypatch):
    install_fakes(monkeypatch)
    monkeypatch.setattr(binmod, "probe", lambda settings, refresh=False: _probe(found=False))
    out = bridge.capability_test("opus", settings=settings)
    assert out["status"] == "BLOCKED" and out["steps"][-1]["status"] == "BLOCKED"


def test_capability_test_without_verification_module_says_not_persisted(settings, fake_claude, monkeypatch):
    install_fakes(monkeypatch, verification=False)
    out = bridge.capability_test("opus", settings=settings)
    assert out["status"] == "VERIFIED" and out["evidence_id"] is None


def test_api_status_and_capability_test_rbac(client: AuthedClient, db, fake_claude, monkeypatch):
    """§50 'VIEWER memanggil write endpoint langsung' -> rejected by the backend."""
    install_fakes(monkeypatch)
    assert client.get("/api/claude/status").status_code == 401
    create_admin(db)
    create_viewer(db)
    client.login("viewer", VIEWER_PW)
    r = client.get("/api/claude/status")
    assert r.status_code == 200
    body = r.json()
    assert body["binary"]["path_logical"] == "claude.exe (PATH)" and body["quota"]["state"] == "UNKNOWN"
    assert "--bare" in body["invocation"]["forbidden_flags"]
    assert client.post("/api/claude/capability-test", json={"model": "opus"}).status_code == 403
    from tests.conftest import ADMIN_PW

    client.login("owner", ADMIN_PW)
    r = client.post("/api/claude/capability-test", json={"model": "opus"})
    assert r.status_code == 200 and r.json()["status"] == "VERIFIED"
    assert client.post("/api/claude/capability-test", json={"model": "gpt"}).status_code == 422
    hist = client.get("/api/claude/capability-tests").json()
    assert hist["items"] and hist["items"][0]["status"] == "VERIFIED"
