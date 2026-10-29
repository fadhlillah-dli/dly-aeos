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
from tests.unit.test_claude_bridge_parsing import FABLE_ANSWER, OPUS_OK, SCHEMA
from tests.unit.test_routing_fakes import install_fakes

PROMPT = "Rencanakan fitur kecil. Kembalikan {\"ok\": true}."


def _probe(found: bool = True, auth: str = "VERIFIED", fingerprint: str = "sha256:" + "a" * 64,
           version: str = "2.1.302") -> Probe:
    info = BinaryInfo(found=found, path=Path("claude.exe") if found else None, source="PATH",
                      version=version if found else None, fingerprint=fingerprint if found else None,
                      reasons=[] if found else ["official Claude Code binary not found on PATH"])
    return Probe(binary=info, auth=AuthInfo(status=auth, logged_in=True, auth_method="claude.ai",
                                            api_provider="firstParty", subscription_type="max",
                                            email="owner@example.com"), checked_at=0.0)


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


def make_project(db, key: str, domain: str) -> str:
    from aeos.models.portfolio import Project

    p = Project(project_key=key, name=f"Project {key}", domain=domain)
    db.add(p)
    db.commit()
    return p.id


def test_run_structured_contract(settings, db, fake_claude, monkeypatch, tmp_path):
    """Prompt on stdin; required flags; no --bare; scrubbed env; fresh supervisor cwd; trace redacted."""
    reg = install_fakes(monkeypatch)
    _seed_routes(db)
    pid = make_project(db, "bridge-p1", "PERSONAL")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "q" * 30)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    ctx = tmp_path / "context.md"
    ctx.write_text("konteks proyek", encoding="utf-8")
    res = bridge.run_structured(ClaudeRequest(purpose="planning.primary", role="Primary Planner", model="opus",
                                              prompt=PROMPT, json_schema=SCHEMA, project_id=pid, domain="PERSONAL",
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


def test_opus_request_answered_by_fable_is_rejected_and_not_recorded_as_resolution(settings, db, fake_claude,
                                                                                   monkeypatch):
    """§12 'tolak penerimaan hasil bila model ... tidak sesuai policy', ADR-0131: an opus request whose answer came
    from a Fable model is a MODEL_POLICY_MISMATCH (fail closed); no structured answer is handed out."""
    install_fakes(monkeypatch)
    _seed_routes(db)
    fake_claude(FABLE_ANSWER)
    res = bridge.run_structured(ClaudeRequest(purpose="review.checkpoint", role="Reviewer", model="opus",
                                              prompt=PROMPT, json_schema=SCHEMA, domain="PERSONAL"), settings=settings)
    assert not res.ok and res.error_code == "MODEL_POLICY_MISMATCH" and res.model_resolved == "claude-fable-5-1"
    assert res.structured is None  # the off-policy answer never reaches the caller
    db.expire_all()
    assert db.query(ModelRoute).filter_by(route_key="claude-native/opus").one().resolved_version is None
    assert db.query(ModelRoute).filter_by(route_key="claude-native/fable").one().resolved_version is None


def test_quota_exhausted_checkpoint_wait_no_fallback(settings, db, fake_claude, monkeypatch):
    """§50 'Fable Max quota habis' (since ADR-0131 the review model is opus): ok=False QUOTA_EXHAUSTED, QuotaState
    EXHAUSTED, single call only."""
    install_fakes(monkeypatch)
    out = dict(OPUS_OK, is_error=True, subtype="error_during_execution", structured_output=None,
               result="Claude AI usage limit reached|1893456000")  # resets 2030-01-01
    fake_claude(out, exit_code=1)
    res = bridge.run_structured(ClaudeRequest(purpose="review.checkpoint", role="Reviewer", model="opus",
                                              prompt=PROMPT, json_schema=SCHEMA, domain="PERSONAL"), settings=settings)
    assert not res.ok and res.quota_exhausted and res.error_code == "QUOTA_EXHAUSTED"
    assert fake_claude.state["calls"] == 1  # no retry with another model
    q = db.query(QuotaState).filter_by(provider="claude-native", scope="opus").one()
    assert q.state == "EXHAUSTED" and q.resets_at is not None
    assert bridge.quota_view(db)["state"] == "EXHAUSTED"
    q.resets_at = q.resets_at.replace(year=2020)  # reset passed -> back to UNKNOWN (never "OK")
    db.commit()
    assert bridge.quota_view(db)["state"] == "UNKNOWN"


def test_exhaustion_without_reset_time_clears_after_a_successful_call(settings, db, fake_claude, monkeypatch):
    """A limit observed without a reset time must not block the queue forever: the next successful
    call of that family turns EXHAUSTED back into UNKNOWN (never 'OK': remaining quota is unpublished)."""
    reg = install_fakes(monkeypatch)
    fake_claude(dict(OPUS_OK, is_error=True, subtype="error_during_execution", structured_output=None,
                     result="You've hit your usage limit"), exit_code=1)
    req = ClaudeRequest(purpose="planning.primary", role="Planner", model="opus", prompt=PROMPT, json_schema=SCHEMA,
                        domain="PERSONAL")
    assert bridge.run_structured(req, settings=settings).error_code == "QUOTA_EXHAUSTED"
    db.expire_all()
    q = db.query(QuotaState).filter_by(provider="claude-native", scope="opus").one()
    assert q.state == "EXHAUSTED" and q.resets_at is None
    assert bridge.quota_view(db)["state"] == "EXHAUSTED"
    fake_claude(OPUS_OK)
    assert bridge.run_structured(req, settings=settings).ok
    db.expire_all()
    q = db.query(QuotaState).filter_by(provider="claude-native", scope="opus").one()
    assert q.state == "UNKNOWN" and q.source == "observed-successful-call"
    assert bridge.quota_view(db)["state"] == "UNKNOWN"
    from aeos.models.audit import Event

    kinds = {e.kind for e in db.query(Event).filter(Event.source == "claude_bridge")}
    assert {"claude.quota_exhausted", "claude.quota_available"} <= kinds  # dashboard sees both transitions
    assert len(reg.usage) == 2 and all(u["cost_usd"] is None for u in reg.usage)


def test_blocked_bridge_never_runs(settings, monkeypatch):
    called = []
    monkeypatch.setattr(bridge, "run_process", lambda *a, **k: called.append(1))
    monkeypatch.setattr(binmod, "probe", lambda settings, refresh=False: _probe(found=False))
    res = bridge.run_structured(ClaudeRequest(purpose="x", role="r", model="opus", prompt=PROMPT, json_schema=SCHEMA,
                                              domain="PERSONAL"), settings=settings)
    assert not res.ok and res.error_code == "BRIDGE_BLOCKED" and "manual package" in res.error and not called
    monkeypatch.setattr(binmod, "probe", lambda settings, refresh=False: _probe(auth="BLOCKED"))
    res = bridge.run_structured(ClaudeRequest(purpose="x", role="r", model="opus", prompt=PROMPT, json_schema=SCHEMA,
                                              domain="PERSONAL"), settings=settings)
    assert res.error_code == "BRIDGE_BLOCKED" and not called


@pytest.mark.parametrize("kwargs, code", [
    ({"model": "gpt-6"}, "REQUEST_INVALID"),
    ({"model": "haiku"}, "REQUEST_INVALID"),
    # ADR-0131: the Claude Fable family is never requested, as an alias or a pinned id
    ({"model": "fable"}, "MODEL_FAMILY_REFUSED"),
    ({"model": "claude-fable-5-1"}, "MODEL_FAMILY_REFUSED"),
    ({"allowed_tools": ["Read", "Bash"]}, "TOOL_NOT_ALLOWED"),
    ({"allowed_tools": ["Edit"]}, "TOOL_NOT_ALLOWED"),
    ({"project_id": "p1", "domain": None}, "DOMAIN_REQUIRED"),
    # §50 'Submit prompt tanpa domain -> ditolak backend; tidak ada LLM/provider call'
    ({"domain": None}, "DOMAIN_REQUIRED"),
    ({"domain": None, "purpose": "planning.primary"}, "DOMAIN_REQUIRED"),
    ({"domain": "personal"}, "REQUEST_INVALID"),
    ({"effort": "ultra"}, "REQUEST_INVALID"),
    ({"json_schema": {"type": "nope"}}, "SCHEMA_INVALID"),
    ({"prompt": "use key ghp_" + "c" * 36}, "SECRET_IN_PROMPT"),
    ({"project_id": "never-registered"}, "PROJECT_NOT_FOUND"),
])
def test_invalid_requests_never_reach_the_binary(settings, monkeypatch, kwargs, code):
    called = []
    monkeypatch.setattr(bridge, "run_process", lambda *a, **k: called.append(1))
    monkeypatch.setattr(binmod, "probe", lambda settings, refresh=False: _probe())
    base = {"purpose": "x", "role": "r", "model": "opus", "prompt": PROMPT, "json_schema": SCHEMA, "domain": "PERSONAL"}
    res = bridge.run_structured(ClaudeRequest(**{**base, **kwargs}), settings=settings)
    assert not res.ok and res.error_code == code and not called


def test_project_domain_must_match_the_projects_immutable_domain(settings, db, monkeypatch, fake_claude):
    """U-05 / §50 'Prompt memerintahkan OFFICE menjadi PERSONAL': an OFFICE project sent as PERSONAL
    (which would skip the OFFICE policy) is refused before any model call."""
    install_fakes(monkeypatch)
    office = make_project(db, "bridge-office", "OFFICE")
    req = ClaudeRequest(purpose="planning.primary", role="Planner", model="opus", prompt=PROMPT, json_schema=SCHEMA,
                        project_id=office, domain="PERSONAL")
    res = bridge.run_structured(req, settings=settings)
    assert not res.ok and res.error_code == "DOMAIN_MISMATCH" and "calls" not in fake_claude.state


def test_context_from_the_other_domain_partition_is_refused(settings, db, monkeypatch, fake_claude):
    """§12 'tidak memberikan runtime supervisor akses bebas ke kedua domain': a PERSONAL request may not
    carry a file from the OFFICE data partition (nor a platform request from either partition)."""
    install_fakes(monkeypatch)
    from aeos.core.enums import Domain

    office_file = settings.layout.domain_root(Domain.OFFICE) / "projects" / "acme" / "evidence" / "spec.md"
    office_file.parent.mkdir(parents=True, exist_ok=True)
    office_file.write_text("rahasia kantor", encoding="utf-8")
    for domain, purpose in (("PERSONAL", "planning.primary"), (None, "doctor.capability_test")):
        res = bridge.run_structured(ClaudeRequest(purpose=purpose, role="Planner", model="opus", prompt=PROMPT,
                                                  json_schema=SCHEMA, domain=domain, context_files=[office_file]),
                                    settings=settings)
        assert not res.ok and res.error_code == "CONTEXT_DOMAIN_DENIED", (domain, res.error)
    assert "calls" not in fake_claude.state
    # no half-prepared context copy is left in any supervisor workspace
    assert not list(settings.layout.supervisor_workspace.glob("*/context"))


def test_office_denied_by_default_and_when_policy_missing(settings, db, monkeypatch, fake_claude):
    """§23/§51: Office policy applies from the first planning call; missing policy fails closed."""
    office = make_project(db, "bridge-p-office", "OFFICE")
    req = ClaudeRequest(purpose="planning.primary", role="Planner", model="opus", prompt=PROMPT, json_schema=SCHEMA,
                        project_id=office, domain="OFFICE")
    res = bridge.run_structured(req, settings=settings)  # policy module not installed
    assert res.error_code == "OFFICE_POLICY_DENIED"
    reg = install_fakes(monkeypatch)
    assert bridge.run_structured(req, settings=settings).error_code == "OFFICE_POLICY_DENIED"
    assert "calls" not in fake_claude.state
    reg.office.update(claude_max_allowed_for_office_data=True, office_authorization=True)
    # W6-157: Claude Max approved, but the project's data class (INTERNAL) is not among the approved classes
    denied = bridge.run_structured(req, settings=settings)
    assert denied.error_code == "OFFICE_POLICY_DENIED" and "approved_data_classes" in denied.error
    reg.office["approved_data_classes"] = ["PUBLIC"]
    assert bridge.run_structured(req, settings=settings).error_code == "OFFICE_POLICY_DENIED"
    assert "calls" not in fake_claude.state
    reg.office["approved_data_classes"] = ["INTERNAL"]
    assert bridge.run_structured(req, settings=settings).ok
    # OFFICE data without a project carries no classification: UNKNOWN is never company-approved (fail closed)
    calls = fake_claude.state["calls"]
    for classes in (["INTERNAL"], ["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"]):
        reg.office["approved_data_classes"] = classes
        bare = bridge.run_structured(ClaudeRequest(purpose="doctor.explain", role="readiness-explainer", model="opus",
                                                   prompt=PROMPT, json_schema=SCHEMA, domain="OFFICE"),
                                     settings=settings)
        assert bare.error_code == "OFFICE_POLICY_DENIED" and "UNKNOWN" in bare.error, bare.error
    assert fake_claude.state["calls"] == calls


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
                                              domain="PERSONAL", timeout_seconds=2), settings=settings)
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


def test_capability_test_without_verification_module_says_not_persisted(settings, fake_claude, monkeypatch, absent_modules):
    install_fakes(monkeypatch, verification=False)
    absent_modules("aeos.verification.collector")
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
    assert "owner@example.com" not in r.text  # the Max account e-mail is shown to SUPER_ADMIN only
    assert "owner@example.com" not in client.get("/api/secrets/integrations").text
    assert client.post("/api/claude/capability-test", json={"model": "opus"}).status_code == 403
    from tests.conftest import ADMIN_PW

    client.login("owner", ADMIN_PW)
    assert client.get("/api/claude/status").json()["auth"]["email"] == "owner@example.com"
    r = client.post("/api/claude/capability-test", json={"model": "opus"})
    assert r.status_code == 200 and r.json()["status"] == "VERIFIED"
    assert client.post("/api/claude/capability-test", json={"model": "gpt"}).status_code == 422
    hist = client.get("/api/claude/capability-tests").json()
    assert hist["items"] and hist["items"][0]["status"] == "VERIFIED"


def test_bridge_cli_status_and_test_are_ascii_and_fail_closed(settings, db, monkeypatch, fake_claude):
    """`aeos bridge status|test`: an API-key login is BLOCKED (never an API fallback), the output is ASCII
    (legacy Windows consoles) and a blocked test exits non-zero without calling the model."""
    from typer.testing import CliRunner

    from aeos.claude_bridge.binary import parse_auth_status
    from aeos.cli import app

    install_fakes(monkeypatch)
    _seed_routes(db)
    api_key_auth = parse_auth_status(0, json.dumps({"loggedIn": True, "authMethod": "apiKey",
                                                    "apiProvider": "firstParty", "subscriptionType": None}))
    blocked = Probe(binary=_probe().binary, auth=api_key_auth, checked_at=0.0)
    monkeypatch.setattr(binmod, "probe", lambda settings, refresh=False: blocked)
    runner = CliRunner()
    r = runner.invoke(app, ["bridge", "status"])
    assert r.exit_code == 0, r.output
    assert "auth                   BLOCKED method=apiKey" in r.output and "bridge                 BLOCKED" in r.output
    assert "quota                  UNKNOWN" in r.output and "never - run: aeos bridge test" in r.output
    assert r.output.isascii()
    r = runner.invoke(app, ["bridge", "test", "--model", "opus"])
    assert r.exit_code == 1 and "overall                BLOCKED" in r.output and r.output.isascii()
    assert "calls" not in fake_claude.state  # nothing was sent to the model
    assert runner.invoke(app, ["bridge", "test", "--model", "sonnet"]).exit_code != 0  # supervisor aliases only
    js = runner.invoke(app, ["bridge", "status", "--json"])
    assert js.exit_code == 0 and json.loads(js.output)["auth"]["status"] == "BLOCKED"


def test_cli_schema_drops_the_meta_schema_declaration_the_official_cli_rejects():
    """E3 finding (Claude Code 2.1.302): --json-schema rejects a "$schema" draft-2020-12 declaration
    ("no schema with key or ref"). The argument omits it; our own validation keeps the full schema."""
    import json as _json
    import pathlib

    from aeos.claude_bridge.parsing import cli_schema

    for f in sorted(pathlib.Path("contracts/planning").glob("*.schema.json")):
        full = _json.loads(f.read_text(encoding="utf-8"))
        sent = cli_schema(full)
        assert "$schema" not in sent and "$id" not in sent, f.name
        assert {k: v for k, v in full.items() if not k.startswith("$")} == {k: v for k, v in sent.items() if not k.startswith("$")}
