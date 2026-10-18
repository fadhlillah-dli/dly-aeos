"""Canonical source identity (§37, §39, §50).

The same upstream can be written many ways (``https://github.com/Owner/Repo``, ``.git`` suffix,
``git@github.com:owner/repo``, ``/tree/main`` sub-paths, ``www.``, trailing slashes). Proposals and
registry entries compare *canonical* identities so duplicates are linked, while a fork, a rename or
a redirect is never silently treated as the same trusted source (redirects are resolved only via
recorded upstream checks, see ``aeos.research.service.resolve_alias``).

Forms:
  github:<owner>/<repo>     gitlab:<group>/<...>/<repo>     bitbucket:<owner>/<repo>
  url:<host><path>          name:<slug>
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from aeos.core.ids import slugify

_SCP_GIT = re.compile(r"^(?:ssh://)?git@(?P<host>[^:/\s]+)[:/](?P<path>[^\s?#]+?)/?$")
_HOSTLIKE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}(?::\d+)?(?:/|$)")
_FORGES = {"github.com": "github", "gitlab.com": "gitlab", "bitbucket.org": "bitbucket"}
# GitHub paths that are not repositories.
_GITHUB_RESERVED = {"orgs", "users", "settings", "marketplace", "topics", "collections", "sponsors", "features"}


class IdentityError(ValueError):
    pass


def _strip_git(name: str) -> str:
    return name[:-4] if name.lower().endswith(".git") else name


def _forge_identity(forge: str, path: str) -> str:
    parts = [p for p in path.split("/") if p]
    if not parts:
        raise IdentityError("repository path missing")
    if forge == "gitlab":
        # GitLab uses "/-/" to separate the project path from sub-pages.
        if "-" in parts:
            parts = parts[: parts.index("-")]
        parts[-1] = _strip_git(parts[-1])
        return f"gitlab:{'/'.join(parts).lower()}"
    owner = parts[0]
    if forge == "github" and owner.lower() in _GITHUB_RESERVED:
        raise IdentityError("not a repository URL")
    if len(parts) == 1:
        return f"{forge}:{owner.lower()}"
    return f"{forge}:{owner.lower()}/{_strip_git(parts[1]).lower()}"


def is_url(ref: str) -> bool:
    s = ref.strip()
    return "://" in s or s.startswith("git@") or bool(_HOSTLIKE.match(s))


def canonical_identity(ref: str) -> str:
    """Normalise a URL or a free-text name into a canonical identity string."""
    s = (ref or "").strip()
    if not s:
        raise IdentityError("empty reference")
    if len(s) > 2048:
        raise IdentityError("reference too long")
    m = _SCP_GIT.match(s)
    if m:
        host = m.group("host").lower().removeprefix("www.")
        forge = _FORGES.get(host)
        if forge:
            return _forge_identity(forge, m.group("path"))
        return f"url:{host}/{_strip_git(m.group('path')).strip('/')}"
    if is_url(s):
        if "://" not in s:
            s = "https://" + s
        parts = urlsplit(s)
        if parts.scheme.lower() not in ("http", "https", "ssh", "git"):
            raise IdentityError(f"unsupported URL scheme {parts.scheme!r}")
        if parts.username or parts.password:
            raise IdentityError("URL must not contain credentials")
        host = (parts.hostname or "").lower().removeprefix("www.")
        if not host:
            raise IdentityError("URL host missing")
        forge = _FORGES.get(host)
        if forge:
            return _forge_identity(forge, parts.path)
        path = parts.path.rstrip("/")
        port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
        return f"url:{host}{port}{path}"
    return f"name:{slugify(s, max_len=96)}"


def github_repo(identity: str) -> tuple[str, str] | None:
    """(owner, repo) for a ``github:owner/repo`` identity, else None."""
    if not identity.startswith("github:"):
        return None
    rest = identity.split(":", 1)[1]
    if rest.count("/") != 1:
        return None
    owner, repo = rest.split("/")
    if not owner or not repo:
        return None
    return owner, repo


def safe_public_url(url: str) -> str:
    """Validate a user-supplied URL for the registry (http/https, no credentials)."""
    s = (url or "").strip()
    if "://" not in s:
        s = "https://" + s
    parts = urlsplit(s)
    if parts.scheme.lower() not in ("http", "https"):
        raise IdentityError("only http(s) URLs can be added")
    if parts.username or parts.password:
        raise IdentityError("URL must not contain credentials")
    if not parts.hostname:
        raise IdentityError("URL host missing")
    return s
