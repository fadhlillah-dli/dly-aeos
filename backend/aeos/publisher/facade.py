"""Cross-module contract for read-only provider lookups (§04). Owned by ``aeos.publisher``.

Only read operations are exposed here; every write goes through the publication outbox and its
interlock, never through this facade.
"""

from __future__ import annotations

from typing import Any

from aeos.publisher.providers.base import ProviderError

__all__ = ["ProviderError", "bitbucket_read_auth", "bitbucket_reader", "bitbucket_repository_reference",
           "observe_httpx_response", "observe_provider_response"]


def observe_provider_response(method: str, url: str, status: int, headers: Any = None) -> None:
    """Additive (W7, 2026-10-25): report one provider response that did not go through the publisher's ``send``
    (delivery GitHub reads, broker PR calls, ``git push``/``ls-remote`` over HTTPS) to the same response-observer
    hook (§08 per-call permission observation). Method, URL, status and response headers only - never a body, a
    request header or a credential. Never raises."""
    from aeos.publisher.providers.base import notify_response

    try:
        pairs: list[tuple[Any, Any]] = list(dict(headers or {}).items())
    except (TypeError, ValueError):
        pairs = []
    notify_response(str(method), str(url), int(status), {str(k).lower(): str(v) for k, v in pairs})


def observe_httpx_response(response: Any) -> None:
    """Additive (W7, 2026-10-25): ``httpx`` response event hook form of :func:`observe_provider_response`."""
    try:
        req = response.request
        observe_provider_response(req.method, str(req.url.copy_with(query=None)), response.status_code,
                                  response.headers)
    except Exception:  # an observer never breaks a provider call
        return


def bitbucket_read_auth(*, use: str, actor: str) -> str:
    """API Authorization value of the OFFICE Bitbucket credential for a trusted in-process GET caller
    (secrets purpose ``office:<use>``). Additive (K1, 2026-10-19); never returned to UI/logs."""
    from aeos.publisher.providers.atlassian import bitbucket_read_auth as impl

    return impl(use=use, actor=actor)


def bitbucket_reader(full_name: str, *, auth_header: str, client: Any = None) -> Any:
    """GET-only Bitbucket Cloud client (``BitbucketProvider``) for an exact ``workspace/repo_slug``. The caller
    has already checked the company authorisation, the enabled adapter and Cloud (not Data Center); the
    object has no write method. Close it with ``.close()``. Additive (K1, 2026-10-19)."""
    from aeos.publisher.providers.atlassian import BitbucketProvider

    return BitbucketProvider(full_name=full_name, enabled=True, client=client, auth_header=auth_header)


def bitbucket_repository_reference(full_name: str, *, client: Any = None) -> dict[str, Any]:
    """Read the exact Bitbucket repository identity (OFFICE). The caller has already checked the company
    authorisation and that the adapter is enabled (ADR-0073); raises ``ProviderError`` on any provider refusal."""
    from aeos.publisher.providers.atlassian import BitbucketProvider

    prov = BitbucketProvider(full_name=full_name, enabled=True, client=client)
    try:
        return prov.repository_reference()
    finally:
        prov.close()
