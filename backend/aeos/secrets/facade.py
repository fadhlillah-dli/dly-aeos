"""Cross-module contract for secrets (§26). Owned by ``aeos.secrets``.

Secret references are logical names inside a namespace, e.g. ``personal/openrouter``,
``personal/github``, ``personal/google-oauth``, ``office/atlassian``, ``workers/openrouter``.
Namespaces keep OFFICE, PERSONAL, worker models and per-project credentials apart.

Rules every implementation must keep:
* values are returned only to trusted in-process callers (broker, publisher adapters, bridge);
  never to the UI, models, worker sandboxes or logs; each fetched value is registered with
  ``aeos.core.redaction.register_secret``;
* when the backing store is unavailable the call fails closed with ``Blocked``;
* status calls never include values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

NAMESPACES = ("personal", "office", "workers", "projects", "platform")


@dataclass(frozen=True)
class SecretStatus:
    ref: str
    exists: bool
    backend: str  # "openbao" | "os-keyring" | "none"
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    scope: dict[str, Any] = field(default_factory=dict)
    note: str = ""


def validate_ref(ref: str) -> tuple[str, str]:
    ns, _, name = ref.partition("/")
    if ns not in NAMESPACES or not name or ".." in name or name.startswith("/"):
        raise ValueError(f"invalid secret reference: {ref!r}")
    return ns, name


def get_secret(ref: str, *, purpose: str, actor: str) -> str:
    """Return a secret value for an in-process trusted caller. Raises ``Blocked`` if unavailable."""
    from aeos.secrets.provider import get_provider

    return get_provider().get(ref, purpose=purpose, actor=actor)


def put_secret(ref: str, value: str, *, actor: str) -> SecretStatus:
    from aeos.secrets.provider import get_provider

    return get_provider().put(ref, value, actor=actor)


def secret_status(ref: str) -> SecretStatus:
    from aeos.secrets.provider import get_provider

    return get_provider().status(ref)


def list_status() -> list[SecretStatus]:
    from aeos.secrets.provider import get_provider

    return get_provider().list_status()
