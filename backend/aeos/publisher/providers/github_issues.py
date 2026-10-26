"""PERSONAL work tracking adapter: GitHub Issues (master §04, §13, §14).

* Token via the secret reference ``personal/github`` (store it with
  ``gh auth token | aeos secrets set personal/github --stdin``); never logged or returned.
* ``ensure_label``: labels ``aeos``, ``feature:<key>`` and optional ``milestone:<key>``.
* ``create_issue``: body = TaskSpec summary + acceptance criteria + links + hidden markers
  ``<!-- aeos:logical_id=... -->`` / ``<!-- aeos:project_id=... -->``.
* Reconciliation lists issues labelled ``aeos`` (``state=all``, the consistent list API — not the
  eventually-consistent search index) and matches the hidden marker. If absence cannot be proven
  within the page budget the op stays UNKNOWN instead of risking a duplicate.
* ``issue_state`` feeds ``OperationalWorkProjection``. Closed/Done on GitHub is an operational
  projection only and never unlocks runtime dependencies (§14 "Task Done di Jira/GitHub tetap status
  operasional").
* GitHub Projects (roadmap view) is optional in §04 and NOT implemented by this adapter.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from aeos.core.redaction import register_secret
from aeos.publisher.providers.base import (
    DEFAULT_TIMEOUT,
    OpContext,
    OpResult,
    ProviderBlocked,
    ProviderError,
    classify_http_error,
    refuse_ai_attribution,
    send,
)
from aeos.secrets import facade as secrets

GITHUB_API = "https://api.github.com"
TOKEN_REF = "personal/github"
CONNECT_REMEDY = "gh auth token | aeos secrets set personal/github --stdin"
RECONCILE_MAX_PAGES = 10
ACTOR = "publisher.github"
CAPABILITIES = {"issues": "SUPPORTED", "labels": "SUPPORTED", "milestone_labels": "SUPPORTED",
                "github_projects": "NOT_IMPLEMENTED"}


def marker(logical_id: str) -> str:
    return f"<!-- aeos:logical_id={logical_id} -->"


def project_marker(project_id: str) -> str:
    return f"<!-- aeos:project_id={project_id} -->"


class GitHubIssuesProvider:
    name = "github-issues"

    def __init__(self, *, repo: str, client: httpx.Client | None = None, token: str | None = None):
        if not repo or "/" not in repo:
            raise ProviderBlocked("GitHub repository (owner/name) is not configured for this project",
                                  remedy="hubungkan repository GitHub proyek di Portofolio", code="GITHUB_REPO_MISSING")
        self.repo = repo
        self._own_client = client is None
        self.client = client or httpx.Client(timeout=DEFAULT_TIMEOUT)
        self._token = token if token is not None else secrets.get_secret(
            TOKEN_REF, purpose="personal:publisher.github-issues", actor=ACTOR)
        register_secret(self._token)

    def close(self) -> None:
        if self._own_client:
            self.client.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    def _url(self, path: str) -> str:
        return f"{GITHUB_API}/repos/{self.repo}{path}"

    def execute(self, ctx: OpContext) -> OpResult:
        if ctx.operation == "ensure_label":
            return self._ensure_label(ctx)
        if ctx.operation == "create_issue":
            return self._create_issue(ctx)
        raise ProviderError(f"github adapter does not support operation {ctx.operation!r}")

    # -- labels
    def _get_label(self, name: str) -> dict[str, Any] | None:
        resp = send(self.client, "GET", self._url(f"/labels/{quote(name, safe='')}"), write=False,
                    what="github get label", headers=self._headers())
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise classify_http_error(resp, write=False, what="github get label")
        return resp.json()

    def _ensure_label(self, ctx: OpContext) -> OpResult:
        name = ctx.payload["name"]
        refuse_ai_attribution("GitHub label", {"name": name, "description": ctx.payload.get("description", "")})
        existing = self._get_label(name)
        if existing is not None:
            return OpResult(external_id=name, external_url=existing.get("url"), kind="label",
                            response={"existed": True})
        resp = send(self.client, "POST", self._url("/labels"), write=True, what="github create label",
                    headers=self._headers(), json={"name": name, "color": ctx.payload.get("color", "5b6270"),
                                                   "description": ctx.payload.get("description", "")[:100]})
        if resp.status_code == 422:  # already exists (race) — idempotent success
            return OpResult(external_id=name, kind="label", response={"existed": True})
        if resp.status_code != 201:
            raise classify_http_error(resp, write=True, what="github create label")
        return OpResult(external_id=name, external_url=resp.json().get("url"), kind="label", response={"created": True})

    # -- issues
    def _create_issue(self, ctx: OpContext) -> OpResult:
        body = ctx.payload.get("body", "")
        if marker(ctx.logical_id) not in body:
            body = f"{body}\n\n{marker(ctx.logical_id)}\n{project_marker(ctx.project_id)}\n"
        title = ctx.payload.get("title") or ctx.title
        # owner rule (AGENTS.md invariant 11): nothing with AI attribution lands on GitHub (refused, not stripped)
        refuse_ai_attribution("GitHub issue", {"title": title, "body": body})
        resp = send(self.client, "POST", self._url("/issues"), write=True, what="github create issue",
                    headers=self._headers(), json={"title": title, "body": body,
                                                   "labels": list(ctx.payload.get("labels", []))})
        if resp.status_code != 201:
            raise classify_http_error(resp, write=True, what="github create issue")
        data = resp.json()
        return OpResult(external_id=str(data["number"]), external_url=data.get("html_url"), kind="issue",
                        response={"state": data.get("state"), "node_id": data.get("node_id")})

    def find_issue_by_marker(self, project_id: str, logical_id: str) -> dict[str, Any] | None:
        want, pmark = marker(logical_id), project_marker(project_id)
        for page in range(1, RECONCILE_MAX_PAGES + 1):
            resp = send(self.client, "GET", self._url("/issues"), write=False, what="github reconcile list",
                        headers=self._headers(), params={"labels": "aeos", "state": "all", "per_page": "100",
                                                         "page": str(page), "sort": "created", "direction": "asc"})
            if resp.status_code != 200:
                raise classify_http_error(resp, write=False, what="github reconcile list")
            items = resp.json()
            for it in items:
                if "pull_request" in it:
                    continue
                body = it.get("body") or ""
                if want in body and pmark in body:
                    return it
            if len(items) < 100:
                return None
        raise ProviderError("reconcile page budget exhausted — absence not proven; operation stays UNKNOWN",
                            retryable=True)

    def reconcile(self, ctx: OpContext) -> OpResult | None:
        if ctx.operation == "ensure_label":
            label = self._get_label(ctx.payload["name"])
            return None if label is None else OpResult(external_id=ctx.payload["name"], kind="label",
                                                       response={"reconciled": True})
        if ctx.operation == "create_issue":
            it = self.find_issue_by_marker(ctx.project_id, ctx.logical_id)
            if it is None:
                return None
            return OpResult(external_id=str(it["number"]), external_url=it.get("html_url"), kind="issue",
                            response={"reconciled": True, "state": it.get("state")})
        raise ProviderError(f"cannot reconcile {ctx.operation}")

    def issue_state(self, number: str) -> dict[str, Any]:
        resp = send(self.client, "GET", self._url(f"/issues/{quote(str(number), safe='')}"), write=False,
                    what="github get issue", headers=self._headers())
        if resp.status_code != 200:
            raise classify_http_error(resp, write=False, what="github get issue")
        data = resp.json()
        state = data.get("state") or "UNKNOWN"
        if state == "closed" and data.get("state_reason"):
            state = f"closed:{data['state_reason']}"
        return {"external_status": state, "url": data.get("html_url"), "title": data.get("title", "")}
