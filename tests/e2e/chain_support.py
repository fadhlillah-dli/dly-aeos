"""Edge fakes and the owner-driven setup for the execution-chain E2E (seam group W2).

Everything *inside* AEOS is the real module code. Only the edges are faked:

* the OpenCode engine: a scripted ``opencode`` executable placed on PATH (a real child process that
  emits the ``--format json`` events the adapter parses and calls the broker with its capability token);
* the model upstream and the GitHub REST API: respx (process-wide httpx mock, no network);
* the GitHub git transport: the broker builds the real ``https://github.com/...`` remote, passes the
  real push interlock and fetches the token through the secrets contract; only the transfer itself is
  redirected into a local bare repository (``git_broker.push_from_mirror`` wrapper);
* the official ``claude`` binary: ``aeos.claude_bridge.facade.run_structured`` returns scripted NATIVE
  results (planning stages + checkpoint review) — never the Claude API;
* the Readiness Doctor's *host probes* (claude login, docker, GitHub token): the three capabilities the
  chain needs report READY with a ``FAKED`` check, everything else is the real doctor;
* the secret store: an in-memory provider that enforces the real purpose/namespace contract.

No paid API, no real credential, no real GitHub/Google write. Values that stand in for credentials are
built at runtime and are visibly fake.
"""

from __future__ import annotations

import copy
import io
import json
import os
import platform
import socket
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aeos.claude_bridge import facade as bridge_facade
from aeos.core.db import session_factory
from aeos.core.enums import Capability, Domain
from aeos.core.timeutil import utcnow
from aeos.models.routing import ModelRoute
from aeos.models.workforce import TaskExecution, WorkerRun
from aeos.secrets.provider import MemorySecretProvider, check_purpose, set_provider_override
from tests.conftest import ADMIN_PW, AuthedClient, create_admin, make_app
from tests.unit.test_planning_support import (
    challenge_output,
    final_output,
    sample_content,
    synthesis_output,
    task,
)
from tests.unit.test_verification_support import review_payload

REPO = "e2e-owner/e2e-calc"
ROUTE_KEY = "opencode-zen/deepseek-v4.1-flash-free"
GITHUB_API = "https://api.github.com"
RESOLVED = {"opus": "claude-opus-5-5", "fable": "claude-fable-5-1"}
FAKE_ZEN_KEY = "zen-e2e-" + "fake" * 8  # visibly fake, never a real provider key
FAKE_GITHUB_TOKEN = "e2e-fake-github-token-" + "x" * 12

BASE_CALC = "def add(a: int, b: int) -> int:\n    return a - b\n"  # the bug the task asks to fix
TRUSTED_TEST = (
    "from e2ecalc import add\n\n\n"
    "def test_add_positive() -> None:\n    assert add(2, 3) == 5\n\n\n"
    "def test_add_negative() -> None:\n    assert add(-1, 1) == 0\n"
)

# ----------------------------------------------------------------------------- fake OpenCode
FAKE_OPENCODE = r'''
import sys

def ev(obj):
    print(json.dumps(obj), flush=True)

def write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    ev({"type": "tool_use", "timestamp": int(time.time() * 1000), "part": {"type": "tool", "tool": "write",
        "state": {"status": "completed", "input": {"filePath": path}, "metadata": {"filepath": os.path.abspath(path)}}}})

def chat(cfg, model, content):
    opts = cfg["provider"]["aeos-broker"]["options"]
    req = urllib.request.Request(opts["baseURL"] + "/chat/completions", method="POST",
                                 data=json.dumps({"model": model, "messages": [{"role": "user", "content": content}]}).encode(),
                                 headers={"Authorization": "Bearer " + opts["apiKey"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}

def step(body):
    usage = body.get("usage") or {}
    ev({"type": "step_finish", "part": {"reason": "stop", "cost": 0,
        "tokens": {"input": usage.get("prompt_tokens", 0), "output": usage.get("completion_tokens", 0)}}})

argv = sys.argv[1:]
if argv[:1] == ["--version"]:
    print("1.19.3")
    sys.exit(0)
import json, os, subprocess, time, urllib.error, urllib.request  # after the --version probe: it needs none of them
model = argv[argv.index("--model") + 1].split("/", 1)[1]
prompt = open(argv[argv.index("--file") + 1], encoding="utf-8").read()
cfg = json.load(open(os.environ["OPENCODE_CONFIG"], encoding="utf-8"))
scenario = prompt.split("SCENARIO:", 1)[1].split()[0] if "SCENARIO:" in prompt else "ok"
FIX = "def add(a: int, b: int) -> int:\n    return a + b\n"
ev({"type": "step_start", "part": {"type": "step-start"}})
if scenario in ("ok", "protected"):
    for i in range(2):
        status, body = chat(cfg, model, "step %d" % i)
        if status != 200:
            ev({"type": "error", "error": {"name": "APIError", "data": {"message": "broker %d" % status, "statusCode": status}}})
            sys.exit(1)
        step(body)
    write("backend/e2ecalc/__init__.py", FIX)
    if scenario == "protected":
        write(".github/workflows/ci.yml", "on: push\njobs: {}\n")
    ev({"type": "text", "part": {"text": "All tests pass. Task complete!"}})
    sys.exit(0)
if scenario == "quota":
    while True:  # like OpenCode 1.19.3: retries a 429 forever, the executor must stop it
        status, body = chat(cfg, model, "retry")
        if status == 200:
            break
        ev({"type": "error", "error": {"name": "APIError", "data": {"message": "rate limited", "statusCode": status,
                                                                   "isRetryable": True}}})
        time.sleep(0.2)
    sys.exit(0)
if scenario == "hang":
    status, body = chat(cfg, model, "start")
    step(body)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    marker = os.path.join(os.environ.get("TEMP") or os.environ.get("TMPDIR") or ".", "aeos-fake-pids.json")
    with open(marker, "w", encoding="utf-8") as fh:
        json.dump({"engine": os.getpid(), "grandchild": child.pid}, fh)
    write("backend/e2ecalc/__init__.py", "# half-written by a cancelled run\n")
    time.sleep(300)
sys.exit(2)
'''


def make_fake_executable(bin_dir: Path, name: str, source: str) -> Path:
    """A real executable named *name* that runs *source* with this interpreter.

    POSIX: a shebang script. Windows: the same launcher stub pip uses for console scripts (distlib
    ``t64.exe`` + shebang + zipped ``__main__.py``), so ``shutil.which("opencode")`` finds a native .exe."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        script = bin_dir / f"{name}-impl.py"
        script.write_text(source, encoding="utf-8")
        target = bin_dir / name
        # a /bin/sh wrapper survives interpreter paths with spaces (a shebang line would not)
        target.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        target.chmod(0o755)
        return target
    try:
        import pip._vendor.distlib as distlib  # noqa: PLC0415 - vendored launcher stubs
    except ImportError:  # pragma: no cover
        pytest.skip("no console-script launcher stub available to build a fake Windows executable")
    stub = "t64-arm.exe" if platform.machine().lower() in ("arm64", "aarch64") else "t64.exe"
    launcher = (Path(distlib.__file__).parent / stub).read_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("__main__.py", source)
    target = bin_dir / f"{name}.exe"
    target.write_bytes(launcher + f'#!"{sys.executable}"\n'.encode() + buf.getvalue())
    return target


# ----------------------------------------------------------------------------- fake Claude bridge
@dataclass
class FakeClaude:
    """Scripted official-binary results (mode NATIVE): the edge is the ``claude`` process."""

    outputs: dict[str, Any]
    calls: list[str] = field(default_factory=list)
    review_quota_exhausted: bool = False
    review_verdict: str = "PASS-CONTROLLED"
    review_findings: list[dict[str, Any]] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeClaude:
        monkeypatch.setattr(bridge_facade, "run_structured", self.run_structured)
        return self

    def _result(self, req: Any, structured: dict[str, Any] | None, **kw: Any) -> Any:
        return bridge_facade.ClaudeResult(
            ok=kw.pop("ok", True), structured=structured, text="", model_requested=req.model,
            model_resolved=kw.pop("model_resolved", RESOLVED.get(req.model, "claude-fable-5-1")),
            engine_version="2.1.301 (e2e scripted claude)", session_id=f"e2e-{len(self.calls)}",
            usage={"input_tokens": 100, "output_tokens": 40}, duration_ms=5, mode="NATIVE", **kw)

    def run_structured(self, req: Any) -> Any:
        self.calls.append(req.purpose)
        if req.purpose in self.outputs:
            return self._result(req, copy.deepcopy(self.outputs[req.purpose]))
        if req.purpose == "review.checkpoint":
            if self.review_quota_exhausted:
                return self._result(req, None, ok=False, model_resolved=None, error_code="QUOTA_EXHAUSTED",
                                    error="weekly limit reached", quota_exhausted=True)
            return self._result(req, review_payload(self.review_verdict, copy.deepcopy(self.review_findings)),
                                model_resolved="claude-fable-5-1")
        raise AssertionError(f"unexpected bridge purpose {req.purpose}")


def planning_outputs(goal: str, allowed_paths: list[str]) -> dict[str, Any]:
    t = task("T1", paths=allowed_paths, goal=goal, title="Fix add()",
             limits={"max_attempts": 2, "timeout_seconds": 120, "max_output_tokens": 4000})
    v1 = sample_content("E2E calc", tasks=[t])
    v2 = sample_content("E2E calc", tasks=[t], note="v2")
    return {"planning.primary": v1, "planning.challenge": challenge_output(),
            "planning.synthesis": synthesis_output(v2), "planning.final": final_output()}


# ----------------------------------------------------------------------------- other edges
class StrictMemorySecrets(MemorySecretProvider):
    """In-memory store that enforces the real ``<namespace>:<use>`` purpose rule (aeos.secrets)."""

    def get(self, ref: str, *, purpose: str, actor: str) -> str:
        check_purpose(ref, purpose)
        return super().get(ref, purpose=purpose, actor=actor)


FAKED_CAPABILITIES = frozenset({Capability.CAN_RUN_WORKER, Capability.CAN_REVIEW_WITH_MAX, Capability.CAN_PREPARE_PR})


def fake_doctor_host_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    from aeos.doctor import capabilities as doctor_caps
    from aeos.doctor.facade import CapabilityState

    real = doctor_caps.capability_state

    def capability_state(session: Any, capability: Capability, *, domain: str | None = None,
                         project_id: str | None = None, **kwargs: Any) -> CapabilityState:
        # ``**kwargs``: any additive facade keyword (e.g. ``exclude_checks``) is forwarded to the real gate
        # unchanged, so only the host tool probes of FAKED_CAPABILITIES are replaced.
        if capability in FAKED_CAPABILITIES:
            return CapabilityState(capability=capability, status="READY", reasons=[],
                                   checks=[{"check_id": "e2e.host-probes", "status": "FAKED",
                                            "note": "host tool probes replaced by the E2E harness"}])
        return real(session, capability, domain=domain, project_id=project_id, **kwargs)

    monkeypatch.setattr(doctor_caps, "capability_state", capability_state)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class LiveServer:
    """The same FastAPI app on a real loopback port, so the worker child process can reach the broker."""

    def __init__(self, app: Any, port: int):
        import uvicorn

        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                                    lifespan="off", access_log=False))
        self.thread = threading.Thread(target=self.server.run, daemon=True, name="e2e-live-server")

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("live server did not start")
            time.sleep(0.05)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(15)


def git(cwd: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1")
    proc = subprocess.run(["git", "-c", "user.name=E2E Owner", "-c", "user.email=owner@example.invalid",
                           "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", "-c", "core.hooksPath=",
                           *args], cwd=str(cwd), capture_output=True, text=True, env=env, check=False, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def make_repositories(root: Path) -> tuple[Path, Path]:
    """The owner's project checkout (main branch with the trusted acceptance suite) + a bare remote."""
    repo = root / "e2e-calc"
    for rel, text in {"backend/e2ecalc/__init__.py": BASE_CALC, "tests/acceptance/test_calc.py": TRUSTED_TEST,
                      "pyproject.toml": "[tool.ruff]\nline-length = 110\n", "README.md": "# e2e calc\n"}.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    git(root, "init", "-q", "-b", "main", str(repo))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial: calc with a bug and its acceptance test")
    bare = root / "remote.git"
    git(root, "init", "-q", "--bare", str(bare))
    git(repo, "push", "-q", str(bare), "main:refs/heads/main")
    return repo, bare


def remote_head(bare: Path, branch: str) -> str:
    return git(bare, "rev-parse", f"refs/heads/{branch}")


def secret_leaks(root: Path) -> list[str]:
    """Files under *root* (data dir with the SQLite DB/WAL, evidence, sandboxes, worktrees, mirrors,
    the bare remote) that contain an upstream credential in any form (§26: never in logs, events,
    evidence, sandboxes or config files)."""
    import base64

    needles = [FAKE_ZEN_KEY.encode(), FAKE_GITHUB_TOKEN.encode(),
               base64.b64encode(f"x-access-token:{FAKE_GITHUB_TOKEN}".encode())]
    hits: list[str] = []
    for p in root.rglob("*"):
        try:
            data = p.read_bytes() if p.is_file() else b""
        except OSError:
            continue
        if any(n in data for n in needles):
            hits.append(str(p.relative_to(root)))
    return hits


# ----------------------------------------------------------------------------- the chain
@dataclass
class Chain:
    settings: Any
    client: AuthedClient
    bridge: FakeClaude
    respx_mock: Any
    repo: Path
    bare: Path
    project_id: str = ""
    plan_id: str = ""
    task_id: str = ""
    upstream_calls: list[dict[str, Any]] = field(default_factory=list)
    pr_posts: list[dict[str, Any]] = field(default_factory=list)
    git_pushes: list[dict[str, Any]] = field(default_factory=list)
    upstream_status: int = 200

    # ------------------------------------------------------------ helpers
    def ok(self, r: Any, *codes: int) -> dict[str, Any]:
        assert r.status_code in (codes or (200,)), f"{r.request.method} {r.request.url}: {r.status_code} {r.text}"
        return r.json()

    def te(self) -> TaskExecution:
        with session_factory()() as s:
            te = s.scalar(select(TaskExecution).where(TaskExecution.task_id == self.task_id))
            s.expunge_all()
            return te

    def run(self, run_id: str) -> WorkerRun:
        with session_factory()() as s:
            run = s.get(WorkerRun, run_id)
            s.expunge_all()
            return run

    def route(self) -> ModelRoute:
        with session_factory()() as s:
            r = s.scalar(select(ModelRoute).where(ModelRoute.route_key == ROUTE_KEY))
            s.expunge_all()
            return r

    # ------------------------------------------------------------ edges: model upstream + GitHub
    def mock_upstream(self) -> None:
        r = self.route()
        url = f"{r.endpoint.rstrip('/')}/chat/completions"

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.upstream_calls.append({"model": body.get("model"), "auth": request.headers.get("authorization"),
                                        "url": str(request.url)})
            if self.upstream_status == 429:
                return httpx.Response(429, headers={"Retry-After": "120"}, json={"error": {"message": "rate limited"}})
            return httpx.Response(200, json={
                "id": "cmpl-e2e", "object": "chat.completion", "model": body.get("model"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 7}})

        self.respx_mock.post(url).mock(side_effect=handler)

    def mock_github(self) -> None:
        def create_pr(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.pr_posts.append({**body, "auth": request.headers.get("authorization")})
            return httpx.Response(201, json=self._pull(body["head"]))

        def get_pr(request: httpx.Request) -> httpx.Response:
            head_ref = self.pr_posts[-1]["head"]
            return httpx.Response(200, json=self._pull(head_ref))

        self.respx_mock.post(f"{GITHUB_API}/repos/{REPO}/pulls").mock(side_effect=create_pr)
        self.respx_mock.get(f"{GITHUB_API}/repos/{REPO}/pulls/7").mock(side_effect=get_pr)
        self.respx_mock.get(host="api.github.com", path__regex=r"^/repos/.+/commits/[0-9a-f]+/check-runs$").mock(
            return_value=httpx.Response(200, json={"total_count": 0, "check_runs": []}))

    def _pull(self, head_ref: str) -> dict[str, Any]:
        return {"number": 7, "state": "open", "draft": True, "merged": False, "merged_by": None, "merged_at": None,
                "merge_commit_sha": None, "html_url": f"https://github.com/{REPO}/pull/7",
                "head": {"sha": remote_head(self.bare, head_ref), "ref": head_ref},
                "base": {"sha": remote_head(self.bare, "main"), "ref": "main"}}

    # ------------------------------------------------------------ owner setup through the API
    def setup(self, scenario: str, allowed_paths: list[str]) -> None:
        c = self.client
        self.bridge.outputs.update(planning_outputs(f"SCENARIO:{scenario} make add() return the sum", allowed_paths))
        owner, name = REPO.split("/")
        project = self.ok(c.post("/api/projects", json={
            "domain": "PERSONAL", "name": "E2E calc", "idea": "Kalkulator kecil untuk uji rantai eksekusi",
            "repository": {"provider": "GITHUB", "owner": owner, "name": name, "role": "project",
                           "local_path": str(self.repo), "default_branch": "main", "verify": False}}))["project"]
        self.project_id = project["id"]
        # Grader for this project: tests/acceptance of the reviewed main commit, outside every candidate.
        self.ok(c.post(f"/api/projects/{self.project_id}/trusted-suite",
                       json={"rationale": "acceptance suite reviewed on main", "commit": "main"}), 201)
        plan = self.ok(c.post(f"/api/projects/{self.project_id}/plans",
                              json={"title": "Perbaiki add", "prompt": "Perbaiki fungsi add agar menjumlahkan"}), 201)
        self.plan_id = plan["id"]
        self.ok(c.post(f"/api/plans/{self.plan_id}/run"), 202)
        from aeos.planning import runner

        runner.join_all(60)
        detail = self.ok(c.get(f"/api/plans/{self.plan_id}"))
        assert detail["state"] == "READY_FOR_HUMAN_REVIEW", detail.get("state_reason")
        version = detail["versions"][-1]
        decided = self.ok(c.post(f"/api/plans/{self.plan_id}/decision", json={
            "action": "APPROVE", "version_id": version["id"], "version_hash": version["manifest_hash"],
            "targets": [{"provider": "github-repo", "repo": REPO}], "rationale": "rencana disetujui pemilik"}))
        assert decided["compile"]["tasks"] == 1
        # Route activation (TRIAL) of a free worker route after the owner reviewed its terms. The seed's
        # catalog date ages; a catalog refresh (edge) would stamp pricing_checked_at and observe the route in
        # the OpenCode catalog (availability) — neither is part of the hash.
        with session_factory()() as s:
            r = s.scalar(select(ModelRoute).where(ModelRoute.route_key == ROUTE_KEY))
            r.pricing_checked_at = utcnow()
            r.availability, r.availability_checked_at = "AVAILABLE", utcnow()
            s.commit()
        cfg = self.ok(c.get(f"/api/routes/{ROUTE_KEY}"))["config_hash"]
        # S07: free-tier Zen chat models require the OpenCode client; the owner decides explicitly that the broker may
        # call the route as its own client (ADR-0041 amendment 2026-10-25) - without it the activation is refused
        self.ok(c.post(f"/api/routes/{ROUTE_KEY}/activate", json={
            "mode": "TRIAL", "rationale": "uji coba rute gratis untuk pekerja", "expected_config_hash": cfg,
            "terms_reviewed": True,
            "client_terms": {"accepted": True, "note": "aturan klien S07 dibaca; pemilik menerima proksi broker"}}))
        expires = (utcnow() + timedelta(days=2)).isoformat()
        # The owner also covers R3 (protected-path tasks) under the process sandbox explicitly (ADR-0051
        # amendment 2026-10-19); without it an R3 task waits for the container sandbox.
        self.ok(c.post("/api/decisions", json={
            "kind": "EXCEPTION", "target_type": "worker_sandbox", "target_id": "process",
            "auth_scope": {"domain": "PERSONAL", "sandbox": "process"}, "expires_at": expires,
            "rationale": "process sandbox WEAK diterima untuk uji PERSONAL", "details": {"allow_process_sandbox": True},
            "allowed_operations": ["run_worker_process_sandbox", "run_worker_process_sandbox_r3"]}),
            201)
        self.ok(c.post("/api/budgets", json={
            "scope_type": "PROJECT", "scope_id": self.project_id, "period": "MONTH", "limit_usd": 0,
            "limit_tokens": 50_000_000, "limit_requests": 5000, "hard_stop": True,
            "rationale": "anggaran proyek: hanya rute gratis"}))
        self.ok(c.post("/api/decisions", json={
            "kind": "WRITE_ENABLEMENT", "provider": "github-repo", "enabled": True,
            "auth_scope": {"provider": "github-repo", "repo": REPO},
            "rationale": "izinkan broker membuat branch aeos/* dan draft PR"}), 201)
        self.ok(c.post("/api/workforce/agents/opencode-implementer/enable", json={"reason": "uji rantai e2e"}))
        self.ok(c.post("/api/workforce/scheduler", json={"enabled": True}))
        out = self.ok(c.post(f"/api/plans/{self.plan_id}/execute"))
        assert out["ready"] == 1, out
        with session_factory()() as s:
            self.task_id = s.scalar(select(TaskExecution.task_id).where(TaskExecution.plan_id == self.plan_id))

    def tick(self) -> list[str]:
        from aeos.workforce import loop

        return list(loop.run_once(self.settings, execute=False)["leased"])

    def execute(self, run_id: str, **kw: Any) -> dict[str, Any]:
        from aeos.worker_runtime import executor

        return executor.execute_run(self.settings, run_id, heartbeat_seconds=kw.pop("heartbeat_seconds", 0.3), **kw)


def build_chain(settings: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, respx_mock: Any) -> Any:
    """Generator body for the fixture: yields a ready :class:`Chain` with the live server running."""
    secrets = StrictMemorySecrets()
    set_provider_override(secrets)
    secrets.put("workers/opencode-zen", FAKE_ZEN_KEY, actor="owner")
    secrets.put("personal/github", FAKE_GITHUB_TOKEN, actor="owner")

    port = free_port()
    monkeypatch.setattr(settings, "port", port)
    bin_dir = tmp_path / "bin"
    make_fake_executable(bin_dir, "opencode", FAKE_OPENCODE)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    from aeos.worker_runtime import opencode

    opencode._status_cache.update(at=0.0, value=None)
    fake_doctor_host_probes(monkeypatch)
    bridge = FakeClaude(outputs={}).install(monkeypatch)
    repo, bare = make_repositories(tmp_path / "work")
    from aeos.broker import git_broker

    # The GitHub git transport is the edge: the broker builds the real https remote, runs the real
    # external-write interlock and fetches the token through the real secrets contract; only the git
    # transfer itself lands in the local bare repository (a stand-in for github.com).
    git_pushes: list[dict[str, Any]] = []
    real_push_from_mirror = git_broker.push_from_mirror

    def push_via_fake_github(*, remote_url: str, token: str | None, **kw: Any) -> dict[str, Any]:
        git_pushes.append({"remote_url": remote_url, "token_is_owner_credential": token == FAKE_GITHUB_TOKEN})
        if remote_url != f"https://github.com/{REPO}.git":
            raise AssertionError(f"unexpected push remote {remote_url}")
        return real_push_from_mirror(remote_url=str(bare), token=token, **kw)

    monkeypatch.setattr(git_broker, "push_from_mirror", push_via_fake_github)

    with session_factory()() as s:  # bootstrap seeds (what `aeos init` materialises)
        from aeos.routing import seed as routing_seed
        from aeos.workforce import seed as workforce_seed

        routing_seed.run(s)
        workforce_seed.run(s)
        create_admin(s)
        s.commit()

    app = make_app(settings)
    server = LiveServer(app, port)
    server.start()
    try:
        with TestClient(app, base_url="http://testserver") as tc:
            client = AuthedClient(tc)
            client.login("owner", ADMIN_PW)
            client.select_domain(Domain.PERSONAL)
            chain = Chain(settings=settings, client=client, bridge=bridge, respx_mock=respx_mock, repo=repo, bare=bare,
                          git_pushes=git_pushes)
            chain.mock_upstream()
            chain.mock_github()
            yield chain
    finally:
        server.stop()
        from aeos.worker_runtime import processes

        for run_id in list(getattr(processes, "_registry", {}) or {}):
            processes.kill_run_processes(run_id)
        set_provider_override(None)
