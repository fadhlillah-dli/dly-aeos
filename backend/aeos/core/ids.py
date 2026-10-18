from __future__ import annotations

import re
import secrets
import uuid

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def new_id() -> str:
    """Stable UUID identity (§04: names/slugs are display attributes, never the key)."""
    return str(uuid.uuid4())


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def slugify(name: str, max_len: int = 48) -> str:
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    return (slug or "project")[:max_len].strip("-") or "project"
