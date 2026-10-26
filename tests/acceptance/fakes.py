"""Fakes for EXTERNAL tools and services only (part of the trusted suite; no test functions).

Everything DLY AEOS owns runs for real in the acceptance suite. Only what lives outside the control
plane is replaced, at the narrowest boundary the product itself uses:

* host probes (binaries on PATH, ``--version`` output, loopback HTTP health) -> :class:`FakeHost`
  through ``aeos.inventory.probes.set_runner_override`` (the Doctor/inventory probe boundary);
* the secret store (OpenBao / OS keyring) -> :class:`ContractSecretStore`, which keeps the real
  provider contract (``check_purpose`` + ``check_project_scope`` from ``aeos.secrets.provider``);
* the official ``claude`` binary -> :class:`FakeClaude` at the bridge's process boundary
  (``bridge.run_process``/``binary.probe``); the bridge's argv, env scrubbing, parsing, model policy
  and usage recording all run for real;
* GitHub / Google HTTP APIs -> ``respx`` routes declared inside each test.

No fake ever talks to the network, uses a real credential or calls a paid API.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aeos.core.binaries import is_executable_file
from aeos.inventory.probes import HttpResult, ProbeRefused, RunResult, is_loopback_url

FAKE_BIN = "/opt/acceptance-fake/bin"


def ok(stdout: str = "", stderr: str = "") -> RunResult:
    return RunResult(0, stdout, stderr, None, 1)


def failed(code: int = 1, stderr: str = "error") -> RunResult:
    return RunResult(code, "", stderr, None, 1)


class FakeHost:
    """Deterministic host for the probe boundary: only the named binaries exist."""

    def __init__(self) -> None:
        self.binaries: dict[str, str] = {}
        self.outputs: dict[Any, RunResult | Callable[[list[str]], RunResult]] = {}
        self.http: dict[str, HttpResult] = {}
        self.env: dict[str, str] = {}
        self.stats: dict[str, dict[str, Any]] = {}
        self.calls: list[list[str]] = []

    # -- configuration -------------------------------------------------------------------------
    def install(self, name: str, version_out: str | None = None, *, size: int = 100) -> str:
        path = f"{FAKE_BIN}/{name}"
        self.binaries[name] = path
        self.stats[path] = {"size": size, "mtime_ns": 1}
        if version_out is not None:
            self.outputs[name] = ok(version_out)
        return path

    def uninstall(self, name: str) -> None:
        path = self.binaries.pop(name, None)
        if path:
            self.stats.pop(path, None)

    def replace_binary(self, name: str, *, size: int) -> None:
        """The binary on disk changed (upgrade in place): new size/mtime, same path."""
        self.stats[self.binaries[name]] = {"size": size, "mtime_ns": size}

    # -- ProbeRunner protocol ------------------------------------------------------------------
    def which(self, name: str) -> str | None:
        return self.binaries.get(name)

    def run(self, argv: Sequence[str], *, timeout: float, env: Mapping[str, str] | None = None,
            cwd: Path | None = None) -> RunResult:
        argv = list(argv)
        self.calls.append(argv)
        name = next((n for n, p in self.binaries.items() if p == argv[0]), Path(argv[0]).stem.lower())
        r = self.outputs.get((name, tuple(argv[1:]))) or self.outputs.get(name)
        if callable(r) and not isinstance(r, RunResult):
            r = r(argv)
        return r if isinstance(r, RunResult) else RunResult(None, "", "", "NOT_FOUND", 1)

    def http_get(self, url: str, *, timeout: float, headers: Mapping[str, str] | None = None,
                 allow_remote: bool = False) -> HttpResult:
        if not allow_remote and not is_loopback_url(url):
            raise ProbeRefused(url)
        return self.http.get(url) or HttpResult(None, error="UNREACHABLE")

    def environ(self) -> Mapping[str, str]:
        return dict(self.env)

    def stat(self, path: str) -> dict[str, Any] | None:
        if str(path) in self.stats:
            return self.stats[str(path)]
        p = Path(path)
        if p.exists():
            st = p.stat()
            return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
        return None

    def is_executable(self, path: str) -> bool:
        return str(path) in self.stats or str(path) in self.binaries.values() or is_executable_file(path)


def git_answers(head: str = "a" * 40, remote: str = "https://github.com/fadhlillah-dli/dly-aeos.git"
                ) -> Callable[[list[str]], RunResult]:
    def handle(argv: list[str]) -> RunResult:
        args = argv[3:] if len(argv) > 2 and argv[1] == "-C" else argv[1:]
        if args == ["--version"]:
            return ok("git version 2.45.1\n")
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            return ok("/repo\n")
        if args == ["rev-parse", "HEAD"]:
            return ok(head + "\n")
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return ok("main\n")
        if args[:2] == ["status", "--porcelain"]:
            return ok("")
        if args[:3] == ["remote", "get-url", "origin"]:
            return ok(remote + "\n")
        return failed(1, "unsupported in fake")

    return handle


def base_host() -> FakeHost:
    """A host with git only: every optional tool (claude, opencode, docker, bao, node) is absent."""
    host = FakeHost()
    host.install("git")
    host.outputs["git"] = git_answers()
    return host


# ------------------------------------------------------------------------------------------------
# secret store


class ContractSecretStore:
    """In-memory stand-in for OpenBao/OS keyring that enforces the SAME caller contract as the real
    providers (``aeos.secrets.provider.check_purpose`` and ``check_project_scope``): a purpose must
    start with the reference namespace (``personal:...`` for ``personal/*``)."""

    name = "acceptance-contract-store"

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self.denied: list[tuple[str, str]] = []
        self.reads: list[tuple[str, str]] = []

    def get(self, ref: str, *, purpose: str, actor: str) -> str:
        from aeos.core.errors import Blocked, PolicyDenied
        from aeos.core.redaction import register_secret
        from aeos.secrets.provider import check_project_scope, check_purpose

        try:
            check_purpose(ref, purpose)
            check_project_scope(ref)
        except PolicyDenied:
            self.denied.append((ref, purpose))
            raise
        if ref not in self._data:
            raise Blocked(f"secret {ref} is not configured", code="SECRET_MISSING")
        self.reads.append((ref, purpose))
        register_secret(self._data[ref])
        return self._data[ref]

    def put(self, ref: str, value: str, *, actor: str) -> Any:
        from aeos.core.redaction import register_secret
        from aeos.secrets.facade import validate_ref

        validate_ref(ref)
        register_secret(value)
        self._data[ref] = value
        return self.status(ref)

    def status(self, ref: str) -> Any:
        from aeos.secrets.facade import SecretStatus

        return SecretStatus(ref=ref, exists=ref in self._data, backend="openbao")

    def list_status(self) -> list[Any]:
        return [self.status(r) for r in sorted(self._data)]

    def delete(self, ref: str, *, actor: str) -> None:
        self._data.pop(ref, None)


# ------------------------------------------------------------------------------------------------
# official claude binary (process boundary)


@dataclass
class _Proc:
    returncode: int
    stdout: str
    stderr: str = ""
    timed_out: bool = False
    duration_ms: int = 5
    killed_pids: tuple[int, ...] = ()


def claude_result(structured: Any, *, model: str = "claude-opus-5-5", session_id: str | None = None) -> dict[str, Any]:
    """Shape of ``claude -p --output-format json --json-schema`` (Claude Code 2.1.x, ADR-0040)."""
    import uuid

    text = json.dumps(structured)
    return {
        "type": "result", "subtype": "success", "is_error": False, "duration_ms": 12, "num_turns": 1,
        "session_id": session_id or str(uuid.uuid4()), "result": text, "structured_output": structured,
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "modelUsage": {model: {"inputTokens": 10, "outputTokens": 20, "canonicalModel": model,
                               "provider": "firstParty"}},
        "terminal_reason": "completed", "api_error_status": None, "permission_denials": [],
    }


class FakeClaude:
    """Replaces only the child process of the official binary. ``responder(argv, prompt)`` returns
    either a structured object (wrapped into the client's JSON) or a ``(returncode, stdout, stderr)``
    tuple for error scenarios (quota messages, crashes)."""

    def __init__(self, monkeypatch: Any, responder: Callable[[list[str], str], Any], *,
                 resolved: Mapping[str, str] | None = None) -> None:
        from aeos.claude_bridge import binary as binmod
        from aeos.claude_bridge import bridge

        self.calls: list[dict[str, Any]] = []
        self.responder = responder
        self.resolved = dict(resolved or {"opus": "claude-opus-5-5", "fable": "claude-fable-5-1",
                                          "sonnet": "claude-sonnet-5-0"})
        probe = binmod.Probe(
            binary=binmod.BinaryInfo(found=True, path=Path("claude.exe"), source="PATH", version="2.1.302",
                                     fingerprint="sha256:" + "c" * 64),
            auth=binmod.AuthInfo(status="VERIFIED", logged_in=True, auth_method="claude.ai",
                                 api_provider="firstParty", subscription_type="max"),
            checked_at=0.0)
        monkeypatch.setattr(binmod, "probe", lambda settings, refresh=False: probe)
        monkeypatch.setattr(bridge, "run_process", self._run)

    def _run(self, argv: list[str], *, stdin_text: str = "", cwd: Any = None, env: Any = None,
             timeout: float = 0.0) -> _Proc:
        self.calls.append({"argv": list(argv), "prompt": stdin_text, "env": dict(env or {}), "cwd": cwd})
        out = self.responder(list(argv), stdin_text)
        if isinstance(out, tuple):
            rc, stdout, stderr = out
            return _Proc(rc, stdout, stderr)
        model_arg = argv[argv.index("--model") + 1] if "--model" in argv else "opus"
        resolved = self.resolved.get(model_arg, model_arg)
        return _Proc(0, json.dumps(claude_result(out, model=resolved)))

    @staticmethod
    def schema_of(argv: list[str]) -> dict[str, Any]:
        return json.loads(argv[argv.index("--json-schema") + 1])


# ------------------------------------------------------------------------------------------------
# Google Drive / Docs and GitHub REST (respx; the adapters' real httpx calls are answered here)


class FakeGoogleApi:
    """Stateful Drive v3 + Docs v1 subset: folder create, DOCX upload (conversion), document get,
    ``batchUpdate`` honouring ``writeControl.requiredRevisionId``, and marker search on appProperties."""

    DRIVE = "https://www.googleapis.com/drive/v3/files"
    UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
    DOCS = "https://docs.googleapis.com/v1/documents"

    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self.timeout_after_create: set[str] = set()  # logical ids: created remotely, response lost
        self.fail_create: dict[str, int] = {}  # logical id -> HTTP status for a definitive failure
        self.writes: list[tuple[str, str]] = []  # (operation, logical id)
        self._n = 0

    def mount(self, router: Any) -> FakeGoogleApi:
        import re

        import httpx

        def new_file(meta: dict[str, Any], mime: str) -> tuple[str, dict[str, Any]]:
            self._n += 1
            fid = f"g{self._n:04d}"
            f = {"id": fid, "name": meta.get("name"), "mimeType": mime, "parents": meta.get("parents") or [],
                 "appProperties": meta.get("appProperties") or {}, "createdTime": f"2026-10-18T00:00:{self._n:02d}Z",
                 "revision": "r1", "text": f"{meta.get('name')}\n"}
            self.files[fid] = f
            return fid, f

        def lid(meta: dict[str, Any]) -> str:
            return (meta.get("appProperties") or {}).get("aeos_logical_id", "")

        def create(request: httpx.Request, mime: str | None = None) -> httpx.Response:
            if mime is None:
                meta = json.loads(request.content or b"{}")
                mime = meta.get("mimeType", "")
            else:
                raw = request.content.decode("utf-8", "replace")
                start = raw.index("{")
                meta = json.JSONDecoder().raw_decode(raw[start:])[0]
            logical = lid(meta)
            if logical in self.fail_create:
                return httpx.Response(self.fail_create[logical], json={"error": {"message": "refused by fake"}})
            fid, f = new_file(meta, mime)
            self.writes.append(("create", logical))
            if logical in self.timeout_after_create:
                self.timeout_after_create.discard(logical)
                raise httpx.ReadTimeout("response lost after the create", request=request)
            return httpx.Response(200, json={"id": fid, "name": f["name"], "mimeType": f["mimeType"],
                                             "webViewLink": f"https://drive.google.com/{fid}"})

        def document(request: httpx.Request, doc_id: str) -> httpx.Response:
            f = self.files.get(doc_id)
            if f is None:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            text = f["text"]
            return httpx.Response(200, json={
                "documentId": doc_id, "revisionId": f["revision"],
                "body": {"content": [{"endIndex": 1}, {"startIndex": 1, "endIndex": 1 + len(text),
                                                       "paragraph": {"elements": [{"textRun": {"content": text}}]}}]}})

        def batch_update(request: httpx.Request, doc_id: str) -> httpx.Response:
            f = self.files.get(doc_id)
            if f is None:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            body = json.loads(request.content or b"{}")
            required = (body.get("writeControl") or {}).get("requiredRevisionId")
            if required != f["revision"]:
                return httpx.Response(400, json={"error": {"status": "FAILED_PRECONDITION",
                                                           "message": "revision mismatch"}})
            inserted = "".join(r.get("insertText", {}).get("text", "") for r in body.get("requests", []))
            f["text"] = inserted or f["text"]
            f["revision"] = f"r{int(f['revision'][1:]) + 1}"
            self.writes.append(("update", f["appProperties"].get("aeos_logical_id", "")))
            return httpx.Response(200, json={"replies": [], "writeControl": {"requiredRevisionId": f["revision"]}})

        def search(request: httpx.Request) -> httpx.Response:
            q = request.url.params.get("q", "")
            want = dict(re.findall(r"key='(\w+)' and value='([^']*)'", q))
            hits = [{"id": f["id"], "name": f["name"], "mimeType": f["mimeType"], "createdTime": f["createdTime"],
                     "webViewLink": f"https://drive.google.com/{f['id']}", "appProperties": f["appProperties"]}
                    for f in self.files.values()
                    if all(f["appProperties"].get(k) == v for k, v in want.items())]
            return httpx.Response(200, json={"files": hits})

        router.post(self.DRIVE).mock(side_effect=lambda r: create(r))
        router.get(self.DRIVE).mock(side_effect=search)
        router.post(self.UPLOAD).mock(side_effect=lambda r: create(r, "application/vnd.google-apps.document"))
        router.post(url__regex=rf"^{re.escape(self.DOCS)}/(?P<doc_id>[^/:]+):batchUpdate$").mock(
            side_effect=batch_update)
        router.get(url__regex=rf"^{re.escape(self.DOCS)}/(?P<doc_id>[^/:?]+)(\?.*)?$").mock(side_effect=document)
        return self

    def human_edit(self, logical_id: str, text: str = "Diedit manusia\n") -> str:
        """A person edits the document in Google Docs: new text, new revision id."""
        f = next(x for x in self.files.values() if x["appProperties"].get("aeos_logical_id") == logical_id)
        f["text"] = text
        f["revision"] = f"r{int(f['revision'][1:]) + 1}"
        return f["id"]

    def by_logical(self, logical_id: str) -> list[dict[str, Any]]:
        return [f for f in self.files.values() if f["appProperties"].get("aeos_logical_id") == logical_id]


class FakeGitHubApi:
    """Stateful GitHub REST subset for issues/labels of one repository."""

    def __init__(self, repo: str) -> None:
        self.repo = repo
        self.labels: dict[str, dict[str, Any]] = {}
        self.issues: list[dict[str, Any]] = []
        self.fail_create_issue: dict[str, int] = {}  # title fragment -> HTTP status

    def mount(self, router: Any) -> FakeGitHubApi:
        import re
        from urllib.parse import unquote

        import httpx

        base = f"https://api.github.com/repos/{self.repo}"

        def get_label(request: httpx.Request, name: str) -> httpx.Response:
            label = self.labels.get(unquote(name))
            return httpx.Response(200, json=label) if label else httpx.Response(404, json={"message": "Not Found"})

        def post_label(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.labels[body["name"]] = {"name": body["name"], "url": f"{base}/labels/{body['name']}"}
            return httpx.Response(201, json=self.labels[body["name"]])

        def post_issue(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            for frag, status in self.fail_create_issue.items():
                if frag in body.get("title", ""):
                    return httpx.Response(status, json={"message": "Validation Failed (fake)"})
            n = len(self.issues) + 1
            issue = {"number": n, "title": body["title"], "body": body.get("body", ""), "state": "open",
                     "labels": [{"name": x} for x in body.get("labels", [])],
                     "html_url": f"https://github.com/{self.repo}/issues/{n}", "node_id": f"I_{n}"}
            self.issues.append(issue)
            return httpx.Response(201, json=issue)

        def list_issues(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=list(self.issues))

        def get_issue(request: httpx.Request, number: str) -> httpx.Response:
            it = next((i for i in self.issues if str(i["number"]) == number), None)
            return httpx.Response(200, json=it) if it else httpx.Response(404, json={"message": "Not Found"})

        router.get(url__regex=rf"^{re.escape(base)}/labels/(?P<name>[^/?]+)$").mock(side_effect=get_label)
        router.post(f"{base}/labels").mock(side_effect=post_label)
        router.post(f"{base}/issues").mock(side_effect=post_issue)
        router.get(f"{base}/issues").mock(side_effect=list_issues)
        router.get(url__regex=rf"^{re.escape(base)}/issues/(?P<number>\d+)$").mock(side_effect=get_issue)
        return self

    def close_issue(self, number: int) -> None:
        for it in self.issues:
            if it["number"] == number:
                it["state"], it["state_reason"] = "closed", "completed"


def google_token_json() -> str:
    from datetime import timedelta

    from aeos.core.timeutil import utcnow

    return json.dumps({"token": "ya29." + "x" * 20, "refresh_token": "1//" + "y" * 20, "client_id": "cid",
                       "client_secret": "csecret", "token_uri": "https://oauth2.googleapis.com/token",
                       "scopes": ["https://www.googleapis.com/auth/drive.file"],
                       "expiry": (utcnow() + timedelta(hours=2)).isoformat()})
