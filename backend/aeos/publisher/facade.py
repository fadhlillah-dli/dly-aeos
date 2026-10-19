"""Cross-module contract for read-only provider lookups (§04). Owned by ``aeos.publisher``.

Only read operations are exposed here; every write goes through the publication outbox and its
interlock, never through this facade.
"""

from __future__ import annotations

from typing import Any

from aeos.publisher.providers.base import ProviderError

__all__ = ["ProviderError", "bitbucket_repository_reference"]


def bitbucket_repository_reference(full_name: str, *, client: Any = None) -> dict[str, Any]:
    """Read the exact Bitbucket repository identity (OFFICE). The caller has already checked the company
    authorisation and that the adapter is enabled (ADR-0073); raises ``ProviderError`` on any provider refusal."""
    from aeos.publisher.providers.atlassian import BitbucketProvider

    prov = BitbucketProvider(full_name=full_name, enabled=True, client=client)
    try:
        return prov.repository_reference()
    finally:
        prov.close()
