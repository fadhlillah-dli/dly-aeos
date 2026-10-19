"""Worker engine contract test (§08 group Worker, §07 step 5): the harness, its verdicts, the evidence it records
and the Readiness Doctor check that reads it. The OpenCode binary is absent on the build host, so a scripted
stand-in plays the engine here: one that behaves like the documented OpenCode (config precedence, permissions,
external_directory) and one that honours a repository opencode.json (must FAIL). The real binary runs the same
harness via `aeos worker contract-test` (tests/integration/test_worker_runtime_opencode_e2e.py)."""

from __future__ import annotations

import sys

import pytest

from aeos.worker_runtime import contract, opencode

FAKE_OPENCODE = r'''
import json, os, sys, urllib.request
args = sys.argv[1:]
naive = args[0] == "naive"
args = args[1:]
d = args[args.index("--dir") + 1]
cfg = json.load(open(os.environ["OPENCODE_CONFIG"], encoding="utf-8"))
def merge(a, b):
    for k, v in b.items():
        a[k] = merge(a.get(k, {}), v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
    return a
proj = os.path.join(d, "opencode.json")
inline = json.loads(os.environ.get("OPENCODE_CONFIG_CONTENT") or "{}")
if naive and os.path.exists(proj):  # documented precedence: project over OPENCODE_CONFIG, inline last
    merge(cfg, json.load(open(proj, encoding="utf-8")))
    merge(cfg, inline)
    merge(cfg, json.load(open(proj, encoding="utf-8")))  # naive engine lets the repository win
else:
    if os.environ.get("OPENCODE_DISABLE_PROJECT_CONFIG") != "1" and os.path.exists(proj):
        merge(cfg, json.load(open(proj, encoding="utf-8")))
    merge(cfg, inline)
perm = cfg.get("permission", {})
default = perm.get("*", "allow")
names = ["read", "glob", "grep", "list", "edit", "write", "bash", "webfetch", "task", "skill", "websearch", "todowrite"]
tools = [n for n in names if perm.get(n, default) != "deny"]
tools += [f"{k}_tool" for k in (cfg.get("mcp") or {})]
prov = cfg["provider"]["aeos-broker"]
base, key = prov["options"]["baseURL"], prov["options"]["apiKey"]
model = cfg["model"].split("/", 1)[1]
limit = prov["models"][model].get("limit", {}).get("output", 4096)
def ev(o):
    print(json.dumps(o), flush=True)
messages = [{"role": "user", "content": "task"}]
for step in range(6):
    body = {"model": model, "stream": True, "max_tokens": limit, "messages": messages,
            "tools": [{"type": "function", "function": {"name": t}} for t in tools]}
    req = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + key, "content-type": "application/json"})
    calls, text = [], ""
    with urllib.request.urlopen(req, timeout=120) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            obj = json.loads(line[6:])
            for ch in obj.get("choices") or []:
                delta = ch.get("delta") or {}
                text += delta.get("content") or ""
                calls += delta.get("tool_calls") or []
    ev({"type": "step_start", "part": {}})
    if not calls:
        ev({"type": "text", "part": {"text": text}})
        ev({"type": "step_finish", "part": {"reason": "stop", "tokens": {"input": 1, "output": 1}}})
        sys.exit(0)
    for c in calls:
        fn = c["function"]["name"]
        a = json.loads(c["function"]["arguments"])
        p = a.get("filePath", "")
        full = os.path.abspath(p if os.path.isabs(p) else os.path.join(d, p))
        inside = os.path.commonpath([full, os.path.abspath(d)]) == os.path.abspath(d)
        if not inside and perm.get("external_directory", default) == "deny":
            result, status = "Error: access outside the project directory is denied", "error"
        elif fn == "read":
            result, status = open(full, encoding="utf-8").read(), "completed"
        else:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            open(full, "w", encoding="utf-8").write(a.get("content") or a.get("newString") or "")
            result, status = "ok", "completed"
        ev({"type": "tool_use", "part": {"tool": fn, "state": {"status": status, "input": {"filePath": p}}}})
        messages += [{"role": "assistant", "tool_calls": [c]}, {"role": "tool", "tool_call_id": c["id"], "content": result}]
sys.exit(0)
'''


@pytest.fixture
def engine(monkeypatch, tmp_path):
    script = tmp_path / "fake_opencode.py"
    script.write_text(FAKE_OPENCODE, encoding="utf-8")
    mode = {"kind": "good"}
    monkeypatch.setattr(opencode, "opencode_status", lambda configured=None, refresh=False: {
        "status": "INSTALLED", "binary": "opencode-fake", "version": opencode.PINNED_VERSION, "drift": False,
        "evidence": "scripted stand-in"})
    monkeypatch.setattr(opencode, "build_command", lambda binary, *, model, worktree, task_file: [
        sys.executable, str(script), "naive" if mode["kind"] == "naive" else "good", "run", "msg", "--format", "json",
        "--model", f"aeos-broker/{model}", "--dir", str(worktree), "--file", str(task_file)])
    return mode


def _by(report):
    return {r["check"]: r for r in report["results"]}


def test_contract_test_passes_for_a_conforming_engine_and_is_recorded(settings, db, engine):
    from aeos.doctor.checks.worker import worker_contract
    from aeos.doctor.model import CheckContext

    rep = contract.run_contract_test()
    by = _by(rep)
    assert rep["overall"] == "PASS", {k: v for k, v in by.items() if v["status"] != "PASS"}
    assert set(contract.CHECKS) == set(by)
    assert "NOT_PROVEN" in rep["scope"]["model_behaviour"]  # a scripted model never proves a real route
    ev_id = contract.record_contract_test(db, rep, actor="cli:test")
    db.commit()
    from aeos.models.evidence import EvidenceArtifact

    ev = db.get(EvidenceArtifact, ev_id)
    assert ev.kind == "WORKER_CONTRACT_TEST" and ev.trusted and ev.mode == "LIVE" and ev.evidence_level == "E2"
    from types import SimpleNamespace

    out = worker_contract(SimpleNamespace(session=db))  # the registered check function itself
    assert out.status == "VERIFIED" and out.evidence["overall"] == "PASS"
    assert any("NOT-PROVEN" in r for r in out.evidence["residual_risks"])
    assert CheckContext  # the real context type the doctor passes (only .session is read)


def test_engine_that_lets_repository_config_widen_tools_fails(settings, db, engine):
    engine["kind"] = "naive"
    rep = contract.run_contract_test()
    by = _by(rep)
    assert rep["overall"] == "FAIL"
    assert by["project_config_ignored"]["status"] == "FAIL" and "bash" in by["project_config_ignored"]["evidence"]
    assert by["tool_schema"]["status"] == "FAIL"


def test_missing_engine_is_not_run_and_records_nothing(settings, db, monkeypatch):
    monkeypatch.setattr(opencode, "opencode_status", lambda configured=None, refresh=False: {
        "status": "MISSING", "binary": None, "version": None, "evidence": "opencode not found"})
    rep = contract.run_contract_test()
    assert rep["overall"] == "NOT_RUN" and {r["status"] for r in rep["results"]} == {"NOT_RUN"}
    assert contract.record_contract_test(db, rep, actor="cli:test") is None
