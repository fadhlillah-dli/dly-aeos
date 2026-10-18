"""OpenCode adapter: per-run config, command form and JSON event parsing.

The event fixtures below are real lines captured from ``opencode-ai@1.19.3 run --format json`` against
a local fake OpenAI-compatible endpoint (no credentials) on 2026-10-18.
"""

from __future__ import annotations

import json
from pathlib import Path

from aeos.worker_runtime import opencode

WT = Path("C:/work/run/repo") if __import__("sys").platform == "win32" else Path("/work/run/repo")

REAL_EVENTS = [
    '{"type":"step_start","timestamp":1792321976867,"sessionID":"ses_1","part":{"id":"p1","messageID":"m1",'
    '"sessionID":"ses_1","type":"step-start"}}',
    '{"type":"text","timestamp":1792321976868,"sessionID":"ses_1","part":{"id":"p2","messageID":"m1","sessionID":'
    '"ses_1","type":"text","text":"Done. All tests pass (narration).","time":{"start":1,"end":2}}}',
    '{"type":"tool_use","timestamp":1792321976902,"sessionID":"ses_1","part":{"type":"tool","tool":"write",'
    '"callID":"call_1","state":{"status":"completed","input":{"filePath":"hello.txt","content":"hi\\n"},'
    '"output":"Wrote file successfully.","metadata":{"diagnostics":{},"filepath":"' + str(WT / "hello.txt").replace("\\", "\\\\")
    + '","exists":false,"truncated":false},"title":"hello.txt","time":{"start":1,"end":2}},"id":"p3",'
    '"sessionID":"ses_1","messageID":"m1"}}',
    '{"type":"step_finish","timestamp":1792321976903,"sessionID":"ses_1","part":{"id":"p4","reason":"tool-calls",'
    '"messageID":"m1","sessionID":"ses_1","type":"step-finish","tokens":{"total":112,"input":102,"output":10,'
    '"reasoning":0,"cache":{"write":0,"read":0}},"cost":0}}',
    '{"type":"tool_use","timestamp":1792322599016,"sessionID":"ses_2","part":{"type":"tool","tool":"read",'
    '"callID":"call_r","state":{"status":"error","input":{"filePath":"C:\\\\Users\\\\someone\\\\outside-secret.txt"},'
    '"error":"The user has specified a rule which prevents you from using this specific tool call.",'
    '"time":{"start":1,"end":2}},"id":"p5","sessionID":"ses_2","messageID":"m2"}}',
    '{"type":"error","timestamp":1792322374683,"sessionID":"ses_3","error":{"name":"APIError","data":{"message":'
    '"capability token rejected","statusCode":403,"isRetryable":false,"responseHeaders":{},"responseBody":"{}",'
    '"metadata":{"url":"http://127.0.0.1:18799/broker/v1/chat/completions"}}}}',
]


def test_parser_turns_events_into_observed_actions():
    p = opencode.EventParser(worktree=WT)
    acts = [p.parse_line(line) for line in REAL_EVENTS]
    kinds = [a.kind if a else None for a in acts]
    assert kinds == [None, "narration", "edit_file", "step", "tool_error", "error"]
    assert acts[1].is_narration
    assert acts[1].as_payload()["source"] == "AI narration (summary, not evidence)"
    assert acts[2].as_payload()["source"].startswith("opencode --format json (observed")
    assert acts[2].path == "hello.txt" and acts[2].status == "completed"
    assert p.touched == ["hello.txt"]
    assert p.tokens_input == 102 and p.tokens_output == 10
    assert p.outside_attempts and "outside-secret" in p.outside_attempts[0]
    assert p.errors[0]["status_code"] == 403 and p.errors[0]["retryable"] is False
    assert p.last_narration.startswith("Done.")  # recorded, but never progress/evidence


def test_parser_tolerates_garbage():
    p = opencode.EventParser(worktree=WT)
    assert p.parse_line("not json") is None and p.parse_line("[1,2]") is None and p.unparsable == 2


def test_config_uses_broker_capability_and_denies_by_default():
    cfg = opencode.build_config(broker_base_url="http://127.0.0.1:8787/broker/v1/", token="aeoscap_x", model="m-1",
                                tools_allowed=[], egress_allowed=[], max_output_tokens=4096)
    prov = cfg["provider"]["aeos-broker"]
    assert prov["npm"] == "@ai-sdk/openai-compatible"
    assert prov["options"] == {"baseURL": "http://127.0.0.1:8787/broker/v1", "apiKey": "aeoscap_x"}
    # 1.19.3 rejects a partial limit ("Missing key ...limit.context"): both keys always present.
    assert prov["models"]["m-1"]["limit"] == {"context": opencode.DEFAULT_CONTEXT_LIMIT, "output": 4096}
    assert cfg["model"] == "aeos-broker/m-1" and cfg["enabled_providers"] == ["aeos-broker"]
    assert cfg["mcp"] == {} and cfg["plugin"] == [] and cfg["formatter"] is False and cfg["lsp"] is False
    perm = cfg["permission"]
    assert next(iter(perm)) == "*" and perm["*"] == "deny"  # catch-all first: last match wins in OpenCode
    assert perm["edit"] == "allow" and perm["bash"] == "deny" and perm["webfetch"] == "deny"
    for k in ("task", "skill", "external_directory", "websearch", "doom_loop"):
        assert perm[k] == "deny"
    assert cfg["share"] == "disabled" and cfg["autoupdate"] is False


def test_config_tool_grants_follow_taskspec():
    cfg = opencode.build_config(broker_base_url="http://x/broker/v1", token="t", model="m", egress_allowed=[],
                                tools_allowed=["bash:pytest *", "bash:ruff check *", "webfetch"])
    assert cfg["permission"]["bash"] == {"*": "deny", "pytest *": "allow", "ruff check *": "allow"}
    assert cfg["permission"]["webfetch"] == "deny"  # no egress allowance -> still denied
    cfg = opencode.build_config(broker_base_url="http://x/broker/v1", token="t", model="m",
                                tools_allowed=["webfetch"], egress_allowed=["https://docs.example/*"])
    assert cfg["permission"]["webfetch"] == "allow"


def test_command_form_verified_against_cli(tmp_path):
    argv = opencode.build_command(tmp_path / "opencode.exe", model="m-1", worktree=tmp_path / "repo",
                                  task_file=tmp_path / "task.md")
    assert argv[1] == "run" and argv[2] == opencode.RUN_MESSAGE
    assert argv.index("--file") > 2  # message must precede the array option --file
    assert argv[argv.index("--format") + 1] == "json" and "--pure" in argv
    assert "--auto" not in argv  # never auto-approve permissions
    import pytest

    with pytest.raises(ValueError):
        opencode.build_command("x", model="m & calc", worktree=tmp_path, task_file=tmp_path)


def test_exposed_model_id_is_safe():
    assert opencode.exposed_model_id("worker/deepseek v4:free") == "worker-deepseek-v4-free"


def test_resolve_binary_from_npm_shim(tmp_path, monkeypatch):
    bin_dir = tmp_path / "node_modules" / ".bin"
    native = tmp_path / "node_modules" / "opencode-ai" / "bin" / ("opencode.exe" if __import__("sys").platform ==
                                                                  "win32" else "opencode")
    bin_dir.mkdir(parents=True)
    native.parent.mkdir(parents=True)
    native.write_bytes(b"")
    shim = bin_dir / "opencode.cmd"
    shim.write_text("@echo off", encoding="utf-8")
    assert opencode.resolve_binary(str(shim)) == native.resolve()
    monkeypatch.delenv("AEOS_OPENCODE_BINARY", raising=False)
    monkeypatch.setattr(opencode.shutil, "which", lambda n: None)
    assert opencode.resolve_binary(None) is None


def test_status_missing_is_not_verified(monkeypatch):
    monkeypatch.setattr(opencode, "resolve_binary", lambda configured=None: None)
    st = opencode.opencode_status("nowhere", refresh=True)
    assert st["status"] == "MISSING" and st["functional"] == "NOT_PROVEN"


def test_config_file_written_outside_worktree(tmp_path):
    cfg = opencode.build_config(broker_base_url="http://x/broker/v1", token="aeoscap_secret", model="m",
                                tools_allowed=[], egress_allowed=[])
    (tmp_path / "config").mkdir()
    path = opencode.write_config(tmp_path / "config", cfg)
    assert path.parent == tmp_path / "config"
    assert json.loads(path.read_text(encoding="utf-8"))["provider"]["aeos-broker"]["options"]["apiKey"] == "aeoscap_secret"
