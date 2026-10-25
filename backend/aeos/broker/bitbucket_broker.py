"""Trusted Bitbucket Cloud broker for OFFICE delivery (§04, §26, §31; ADR-0111).

The same boundary as the GitHub path (``aeos.broker.git_broker``, every rule tested):

* only the run's runtime-recorded head is pushed, sha-pinned ``<sha>:refs/heads/aeos/<...>`` from a
  broker-private bare mirror - never forced, no deletion, no tags, never the main/protected branch;
* OFFICE only: the credential is ``office/bitbucket`` (secrets purpose ``office:broker.bitbucket``), given
  to one git invocation through ``GIT_CONFIG_COUNT`` or to one HTTP request; never to a PERSONAL run;
* fail closed before any credential or network call: the project's ``bitbucket/code`` integration is
  enabled (ADR-0073 disabled by default), the deployment is Bitbucket **Cloud** (Data Center ->
  ``BITBUCKET_DC_NOT_SUPPORTED``, §04), the repository is registered and its exact identity VERIFIED;
* every remote write needs an ACTIVE run-bound capability grant (GIT_BRANCH / PR_CREATE) **and** the
  external-write interlock: the company authorisation POLICY decision listing ``bitbucket`` (OFFICE default
  deny, §23), write enablement, and the human APPROVAL of the run's plan version that allows the operation
  on exactly this repository;
* PR creation is one ``POST /pullrequests`` (draft, ``close_source_branch: false``) behind an idempotent
  outbox row with read-only reconciliation. The broker has no operation that integrates, approves or
  declines a pull request: that stays a human action (U-09).

Exercised with mocked HTTP (respx) and local bare remotes; live Bitbucket Cloud behaviour is NOT-PROVEN
until the company authorises a tenant (M5).
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.broker.git_broker import (
    _branch_updated,
    _interlock,
    _is_local_remote,
    _ls_remote_head,
    _require_grant,
    _require_publishable,
    _side_effect,
    push_from_mirror,
    validate_branch,
    validate_remote_url,
)
from aeos.core.errors import Blocked, PolicyDenied, ValidationFailed
from aeos.core.redaction import redact_text, register_secret
from aeos.events import service as events
from aeos.models.broker_ext import BrokerSideEffect
from aeos.models.portfolio import ProjectIntegration, RepositoryReference
from aeos.models.workforce import WorkerRun

PROVIDER = "bitbucket"
BITBUCKET_API = "https://api.bitbucket.org/2.0"
BITBUCKET_GIT = "https://bitbucket.org"
SECRET_REF = "office/bitbucket"
SECRET_PURPOSE = "office:broker.bitbucket"
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,61}/[A-Za-z0-9._-]{1,100}$")
_BASE_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
PR_STATES_ALL = ("OPEN", "MERGED", "DECLINED", "SUPERSEDED")
# HTTP users Bitbucket Cloud documents for token-based Git over HTTPS.
ACCESS_TOKEN_GIT_USER = "x-token-auth"
API_TOKEN_GIT_USER = "x-bitbucket-api-token-auth"


def validate_repo(repo_full_name: str) -> str:
    if not isinstance(repo_full_name, str) or not _REPO_RE.match(repo_full_name) or ".." in repo_full_name:
        raise ValidationFailed("Bitbucket repository must be workspace/repo_slug")
    return repo_full_name


def bitbucket_remote(repo_full_name: str) -> str:
    return f"{BITBUCKET_GIT}/{validate_repo(repo_full_name)}.git"


def pinned_remote(repo_full_name: str, remote_url: str | None) -> str:
    """The remote a Bitbucket push/ls-remote may use. The ``office/bitbucket`` credential only ever goes to the
    repository's own Bitbucket Cloud URL: any other https host or repository is refused before the credential is
    read (no exfiltration through a caller-chosen ``remote_url``). A local bare remote receives no credential."""
    expected = bitbucket_remote(repo_full_name)
    url = validate_remote_url(remote_url or expected)
    if _is_local_remote(url):
        return url

    def norm(u: str) -> str:
        u = u.strip().rstrip("/").lower()
        return u[:-4] if u.endswith(".git") else u

    if norm(url) != norm(expected):
        raise PolicyDenied("Bitbucket remote must be the repository's own Bitbucket Cloud URL "
                           f"({expected}); the OFFICE credential is never sent to another host or repository",
                           code="REMOTE_REJECTED")
    return url


# ----------------------------------------------------------------------------- credential
@dataclass(frozen=True)
class BitbucketCredential:
    api_header: str  # value of the Authorization header for the REST API
    git_user: str  # HTTP Basic user for Git over HTTPS
    git_secret: str

    def __repr__(self) -> str:  # never print the secret
        return f"BitbucketCredential(git_user={self.git_user!r})"


def parse_credential(raw: str) -> BitbucketCredential:
    """``office/bitbucket`` shapes: ``{"access_token"}`` (repository/workspace access token: API Bearer, git
    ``x-token-auth``), ``{"email", "api_token"[, "git_username"]}`` (Atlassian API token: API Basic email:token,
    git ``x-bitbucket-api-token-auth`` unless ``git_username`` is given), legacy ``{"username", "app_password"}``
    or ``user:secret`` (Basic for both). Anything else is refused (fail closed)."""
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get("access_token"):
        token = str(data["access_token"])
        register_secret(token)
        return BitbucketCredential(f"Bearer {token}", ACCESS_TOKEN_GIT_USER, token)
    if isinstance(data, dict) and data.get("api_token") and data.get("email"):
        token = str(data["api_token"])
        user = str(data.get("git_username") or API_TOKEN_GIT_USER)
        api_user = str(data["email"])
    elif isinstance(data, dict) and data.get("app_password") and data.get("username"):
        token, user = str(data["app_password"]), str(data["username"])
        api_user = user
    elif data is None and ":" in raw:
        user, _, token = raw.partition(":")
        api_user = user
    else:
        raise Blocked(f"credential {SECRET_REF} is incomplete (access_token, email+api_token or "
                      "username+app_password)", code="BITBUCKET_CREDENTIAL_INVALID")
    if not user or not token:
        raise Blocked(f"credential {SECRET_REF} is incomplete", code="BITBUCKET_CREDENTIAL_INVALID")
    register_secret(token)
    basic = base64.b64encode(f"{api_user}:{token}".encode()).decode()
    register_secret(basic)
    return BitbucketCredential(f"Basic {basic}", user, token)


def _credential() -> BitbucketCredential:
    from aeos.secrets import facade as secrets

    return parse_credential(secrets.get_secret(SECRET_REF, purpose=SECRET_PURPOSE, actor="service:broker"))


# ----------------------------------------------------------------------------- OFFICE target (fail closed)
def office_target(session: Session, run: WorkerRun, repo_full_name: str) -> RepositoryReference:
    """The exact, verified Bitbucket Cloud repository of this OFFICE run - checked before any credential is read."""
    if run.domain != "OFFICE":
        raise PolicyDenied("Bitbucket credentials serve OFFICE runs only", code="DOMAIN_CREDENTIAL")
    validate_repo(repo_full_name)
    ref = session.scalar(select(RepositoryReference).where(RepositoryReference.project_id == run.project_id,
                                                           RepositoryReference.full_name == repo_full_name))
    if ref is None or (ref.provider or "").upper() != "BITBUCKET":
        raise PolicyDenied("repository is not a registered Bitbucket repository of this project",
                           code="REPO_NOT_REGISTERED")
    integ = session.scalar(select(ProjectIntegration).where(
        ProjectIntegration.project_id == run.project_id, ProjectIntegration.provider == PROVIDER,
        ProjectIntegration.purpose == "code").order_by(ProjectIntegration.created_at.desc()).limit(1))
    cfg = dict(integ.config or {}) if integ is not None else {}
    if integ is None or cfg.get("enabled") is not True:
        raise Blocked("Bitbucket adapter disabled by default (OFFICE): integration bitbucket/code with "
                      "config.enabled=true required (ADR-0073)", code="OFFICE_ADAPTER_DISABLED")
    if not integ.root_id or integ.root_id.strip().lower() != repo_full_name.lower():
        raise PolicyDenied("repository differs from the project's configured Bitbucket integration root "
                           "(BITBUCKET_REPOSITORY)", code="TARGET_MISMATCH")
    deployment = str(cfg.get("deployment") or "").upper()
    try:
        from aeos.policy import facade as policy

        policy_deployment = str(policy.office_policy(session).get("atlassian_deployment") or "").upper()
    except ImportError as exc:
        raise Blocked("policy module unavailable (fail closed)", code="INTERLOCK_UNAVAILABLE") from exc
    if "DATA_CENTER" in (deployment, policy_deployment):
        raise Blocked("Bitbucket Data Center is NOT SUPPORTED by this adapter (Cloud only, §04)",
                      code="BITBUCKET_DC_NOT_SUPPORTED")
    if (deployment or policy_deployment) != "CLOUD":
        raise Blocked("Bitbucket deployment (Cloud vs Data Center) is not verified", code="BITBUCKET_DEPLOYMENT_UNKNOWN")
    if ref.verification_status != "VERIFIED":
        raise Blocked("the Bitbucket repository identity is not VERIFIED (portfolio verify)", code="REPO_NOT_VERIFIED")
    return ref


# ----------------------------------------------------------------------------- push
def push_task_branch(session: Session, *, run: WorkerRun, worktree: str, branch: str, repo_full_name: str,
                     remote_url: str | None = None, expected_head: str | None = None) -> dict[str, Any]:
    """Sha-pinned push of the run's runtime head to ``refs/heads/aeos/*`` on Bitbucket Cloud (never forced).

    Returns ``status`` PUSHED | FAILED | UNKNOWN with ``head_sha`` like the GitHub path (FAILED/UNKNOWN are
    returned, not raised, so the outbox row and audit record persist with the caller's commit)."""
    from aeos.core.config import get_settings

    ref = office_target(session, run, repo_full_name)
    validate_branch(branch, default_branch=ref.default_branch or "main")
    if run.branch and branch != run.branch:
        raise PolicyDenied("branch does not belong to this run", code="BRANCH_NOT_ALLOWED")
    if not run.workspace_ref or Path(worktree).resolve() != Path(run.workspace_ref).resolve():
        raise PolicyDenied("worktree does not belong to this run", code="WORKTREE_MISMATCH")
    _require_publishable(session, run, "bitbucket.push_branch")
    if expected_head and expected_head != run.head_commit:
        raise PolicyDenied("expected head differs from the runtime-recorded head", code="HEAD_MISMATCH")
    expected_head = run.head_commit
    source = Path(run.git_dir_ref) if run.git_dir_ref and Path(run.git_dir_ref).is_dir() else Path(worktree)
    url = pinned_remote(repo_full_name, remote_url)
    local = _is_local_remote(url)
    target = {"provider": PROVIDER, "repo": repo_full_name, "branch": branch, "remote": "local" if local else url,
              "remote_url": url, "expected_head": expected_head}
    grant = _require_grant(session, run, "GIT_BRANCH", {"repo": repo_full_name, "branch": branch})
    cred: BitbucketCredential | None = None
    if not local:
        _interlock(session, run, provider=PROVIDER, operation="push_branch", target=target, grant=grant)
        cred = _credential()
    se = _side_effect(session, run, kind="PUSH_BRANCH", key=f"bbpush:{repo_full_name}:{branch}:{expected_head or ''}",
                      target=target)
    work_root = get_settings().layout.root / "broker" / "git"
    work_root.mkdir(parents=True, exist_ok=True)
    res = push_from_mirror(source_repo=source, branch=branch, remote_url=url, token=cred.git_secret if cred else None,
                           work_root=work_root, expected_head=expected_head,
                           basic_user=cred.git_user if cred else ACCESS_TOKEN_GIT_USER)
    se.status = "SUCCEEDED" if res["ok"] else ("UNKNOWN" if res.get("unknown") else "FAILED")
    se.commit_sha = res.get("commit")
    se.result_summary = (res.get("output") or res.get("error") or "")[:500]
    audit.record(session, actor_type="service", actor_id="broker", action="broker.bitbucket.push_branch",
                 result="SUCCESS" if res["ok"] else "FAILED", target_type="run", target_id=run.id,
                 project_id=run.project_id, domain=run.domain, reason=res.get("error", ""),
                 details={"repo": repo_full_name, "branch": branch, "commit": res.get("commit"), "stage": res["stage"],
                          "provider": PROVIDER})
    events.emit(session, source="broker", kind="run.side_effect", entity_type="run", entity_id=run.id, run_id=run.id,
                project_id=run.project_id, domain=run.domain,
                payload={"kind": "PUSH_BRANCH", "status": se.status, "branch": branch, "commit": res.get("commit"),
                         "provider": PROVIDER})
    head_sha = res.get("commit") or expected_head
    hook = _branch_updated(session, repo_full_name, branch, head_sha) if res["ok"] and head_sha else "NOT_CALLED"
    return {"status": "PUSHED" if res["ok"] else se.status, "ok": bool(res["ok"]), "branch": branch,
            "commit": res.get("commit"), "head_sha": head_sha, "side_effect_id": se.id, "remote": target["remote"],
            "pr_observation": hook, "provider": PROVIDER,
            "error": "" if res["ok"] else f"{res['stage']}: {res.get('error', '')}"[:300]}


# ----------------------------------------------------------------------------- pull request
def _pr_url(api_base: str, repo: str) -> str:
    ws, slug = repo.split("/", 1)
    return f"{api_base}/repositories/{quote(ws, safe='')}/{quote(slug, safe='')}/pullrequests"


def _find_pr(client: httpx.Client, api_base: str, repo: str, head: str, base: str, headers: dict[str, str],
             *, states: tuple[str, ...] = ("OPEN",)) -> dict[str, Any] | None:
    """Read-only: the PR from *head* into *base* (head/base are validated names without quotes)."""
    params: list[tuple[str, str | int | float | bool | None]] = [
        ("q", f'source.branch.name="{head}" AND destination.branch.name="{base}"'), ("pagelen", "10")]
    params += [("state", s) for s in states]
    r = client.get(_pr_url(api_base, repo), params=params, headers=headers)
    if r.status_code != 200:
        raise httpx.HTTPStatusError(f"Bitbucket {r.status_code}", request=r.request, response=r)
    values = (r.json() or {}).get("values") or []
    return values[0] if values and isinstance(values[0], dict) else None


def _pr_link(pr: dict[str, Any]) -> str:
    return str(((pr.get("links") or {}).get("html") or {}).get("href") or "")


def create_pull_request(session: Session, *, run: WorkerRun, repo_full_name: str, head: str, base: str, title: str,
                        body: str, api_base: str = BITBUCKET_API) -> dict[str, Any]:
    """Open a draft Bitbucket PR ``head -> base`` for the run (idempotent outbox; never integrates it)."""
    ref = office_target(session, run, repo_full_name)
    validate_branch(head, default_branch=ref.default_branch or "main")
    if base.startswith("aeos/") or not _BASE_RE.match(base) or ".." in base:
        raise PolicyDenied("invalid PR base", code="PR_BASE_REJECTED")
    if run.branch and head != run.branch:
        raise PolicyDenied("PR head does not belong to this run", code="BRANCH_NOT_ALLOWED")
    _require_publishable(session, run, "bitbucket.create_pr")
    target = {"provider": PROVIDER, "repo": repo_full_name, "head": head, "base": base}
    grant = _require_grant(session, run, "PR_CREATE", {"repo": repo_full_name, "head": head, "base": base})
    _interlock(session, run, provider=PROVIDER, operation="create_pr", target=target, grant=grant)
    key = f"bbpr:{repo_full_name}:{head}:{base}"
    existing = session.scalar(select(BrokerSideEffect).where(BrokerSideEffect.idempotency_key == key))
    if existing is not None and existing.status == "SUCCEEDED":
        audit.record(session, actor_type="service", actor_id="broker", action="broker.bitbucket.create_pr",
                     result="SUCCESS", target_type="run", target_id=run.id, project_id=run.project_id,
                     domain=run.domain, reason="already created (idempotent outbox); no API call",
                     details={"repo": repo_full_name, "head": head, "base": base, "number": existing.external_id})
        return {"status": "EXISTS", "ok": True, "number": existing.external_id, "url": existing.external_url,
                "side_effect_id": existing.id, "provider": PROVIDER}
    cred = _credential()
    se = _side_effect(session, run, kind="CREATE_PR", key=key, target=target)
    headers = {"Authorization": cred.api_header, "Accept": "application/json", "User-Agent": "dly-aeos-broker"}
    payload = {"title": redact_text(title)[:250], "description": redact_text(body)[:60000],
               "source": {"branch": {"name": head}}, "destination": {"branch": {"name": base}},
               "close_source_branch": False, "draft": True}
    pr: dict[str, Any] | None = None
    outcome, detail = "FAILED", ""
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False, trust_env=False) as client:
        try:
            r = client.post(_pr_url(api_base, repo_full_name), json=payload, headers=headers)
            if r.status_code == 201:
                pr, outcome = r.json(), "SUCCEEDED"
            elif r.status_code in (400, 409):
                # An open PR for this head/base already exists (or there is nothing to compare): read, never retry.
                pr = _find_pr(client, api_base, repo_full_name, head, base, headers)
                outcome = "SUCCEEDED" if pr else "FAILED"
                detail = "reconciled existing PR" if pr else f"Bitbucket {r.status_code}: {redact_text(r.text)[:300]}"
            else:
                detail = f"Bitbucket {r.status_code}: {redact_text(r.text)[:300]}"
        except httpx.TimeoutException:
            # §50: the provider may have created the PR - reconcile before any retry.
            try:
                pr = _find_pr(client, api_base, repo_full_name, head, base, headers)
            except (httpx.HTTPError, ValueError):
                pr = None
            outcome = "SUCCEEDED" if pr else "UNKNOWN"
            detail = "timeout; reconciled" if pr else "timeout; outcome UNKNOWN until reconciled"
        except httpx.HTTPError as exc:
            detail = type(exc).__name__
    se.status = outcome
    if pr:
        se.external_id = str(pr.get("id"))
        se.external_url = _pr_link(pr)
    se.result_summary = detail[:500]
    audit.record(session, actor_type="service", actor_id="broker", action="broker.bitbucket.create_pr",
                 result="SUCCESS" if outcome == "SUCCEEDED" else "FAILED", target_type="run", target_id=run.id,
                 project_id=run.project_id, domain=run.domain, reason=detail,
                 details={"repo": repo_full_name, "head": head, "base": base, "number": se.external_id})
    events.emit(session, source="broker", kind="run.side_effect", entity_type="run", entity_id=run.id, run_id=run.id,
                project_id=run.project_id, domain=run.domain,
                payload={"kind": "CREATE_PR", "status": outcome, "number": se.external_id, "url": se.external_url,
                         "provider": PROVIDER})
    src_hash = str((((pr or {}).get("source") or {}).get("commit") or {}).get("hash") or "")
    # Bitbucket reports an abbreviated source hash: the run's full head when it is a prefix of it, else what the
    # provider says (a different commit stays visible and the PR sync invalidates the acceptance).
    head_sha = run.head_commit if src_hash and run.head_commit and run.head_commit.startswith(src_hash) else (
        src_hash or None)
    return {"status": "CREATED" if outcome == "SUCCEEDED" else outcome, "ok": outcome == "SUCCEEDED",
            "number": se.external_id, "url": se.external_url, "side_effect_id": se.id, "error": detail[:300],
            "head_sha": head_sha, "base_sha": None, "provider": PROVIDER,
            "note": "draft PR; integrating it into the base branch is a human-only action"}


# ----------------------------------------------------------------------------- reconciliation (read-only)
def reconcile_side_effect(session: Session, se: BrokerSideEffect, *, actor: str,
                          api_base: str = BITBUCKET_API) -> dict[str, Any]:
    """Read what actually exists for a PENDING/UNKNOWN Bitbucket push/PR (§14, §50). Never writes."""
    if se.domain != "OFFICE":
        raise PolicyDenied("Bitbucket credentials serve OFFICE side effects only", code="DOMAIN_CREDENTIAL")
    target = dict(se.target or {})
    before, detail = se.status, ""
    if se.kind == "PUSH_BRANCH":
        branch = validate_branch(str(target.get("branch", "")))
        url = pinned_remote(str(target.get("repo", "")), str(target.get("remote_url") or "") or None)
        cred = None if _is_local_remote(url) else _credential()
        try:
            sha = _ls_remote_head(url, branch, cred.git_secret if cred else None,
                                  basic_user=cred.git_user if cred else ACCESS_TOKEN_GIT_USER)
        except subprocess.TimeoutExpired:
            detail = "ls-remote timed out; outcome stays UNKNOWN"
        else:
            expected = target.get("expected_head") or se.commit_sha
            if sha and (not expected or sha == expected):
                se.status, se.commit_sha, detail = "SUCCEEDED", sha, f"remote branch at {sha[:12]} (reconciled)"
            elif sha:
                se.status, detail = "FAILED", f"remote branch at {sha[:12]}, not the expected {str(expected)[:12]}"
            else:
                se.status, detail = "FAILED", "branch not present on the remote (push did not land)"
    elif se.kind == "CREATE_PR":
        repo = validate_repo(str(target.get("repo", "")))
        head = validate_branch(str(target.get("head", "")))
        base = str(target.get("base", ""))
        if not _BASE_RE.match(base):
            raise PolicyDenied("invalid PR base", code="PR_BASE_REJECTED")
        cred = _credential()
        headers = {"Authorization": cred.api_header, "Accept": "application/json", "User-Agent": "dly-aeos-broker"}
        with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False, trust_env=False) as client:
            try:
                pr = _find_pr(client, api_base, repo, head, base, headers, states=PR_STATES_ALL)
            except (httpx.HTTPError, ValueError):
                pr, detail = None, "Bitbucket could not be read; outcome stays UNKNOWN"
            else:
                if pr:
                    se.status, se.external_id, se.external_url = "SUCCEEDED", str(pr.get("id")), _pr_link(pr)
                    detail = f"PR #{se.external_id} exists (reconciled)"
                else:
                    se.status, detail = "FAILED", "no PR exists for this head/base (creation did not happen)"
    else:
        raise ValidationFailed(f"unknown side effect kind {se.kind!r}")
    se.result_summary = redact_text(detail)[:500]
    audit.record(session, actor_type="user" if actor.startswith("user:") else "service", actor_id=actor,
                 action="broker.side_effect.reconcile", result="SUCCESS" if se.status != before else "FAILED",
                 target_type="broker_side_effect", target_id=se.id, project_id=se.project_id, domain=se.domain,
                 reason=detail, details={"kind": se.kind, "from": before, "to": se.status, "provider": PROVIDER})
    events.emit(session, source="broker", kind="run.side_effect", entity_type="run", entity_id=se.run_id,
                run_id=se.run_id, project_id=se.project_id, domain=se.domain,
                payload={"kind": se.kind, "status": se.status, "reconciled": True, "detail": se.result_summary,
                         "provider": PROVIDER})
    return {"id": se.id, "kind": se.kind, "status": se.status, "changed": se.status != before, "detail": detail}
