"""OFFICE adapters: Confluence (docs), Jira (work items), Bitbucket (repo/PR references).

Master §04, §23, §29, S13-S15. These adapters are **disabled by default**: OFFICE data and writes
require a recorded company authorisation, enforced by the external-write interlock for every
operation, plus an explicit ``enabled: true`` on the project integration. They are implemented
against the documented Atlassian Cloud REST APIs and exercised only with mocked HTTP (respx) in this
build — real tenant behaviour is NOT-PROVEN (§51 P2).

* Atlassian Cloud vs Data Center is a capability check: Data Center → NOT SUPPORTED by this adapter
  (needs its own adapter); unknown deployment → BLOCKED until verified (§04).
* Confluence uses a real project folder under ``CONFLUENCE_PROJECTS_PARENT_ID`` (REST v2 folders,
  S15). If the tenant/parent does not support folders the run is BLOCKED (needs action) — the
  structure is never silently changed into pages.
* Content uses the storage format; images go as page attachments; raw Mermaid is never assumed to
  render (source is shown as a code block next to a "visual pending" note).
* Bitbucket is read-only here (repository/PR references). There is no merge operation (U-09).
"""

from __future__ import annotations

import base64
import html
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from aeos.core.hashing import sha256_text
from aeos.core.redaction import register_secret
from aeos.publisher.providers.base import (
    DEFAULT_TIMEOUT,
    OpContext,
    OpResult,
    ProviderBlocked,
    ProviderConflict,
    ProviderError,
    classify_http_error,
    send,
)
from aeos.secrets import facade as secrets

CONFLUENCE_PARENT_KEY = "CONFLUENCE_PROJECTS_PARENT_ID"  # canonical config name (§04); no legacy alias
ATLASSIAN_REF = "office/atlassian"
BITBUCKET_REF = "office/bitbucket"
BITBUCKET_API = "https://api.bitbucket.org/2.0"
ACTOR = "publisher.atlassian"
PROPERTY_KEY = "aeos"

DISABLED_REMEDY = ("OFFICE adapter nonaktif secara default. Perlu otorisasi perusahaan yang tercatat (Decision) "
                   "dan enabled=true pada integrasi proyek; deployment harus diverifikasi CLOUD.")


@dataclass
class AtlassianConfig:
    base_url: str = ""
    enabled: bool = False
    deployment: str = "UNKNOWN"  # CLOUD | DATA_CENTER | UNKNOWN
    space_id: str = ""
    parent_id: str = ""
    project_key: str = ""
    issue_type: str = "Task"
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_integration(cls, tenant_id: str | None, root_id: str | None, config: dict[str, Any] | None
                         ) -> AtlassianConfig:
        cfg = dict(config or {})
        return cls(
            base_url=str(cfg.get("base_url") or tenant_id or "").rstrip("/"),
            enabled=cfg.get("enabled") is True,
            deployment=str(cfg.get("deployment") or "UNKNOWN").upper(),
            space_id=str(cfg.get("space_id") or ""),
            parent_id=str(cfg.get(CONFLUENCE_PARENT_KEY) or root_id or ""),
            project_key=str(cfg.get("project_key") or ""),
            issue_type=str(cfg.get("issue_type") or "Task"),
            extra=cfg,
        )


def readiness(cfg: AtlassianConfig, *, product: str) -> tuple[str, list[str], str]:
    """Static readiness (no network): (status, reasons, remedy)."""
    if not cfg.enabled:
        return "BLOCKED", [f"{product} adapter disabled by default (OFFICE)"], DISABLED_REMEDY
    if cfg.deployment == "DATA_CENTER":
        return "NOT_SUPPORTED", [f"Atlassian Data Center is NOT SUPPORTED by this {product} adapter"], \
            "Data Center memerlukan adapter tersendiri dan capability check (§04)."
    if cfg.deployment != "CLOUD":
        return "BLOCKED", ["Atlassian deployment (Cloud vs Data Center) belum diverifikasi"], \
            "Verifikasi tenant lalu set deployment=CLOUD pada integrasi proyek."
    reasons = []
    if not cfg.base_url.startswith("https://"):
        reasons.append("base_url tenant (https://<site>.atlassian.net) belum dikonfigurasi")
    if product == "confluence":
        if not cfg.space_id:
            reasons.append("space_id Confluence belum dikonfigurasi")
        if not cfg.parent_id:
            reasons.append(f"{CONFLUENCE_PARENT_KEY} belum dikonfigurasi")
    if product == "jira" and not cfg.project_key:
        reasons.append("project_key Jira belum dikonfigurasi")
    if reasons:
        return "BLOCKED", reasons, "Lengkapi konfigurasi integrasi proyek (tanpa secret)."
    return "READY_TO_CHECK", [], ""


def _basic_auth(ref: str) -> str:
    raw = secrets.get_secret(ref, purpose="atlassian publication", actor=ACTOR)
    try:
        data = json.loads(raw)
        user = data.get("email") or data.get("username") or ""
        token = data.get("api_token") or data.get("app_password") or data.get("token") or ""
    except ValueError:
        user, _, token = raw.partition(":")
    if not user or not token:
        raise ProviderBlocked(f"credential {ref} is incomplete", remedy=f"aeos secrets set {ref}",
                              code="ATLASSIAN_CREDENTIAL_INVALID")
    register_secret(token)
    value = "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()
    register_secret(value.split(" ", 1)[1])
    return value


def _require_enabled(cfg: AtlassianConfig, product: str) -> None:
    status, reasons, remedy = readiness(cfg, product=product)
    if status == "NOT_SUPPORTED":
        raise ProviderBlocked("; ".join(reasons), remedy=remedy, code="ATLASSIAN_DC_NOT_SUPPORTED")
    if status != "READY_TO_CHECK":
        raise ProviderBlocked("; ".join(reasons), remedy=remedy, code="OFFICE_ADAPTER_DISABLED"
                              if not cfg.enabled else "ATLASSIAN_NOT_CONFIGURED")


# ---------------------------------------------------------------------------------------------
# Markdown → Confluence storage format (subset)

_INL = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\)|\*[^*\s][^*]*\*|_[^_\s][^_]*_)")
_BULLET_RE = re.compile(r"^\s*[-*]\s+")
_NUMBER_RE = re.compile(r"^\s*\d+[.)]\s+")


def _inline(text: str) -> str:
    out, pos = [], 0
    for m in _INL.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        tok = m.group(0)
        if tok.startswith("**"):
            out.append(f"<strong>{html.escape(tok[2:-2])}</strong>")
        elif tok.startswith("`"):
            out.append(f"<code>{html.escape(tok[1:-1])}</code>")
        elif tok.startswith("["):
            label, _, url = tok[1:-1].partition("](")
            if re.match(r"^(https?://|mailto:)", url.strip(), re.I):
                out.append(f'<a href="{html.escape(url.strip(), quote=True)}">{html.escape(label)}</a>')
            else:
                out.append(html.escape(label))
        else:
            out.append(f"<em>{html.escape(tok[1:-1])}</em>")
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def attachment_filename(key: str) -> str:
    return f"aeos-{re.sub(r'[^a-z0-9-]', '-', key.lower())}.png"


def markdown_to_storage(markdown: str, *, rendered_images: set[str] | None = None) -> str:
    rendered_images = rendered_images or set()
    lines = markdown.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s or s.startswith("<!--"):
            i += 1
            continue
        if s.startswith("```"):
            lang = s[3:].strip() or "text"
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            body = "\n".join(code).replace("]]>", "]]]]><![CDATA[>")
            title = "Source (Mermaid — tidak diasumsikan dirender oleh tenant)" if lang == "mermaid" else ""
            params = '<ac:parameter ac:name="language">text</ac:parameter>'
            if title:
                params += f'<ac:parameter ac:name="title">{html.escape(title)}</ac:parameter>'
            out.append(f'<ac:structured-macro ac:name="code">{params}<ac:plain-text-body><![CDATA[{body}]]>'
                       "</ac:plain-text-body></ac:structured-macro>")
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            n = len(m.group(1))
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
            i += 1
            continue
        im = re.match(r"^!\[([^\]]*)\]\(visual:([^)]+)\)$", s)
        if im:
            alt, key = im.group(1), im.group(2)
            if key in rendered_images:
                out.append(f'<p><ac:image ac:alt="{html.escape(alt, quote=True)}"><ri:attachment ri:filename="'
                           f'{attachment_filename(key)}"/></ac:image></p>')
            else:
                out.append(f"<p><em>[visual pending: {html.escape(alt)}]</em></p>")
            i += 1
            continue
        if s.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in re.split(r"(?<!\\)\|", lines[i].strip().strip("|"))]
                if not all(re.fullmatch(r":?-{3,}:?", c or "---") for c in cells):
                    rows.append(cells)
                i += 1
            if rows:
                head = "".join(f"<th>{_inline(c)}</th>" for c in rows[0])
                body = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows[1:])
                out.append(f"<table><tbody><tr>{head}</tr>{body}</tbody></table>")
            continue
        if _BULLET_RE.match(s):
            items = []
            while i < len(lines) and _BULLET_RE.match(lines[i]):
                items.append("<li>" + _inline(_BULLET_RE.sub("", lines[i], count=1)) + "</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        if _NUMBER_RE.match(s):
            items = []
            while i < len(lines) and _NUMBER_RE.match(lines[i]):
                items.append("<li>" + _inline(_NUMBER_RE.sub("", lines[i], count=1)) + "</li>")
                i += 1
            out.append("<ol>" + "".join(items) + "</ol>")
            continue
        if s.startswith(">"):
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip()[1:].strip())
                i += 1
            out.append("<blockquote><p>" + _inline(" ".join(q for q in quote_lines if q)) + "</p></blockquote>")
            continue
        out.append(f"<p>{_inline(s)}</p>")
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------------------------


class ConfluenceProvider:
    name = "confluence"

    def __init__(self, cfg: AtlassianConfig, *, client: httpx.Client | None = None, auth_header: str | None = None):
        _require_enabled(cfg, "confluence")
        self.cfg = cfg
        self._own_client = client is None
        self.client = client or httpx.Client(timeout=DEFAULT_TIMEOUT)
        self._auth = auth_header or _basic_auth(ATLASSIAN_REF)
        self._caps: dict[str, Any] | None = None

    def close(self) -> None:
        if self._own_client:
            self.client.close()

    def _h(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {"Authorization": self._auth, "Accept": "application/json", **(extra or {})}

    def _v2(self, path: str) -> str:
        return f"{self.cfg.base_url}/wiki/api/v2{path}"

    def check_capabilities(self) -> dict[str, Any]:
        """Cloud REST v2 + folder support under CONFLUENCE_PROJECTS_PARENT_ID (read-only probes)."""
        if self._caps is not None:
            return self._caps
        caps: dict[str, Any] = {"deployment": self.cfg.deployment, "api_v2": "UNKNOWN", "folders": "UNKNOWN",
                                "status": "BLOCKED", "reasons": []}
        sp = send(self.client, "GET", self._v2(f"/spaces/{quote(self.cfg.space_id, safe='')}"), write=False,
                  what="confluence space", headers=self._h())
        if sp.status_code != 200:
            caps["api_v2"] = "NOT_AVAILABLE" if sp.status_code == 404 else "UNKNOWN"
            caps["reasons"].append(f"space/API v2 tidak tersedia (HTTP {sp.status_code}); Data Center tidak didukung")
            self._caps = caps
            return caps
        caps["api_v2"] = "AVAILABLE"
        fo = send(self.client, "GET", self._v2(f"/folders/{quote(self.cfg.parent_id, safe='')}"), write=False,
                  what="confluence parent folder", headers=self._h())
        if fo.status_code == 200:
            caps["folders"] = "SUPPORTED"
            caps["status"] = "READY"
        elif fo.status_code in (400, 404, 501):
            pg = send(self.client, "GET", self._v2(f"/pages/{quote(self.cfg.parent_id, safe='')}"), write=False,
                      what="confluence parent page", headers=self._h())
            caps["folders"] = "NOT_SUPPORTED" if fo.status_code in (400, 501) else "UNKNOWN"
            if pg.status_code == 200:
                caps["reasons"].append(f"{CONFLUENCE_PARENT_KEY} adalah page, bukan folder; dukungan folder tenant "
                                       "belum terbukti — needs action (struktur tidak diganti diam-diam)")
            else:
                caps["reasons"].append(f"{CONFLUENCE_PARENT_KEY} tidak ditemukan / tidak dapat diakses")
        else:
            caps["reasons"].append(f"pemeriksaan folder gagal (HTTP {fo.status_code})")
        self._caps = caps
        return caps

    def _ensure_ready(self) -> None:
        caps = self.check_capabilities()
        if caps["status"] != "READY":
            raise ProviderBlocked("; ".join(caps["reasons"]) or "Confluence capability not ready",
                                  remedy="Sediakan project parent berupa folder Confluence yang valid; verifikasi "
                                         "tenant Cloud dan izin publisher.", code="CONFLUENCE_FOLDERS_UNSUPPORTED")

    def _set_property(self, kind: str, content_id: str, ctx: OpContext) -> bool:
        """Best effort: the resource already exists, so a failure here must not turn into UNKNOWN."""
        try:
            resp = send(self.client, "POST", self._v2(f"/{kind}/{quote(content_id, safe='')}/properties"),
                        write=True, what="confluence content property",
                        headers=self._h({"Content-Type": "application/json"}),
                        json={"key": PROPERTY_KEY,
                              "value": {"logical_id": ctx.logical_id, "project_id": ctx.project_id}})
        except ProviderError:
            return False
        return resp.status_code in (200, 201)

    def execute(self, ctx: OpContext) -> OpResult:
        self._ensure_ready()
        if ctx.operation == "create_folder":
            parent = ctx.parent_external_id or self.cfg.parent_id
            resp = send(self.client, "POST", self._v2("/folders"), write=True, what="confluence create folder",
                        headers=self._h({"Content-Type": "application/json"}),
                        json={"spaceId": self.cfg.space_id, "title": ctx.title, "parentId": parent})
            if resp.status_code not in (200, 201):
                raise classify_http_error(resp, write=True, what="confluence create folder")
            data = resp.json()
            prop = self._set_property("folders", str(data["id"]), ctx)
            return OpResult(external_id=str(data["id"]), external_url=_link(self.cfg, data), kind="folder",
                            response={"marker_property": prop})
        if ctx.operation == "create_doc":
            storage = markdown_to_storage(ctx.payload.get("markdown", ""),
                                          rendered_images=set(ctx.payload.get("rendered_visuals", [])))
            resp = send(self.client, "POST", self._v2("/pages"), write=True, what="confluence create page",
                        headers=self._h({"Content-Type": "application/json"}),
                        json={"spaceId": self.cfg.space_id, "status": "current", "title": ctx.title,
                              "parentId": ctx.parent_external_id or self.cfg.parent_id,
                              "body": {"representation": "storage", "value": storage}})
            if resp.status_code not in (200, 201):
                raise classify_http_error(resp, write=True, what="confluence create page")
            data = resp.json()
            prop = self._set_property("pages", str(data["id"]), ctx)
            return OpResult(external_id=str(data["id"]), external_url=_link(self.cfg, data),
                            external_revision=str((data.get("version") or {}).get("number", 1)), kind="page",
                            response={"marker_property": prop})
        if ctx.operation == "update_doc":
            return self._update_page(ctx)
        if ctx.operation == "upload_attachment":
            page = ctx.parent_external_id
            name = ctx.payload["filename"]
            blob = ctx.attachments.get(name)
            if not page or blob is None:
                raise ProviderError("attachment page/content missing")
            resp = send(self.client, "PUT", f"{self.cfg.base_url}/wiki/rest/api/content/{quote(page, safe='')}"
                        "/child/attachment", write=True, what="confluence attachment",
                        headers={"Authorization": self._auth, "X-Atlassian-Token": "no-check"},
                        files={"file": (name, blob, "image/png")}, data={"minorEdit": "true"})
            if resp.status_code not in (200, 201):
                raise classify_http_error(resp, write=True, what="confluence attachment")
            results = resp.json().get("results") or [resp.json()]
            return OpResult(external_id=str(results[0].get("id", name)), kind="attachment",
                            response={"filename": name})
        raise ProviderError(f"confluence adapter does not support operation {ctx.operation!r}")

    def _update_page(self, ctx: OpContext) -> OpResult:
        pid = ctx.existing_external_id or ""
        cur = send(self.client, "GET", self._v2(f"/pages/{quote(pid, safe='')}"), write=False,
                   what="confluence get page", headers=self._h(), params={"body-format": "storage"})
        if cur.status_code != 200:
            raise classify_http_error(cur, write=False, what="confluence get page")
        data = cur.json()
        number = int((data.get("version") or {}).get("number", 0))
        if not ctx.existing_revision or str(number) != str(ctx.existing_revision):
            body = ((data.get("body") or {}).get("storage") or {}).get("value", "")
            raise ProviderConflict("Confluence page version changed outside AEOS (human edit) — not overwritten",
                                   expected_revision=ctx.existing_revision, observed_revision=str(number),
                                   current_text=re.sub(r"<[^>]+>", " ", body)[:100_000])
        storage = markdown_to_storage(ctx.payload.get("markdown", ""),
                                      rendered_images=set(ctx.payload.get("rendered_visuals", [])))
        resp = send(self.client, "PUT", self._v2(f"/pages/{quote(pid, safe='')}"), write=True,
                    what="confluence update page", headers=self._h({"Content-Type": "application/json"}),
                    json={"id": pid, "status": "current", "title": ctx.title,
                          "body": {"representation": "storage", "value": storage},
                          "version": {"number": number + 1, "message": f"DLY AEOS publication {ctx.publication_id}"}})
        if resp.status_code == 409:
            raise ProviderConflict("Confluence version conflict — not overwritten",
                                   expected_revision=ctx.existing_revision, observed_revision=None)
        if resp.status_code != 200:
            raise classify_http_error(resp, write=True, what="confluence update page")
        out = resp.json()
        return OpResult(external_id=pid, external_url=_link(self.cfg, out),
                        external_revision=str((out.get("version") or {}).get("number", number + 1)), kind="page")

    def _marker_matches(self, kind: str, content_id: str, ctx: OpContext) -> bool:
        resp = send(self.client, "GET", self._v2(f"/{kind}/{quote(content_id, safe='')}/properties"), write=False,
                    what="confluence properties", headers=self._h(), params={"key": PROPERTY_KEY})
        if resp.status_code != 200:
            return False
        for prop in resp.json().get("results") or []:
            val = prop.get("value") or {}
            if val.get("logical_id") == ctx.logical_id and val.get("project_id") == ctx.project_id:
                return True
        return False

    def reconcile(self, ctx: OpContext) -> OpResult | None:
        if ctx.operation == "update_doc":
            pid = ctx.existing_external_id or ""
            cur = send(self.client, "GET", self._v2(f"/pages/{quote(pid, safe='')}"), write=False,
                       what="confluence reconcile update", headers=self._h())
            if cur.status_code != 200:
                raise classify_http_error(cur, write=False, what="confluence reconcile update")
            ver = cur.json().get("version") or {}
            number = str(ver.get("number", ""))
            if number == str(ctx.existing_revision):
                return None  # our update did not apply → safe to retry
            if ctx.publication_id in str(ver.get("message", "")) and ctx.existing_revision is not None and \
                    number == str(int(ctx.existing_revision) + 1):
                return OpResult(external_id=pid, external_revision=number, kind="page", response={"reconciled": True})
            raise ProviderConflict("page version changed and is not the AEOS update — treated as human edit",
                                   expected_revision=ctx.existing_revision, observed_revision=number)
        if ctx.operation == "create_doc":
            resp = send(self.client, "GET", self._v2("/pages"), write=False, what="confluence reconcile pages",
                        headers=self._h(), params={"space-id": self.cfg.space_id, "title": ctx.title,
                                                   "status": "current"})
            if resp.status_code != 200:
                raise classify_http_error(resp, write=False, what="confluence reconcile pages")
            for page in resp.json().get("results") or []:
                if self._marker_matches("pages", str(page["id"]), ctx):
                    return OpResult(external_id=str(page["id"]), external_url=_link(self.cfg, page),
                                    external_revision=str((page.get("version") or {}).get("number", 1)), kind="page",
                                    response={"reconciled": True})
                raise ProviderError("a page with this title exists but has no AEOS marker — needs human review",
                                    retryable=False)
            return None
        if ctx.operation == "create_folder":
            parent = ctx.parent_external_id or self.cfg.parent_id
            resp = send(self.client, "GET", self._v2(f"/folders/{quote(parent, safe='')}/direct-children"), write=False,
                        what="confluence reconcile folder", headers=self._h(), params={"limit": "250"})
            if resp.status_code != 200:
                raise classify_http_error(resp, write=False, what="confluence reconcile folder")
            for child in resp.json().get("results") or []:
                if child.get("type") != "folder" or child.get("title") != ctx.title:
                    continue
                if self._marker_matches("folders", str(child["id"]), ctx):
                    return OpResult(external_id=str(child["id"]), kind="folder", response={"reconciled": True})
                raise ProviderError("a folder with this title exists without an AEOS marker — needs human review",
                                    retryable=False)
            return None
        if ctx.operation == "upload_attachment":
            page = ctx.parent_external_id or ""
            resp = send(self.client, "GET", f"{self.cfg.base_url}/wiki/rest/api/content/{quote(page, safe='')}"
                        "/child/attachment", write=False, what="confluence reconcile attachment", headers=self._h(),
                        params={"filename": ctx.payload.get("filename", "")})
            if resp.status_code != 200:
                raise classify_http_error(resp, write=False, what="confluence reconcile attachment")
            res = resp.json().get("results") or []
            return OpResult(external_id=str(res[0]["id"]), kind="attachment", response={"reconciled": True}) \
                if res else None
        raise ProviderError(f"cannot reconcile {ctx.operation}")


def _link(cfg: AtlassianConfig, data: dict[str, Any]) -> str | None:
    webui = ((data.get("_links") or {}).get("webui"))
    return f"{cfg.base_url}/wiki{webui}" if webui else None


def jira_marker_label(project_id: str, logical_id: str) -> str:
    return "aeos-" + sha256_text(f"{project_id}:{logical_id}")[:16]


def _adf(text: str) -> dict[str, Any]:
    content = [{"type": "paragraph", "content": [{"type": "text", "text": line[:30000]}]}
               for line in text.splitlines() if line.strip() and not line.strip().startswith("<!--")]
    return {"type": "doc", "version": 1, "content": content or [{"type": "paragraph", "content": []}]}


class JiraProvider:
    name = "jira"

    def __init__(self, cfg: AtlassianConfig, *, client: httpx.Client | None = None, auth_header: str | None = None):
        _require_enabled(cfg, "jira")
        self.cfg = cfg
        self._own_client = client is None
        self.client = client or httpx.Client(timeout=DEFAULT_TIMEOUT)
        self._auth = auth_header or _basic_auth(ATLASSIAN_REF)

    def close(self) -> None:
        if self._own_client:
            self.client.close()

    def _h(self) -> dict[str, str]:
        return {"Authorization": self._auth, "Accept": "application/json", "Content-Type": "application/json"}

    def execute(self, ctx: OpContext) -> OpResult:
        if ctx.operation != "create_issue":
            raise ProviderError(f"jira adapter does not support operation {ctx.operation!r}")
        label = jira_marker_label(ctx.project_id, ctx.logical_id)
        fields = {"project": {"key": self.cfg.project_key}, "summary": (ctx.payload.get("title") or ctx.title)[:250],
                  "issuetype": {"name": self.cfg.issue_type}, "labels": ["aeos", label],
                  "description": _adf(ctx.payload.get("body", ""))}
        resp = send(self.client, "POST", f"{self.cfg.base_url}/rest/api/3/issue", write=True,
                    what="jira create issue", headers=self._h(), json={"fields": fields})
        if resp.status_code != 201:
            raise classify_http_error(resp, write=True, what="jira create issue")
        data = resp.json()
        return OpResult(external_id=data["key"], external_url=f"{self.cfg.base_url}/browse/{data['key']}",
                        kind="issue", response={"id": data.get("id"), "marker_label": label})

    def reconcile(self, ctx: OpContext) -> OpResult | None:
        label = jira_marker_label(ctx.project_id, ctx.logical_id)
        jql = f'project = "{self.cfg.project_key}" AND labels = "{label}"'
        resp = send(self.client, "POST", f"{self.cfg.base_url}/rest/api/3/search/jql", write=False,
                    what="jira reconcile", headers=self._h(),
                    json={"jql": jql, "fields": ["summary", "labels"], "maxResults": 5})
        if resp.status_code != 200:
            raise classify_http_error(resp, write=False, what="jira reconcile")
        issues = resp.json().get("issues") or []
        if not issues:
            return None
        key = issues[0]["key"]
        return OpResult(external_id=key, external_url=f"{self.cfg.base_url}/browse/{key}", kind="issue",
                        response={"reconciled": True})

    def issue_state(self, key: str) -> dict[str, Any]:
        resp = send(self.client, "GET", f"{self.cfg.base_url}/rest/api/3/issue/{quote(key, safe='')}", write=False,
                    what="jira get issue", headers=self._h(), params={"fields": "status,summary"})
        if resp.status_code != 200:
            raise classify_http_error(resp, write=False, what="jira get issue")
        f = resp.json().get("fields") or {}
        status = (f.get("status") or {}).get("name") or "UNKNOWN"
        return {"external_status": status, "url": f"{self.cfg.base_url}/browse/{key}", "title": f.get("summary", "")}


class BitbucketProvider:
    """Read-only repository / pull-request references for OFFICE projects (no merge, U-09)."""

    name = "bitbucket"

    def __init__(self, *, full_name: str, enabled: bool, client: httpx.Client | None = None,
                 auth_header: str | None = None):
        if not enabled:
            raise ProviderBlocked("Bitbucket adapter disabled by default (OFFICE)", remedy=DISABLED_REMEDY,
                                  code="OFFICE_ADAPTER_DISABLED")
        if "/" not in full_name:
            raise ProviderBlocked("Bitbucket repository must be workspace/repo_slug", code="BITBUCKET_REPO_INVALID")
        self.workspace, self.slug = full_name.split("/", 1)
        self._own_client = client is None
        self.client = client or httpx.Client(timeout=DEFAULT_TIMEOUT)
        self._auth = auth_header or _basic_auth(BITBUCKET_REF)

    def close(self) -> None:
        if self._own_client:
            self.client.close()

    def _url(self, path: str = "") -> str:
        return f"{BITBUCKET_API}/repositories/{quote(self.workspace, safe='')}/{quote(self.slug, safe='')}{path}"

    def repository_reference(self) -> dict[str, Any]:
        resp = send(self.client, "GET", self._url(), write=False, what="bitbucket repository",
                    headers={"Authorization": self._auth, "Accept": "application/json"})
        if resp.status_code != 200:
            raise classify_http_error(resp, write=False, what="bitbucket repository")
        d = resp.json()
        return {"full_name": d.get("full_name"), "uuid": d.get("uuid"),
                "url": ((d.get("links") or {}).get("html") or {}).get("href"),
                "main_branch": (d.get("mainbranch") or {}).get("name"), "is_private": d.get("is_private")}

    def pull_request_references(self, state: str = "OPEN") -> list[dict[str, Any]]:
        resp = send(self.client, "GET", self._url("/pullrequests"), write=False, what="bitbucket pull requests",
                    headers={"Authorization": self._auth, "Accept": "application/json"}, params={"state": state})
        if resp.status_code != 200:
            raise classify_http_error(resp, write=False, what="bitbucket pull requests")
        out = []
        for pr in resp.json().get("values") or []:
            out.append({"id": pr.get("id"), "title": pr.get("title"), "state": pr.get("state"),
                        "source": ((pr.get("source") or {}).get("branch") or {}).get("name"),
                        "destination": ((pr.get("destination") or {}).get("branch") or {}).get("name"),
                        "url": ((pr.get("links") or {}).get("html") or {}).get("href")})
        return out
