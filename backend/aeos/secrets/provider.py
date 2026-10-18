"""SecretProvider implementations. PLACEHOLDER — owned and completed by the secrets module agent.

The foundation ships an in-memory provider used only by tests; ``get_provider`` must be replaced
by the OpenBao + OS-keyring implementation (fail closed when neither is available).
"""

from __future__ import annotations

import threading
from typing import Protocol

from aeos.core.errors import Blocked
from aeos.core.redaction import register_secret
from aeos.core.timeutil import utcnow
from aeos.secrets.facade import SecretStatus, validate_ref


class SecretProvider(Protocol):
    name: str

    def get(self, ref: str, *, purpose: str, actor: str) -> str: ...
    def put(self, ref: str, value: str, *, actor: str) -> SecretStatus: ...
    def status(self, ref: str) -> SecretStatus: ...
    def list_status(self) -> list[SecretStatus]: ...


class MemorySecretProvider:
    """Test-only provider. Never selected outside tests."""

    name = "memory-test"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, tuple[str, object]] = {}

    def get(self, ref: str, *, purpose: str, actor: str) -> str:
        validate_ref(ref)
        with self._lock:
            item = self._data.get(ref)
        if item is None:
            raise Blocked(f"secret {ref} is not configured", code="SECRET_MISSING")
        register_secret(item[0])
        return item[0]

    def put(self, ref: str, value: str, *, actor: str) -> SecretStatus:
        validate_ref(ref)
        register_secret(value)
        with self._lock:
            self._data[ref] = (value, utcnow())
        return self.status(ref)

    def status(self, ref: str) -> SecretStatus:
        with self._lock:
            item = self._data.get(ref)
        return SecretStatus(ref=ref, exists=item is not None, backend=self.name,
                            updated_at=item[1] if item else None)  # type: ignore[arg-type]

    def list_status(self) -> list[SecretStatus]:
        with self._lock:
            refs = sorted(self._data)
        return [self.status(r) for r in refs]


_override: SecretProvider | None = None


def set_provider_override(provider: SecretProvider | None) -> None:
    """Tests inject a provider here."""
    global _override
    _override = provider


def get_provider() -> SecretProvider:
    if _override is not None:
        return _override
    raise Blocked("no secret store configured (OpenBao / OS keyring)", code="SECRET_STORE_UNAVAILABLE")
