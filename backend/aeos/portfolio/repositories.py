"""Repository references (§04, §37): exact provider identity, never guessed from a name or account.

* provider follows the domain: PERSONAL -> GITHUB, OFFICE -> BITBUCKET (U-02, U-03, U-04);
* verification is a read-only provider lookup of the exact ``owner/name``; any failure, redirect,
  rename or provider-ID change leaves the record UNVERIFIED with the reason (never a guess);
* a local checkout path is validated (absolute, existing, git top-level, outside the AEOS data dir,
  no UNC/network share, not overlapping a checkout registered in the other domain) and observed
  read-only (branch, head, dirty, origin match);
* repository content is read only when the checkout still belongs to the project's domain: an
  origin on the other domain's code provider, or a path overlapping another domain's checkout, is
  refused (AGENTS.md invariant 9, §50 "Source Office muncul dalam platform personal working tree");
* local paths are sensitive: views show a logical/redacted name only.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy import func, select
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
from aeos.portfolio.service import deny, require_writable

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


def is_network_path(value: str) -> bool:
    r"""UNC / network-share paths (``\\server\share``, ``\\?\UNC\...``, ``//server/share``).
    Extended local paths such as ``\\?\C:\...`` are not network paths."""
    v = value.replace("/", "\\")
    if v.startswith(("\\\\?\\", "\\\\.\\")):
        return v[4:].upper().startswith("UNC\\")
    return v.startswith("\\\\")


def _norm(p: Path) -> str:
    # Windows (normcase) and macOS (default APFS/HFS+) compare paths case-insensitively.
    s = os.path.normcase(str(p))
    return s.casefold() if sys.platform == "darwin" else s


def _overlaps(a: Path, b: Path) -> bool:
    """True when *a* and *b* are the same directory or one contains the other."""
    na, nb = _norm(a).rstrip("\\/"), _norm(b).rstrip("\\/")
    return na == nb or na.startswith(nb + os.sep) or nb.startswith(na + os.sep)


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return _norm(a) == _norm(b)


def _checkout_path(ref: str | None) -> Path | None:
    if not ref:
        return None
    try:
        return (repo_root() if ref == PLATFORM_LOCAL_REF else Path(ref)).resolve()
    except OSError:
        return None


def other_domain_overlap(session: Session, path: Path, domain: str, *, exclude_repo_id: str | None = None) -> bool:
    """Does *path* overlap a local checkout registered by a project of another domain?

    The answer is deliberately a bare boolean: which project/domain owns the other checkout is not
    revealed to the caller (no cross-domain metadata in errors)."""
    rows = session.execute(
        select(RepositoryReference.id, RepositoryReference.local_path_ref)
        .join(Project, Project.id == RepositoryReference.project_id)
        .where(Project.domain != domain, RepositoryReference.local_path_ref.is_not(None))
    ).all()
    for rid, ref in rows:
        if rid == exclude_repo_id:
            continue
        other = _checkout_path(ref)
        if other is not None and _overlaps(path, other):
            return True
    return False


def _split_remote(remote: str | None) -> tuple[str, str]:
    """(host, path) of a git remote in https or scp-like ssh form; ("", "") when unparseable."""
    if not remote:
        return "", ""
    r = remote.strip()
    m = re.match(r"^(?:[\w.-]+@)?([\w.-]+):(?!//)(.+)$", r)  # scp-like ssh: git@github.com:owner/name.git
    if m and "://" not in r:
        rhost, rpath = m.group(1), m.group(2)
    else:
        try:
            parts = urlsplit(r)
        except ValueError:
            return "", ""
        rhost, rpath = (parts.hostname or ""), parts.path
    rpath = rpath.strip("/")
    if rpath.lower().endswith(".git"):
        rpath = rpath[:-4]
    return rhost.lower(), rpath


def origin_domain_conflict(origin: str | None, domain: str) -> str | None:
    """Reason text when the checkout's origin is hosted on the *other* domain's code provider
    (PERSONAL uses GitHub, OFFICE uses Bitbucket: U-02..U-04); ``None`` otherwise."""
    host, _ = _split_remote(origin)
    if not host:
        return None
    if domain == Domain.PERSONAL.value and "bitbucket" in host:
        return (f"the checkout's origin is hosted on {host} (OFFICE code provider); Office source is never read "
                "into a PERSONAL project")
    if domain == Domain.OFFICE.value and host == PROVIDER_HOSTS["GITHUB"]:
        return (f"the checkout's origin is hosted on {host} (PERSONAL code provider); it is never read into an "
                "OFFICE project")
    return None


def validate_local_checkout(raw: str, settings: Settings, *, domain: str | None = None,
                            session: Session | None = None, exclude_repo_id: str | None = None) -> Path:
    """§50 path traversal/symlink: resolve, then require an existing git top-level directory that is
    not inside AEOS runtime state and not a network share. An OFFICE checkout must not overlap the
    PERSONAL platform working tree (§50 "Source Office muncul dalam platform personal working tree"),
    and no checkout may overlap one registered in the other domain (AGENTS.md invariant 9)."""
    value = (raw or "").strip()
    if not value:
        raise ValidationFailed("local path is empty", code="INVALID_LOCAL_PATH")
    if is_network_path(value):
        raise ValidationFailed("network (UNC) paths are not accepted; use a local clone", code="INVALID_LOCAL_PATH")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ValidationFailed("local path must be absolute", code="INVALID_LOCAL_PATH")
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ValidationFailed("local path does not exist", code="LOCAL_PATH_NOT_FOUND") from exc
    if is_network_path(str(resolved)):
        # e.g. a mapped Windows drive letter that resolves to \\server\share
        raise ValidationFailed("network (UNC) paths are not accepted; use a local clone", code="INVALID_LOCAL_PATH")
    if not resolved.is_dir():
        raise ValidationFailed("local path is not a directory", code="INVALID_LOCAL_PATH")
    data_root = settings.data_dir.resolve()
    if _overlaps(resolved, data_root):
        raise PolicyDenied("local path overlaps the AEOS data directory (runtime state is never a workspace)",
                           code="LOCAL_PATH_IN_DATA_DIR")
    if domain == Domain.OFFICE.value and _overlaps(resolved, repo_root().resolve()):
        raise PolicyDenied("an OFFICE checkout cannot live inside (or contain) the PERSONAL DLY AEOS platform "
                           "working tree; clone it to a separate office location", code="OFFICE_SOURCE_IN_PERSONAL_TREE")
    if session is not None and domain and other_domain_overlap(session, resolved, domain,
                                                               exclude_repo_id=exclude_repo_id):
        raise PolicyDenied("this path overlaps a local checkout registered in another domain; PERSONAL and OFFICE "
                           "sources never share a working tree", code="CHECKOUT_CROSS_DOMAIN")
    top = gitread.toplevel(resolved)
    if not _same_dir(top, resolved):
        raise ValidationFailed("local path must be the repository top-level directory (choose it explicitly)",
                               code="NOT_REPO_TOPLEVEL")
    return resolved


def require_checkout_in_domain(session: Session, principal: Principal | None, project: Project,
                               repo: RepositoryReference, path: Path, *, action: str) -> None:
    """Fail closed before repository *content* is read (inventory, context packs): the checkout must
    not overlap another domain's checkout and its origin must not live on the other domain's provider.
    Checked at use time because the checkout (or its remote) can change after registration."""
    resolved = path.resolve()
    reason: str | None = None
    code = ""
    if other_domain_overlap(session, resolved, project.domain, exclude_repo_id=repo.id):
        code, reason = "CHECKOUT_CROSS_DOMAIN", ("the local checkout overlaps a checkout registered in another "
                                                 "domain; repository content is not read")
    elif project.domain == Domain.OFFICE.value and _overlaps(resolved, repo_root().resolve()):
        code, reason = "OFFICE_SOURCE_IN_PERSONAL_TREE", ("an OFFICE checkout inside the PERSONAL platform working "
                                                          "tree is not read")
    else:
        try:
            conflict = origin_domain_conflict(gitread.remote_url(resolved), project.domain)
        except AeosError:
            conflict = None  # no readable origin: identity stays unconfirmed (local_status), not a domain signal
        if conflict:
            code, reason = "CHECKOUT_OTHER_DOMAIN_ORIGIN", conflict + "; review the checkout (§50)"
    if reason:
        _refuse(session, principal, project, action=action, code=code, reason=reason, target_id=repo.id)


def _refuse(session: Session, principal: Principal | None, project: Project, *, action: str, code: str,
            reason: str, target_id: str) -> NoReturn:
    if principal is None:  # CLI / system caller: nothing to attribute; the error itself is the record
        raise PolicyDenied(reason, code=code)
    deny(session, principal, action=action, code=code, project=project, target_type="repository",
         target_id=target_id, reason=reason)


def _validated_checkout(session: Session, principal: Principal, project: Project, raw: str, settings: Settings, *,
                        action: str, exclude_repo_id: str | None = None) -> Path:
    try:
        return validate_local_checkout(raw, settings, domain=project.domain, session=session,
                                       exclude_repo_id=exclude_repo_id)
    except PolicyDenied as exc:  # domain/data-dir boundary denials are audited (fail closed)
        deny(session, principal, action=action, code=exc.code, project=project, target_type="repository",
             target_id=exclude_repo_id, reason=exc.message)


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
    rhost, rpath = _split_remote(remote)
    return bool(rhost) and rhost == PROVIDER_HOSTS[provider] and rpath.lower() == full_name.lower()


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
        error = redact_text(exc.message)[:300]  # git stderr can echo remote URLs or paths
        repo.details = {**(repo.details or {}), "local": {"error": error}}
        return {"status": "FAILED", "error": error}
    matches = remote_matches(origin, repo.provider, repo.full_name)
    owner_project = session.get(Project, repo.project_id)
    conflict = origin_domain_conflict(origin, owner_project.domain) if owner_project is not None else None
    repo.head_commit = head
    repo.local_branch = branch
    repo.dirty = dirty
    repo.local_checked_at = now
    repo.local_status = "OK" if matches and not conflict else "MISMATCH"
    local: dict[str, Any] = {
        "origin_url": _redact_url(origin),
        "origin_matches": matches,
        "dirty_entries": dirty_entries,
        "checked_at": now.isoformat(),
    }
    if conflict:
        local["reason"] = conflict + "; inventory and context packs are refused until the checkout is reviewed"
        local["domain_conflict"] = True
    elif not matches:
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
    require_writable(project)
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
    existing = session.scalar(select(RepositoryReference).where(  # exact, case-insensitive (no LIKE wildcards)
        RepositoryReference.project_id == project.id, RepositoryReference.provider == provider_v,
        func.lower(RepositoryReference.full_name) == full.lower()))
    if existing is not None:
        raise Conflict(f"repository {full} is already registered for this project", code="REPOSITORY_EXISTS")
    if role_v == "platform" and session.scalar(select(RepositoryReference.id).where(
            RepositoryReference.project_id == project.id, RepositoryReference.role == "platform")):
        raise Conflict("the platform repository is already registered", code="PLATFORM_REPOSITORY_EXISTS")

    resolved: Path | None = None
    if local_path_ref == PLATFORM_LOCAL_REF:
        ref: str | None = PLATFORM_LOCAL_REF
    elif local_path:
        resolved = _validated_checkout(session, principal, project, local_path, settings,
                                       action="portfolio.repository.add")
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
    require_writable(project)
    if repo.local_path_ref == PLATFORM_LOCAL_REF:
        raise PolicyDenied("the platform repository checkout is resolved from the running controller",
                           code="PLATFORM_LOCAL_PATH_FIXED")
    resolved = _validated_checkout(session, principal, project, local_path, settings,
                                   action="portfolio.repository.local_path", exclude_repo_id=repo.id)
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

        return secrets.get_secret(GITHUB_SECRET_REF, purpose="personal:portfolio.repository-verify", actor=actor), "token"
    except (ImportError, ModuleNotFoundError):
        return None, "anonymous (secret store module unavailable)"
    except AeosError as exc:  # Blocked: store unavailable or secret missing -> read-only anonymous lookup
        return None, f"anonymous ({exc.code})"
    except Exception as exc:  # a broken keyring/vault backend: fall back to the least-privileged lookup
        return None, f"anonymous (secret store error {exc.__class__.__name__})"


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


BITBUCKET_CLIENT_FACTORY: Any = None  # tests inject an httpx.Client (respx); production uses the provider default


def _verify_bitbucket(session: Session, project: Project, repo: RepositoryReference) -> tuple[str, str, dict[str, Any]]:
    """OFFICE (§04 "the selected Bitbucket repository"): exact identity check behind the company authorisation
    for Bitbucket AND an enabled bitbucket integration (ADR-0073 disabled by default). Never guesses."""
    from aeos.models.portfolio import ProjectIntegration
    from aeos.policy import facade as policy

    attempt: dict[str, Any] = {"at": utcnow().isoformat(), "auth_mode": "none"}
    pol = policy.office_policy(session)
    if not (pol.get("office_authorization") is True and "bitbucket" in (pol.get("authorized_providers") or [])):
        return ("UNVERIFIED", "Bitbucket verification needs a recorded company authorisation that allows Bitbucket "
                              "(OFFICE default deny, §23); no provider call was made", attempt)
    integ = session.scalar(select(ProjectIntegration).where(ProjectIntegration.project_id == project.id,
                                                            ProjectIntegration.provider == "bitbucket"))
    if integ is None or (integ.config or {}).get("enabled") is not True:
        return ("UNVERIFIED", "Bitbucket adapter is disabled for this project (integration bitbucket/code with "
                              "config.enabled=true required, ADR-0073); no provider call was made", attempt)
    from aeos.publisher.facade import ProviderError, bitbucket_repository_reference

    attempt["auth_mode"] = "office/bitbucket"
    client = BITBUCKET_CLIENT_FACTORY() if BITBUCKET_CLIENT_FACTORY else None
    try:
        ref = bitbucket_repository_reference(repo.full_name, client=client)
    except ProviderError as exc:
        attempt["error"] = getattr(exc, "code", type(exc).__name__)
        return ("UNVERIFIED", f"Bitbucket did not confirm {repo.full_name}: {redact_text(str(exc))[:200]} - the "
                              "repository is never guessed", attempt)
    observed = str(ref.get("full_name") or "")
    attempt["observed"] = {"id": ref.get("uuid"), "full_name": observed}
    attempt["bitbucket"] = {"url": ref.get("url"), "main_branch": ref.get("main_branch"),
                            "is_private": ref.get("is_private")}
    if observed.lower() != repo.full_name.lower():
        return ("UNVERIFIED", f"Bitbucket returned {observed or '(none)'} for {repo.full_name} (renamed/moved?); "
                              "register the exact repository", attempt)
    if repo.provider_repo_id and ref.get("uuid") != repo.provider_repo_id:
        return ("UNVERIFIED", "the Bitbucket repository uuid changed since it was verified; re-register it "
                              "explicitly", attempt)
    return "VERIFIED", "identity verified via Bitbucket API (office/bitbucket); scope: repository identity only", attempt


def verify_repository(session: Session, principal: Principal, project: Project, repo: RepositoryReference) -> dict[str, Any]:
    """Read-only identity verification + local observation. Never infers a different repository."""
    require_writable(project)
    if repo.provider == "GITHUB":
        status, reason, attempt = _verify_github(repo, principal.user.id)
    else:
        status, reason, attempt = _verify_bitbucket(session, project, repo)
    details = dict(repo.details or {})
    details["last_attempt"] = attempt
    if status == "VERIFIED" and repo.provider == "BITBUCKET":
        bb = attempt.get("bitbucket", {})
        repo.provider_repo_id = attempt["observed"]["id"]
        repo.default_branch = bb.get("main_branch") or repo.default_branch
        repo.url = bb.get("url") or repo.url
        repo.last_verified_at = utcnow()
        details["url_source"] = "provider"
        details["bitbucket"] = bb
    elif status == "VERIFIED":
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


def repository_detail(session: Session, repo: RepositoryReference) -> dict[str, Any]:
    """§37 user repository view: identity + base branch, PR/CI links, policy/capability and docs targets in one
    place (joined from delivery PR records, project integrations and WRITE_ENABLEMENT decisions)."""
    from aeos.models.portfolio import ProjectIntegration

    view = repository_view(repo)
    prs: list[dict[str, Any]] = []
    try:
        from aeos.models.delivery_ext import PullRequestRecord

        rows = session.scalars(select(PullRequestRecord).where(PullRequestRecord.project_id == repo.project_id,
                                                               PullRequestRecord.repo_full_name == repo.full_name)
                               .order_by(PullRequestRecord.created_at.desc()).limit(20))
        prs = [{"number": p.number, "url": p.url, "state": p.state, "head_ref": p.head_ref, "base_ref": p.base_ref,
                "merged": p.merged, "merged_by_type": p.merged_by_type, "sync_status": p.sync_status,
                "last_synced_at": p.last_synced_at.isoformat() if p.last_synced_at else None} for p in rows]
    except ImportError:  # delivery module absent: say so instead of an empty "no PRs"
        prs = []
    integrations = list(session.scalars(select(ProjectIntegration).where(ProjectIntegration.project_id == repo.project_id)))
    from aeos.policy import facade as policy

    # §26 write enablement is per provider (flag + ACTIVE human decision on provider_write/<provider>), exactly as
    # the interlock checks it; a decision shaped any other way (e.g. on an integration id) enables nothing
    enabled = {i.id: policy.write_enablement(session, i.provider, domain=i.domain) for i in integrations}
    view.update({
        "base_branch": repo.default_branch,
        "pull_requests": prs,
        "ci": [{"provider": i.provider, "integration_id": i.id, "configuration":
                (i.capability_status or {}).get("configuration", "UNKNOWN")} for i in integrations if i.purpose == "ci"],
        "docs_targets": [{"provider": i.provider, "root_id": i.root_id, "integration_id": i.id}
                         for i in integrations if i.purpose == "docs"],
        "capability": {
            "write_enablement": [{"integration_id": i.id, "provider": i.provider, "purpose": i.purpose,
                                  "decision_id": enabled[i.id]["decision_id"], "status": enabled[i.id]["status"],
                                  "scope": "provider (every target of this provider)"}
                                 for i in integrations],
            "merge": "HUMAN_ONLY (AEOS never merges)",
        },
    })
    return view


def inventory_for_repository(session: Session, principal: Principal | None, project: Project, repo: RepositoryReference,
                             *, commit: str | None = None) -> dict[str, Any]:
    """Run the read-only inventory on the registered local checkout and keep the latest result."""
    from aeos.core.errors import Blocked
    from aeos.portfolio.inventory import inventory_repository

    require_writable(project)
    path = resolve_local_path(repo)
    if path is None:
        raise Blocked("no local checkout is registered for this repository; add its local path first",
                      code="LOCAL_CHECKOUT_MISSING")
    require_checkout_in_domain(session, principal, project, repo, path, action="portfolio.repository.inventory")
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
