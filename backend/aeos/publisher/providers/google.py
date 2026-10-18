"""PERSONAL documentation adapter: Google Drive + Google Docs (master §04, §29, S16-S21).

* OAuth 2.0 installed-app (loopback 127.0.0.1) flow requesting ONLY the least-privilege scope
  ``https://www.googleapis.com/auth/drive.file`` (S18). The app can see/edit only files it created
  (or the user opened with it); the Docs API accepts ``drive.file`` for those files.
* The OAuth client config (from the user's ``client_secret*.json``) is stored once via
  ``aeos.secrets.facade.put_secret("personal/google-oauth-client")``; the token at
  ``personal/google-oauth-token``. Tokens are refreshed in-process and never logged/returned.
* Folder per project (S16) named after the project; every file carries ``appProperties``
  ``aeos_project_id`` / ``aeos_logical_id`` so reconciliation finds earlier side effects by marker,
  never by title (§29).
* Documents are created by uploading the DOCX snapshot with conversion to Google Docs (S20); images
  are embedded inside the DOCX — this adapter never creates permissions, so there are no public /
  anyone-with-link files (S21).
* Updates use ``documents.batchUpdate`` with ``writeControl.requiredRevisionId`` (S19). A revision
  mismatch (a human edited the doc) is a CONFLICT: nothing is overwritten and the observed content is
  handed back as revision input. Incremental update writes text + headings + bullets; tables/images
  are only carried by a new DOCX snapshot (documented fidelity limit).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from aeos.core.errors import Blocked, ValidationFailed
from aeos.core.hashing import sha256_text
from aeos.core.redaction import register_secret
from aeos.core.timeutil import UTC, utcnow
from aeos.publisher.providers.base import (
    DEFAULT_TIMEOUT,
    Inspection,
    OpContext,
    OpResult,
    ProviderBlocked,
    ProviderConflict,
    ProviderError,
    classify_http_error,
    send,
)
from aeos.secrets import facade as secrets

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
SCOPES: tuple[str, ...] = (DRIVE_FILE_SCOPE,)  # the ONLY scope this adapter ever requests (S18)
CLIENT_REF = "personal/google-oauth-client"
TOKEN_REF = "personal/google-oauth-token"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
DRIVE_API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
DOCS_API = "https://docs.googleapis.com/v1"
FOLDER_MIME = "application/vnd.google-apps.folder"
GDOC_MIME = "application/vnd.google-apps.document"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
CONNECT_REMEDY = "aeos publish google-connect --client-secret <path ke client_secret.json (OAuth Desktop app)>"
ACTOR = "publisher.google"


# ---------------------------------------------------------------------------------------------
# OAuth (installed-app loopback)


def load_client_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValidationFailed(f"cannot read OAuth client file: {exc.__class__.__name__}") from exc
    if not isinstance(data, dict) or "installed" not in data:
        raise ValidationFailed("OAuth client must be of type 'Desktop app' (JSON key 'installed'); web clients "
                               "are not supported for the loopback flow")
    inst = data["installed"]
    if not inst.get("client_id") or not inst.get("client_secret"):
        raise ValidationFailed("OAuth client file is missing client_id/client_secret")
    return data


def connect(client_secret_path: Path, *, actor: str, open_browser: bool = True, timeout_seconds: int = 300,
            flow_factory: Any = None) -> dict[str, Any]:
    """Run the browser consent on 127.0.0.1 and store client config + token in the secret store.

    Returns a non-secret summary (granted scopes, refs). Raises ``Blocked`` if no secret store.
    """
    client_config = load_client_config(client_secret_path)
    secrets.put_secret(CLIENT_REF, json.dumps(client_config), actor=actor)
    if flow_factory is None:
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow_factory = InstalledAppFlow.from_client_config
    flow = flow_factory(client_config, scopes=list(SCOPES))
    creds = flow.run_local_server(
        host="127.0.0.1", port=0, open_browser=open_browser, timeout_seconds=timeout_seconds,
        authorization_prompt_message="Buka URL ini di browser untuk memberi izin drive.file: {url}",
        success_message="DLY AEOS: izin Google Drive (drive.file) diterima. Jendela ini boleh ditutup.",
        access_type="offline", prompt="consent",
    )
    token_json = creds.to_json()
    token = json.loads(token_json)
    for k in ("token", "refresh_token"):
        register_secret(token.get(k))
    granted = sorted(set(token.get("scopes") or SCOPES))
    secrets.put_secret(TOKEN_REF, token_json, actor=actor)
    return {"client_ref": CLIENT_REF, "token_ref": TOKEN_REF, "requested_scopes": list(SCOPES),
            "granted_scopes": granted, "least_privilege": granted == [DRIVE_FILE_SCOPE]}


def google_status() -> dict[str, Any]:
    """Non-secret status: whether client/token exist and which scopes the token carries."""
    out: dict[str, Any] = {"requested_scopes": list(SCOPES), "client": None, "token": None, "remedy": CONNECT_REMEDY}
    try:
        cs, ts = secrets.secret_status(CLIENT_REF), secrets.secret_status(TOKEN_REF)
    except Blocked as exc:
        out["status"] = "BLOCKED"
        out["reason"] = exc.message
        return out
    out["client"] = {"exists": cs.exists, "backend": cs.backend, "updated_at": cs.updated_at.isoformat()
                     if cs.updated_at else None}
    out["token"] = {"exists": ts.exists, "backend": ts.backend, "updated_at": ts.updated_at.isoformat()
                    if ts.updated_at else None}
    if not (cs.exists and ts.exists):
        out["status"] = "MISSING"
        return out
    try:
        tok = json.loads(secrets.get_secret(TOKEN_REF, purpose="google status (scopes only)", actor=ACTOR))
        out["granted_scopes"] = sorted(tok.get("scopes") or [])
        out["has_refresh_token"] = bool(tok.get("refresh_token"))
    except (Blocked, ValueError):
        out["granted_scopes"] = []
    # Stored ≠ verified: a token in the store is CONFIGURED; a successful API call is the proof.
    out["status"] = "CONFIGURED"
    return out


class GoogleAuth:
    """Access-token holder with refresh (token JSON in ``Credentials.to_json`` format)."""

    def __init__(self, client: httpx.Client, token_json: str | None = None):
        self._client = client
        raw = token_json if token_json is not None else secrets.get_secret(
            TOKEN_REF, purpose="google drive/docs publication", actor=ACTOR)
        try:
            self._tok: dict[str, Any] = json.loads(raw)
        except ValueError as exc:
            raise ProviderBlocked("stored Google token is unreadable", remedy=CONNECT_REMEDY,
                                  code="GOOGLE_TOKEN_INVALID") from exc
        for k in ("token", "refresh_token", "client_secret"):
            register_secret(self._tok.get(k))

    def _expired(self) -> bool:
        exp = self._tok.get("expiry")
        if not self._tok.get("token"):
            return True
        if not exp:
            return False
        try:
            when = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
        except ValueError:
            return True
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return when <= utcnow() + timedelta(seconds=60)

    def refresh(self) -> None:
        rt = self._tok.get("refresh_token")
        if not rt or not self._tok.get("client_id"):
            raise ProviderBlocked("Google token expired and cannot be refreshed", remedy=CONNECT_REMEDY,
                                  code="GOOGLE_TOKEN_EXPIRED")
        resp = send(self._client, "POST", self._tok.get("token_uri") or DEFAULT_TOKEN_URI, write=False,
                    what="google token refresh", data={
                        "grant_type": "refresh_token", "refresh_token": rt,
                        "client_id": self._tok["client_id"], "client_secret": self._tok.get("client_secret", ""),
                    })
        if resp.status_code != 200:
            raise ProviderBlocked(f"Google token refresh failed (HTTP {resp.status_code})", remedy=CONNECT_REMEDY,
                                  code="GOOGLE_TOKEN_REFRESH_FAILED")
        data = resp.json()
        register_secret(data.get("access_token"))
        self._tok["token"] = data.get("access_token")
        self._tok["expiry"] = (utcnow() + timedelta(seconds=int(data.get("expires_in", 3600)))).isoformat()
        if data.get("scope"):
            self._tok["scopes"] = sorted(str(data["scope"]).split())
        try:
            secrets.put_secret(TOKEN_REF, json.dumps(self._tok), actor=ACTOR)
        except Blocked:
            pass  # the in-memory token still works for this run; next run refreshes again

    def header(self) -> dict[str, str]:
        if self._expired():
            self.refresh()
        return {"Authorization": f"Bearer {self._tok['token']}"}


# ---------------------------------------------------------------------------------------------
# Markdown → Docs API requests (incremental update path)

_MD_INLINE = [(re.compile(r"\*\*([^*]+)\*\*"), r"\1"), (re.compile(r"`([^`]+)`"), r"\1"),
              (re.compile(r"\[([^\]]+)\]\(([^)]+)\)"), r"\1 (\2)"), (re.compile(r"(?<![\w*])\*([^*\s][^*]*)\*"), r"\1"),
              (re.compile(r"(?<![\w_])_([^_\s][^_]*)_"), r"\1")]


def _plain_inline(text: str) -> str:
    for pat, rep in _MD_INLINE:
        text = pat.sub(rep, text)
    return text


def _u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def markdown_to_docs_requests(markdown: str) -> tuple[str, list[dict[str, Any]]]:
    """Plain text + style requests (indices in UTF-16 code units, starting at 1)."""
    out_lines: list[str] = []
    headings: list[tuple[int, int, int]] = []
    bullets: list[tuple[int, int]] = []
    index = 1
    in_code = False

    def emit(text: str, *, heading: int = 0, bullet: bool = False) -> None:
        nonlocal index
        line = text + "\n"
        start, end = index, index + _u16(line)
        if heading:
            headings.append((start, end, heading))
        if bullet:
            bullets.append((start, end))
        out_lines.append(line)
        index = end

    for raw in markdown.replace("\r\n", "\n").split("\n"):
        s = raw.strip()
        if s.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            emit(raw)
            continue
        if not s or s.startswith("<!--"):
            continue
        if re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?", s):
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            emit(_plain_inline(m.group(2)), heading=len(m.group(1)))
            continue
        im = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)$", s)
        if im:
            emit(f"[visual: {im.group(1)} — tersemat pada snapshot DOCX; pembaruan gambar inkremental memerlukan "
                 "jalur akses yang disetujui]")
            continue
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            emit(_plain_inline("  |  ".join(cells)))
            continue
        bm = re.match(r"^[-*]\s+(.*)$", s)
        if bm:
            emit(_plain_inline(bm.group(1)), bullet=True)
            continue
        if s.startswith(">"):
            emit(_plain_inline(s.lstrip("> ").strip()))
            continue
        emit(_plain_inline(s))
    text = "".join(out_lines) or "\n"
    reqs: list[dict[str, Any]] = [{"insertText": {"location": {"index": 1}, "text": text}}]
    total_end = 1 + _u16(text)
    reqs.append({"updateParagraphStyle": {"range": {"startIndex": 1, "endIndex": total_end},
                                          "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                          "fields": "namedStyleType"}})
    for start, end, level in headings:
        reqs.append({"updateParagraphStyle": {"range": {"startIndex": start, "endIndex": end},
                                              "paragraphStyle": {"namedStyleType": f"HEADING_{min(level, 6)}"},
                                              "fields": "namedStyleType"}})
    for start, end in bullets:
        reqs.append({"createParagraphBullets": {"range": {"startIndex": start, "endIndex": end},
                                                "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"}})
    return text, reqs


def docs_plain_text(document: dict[str, Any]) -> str:
    """Extract text from a Docs API document resource (for revision input)."""
    parts: list[str] = []

    def walk(content: list[dict[str, Any]]) -> None:
        for el in content or []:
            if "paragraph" in el:
                for pe in el["paragraph"].get("elements", []):
                    tr = pe.get("textRun")
                    if tr and tr.get("content"):
                        parts.append(tr["content"])
            elif "table" in el:
                for row in el["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        walk(cell.get("content", []))
            elif "tableOfContents" in el:
                walk(el["tableOfContents"].get("content", []))

    walk((document.get("body") or {}).get("content", []))
    return "".join(parts)


def _body_end(document: dict[str, Any]) -> int:
    content = (document.get("body") or {}).get("content") or []
    return int(content[-1].get("endIndex", 1)) if content else 1


def marker_value(logical_id: str) -> str:
    # Drive appProperties: key+value <= 124 bytes. Long logical ids use a stable hash form.
    return logical_id if len(logical_id) <= 100 else "sha:" + sha256_text(logical_id)[:40]


def _q_escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace("'", "\\'")


# ---------------------------------------------------------------------------------------------


class GoogleDriveDocsProvider:
    """Implements provider names ``google-drive`` (folders/doc create) and ``google-docs`` (updates)."""

    name = "google"

    def __init__(self, *, client: httpx.Client | None = None, auth: GoogleAuth | None = None,
                 root_folder_id: str | None = None):
        self._own_client = client is None
        self.client = client or httpx.Client(timeout=DEFAULT_TIMEOUT)
        self.auth = auth or GoogleAuth(self.client)
        self.root_folder_id = None if root_folder_id in (None, "", "root") else root_folder_id

    def close(self) -> None:
        if self._own_client:
            self.client.close()

    # -- http with one auth refresh on 401
    def _req(self, method: str, url: str, *, write: bool, what: str, **kw: Any) -> httpx.Response:
        headers = {**kw.pop("headers", {}), **self.auth.header()}
        resp = send(self.client, method, url, write=write, what=what, headers=headers, **kw)
        if resp.status_code == 401:
            self.auth.refresh()
            headers.update(self.auth.header())
            resp = send(self.client, method, url, write=write, what=what, headers=headers, **kw)
        return resp

    def _app_props(self, ctx: OpContext) -> dict[str, str]:
        return {"aeos_project_id": ctx.project_id, "aeos_logical_id": marker_value(ctx.logical_id),
                "aeos_op": ctx.idempotency_key.removeprefix("sha256:")[:32]}

    def _parents(self, ctx: OpContext) -> list[str]:
        parent = ctx.parent_external_id or self.root_folder_id
        return [parent] if parent else []  # empty → user's My Drive root

    def execute(self, ctx: OpContext) -> OpResult:
        if ctx.operation == "create_folder":
            return self._create_folder(ctx)
        if ctx.operation == "create_doc":
            return self._create_doc(ctx)
        if ctx.operation == "update_doc":
            return self._update_doc(ctx)
        raise ProviderError(f"google adapter does not support operation {ctx.operation!r}")

    def _create_folder(self, ctx: OpContext) -> OpResult:
        meta = {"name": ctx.title, "mimeType": FOLDER_MIME, "appProperties": self._app_props(ctx),
                "description": f"DLY AEOS {ctx.logical_id} (project {ctx.project_key})"}
        parents = self._parents(ctx)
        if parents:
            meta["parents"] = parents
        resp = self._req("POST", f"{DRIVE_API}/files", write=True, what="drive create folder",
                         params={"fields": "id,name,webViewLink"}, json=meta)
        if resp.status_code not in (200, 201):
            raise classify_http_error(resp, write=True, what="drive create folder")
        data = resp.json()
        return OpResult(external_id=data["id"], external_url=data.get("webViewLink"), kind="folder",
                        response={"name": data.get("name")})

    def _revision(self, doc_id: str) -> str | None:
        try:
            resp = self._req("GET", f"{DOCS_API}/documents/{quote(doc_id, safe='')}", write=False,
                             what="docs get revision", params={"fields": "documentId,revisionId"})
        except ProviderError:
            return None
        if resp.status_code != 200:
            return None
        return resp.json().get("revisionId")

    def _create_doc(self, ctx: OpContext) -> OpResult:
        docx = ctx.attachments.get("document.docx")
        if not docx:
            raise ProviderError("DOCX snapshot missing for create_doc")
        meta: dict[str, Any] = {"name": ctx.title, "mimeType": GDOC_MIME, "appProperties": self._app_props(ctx),
                                "description": f"DLY AEOS {ctx.logical_id} (project {ctx.project_key})"}
        parents = self._parents(ctx)
        if parents:
            meta["parents"] = parents
        boundary = "aeos-" + uuid.uuid4().hex
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
            f"--{boundary}\r\nContent-Type: {DOCX_MIME}\r\n\r\n"
        ).encode() + docx + f"\r\n--{boundary}--\r\n".encode()
        resp = self._req("POST", f"{UPLOAD_API}/files", write=True, what="drive upload docx",
                         params={"uploadType": "multipart", "fields": "id,name,webViewLink,mimeType"},
                         headers={"Content-Type": f"multipart/related; boundary={boundary}"}, content=body)
        if resp.status_code not in (200, 201):
            raise classify_http_error(resp, write=True, what="drive upload docx")
        data = resp.json()
        rev = self._revision(data["id"])
        return OpResult(external_id=data["id"], external_url=data.get("webViewLink"), external_revision=rev,
                        kind="doc", response={"name": data.get("name"), "mimeType": data.get("mimeType"),
                                              "revision_unknown": rev is None, "import": "docx->google-docs"})

    def _get_document(self, doc_id: str) -> dict[str, Any] | None:
        resp = self._req("GET", f"{DOCS_API}/documents/{quote(doc_id, safe='')}", write=False, what="docs get")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise classify_http_error(resp, write=False, what="docs get")
        return resp.json()

    def _update_doc(self, ctx: OpContext) -> OpResult:
        doc_id = ctx.existing_external_id
        if not doc_id:
            raise ProviderError("update_doc without an existing external id")
        current = self._get_document(doc_id)
        if current is None:
            raise ProviderError("document no longer exists (deleted/trashed outside AEOS)", retryable=False)
        observed = current.get("revisionId")
        if not ctx.existing_revision or observed != ctx.existing_revision:
            raise ProviderConflict(
                "document revision changed outside AEOS (human edit) — not overwritten"
                if ctx.existing_revision else "revision baseline unknown — cannot prove no human edit",
                expected_revision=ctx.existing_revision, observed_revision=observed,
                current_text=docs_plain_text(current)[:100_000])
        _text, reqs = markdown_to_docs_requests(ctx.payload.get("markdown", ""))
        end = _body_end(current)
        requests: list[dict[str, Any]] = []
        if end - 1 > 1:
            requests.append({"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end - 1}}})
        requests += reqs
        resp = self._req("POST", f"{DOCS_API}/documents/{quote(doc_id, safe='')}:batchUpdate", write=True,
                         what="docs batchUpdate",
                         json={"requests": requests, "writeControl": {"requiredRevisionId": ctx.existing_revision}})
        if resp.status_code == 409 or (resp.status_code == 400 and _is_revision_error(resp)):
            raise ProviderConflict("requiredRevisionId rejected — document changed concurrently; not overwritten",
                                   expected_revision=ctx.existing_revision, observed_revision=None)
        if resp.status_code != 200:
            raise classify_http_error(resp, write=True, what="docs batchUpdate")
        data = resp.json()
        new_rev = (data.get("writeControl") or {}).get("requiredRevisionId") or self._revision(doc_id)
        return OpResult(external_id=doc_id, external_url=f"https://docs.google.com/document/d/{doc_id}/edit",
                        external_revision=new_rev, kind="doc",
                        response={"replies": len(data.get("replies") or []), "mode": "docs-batchUpdate",
                                  "images_not_updated": "![" in ctx.payload.get("markdown", "")})

    # -- reconciliation (find by marker, never by title)
    def find_by_marker(self, project_id: str, logical_id: str) -> list[dict[str, Any]]:
        q = (f"appProperties has {{ key='aeos_logical_id' and value='{_q_escape(marker_value(logical_id))}' }} and "
             f"appProperties has {{ key='aeos_project_id' and value='{_q_escape(project_id)}' }} and trashed = false")
        resp = self._req("GET", f"{DRIVE_API}/files", write=False, what="drive reconcile list",
                         params={"q": q, "spaces": "drive", "pageSize": "10",
                                 "fields": "files(id,name,mimeType,webViewLink,createdTime,appProperties)"})
        if resp.status_code != 200:
            raise classify_http_error(resp, write=False, what="drive reconcile list")
        return list(resp.json().get("files") or [])

    def reconcile(self, ctx: OpContext) -> OpResult | None:
        if ctx.operation == "update_doc":
            return self._reconcile_update(ctx)
        files = self.find_by_marker(ctx.project_id, ctx.logical_id)
        if not files:
            return None
        files.sort(key=lambda f: f.get("createdTime") or "")
        f = files[0]
        rev = self._revision(f["id"]) if ctx.operation == "create_doc" else None
        return OpResult(external_id=f["id"], external_url=f.get("webViewLink"), external_revision=rev,
                        kind="folder" if f.get("mimeType") == FOLDER_MIME else "doc",
                        response={"reconciled": True, "duplicates": [x["id"] for x in files[1:]]})

    def _reconcile_update(self, ctx: OpContext) -> OpResult | None:
        current = self._get_document(ctx.existing_external_id or "")
        if current is None:
            raise ProviderError("document missing during reconcile", retryable=False)
        observed = current.get("revisionId")
        if observed == ctx.existing_revision:
            return None  # our update did not apply → safe to retry
        expected_text, _ = markdown_to_docs_requests(ctx.payload.get("markdown", ""))
        if docs_plain_text(current).strip() == expected_text.strip():
            return OpResult(external_id=ctx.existing_external_id or "", external_revision=observed, kind="doc",
                            response={"reconciled": True})
        raise ProviderConflict("document changed and does not match the AEOS update — treated as human edit",
                               expected_revision=ctx.existing_revision, observed_revision=observed,
                               current_text=docs_plain_text(current)[:100_000])

    def inspect(self, external_id: str) -> Inspection:
        current = self._get_document(external_id)
        if current is None:
            return Inspection(exists=False)
        return Inspection(exists=True, revision=current.get("revisionId"), text=docs_plain_text(current)[:100_000],
                          url=f"https://docs.google.com/document/d/{external_id}/edit")


def _is_revision_error(resp: httpx.Response) -> bool:
    try:
        err = resp.json().get("error") or {}
    except ValueError:
        return "revision" in resp.text.lower()
    return err.get("status") == "FAILED_PRECONDITION" or "revision" in str(err.get("message", "")).lower()
