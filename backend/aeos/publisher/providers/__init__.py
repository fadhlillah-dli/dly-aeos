"""Publication adapters (PERSONAL: Google Drive/Docs + GitHub Issues; OFFICE: Confluence + Jira + Bitbucket).

Operation provider names (what the interlock sees; the one vocabulary of ``aeos.portfolio.providers``)
map onto adapter families:

=================  ==============  =========================================
op.provider        family          ExternalReference.provider
=================  ==============  =========================================
google-drive       google          google-drive (folders + docs create/update; doc id = file id)
github-issues      github-issues   github-issues (labels + issues)
confluence         confluence      confluence (folders/pages/attachments)
jira               jira            jira (work items)
=================  ==============  =========================================

``google-docs`` is a legacy spelling of outbox rows planned by earlier builds (Docs API updates); it maps
to the same family/reference and is sent to the interlock as ``google-drive``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from aeos.publisher.providers.base import Provider

FAMILY_OF: dict[str, str] = {
    "google-drive": "google",
    "google-docs": "google",
    "github-issues": "github-issues",
    "confluence": "confluence",
    "jira": "jira",
}
REF_PROVIDER: dict[str, str] = {
    "google-drive": "google-drive",
    "google-docs": "google-drive",
    "github-issues": "github-issues",
    "confluence": "confluence",
    "jira": "jira",
}

ProviderFactory = Callable[[dict[str, Any]], Provider]
_overrides: dict[str, ProviderFactory] = {}


def set_provider_override(family: str, factory: ProviderFactory | None) -> None:
    """Tests inject fake/respx-backed adapters per family here."""
    if factory is None:
        _overrides.pop(family, None)
    else:
        _overrides[family] = factory


def build_provider(family: str, config: dict[str, Any]) -> Provider:
    """Create the adapter for *family* from a resolved target config (raises ProviderBlocked)."""
    if family in _overrides:
        return _overrides[family](config)
    if family == "google":
        from aeos.publisher.providers.google import GoogleDriveDocsProvider

        return GoogleDriveDocsProvider(root_folder_id=config.get("root_folder_id"))
    if family == "github-issues":
        from aeos.publisher.providers.github_issues import GitHubIssuesProvider

        return GitHubIssuesProvider(repo=config.get("repo", ""))
    if family in ("confluence", "jira"):
        from aeos.publisher.providers.atlassian import AtlassianConfig, ConfluenceProvider, JiraProvider

        cfg = AtlassianConfig(**config["atlassian"]) if isinstance(config.get("atlassian"), dict) else \
            AtlassianConfig()
        return ConfluenceProvider(cfg) if family == "confluence" else JiraProvider(cfg)
    raise KeyError(f"unknown provider family {family!r}")
