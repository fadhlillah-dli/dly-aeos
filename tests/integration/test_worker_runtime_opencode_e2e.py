"""Real OpenCode -> AEOS broker (/broker/v1, capability token) -> local fake upstream, end to end.

Needs the OpenCode binary (``AEOS_TEST_OPENCODE_BINARY`` / ``AEOS_OPENCODE_BINARY`` / PATH); skipped
otherwise. No network, no real model, no credentials: the upstream is a loopback fake that scripts a
``write`` tool call. Proves on this host: the worker only ever talks to the broker with its run-bound
token, the broker forwards to the route's upstream without leaking that token, usage is recorded,
tool events become observed actions, the runtime commits the candidate and the run lands in REVIEW.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import func, select

from aeos.core.db import session_factory
from aeos.models.broker_ext import BrokerRequest
from aeos.models.cost import UsageLedgerEntry
from aeos.models.workforce import TaskExecution, WorkerRun
from aeos.worker_runtime import executor
from aeos.worker_runtime.opencode import resolve_binary
from aeos.workforce import scheduler
from tests.conftest import make_app
from tests.unit.test_workforce_support import (
    FakeModules,
    attach_repo,
    init_git_repo,
    make_plan,
    make_project,
    make_route,
    process_sandbox_exception,
    seed_and_enable_implementer,
)

_BIN = resolve_binary(os.environ.get("AEOS_TEST_OPENCODE_BINARY") or os.environ.get("AEOS_OPENCODE_BINARY"))
pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(_BIN is None, reason="OpenCode binary not available (set AEOS_TEST_OPENCODE_BINARY)")]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Upstream(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        _Upstream.seen.append({"path": self.path, "auth": self.headers.get("authorization"), "body": body})
        tools = [t.get("function", {}).get("name") for t in body.get("tools") or []]
        has_result = any(m.get("role") == "tool" for m in body.get("messages", []))
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()

        def send(delta, finish=None):
            obj = {"id": "c", "object": "chat.completion.chunk", "model": body.get("model"),
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")

        if "write" in tools and not has_result:
            send({"role": "assistant", "content": "Writing the file."})
            args = json.dumps({"filePath": "src/hello.py", "content": "print('hello from opencode')\n"})
            send({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                  "function": {"name": "write", "arguments": args}}]})
            send({}, "tool_calls")
        else:
            send({"role": "assistant", "content": "Done. All tests pass."})
            send({}, "stop")
        usage = {"id": "c", "object": "chat.completion.chunk", "choices": [],
                 "usage": {"prompt_tokens": 120, "completion_tokens": 12}}
        self.wfile.write(b"data: " + json.dumps(usage).encode() + b"\n\ndata: [DONE]\n\n")
        self.wfile.flush()


@pytest.fixture
def servers(settings):
    import uvicorn

    up_port, api_port = _free_port(), _free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", up_port), _Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    s2 = settings.model_copy(update={"port": api_port})
    server = uvicorn.Server(uvicorn.Config(make_app(s2), host="127.0.0.1", port=api_port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    yield {"settings": s2, "up_port": up_port, "api_port": api_port}
    server.should_exit = True
    th.join(timeout=10)
    upstream.shutdown()


def test_real_opencode_through_broker_lands_in_review(servers, monkeypatch, tmp_path):
    fakes = FakeModules().install(monkeypatch)
    _Upstream.seen.clear()
    repo = tmp_path / "repo"
    init_git_repo(repo, {"src/__init__.py": "", "README.md": "x\n"})
    with session_factory()() as db:
        p = make_project(db)
        attach_repo(db, p, repo)
        plan, pv, specs = make_plan(db, p, [{"task_key": "hello", "allowed_paths": ["src/**"],
                                             "limits": {"timeout_seconds": 180, "max_output_tokens": 2048}}])
        fakes.approved[plan.id] = pv.id
        make_route(db, key="worker.local-fake", provider="openai-compatible", model_id="fake-upstream-model",
                   endpoint=f"http://127.0.0.1:{servers['up_port']}/v1")
        fakes.route_key = "worker.local-fake"
        seed_and_enable_implementer(db)
        process_sandbox_exception(db)
        scheduler.ensure_executions(db, pv.id, actor="t")
        scheduler.refresh_readiness(db, pv.id)
        run = scheduler.acquire_lease(db, specs[0].id, actor="t")
        db.commit()
        run_id, task_id = run.id, specs[0].id
    res = executor.execute_run(servers["settings"], run_id, binary=str(_BIN),
                               broker_base_url=f"http://127.0.0.1:{servers['api_port']}/broker/v1",
                               heartbeat_seconds=1.0)
    assert res["status"] == "REVIEW", res
    with session_factory()() as s:
        run = s.get(WorkerRun, run_id)
        te = s.scalar(select(TaskExecution).where(TaskExecution.task_id == task_id))
        assert te.state == "REVIEW" and run.execution_status == "REVIEW"
        assert "src/hello.py" in run.touched_paths
        assert [p["path"] for p in run.diff_summary["paths"]] == ["src/hello.py"]
        assert run.engine_version and run.engine == "opencode"
        ok = s.scalar(select(func.count()).select_from(BrokerRequest).where(BrokerRequest.run_id == run_id,
                                                                            BrokerRequest.outcome == "OK"))
        assert ok >= 2
        assert s.scalar(select(func.count()).select_from(UsageLedgerEntry).where(UsageLedgerEntry.run_id == run_id)) >= 2
    assert _Upstream.seen and all(x["body"]["model"] == "fake-upstream-model" for x in _Upstream.seen)
    assert all(not (x["auth"] or "").count("aeoscap_") for x in _Upstream.seen)  # worker token never forwarded


def test_real_opencode_contract_test_and_repository_config_cannot_widen_tools(settings, db):
    """§08 worker contract + §25 'konfigurasi OpenCode tingkat repository tidak dapat mengubah permission/tool/MCP':
    the pinned binary, a hostile repository opencode.json (bash/webfetch/MCP), and the tool list the engine
    really offers the model. Recorded as WORKER_CONTRACT_TEST evidence for the Readiness Doctor."""
    from aeos.worker_runtime.contract import record_contract_test, run_contract_test

    rep = run_contract_test(binary=str(_BIN))
    failing = {r["check"]: r["evidence"] for r in rep["results"] if r["status"] != "PASS"}
    assert rep["overall"] == "PASS", failing
    assert record_contract_test(db, rep, actor="cli:integration")
