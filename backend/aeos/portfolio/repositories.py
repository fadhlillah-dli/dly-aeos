"""Repository references (§04, §37): exact provider identity, never guessed from a name or account.

* provider follows the domain: PERSONAL -> GITHUB, OFFICE -> BITBUCKET (U-02, U-03, U-04);
* verification is a read-only provider lookup of the exact ``owner/name``; any failure, redirect,
  rename or provider-ID change leaves the record UNVERIFIED with the reason (never a guess);
* a local checkout path is validated (absolute, existing, git top-level, outside the AEOS data dir,
  no UNC/network share) and observed read-only (branch, head, dirty, origin match);
* local paths are sensitive: views show a logical/redacted name only.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.config import Settings, get_settings
from aeos.core.enums import Domain
from aeos.core.errors import AeosError, Conflict, PolicyDenied, ValidationFailed
from aeos.core.paths import repo_root
from aeos.core.redaction import redact_text
from aeos.core.timeutil import utcnow
from aeos.events import service as events
from aeos.identity.service import Principal
from aeos.models.portfolio import Project, RepositoryReference
from aeos.portfolio import gitread
from aeos.portfolio.service import deny

CODE_PROVIDER = {Domain.PERSONAL.value: "GITHUB", Domain.OFFICE.value: "BITBUCKET"}
PROVIDER_HOSTS = {"GITHUB": "github.com", "BITBUCKET": "bitbucket.org"}
ROLES = ("platform", "project")
PLATFORM_LOCAL_REF = "platform-repo"  # logical reference resolved at use time (§09)
GITHUB_API = "https://api.github.com"
GITHUB_SECRET_REF = "personal/github"
HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

_GITHUB_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_BITBUCKET_WORKSPACE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,61}$")
_REPO_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,199}$")


# ---------------------------------------------------------------- validation


def validate_identity(provider: str, owner: str, name: str) -> tuple[str, str, str]:
    owner = (owner or "").strip()
    name = (name or "").strip()
    if name.lower().endswith(".git"):
        name = name[:-4]
    owner_re = _GITHUB_OWNER if provider == "GITHUB" else _BITBUCKET_WORKSPACE
    if not owner_re.match(owner):
        raise ValidationFailed(f"invalid {provider} owner/workspace {owner!r}", code="INVALID_REPO_OWNER")
    if not _REPO_NAME.match(name) or name in {".", ".."}:
        raise ValidationFailed(f"invalid repository name {name!r}", code="INVALID_REPO_NAME")
    return owner, name, f"{owner}/{name}"


def validate_local_checkout(raw: str, settings: Settings) -> Path:
    """§50 path traversal/symlink: resolve, then require an existing git top-level directory that is
    not inside AEOS runtime state and not a network share."""
    value = (raw or "").strip()
    if not value:
        raise ValidationFailed("local path is empty", code="INVALID_LOCAL_PATH")
    if value.startswith(("\\\\", "//")):
        raise ValidationFailed("network (UNC) paths are not accepted; use a local clone", code="INVALID_LOCAL_PATH")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ValidationFailed("local path must be absolute", code="INVALID_LOCAL_PATH")
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ValidationFailed("local path does not exist", code="LOCAL_PATH_NOT_FOUND") from exc
    if not resolved.is_dir():
        raise ValidationFailed("local path is not a directory", code="INVALID_LOCAL_PATH")
    data_root = settings.data_dir.resolve()
    if resolved == data_root or data_root in resolved.parents or resolved in data_root.parents:
        raise PolicyDenied("local path overlaps the AEOS data directory (runtime state is never a workspace)",
                           code="LOCAL_PATH_IN_DATA_DIR")
    top = gitread.toplevel(resolved)
    if os.path.normcase(str(top)) != os.path.normcase(str(resolved)):
        raise ValidationFailed("local path must be the repository top-level directory (choose it explicitly)",
                               code="NOT_REPO_TOPLEVEL")
    return resolved


def resolve_local_path(repo: RepositoryReference) -> Path | None:
    ref = repo.local_path_ref
    if not ref:
        return None
    path = repo_root() if ref == PLATFORM_LOCAL_REF else Path(ref)
    return path if path.is_dir() else None


def local_path_display(ref: str | None) -> str | None:
    """§37: local paths can be sensitive; show a logical or redacted name only."""
    if not ref:
        return None
    if ref == PLATFORM_LOCAL_REF:
        return "platform-repo (checkout DLY AEOS ini)"
    parts = Path(ref).parts
    return "…/" + "/".join(parts[-2:]) if len(parts) > 2 else Path(ref).name


def _redact_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return redact_text(url)
    if parts.username or parts.password:
        netloc = parts.hostname or ""
        if parts.port:
            netloc += f":{parts.port}"
        url = parts._replace(netloc=netloc).geturl()
    return redact_text(url)


def remote_matches(remote: str | None, provider: str, full_name: str) -> bool:
    """True only for the exact provider host + owner/name (https or ssh forms)."""
    if not remote:
        return False
    host = PROVIDER_HOSTS[provider]
    r = remote.strip()
    m = re.match(r"^(?:[\w.-]+@)?([\w.-]+):(?!//)(.+)$", r)  # scp-like ssh: git@github.com:owner/name.git
    if m and "://" not in r:
        rhost, rpath = m.group(1), m.group(2)
    else:
        try:
            parts = urlsplit(r)
        except ValueError:
            return False
        rhost, rpath = (parts.hostname or ""), parts.path
    rpath = rpath.strip("/")
    if rpath.lower().endswith(".git"):
        rpath = rpath[:-4]
    return rhost.lower() == host and rpath.lower() == full_name.lower()


# ---------------------------------------------------------------- local observation


def observe_local(session: Session, repo: RepositoryReference, *, path: Path | None = None) -> dict[str, Any]:
    """Read-only branch/head/dirty/origin observation of the local checkout (§37, §08 Repository)."""
    path = path or resolve_local_path(repo)
    now = utcnow()
    if path is None:
        if repo.local_path_ref:
            repo.local_status = "FAILED"
            repo.local_checked_at = now
            repo.details = {**(repo.details or {}), "local": {"error": "local checkout not found at the registered path"}}
        else:
            repo.local_status = "NOT_CHECKED"
        return {"status": repo.local_status}
    previous_head = repo.head_commit
    try:
        try:
            head: str | None = gitread.resolve_commit(path, "HEAD")
        except ValidationFailed:
            head = None  # empty repository (no commits yet)
        branch = gitread.current_branch(path)
        dirty, dirty_entries = gitread.is_dirty(path)
        origin = gitread.remote_url(path)
    except AeosError as exc:
        repo.local_status = "FAILED"
        repo.local_checked_at = now
        repo.details = {**(repo.details or {}), "local": {"error": exc.message[:300]}}
        return {"status": "FAILED", "error": exc.message}
    matches = remote_matches(origin, repo.provider, repo.full_name)
    repo.head_commit = head
    repo.local_branch = branch
    repo.dirty = dirty
    repo.local_checked_at = now
    repo.local_status = "OK" if matches else "MISMATCH"
    local = {
        "origin_url": _redact_url(origin),
        "origin_matches": matches,
        "dirty_entries": dirty_entries,
        "checked_at": now.isoformat(),
    }
    if not matches:
        local["reason"] = ("origin remote does not point at the declared repository" if origin
                           else "no origin remote configured; cannot confirm this checkout is the declared repository")
    repo.details = {**(repo.details or {}), "local": local}
    if previous_head and head and previous_head != head:
        try:
            from aeos.knowledge.packs import mark_stale_for_repository

            mark_stale_for_repository(session, repo.id, head, reason=f"repository head moved {previous_head[:12]} -> {head[:12]}")
        except ImportError:  # pragma: no cover - knowledge module always present in this build
            pass
    return {"status": repo.local_status, "head": head, "branch": branch, "dirty": dirty}


# ---------------------------------------------------------------- CRUD


def add_repository(
    session: Session,
    principal: Principal,
    project: Project,
    *,
    provider: str,
    owner: str,
    name: str,
    role: str = "project",
    local_path: str | None = None,
    default_branch: str | None = None,
    settings: Settings | None = None,
    local_path_ref: str | None = None,
) -> RepositoryReference:
    settings = settings or get_settings()
    provider_v = (provider or "").strip().upper()
    expected = CODE_PROVIDER[project.domain]
    if provider_v != expected:
        deny(session, principal, action="portfolio.repository.add", code="PROVIDER_DOMAIN_MISMATCH", project=project,
             reason=f"{project.domain} projects use {expected} repositories, not {provider_v or '(none)'} (U-02)",
             details={"expected": expected, "requested": provider_v})
    owner_v, name_v, full = validate_identity(provider_v, owner, name)
    role_v = (role or "project").strip().lower()
    if role_v not in ROLES:
        raise ValidationFailed("role must be platform or project", code="INVALID_REPO_ROLE")
    if role_v == "platform" and not project.is_platform:
        deny(session, principal, action="portfolio.repository.add", code="PLATFORM_ROLE_DENIED", project=project,
             reason="only the DLY AEOS platform project can hold the platform repository (§09)")
    branch = (default_branch or "main").strip()
    if not _BRANCH.match(branch) or ".." in branch:
        raise ValidationFailed("invalid default branch name", code="INVALID_BRANCH")
    existing = session.scalar(select(RepositoryReference).where(
        RepositoryReference.project_id == project.id, RepositoryReference.provider == provider_v,
        RepositoryReference.full_name.ilike(full)))
    if existing is not None:
        raise Conflict(f"repository {full} is already registered for this project", code="REPOSITORY_EXISTS")
    if role_v == "platform" and session.scalar(select(RepositoryReference.id).where(
            RepositoryReference.project_id == project.id, RepositoryReference.role == "platform")):
        raise Conflict("the platform repository is already registered", code="PLATFORM_REPOSITORY_EXISTS")

    resolved: Path | None = None
    if local_path_ref == PLATFORM_LOCAL_REF:
        ref: str | None = PLATFORM_LOCAL_REF
    elif local_path:
        resolved = validate_local_checkout(local_path, settings)
        ref = str(resolved)
    else:
        ref = None
    repo = RepositoryReference(
        project_id=project.id, provider=provider_v, full_name=full,
        url=f"https://{PROVIDER_HOSTS[provider_v]}/{full}", default_branch=branch, role=role_v,
        local_path_ref=ref, verification_status="UNVERIFIED",
        verification_reason="identity not yet verified with the provider",
        details={"url_source": "declared", "declared_owner": owner_v, "declared_name": name_v},
    )
    session.add(repo)
    session.flush()
    if ref:
        observe_local(session, repo, path=resolved)
    audit.record(session, actor_type="user", actor_id=principal.user.id, action="portfolio.repository.add",
                 target_type="repository", target_id=repo.id, project_id=project.id, domain=project.domain,
                 details={"provider": provider_v, "full_name": full, "role": role_v, "local": bool(ref),
                          "local_status": repo.local_status})
    events.emit(session, source="portfolio", kind="portfolio.repository.added", entity_type="repository",
                entity_id=repo.id, project_id=project.id, domain=project.domain,
                payload={"full_name": full, "provider": provider_v, "verification_status": repo.verification_status})
    return repo


def set_local_path(session: Session, principal: Principal, project: Project, repo: RepositoryReference,
                   local_path: str, settings: Settings | None = None) -> RepositoryReference:
    settings = settings or get_settings()
    if repo.local_path_ref == PLATFORM_LOCAL_REF:
        raise PolicyDenied("the platform repository checkout is resolved from the running controller",
                           code="PLATFORM_LOCAL_PATH_FIXED")
    resolved = validate_local_checkout(local_path, settings)
    repo.local_path_ref = str(resolved)
    observe_local(session, repo, path=resolved)
    audit.record(session, actor_type="user", actor_id=principal.user.id, action="portfolio.repository.local_path",
                 target_type="repository", target_id=repo.id, project_id=project.id, domain=project.domain,
                 details={"local_status": repo.local_status})
    return repo


# ---------------------------------------------------------------- provider verification


def _github_token(actor: str) -> tuple[str | None, str]:
    try:
        from aeos.secrets import facade as secrets

        return secrets.get_secret(GITHUB_SECRET_REF, purpose="repository-verify", actor=actor), "token"
    except (ImportError, ModuleNotFoundError):
        return None, "anonymous (secret store module unavailable)"
    except AeosError as exc:  # Blocked: store unavailable or secret missing -> read-only anonymous lookup
        return None, f"anonymous ({exc.code})"


def _verify_github(repo: RepositoryReference, actor: str) -> tuple[str, str, dict[str, Any]]:
    owner, _, name = repo.full_name.partition("/")
    token, auth_mode = _github_token(actor)
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "dly-aeos-control-plane"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{GITHUB_API}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
    attempt: dict[str, Any] = {"at": utcnow().isoformat(), "auth_mode": auth_mode, "endpoint": "GET /repos/{owner}/{repo}"}
    try:
        resp = httpx.get(url, headers=headers, timeout=HTTP_TIMEOUT, follow_redirects=False)
    except httpx.HTTPError as exc:
        attempt["error"] = exc.__class__.__name__
        return "UNVERIFIED", f"network error contacting GitHub ({exc.__class__.__name__}); not verified", attempt
    attempt["http_status"] = resp.status_code
    if resp.status_code in (301, 302, 307, 308):
        return ("UNVERIFIED", "GitHub reports the repository moved/renamed; confirm the canonical owner/name "
                "explicitly (trust is not inherited across renames)", attempt)
    if resp.status_code == 404:
        hint = "" if token else f"; private repositories need the {GITHUB_SECRET_REF} credential"
        return "UNVERIFIED", f"repository not found or not accessible ({auth_mode}){hint}", attempt
    if resp.status_code == 401:
        return "UNVERIFIED", "GitHub rejected the credential (401); rotate personal/github", attempt
    if resp.status_code in (403, 429):
        return "UNVERIFIED", f"GitHub refused or rate-limited the lookup ({resp.status_code}); retry later", attempt
    if resp.status_code != 200:
        return "UNVERIFIED", f"unexpected GitHub response {resp.status_code}; not verified", attempt
    try:
        data = resp.json()
    except ValueError:
        return "UNVERIFIED", "GitHub returned an unreadable body; not verified", attempt
    if not isinstance(data, dict) or "id" not in data:
        return "UNVERIFIED", "GitHub response lacks a repository id; not verified", attempt
    observed_full = str(data.get("full_name") or "")
    observed_id = str(data["id"])
    attempt["observed"] = {"full_name": observed_full, "id": observed_id}
    if observed_full.lower() != repo.full_name.lower():
        return ("UNVERIFIED", f"GitHub resolved {repo.full_name} to {observed_full}; identity mismatch (rename or "
                "redirect) must be confirmed explicitly", attempt)
    if repo.provider_repo_id and repo.provider_repo_id != observed_id:
        return ("UNVERIFIED", f"provider repository id changed ({repo.provider_repo_id} -> {observed_id}); a "
                "repository with the same name is not the same target (§37)", attempt)
    raw_perms = data.get("permissions")
    perms: dict[str, Any] = raw_perms if isinstance(raw_perms, dict) else {}
    attempt["github"] = {
        "private": bool(data.get("private")),
        "visibility": data.get("visibility"),
        "fork": bool(data.get("fork")),
        "parent": (data.get("parent") or {}).get("full_name") if isinstance(data.get("parent"), dict) else None,
        "archived": bool(data.get("archived")),
        "owner_type": (data.get("owner") or {}).get("type") if isinstance(data.get("owner"), dict) else None,
        "permissions": {k: bool(v) for k, v in perms.items() if isinstance(v, bool)},
        "pushed_at": data.get("pushed_at"),
        "html_url": data.get("html_url"),
        "default_branch": data.get("default_branch"),
    }
    return "VERIFIED", f"identity verified via GitHub API ({auth_mode}); scope: repository identity only", attempt


def verify_repository(session: Session, principal: Principal, project: Project, repo: RepositoryReference) -> dict[str, Any]:
    """Read-only identity verification + local observation. Never infers a different repository."""
    if repo.provider == "GITHUB":
        status, reason, attempt = _verify_github(repo, principal.user.id)
    else:
        status, reason, attempt = (
            "UNVERIFIED",
            "Bitbucket verification adapter is not available in this build (OFFICE, M5); it needs a company-"
            "authorised office/bitbucket credential and CAN_USE_OFFICE_INTEGRATIONS",
            {"at": utcnow().isoformat(), "auth_mode": "none"},
        )
    details = dict(repo.details or {})
    details["last_attempt"] = attempt
    if status == "VERIFIED":
        gh = attempt.get("github", {})
        repo.provider_repo_id = attempt["observed"]["id"]
        repo.default_branch = gh.get("default_branch") or repo.default_branch
        repo.url = gh.get("html_url") or repo.url
        repo.last_verified_at = utcnow()
        details["url_source"] = "provider"
        details["github"] = gh
    repo.verification_status = status
    repo.verification_reason = reason
    repo.details = details
    local = observe_local(session, repo) if repo.local_path_ref else {"status": "NOT_CHECKED"}
    session.flush()
    audit.record(session, actor_type="user", actor_id=principal.user.id, action="portfolio.repository.verify",
                 result="SUCCESS" if status == "VERIFIED" else "FAILED", target_type="repository", target_id=repo.id,
                 project_id=project.id, domain=project.domain, reason=reason,
                 details={"provider": repo.provider, "full_name": repo.full_name, "http_status": attempt.get("http_status"),
                          "auth_mode": attempt.get("auth_mode"), "local_status": local.get("status")})
    events.emit(session, source="portfolio", kind="portfolio.repository.verified", entity_type="repository",
                entity_id=repo.id, project_id=project.id, domain=project.domain,
                payload={"verification_status": status, "local_status": repo.local_status})
    return {"verification_status": status, "reason": reason, "local": local}


# ---------------------------------------------------------------- views


def repository_view(repo: RepositoryReference, *, summary: bool = False) -> dict[str, Any]:
    view: dict[str, Any] = {
        "id": repo.id,
        "project_id": repo.project_id,
        "provider": repo.provider,
        "full_name": repo.full_name,
        "role": repo.role,
        "url": repo.url,
        "verification_status": repo.verification_status,
        "verification_reason": repo.verification_reason,
        "last_verified_at": repo.last_verified_at.isoformat() if repo.last_verified_at else None,
        "local_path": local_path_display(repo.local_path_ref),
        "local_status": repo.local_status,
        "head_commit": repo.head_commit,
        "local_branch": repo.local_branch,
        "dirty": repo.dirty,
        "local_checked_at": repo.local_checked_at.isoformat() if repo.local_checked_at else None,
    }
    if summary:
        return view
    details = repo.details or {}
    view.update({
        "provider_repo_id": repo.provider_repo_id,
        "default_branch": repo.default_branch,
        "url_source": details.get("url_source", "declared"),
        "github": details.get("github"),
        "local": details.get("local"),
        "last_attempt": details.get("last_attempt"),
        "inventory": repo.inventory or None,
        "inventory_commit": repo.inventory_commit,
        "inventory_at": repo.inventory_at.isoformat() if repo.inventory_at else None,
        "created_at": repo.created_at.isoformat(),
    })
    return view


def inventory_for_repository(session: Session, principal: Principal | None, project: Project, repo: RepositoryReference,
                             *, commit: str | None = None) -> dict[str, Any]:
    """Run the read-only inventory on the registered local checkout and keep the latest result."""
    from aeos.core.errors import Blocked
    from aeos.portfolio.inventory import inventory_repository

    path = resolve_local_path(repo)
    if path is None:
        raise Blocked("no local checkout is registered for this repository; add its local path first",
                      code="LOCAL_CHECKOUT_MISSING")
    result = inventory_repository(path, commit=commit)
    result["repository"] = {"id": repo.id, "provider": repo.provider, "full_name": repo.full_name,
                            "verification_status": repo.verification_status}
    repo.inventory = result
    repo.inventory_commit = result["commit"]
    repo.inventory_at = utcnow()
    session.flush()
    audit.record(session, actor_type="user" if principal else "system",
                 actor_id=principal.user.id if principal else "portfolio", action="portfolio.repository.inventory",
                 target_type="repository", target_id=repo.id, project_id=project.id, domain=project.domain,
                 details={"commit": result["commit"], "files": result["files"], "hash": result["inventory_hash"]})
    events.emit(session, source="portfolio", kind="portfolio.repository.inventoried", entity_type="repository",
                entity_id=repo.id, entity_version=result["commit"], project_id=project.id, domain=project.domain,
                payload={"files": result["files"], "languages": [x["language"] for x in result["languages"][:5]]})
    return result


def primary_repository(session: Session, project_id: str) -> RepositoryReference | None:
    repos = list(session.scalars(select(RepositoryReference).where(RepositoryReference.project_id == project_id)))
    if not repos:
        return None
    repos.sort(key=lambda r: (r.role != "platform", r.local_path_ref is None, r.created_at))
    return repos[0]
