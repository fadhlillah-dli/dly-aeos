"""Faked external services for end-to-end/seam tests (no tests here).

* ``FakeClaude`` — a scripted stand-in for the official ``claude`` binary. The real bridge builds its argv,
  scrubbed environment and supervisor workspace; only the child process is swapped for a small Python
  script that answers with schema-valid JSON in the client's ``--output-format json`` shape and a
  resolved model per alias (opus -> claude-opus-5-5, fable -> claude-fable-5-1). Never calls a model.
* ``FakeDrive`` / ``FakeGitHub`` — stateful respx routes for Google Drive/Docs and GitHub Issues. Nothing
  leaves the process: respx refuses every unmocked request, so the owner's real Drive/GitHub are never
  touched and no real credential is used (tokens are visibly fake).
"""

from __future__ import annotations

import json
import re
import sys
import textwrap
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from aeos.core.timeutil import utcnow

RESOLVED = {"opus": "claude-opus-5-5", "fable": "claude-fable-5-1"}
STAGES = ("PRIMARY_PLANNING", "CHALLENGE_REVIEW", "SYNTHESIS", "FINAL_REVIEW")

_SCRIPT = textwrap.dedent(r'''
    import json, os, sys, uuid
    cfg_path, log_path = sys.argv[1], sys.argv[2]
    argv = sys.argv[3:]
    prompt = sys.stdin.read()
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    model = argv[argv.index("--model") + 1]
    props = set(schema.get("properties") or {})
    stage = "PRIMARY_PLANNING"
    for key, name in (("verdict", "FINAL_REVIEW"), ("resolutions", "SYNTHESIS"), ("contradictions", "CHALLENGE_REVIEW")):
        if key in props:
            stage = name
            break
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    ov = cfg.get("overrides", {}).get(stage, {})
    rec = {"stage": stage, "model": model, "argv": argv, "cwd": os.getcwd(), "prompt": prompt,
           "env": sorted(os.environ)}
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    if ov.get("quota"):
        out = {"type": "result", "subtype": "error_during_execution", "is_error": True,
               "result": "Claude AI usage limit reached|1893456000", "session_id": str(uuid.uuid4())}
        sys.stdout.write(json.dumps(out))
        sys.exit(1)
    structured = cfg["outputs"][stage]
    resolved = ov.get("resolved") or cfg["resolved"][model]
    usage = {resolved: {"inputTokens": 120, "outputTokens": 80, "canonicalModel": resolved,
                        "provider": "firstParty", "costUSD": 1.23, "costBasis": "list"}}
    out = {"type": "result", "subtype": "success", "is_error": False, "api_error_status": None,
           "result": json.dumps(structured), "structured_output": structured, "session_id": str(uuid.uuid4()),
           "usage": {"input_tokens": 120, "output_tokens": 80}, "duration_ms": 12, "num_turns": 1,
           "terminal_reason": "completed", "permission_denials": [], "safety_stops": 0,
           "modelUsage": usage, "total_cost_usd": 1.23}
    sys.stdout.write(json.dumps(out))
''')


class FakeClaude:
    """Swap the official binary's child process for a scripted fake (everything else is the real bridge)."""

    def __init__(self, monkeypatch: Any, tmp_path: Path, outputs: dict[str, Any]) -> None:
        from aeos.claude_bridge import binary as binmod
        from aeos.claude_bridge import bridge, process

        self.dir = tmp_path / "fake-claude"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.script = self.dir / "claude_fake.py"
        self.script.write_text(_SCRIPT, encoding="utf-8")
        self.cfg_path = self.dir / "config.json"
        self.log_path = self.dir / "calls.jsonl"
        self.outputs = dict(outputs)
        self.overrides: dict[str, dict[str, Any]] = {}
        self._write()

        def fake_run(argv: list[str], **kw: Any):
            return process.run_process([sys.executable, str(self.script), str(self.cfg_path), str(self.log_path),
                                       *argv[1:]], **kw)

        monkeypatch.setattr(bridge, "run_process", fake_run)
        monkeypatch.setattr(binmod, "probe", lambda settings, refresh=False: _probe())

    def _write(self) -> None:
        self.cfg_path.write_text(json.dumps({"outputs": self.outputs, "overrides": self.overrides,
                                             "resolved": RESOLVED}), encoding="utf-8")

    def set_override(self, stage: str, **values: Any) -> None:
        if values:
            self.overrides[stage] = dict(values)
        else:
            self.overrides.pop(stage, None)
        self._write()

    def calls(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _probe():
    from aeos.claude_bridge.binary import AuthInfo, BinaryInfo, Probe

    info = BinaryInfo(found=True, path=Path("claude.exe"), source="PATH", version="2.1.302 (fake)",
                      fingerprint="sha256:" + "f" * 64, reasons=[])
    return Probe(binary=info, auth=AuthInfo(status="VERIFIED", logged_in=True, auth_method="claude.ai",
                                            api_provider="firstParty", subscription_type="max",
                                            email="owner@example.invalid"), checked_at=0.0)


# ---------------------------------------------------------------------------------------------------- Google

DRIVE_API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
DOCS_API = "https://docs.googleapis.com/v1"
FOLDER_MIME = "application/vnd.google-apps.folder"
_Q_VALUE = r"key='{key}' and value='((?:[^'\\]|\\.)*)'"


class FakeDrive:
    """Stateful Google Drive (drive.file) + Docs API. ``lose_response_for`` makes the next create of a
    logical id succeed remotely but time out on the way back (outcome UNKNOWN for the publisher)."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self.n = 0
        self.lose_response_for: set[str] = set()
        self.batch_updates: list[dict[str, Any]] = []
        self.permission_calls = 0

    # -- helpers
    def _new_id(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}{self.n:04d}fakeid"

    def by_logical(self, logical_id: str) -> list[dict[str, Any]]:
        return [f for f in self.files.values() if f["appProperties"].get("aeos_logical_id") == logical_id]

    def human_edit(self, doc_id: str, text: str) -> None:
        f = self.files[doc_id]
        f["text"] = text
        f["revision"] = f["revision"] + "-human"

    def _doc_resource(self, f: dict[str, Any]) -> dict[str, Any]:
        text = f.get("text") or "\n"
        return {"documentId": f["id"], "revisionId": f["revision"], "title": f["name"],
                "body": {"content": [{"endIndex": 1 + len(text),
                                      "paragraph": {"elements": [{"textRun": {"content": text}}]}}]}}

    # -- routes
    def _create_folder(self, request: httpx.Request) -> httpx.Response:
        meta = json.loads(request.content)
        fid = self._new_id("FOLDER")
        self.files[fid] = {"id": fid, "name": meta["name"], "mimeType": meta["mimeType"],
                           "appProperties": meta.get("appProperties") or {}, "parents": meta.get("parents") or [],
                           "revision": "r1", "createdTime": utcnow().isoformat()}
        return self._maybe_lose(meta, httpx.Response(200, json={
            "id": fid, "name": meta["name"], "webViewLink": f"https://drive.google.com/drive/folders/{fid}"}))

    def _upload(self, request: httpx.Request) -> httpx.Response:
        head = request.content.split(b"\r\n\r\n", 1)[1]
        meta = json.loads(head.split(b"\r\n--", 1)[0])
        did = self._new_id("DOC")
        self.files[did] = {"id": did, "name": meta["name"], "mimeType": meta["mimeType"],
                           "appProperties": meta.get("appProperties") or {}, "parents": meta.get("parents") or [],
                           "revision": "r1", "text": f"{meta['name']}\n", "createdTime": utcnow().isoformat()}
        return self._maybe_lose(meta, httpx.Response(200, json={
            "id": did, "name": meta["name"], "mimeType": meta["mimeType"],
            "webViewLink": f"https://docs.google.com/document/d/{did}/edit"}))

    def _maybe_lose(self, meta: dict[str, Any], resp: httpx.Response) -> httpx.Response:
        lid = (meta.get("appProperties") or {}).get("aeos_logical_id")
        if lid in self.lose_response_for:
            self.lose_response_for.discard(lid)
            raise httpx.ReadTimeout("simulated: created remotely, response lost")
        return resp

    def _list(self, request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("q", "")
        lid = re.search(_Q_VALUE.format(key="aeos_logical_id"), q)
        pid = re.search(_Q_VALUE.format(key="aeos_project_id"), q)
        out = [{"id": f["id"], "name": f["name"], "mimeType": f["mimeType"], "createdTime": f["createdTime"],
                "appProperties": f["appProperties"],
                "webViewLink": f"https://drive.google.com/open?id={f['id']}"}
               for f in self.files.values()
               if lid and pid and f["appProperties"].get("aeos_logical_id") == lid.group(1)
               and f["appProperties"].get("aeos_project_id") == pid.group(1)]
        return httpx.Response(200, json={"files": out})

    def _get_doc(self, request: httpx.Request) -> httpx.Response:
        doc_id = request.url.path.rsplit("/", 1)[-1]
        f = self.files.get(doc_id)
        if f is None:
            return httpx.Response(404, json={"error": {"code": 404}})
        return httpx.Response(200, json=self._doc_resource(f))

    def _batch_update(self, request: httpx.Request) -> httpx.Response:
        doc_id = request.url.path.rsplit("/", 1)[-1].split(":", 1)[0]
        body = json.loads(request.content)
        f = self.files.get(doc_id)
        required = (body.get("writeControl") or {}).get("requiredRevisionId")
        self.batch_updates.append({"doc_id": doc_id, "required": required})
        if f is None:
            return httpx.Response(404, json={"error": {"code": 404}})
        if required != f["revision"]:
            return httpx.Response(400, json={"error": {"status": "FAILED_PRECONDITION",
                                                       "message": "The required revision ID does not match"}})
        text = "".join(r["insertText"]["text"] for r in body.get("requests", []) if "insertText" in r)
        f["text"] = text
        f["revision"] = f"r{int(f['revision'].lstrip('r').split('-')[0]) + 1}"
        return httpx.Response(200, json={"documentId": doc_id, "replies": [],
                                         "writeControl": {"requiredRevisionId": f["revision"]}})

    def _permissions(self, request: httpx.Request) -> httpx.Response:
        self.permission_calls += 1
        return httpx.Response(403)

    def install(self, router: Any) -> None:
        router.post(f"{DRIVE_API}/files").mock(side_effect=self._create_folder)
        router.get(f"{DRIVE_API}/files").mock(side_effect=self._list)
        router.post(url__regex=rf"^{re.escape(DRIVE_API)}/files/[^/]+/permissions").mock(side_effect=self._permissions)
        router.post(f"{UPLOAD_API}/files").mock(side_effect=self._upload)
        router.post(url__regex=rf"^{re.escape(DOCS_API)}/documents/[^/?]+:batchUpdate").mock(
            side_effect=self._batch_update)
        router.get(url__regex=rf"^{re.escape(DOCS_API)}/documents/[^/?:]+(\?.*)?$").mock(side_effect=self._get_doc)


# ---------------------------------------------------------------------------------------------------- GitHub

GITHUB_API = "https://api.github.com"


class FakeGitHub:
    """Stateful GitHub labels + issues for one repository. ``lose_response_for`` (issue logical ids) makes
    the create succeed remotely and time out on the way back."""

    def __init__(self, repo: str) -> None:
        self.repo = repo
        self.labels: dict[str, dict[str, Any]] = {}
        self.issues: list[dict[str, Any]] = []
        self.lose_response_for: set[str] = set()

    def issues_for(self, logical_id: str) -> list[dict[str, Any]]:
        mark = f"<!-- aeos:logical_id={logical_id} -->"
        return [i for i in self.issues if mark in (i.get("body") or "")]

    def _get_label(self, request: httpx.Request) -> httpx.Response:
        from urllib.parse import unquote

        name = unquote(request.url.path.rsplit("/", 1)[-1])
        if name not in self.labels:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json=self.labels[name])

    def _create_label(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["name"] in self.labels:
            return httpx.Response(422, json={"message": "already_exists"})
        self.labels[body["name"]] = {"name": body["name"], "url": f"{GITHUB_API}/repos/{self.repo}/labels/x"}
        return httpx.Response(201, json=self.labels[body["name"]])

    def _create_issue(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        num = len(self.issues) + 1
        issue = {"number": num, "title": body["title"], "body": body["body"], "labels": body.get("labels", []),
                 "state": "open", "html_url": f"https://github.com/{self.repo}/issues/{num}", "node_id": f"I_{num}"}
        self.issues.append(issue)
        for lid in list(self.lose_response_for):
            if f"<!-- aeos:logical_id={lid} -->" in body["body"]:
                self.lose_response_for.discard(lid)
                raise httpx.ReadTimeout("simulated: issue created remotely, response lost")
        return httpx.Response(201, json=issue)

    def _list_issues(self, request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json=self.issues[(page - 1) * 100: page * 100])

    def _get_issue(self, request: httpx.Request) -> httpx.Response:
        num = int(request.url.path.rsplit("/", 1)[-1])
        if not 0 < num <= len(self.issues):
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json=self.issues[num - 1])

    def install(self, router: Any) -> None:
        base = f"{GITHUB_API}/repos/{self.repo}"
        router.get(url__regex=rf"^{re.escape(base)}/labels/.+").mock(side_effect=self._get_label)
        router.post(f"{base}/labels").mock(side_effect=self._create_label)
        router.post(f"{base}/issues").mock(side_effect=self._create_issue)
        router.get(url__regex=rf"^{re.escape(base)}/issues/\d+$").mock(side_effect=self._get_issue)
        router.get(url__regex=rf"^{re.escape(base)}/issues(\?.*)?$").mock(side_effect=self._list_issues)


# ---------------------------------------------------------------------------------------------------- secrets


def seed_fake_personal_secrets(secrets_memory: Any) -> None:
    """Visibly fake Google OAuth client/token and GitHub token (in-memory store only)."""
    secrets_memory.put("personal/google-oauth-client",
                       json.dumps({"installed": {"client_id": "fake-client-id", "client_secret": "fake-client-secret"}}),
                       actor="test")
    secrets_memory.put("personal/google-oauth-token", json.dumps({
        "token": "ya29.FAKE-test-token-not-real-0000000000", "refresh_token": "1//FAKE-refresh-not-real-0000000",
        "client_id": "fake-client-id", "client_secret": "fake-client-secret",
        "token_uri": "https://oauth2.googleapis.com/token", "scopes": ["https://www.googleapis.com/auth/drive.file"],
        "expiry": (utcnow() + timedelta(hours=2)).isoformat()}), actor="test")
    secrets_memory.put("personal/github", "ghp_FAKEtesttokenNOTREAL000000000000000000", actor="test")
