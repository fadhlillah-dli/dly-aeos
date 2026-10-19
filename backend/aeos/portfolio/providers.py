"""One provider vocabulary for the Integration Registry, publisher, interlock, doctor and UI (§04, §10, §13).

Every module that names an external provider uses these lowercase ids. ``ProjectIntegration.provider``
stores them; the publisher's operations carry them as ``WriteIntent.provider``; the external-write
interlock and write enablement allow-list them per domain; approvals bind targets as
``{"provider": <id>, "root_id": <exact id>}``. Legacy spellings (``GOOGLE_DRIVE``, ``GOOGLE_DOCS``,
``GITHUB_ISSUES``, ``google-docs``, ...) are accepted on input only and normalised here; they are
never stored again (the portfolio seed migrates old rows).

This module is a leaf (imports only core enums) so policy, publisher and doctor can import it.
"""

from __future__ import annotations

from aeos.core.enums import Domain

GOOGLE_DRIVE = "google-drive"
GITHUB_REPO = "github-repo"
GITHUB_ISSUES = "github-issues"
GITHUB_PROJECTS = "github-projects"
GITHUB_ACTIONS = "github-actions"
CONFLUENCE = "confluence"
JIRA = "jira"
BITBUCKET = "bitbucket"
BITBUCKET_PIPELINES = "bitbucket-pipelines"

# domain -> provider -> allowed integration purposes (U-02..U-04, §04 table)
INTEGRATION_PROVIDERS: dict[str, dict[str, frozenset[str]]] = {
    Domain.PERSONAL.value: {
        GITHUB_REPO: frozenset({"code"}),
        GITHUB_ISSUES: frozenset({"issues"}),
        GITHUB_PROJECTS: frozenset({"roadmap"}),
        GITHUB_ACTIONS: frozenset({"ci"}),
        GOOGLE_DRIVE: frozenset({"docs"}),
        # §01/§04 per-project environment: the owner's chosen PERSONAL staging/deploy target, named by an
        # exact identity (container:/host:/vm:<name>). Registering it configures; deploying to it still
        # needs a human WRITE_ENABLEMENT decision for that exact target (§04 "with owner authorisation").
        "local-target": frozenset({"staging", "deploy"}),
    },
    Domain.OFFICE.value: {
        BITBUCKET: frozenset({"code"}),
        BITBUCKET_PIPELINES: frozenset({"ci"}),
        JIRA: frozenset({"issues", "roadmap"}),
        CONFLUENCE: frozenset({"docs"}),
    },
}

# Providers AEOS may *write* to, per domain (the external-write interlock allow-list, §26).
# Google Docs are files inside the project's Google Drive folder: creating (DOCX import) and
# updating (Docs API batchUpdate under requiredRevisionId) a document are operations on the
# ``google-drive`` target, so one write enablement and one approved target cover both.
WRITE_PROVIDERS: dict[str, tuple[str, ...]] = {
    Domain.PERSONAL.value: (GOOGLE_DRIVE, GITHUB_ISSUES, GITHUB_REPO),
    Domain.OFFICE.value: (CONFLUENCE, JIRA, BITBUCKET),
}

# Publication purpose -> provider per domain profile (§04): the domain decides, never configuration.
PUBLICATION_PROVIDER: dict[str, dict[str, str]] = {
    Domain.PERSONAL.value: {"docs": GOOGLE_DRIVE, "issues": GITHUB_ISSUES},
    Domain.OFFICE.value: {"docs": CONFLUENCE, "issues": JIRA},
}

# Name of the exact target identifier stored in ``ProjectIntegration.root_id``.
ROOT_ID_NAMES: dict[str, str] = {
    "local-target": "ENVIRONMENT_TARGET_ID",
    GOOGLE_DRIVE: "GOOGLE_DRIVE_FOLDER_ID",
    GITHUB_ISSUES: "GITHUB_REPOSITORY",
    GITHUB_REPO: "GITHUB_REPOSITORY",
    GITHUB_PROJECTS: "GITHUB_PROJECT_ID",
    CONFLUENCE: "CONFLUENCE_PROJECTS_PARENT_ID",
    JIRA: "JIRA_PROJECT_KEY",
    BITBUCKET: "BITBUCKET_REPOSITORY",
}
# Google Drive root folder is optional: without it the project folder is created in My Drive.
MY_DRIVE_ROOT = "root"

LABELS: dict[str, str] = {
    "local-target": "Target lingkungan PERSONAL (staging/deploy)",
    GOOGLE_DRIVE: "Google Drive / Google Docs",
    GITHUB_REPO: "GitHub repository",
    GITHUB_ISSUES: "GitHub Issues",
    GITHUB_PROJECTS: "GitHub Projects",
    GITHUB_ACTIONS: "GitHub Actions",
    CONFLUENCE: "Confluence",
    JIRA: "Jira",
    BITBUCKET: "Bitbucket",
    BITBUCKET_PIPELINES: "Bitbucket Pipelines",
}

# Default credential *references* (never values) per provider. Google: the OAuth token written by
# ``aeos publish google-connect`` (the client config lives next to it at personal/google-oauth-client).
GOOGLE_TOKEN_REF = "personal/google-oauth-token"
GOOGLE_CLIENT_REF = "personal/google-oauth-client"
LEGACY_GOOGLE_REF = "personal/google-oauth"  # earlier seed default; never written by the publisher
DEFAULT_CREDENTIAL_REFS: dict[str, str] = {
    GOOGLE_DRIVE: GOOGLE_TOKEN_REF,
    GITHUB_ISSUES: "personal/github",
    GITHUB_REPO: "personal/github",
    CONFLUENCE: "office/atlassian",
    JIRA: "office/atlassian",
    BITBUCKET: "office/bitbucket",
}

# Readiness Doctor checks that probe each provider (ids registered in ``aeos.doctor.checks``).
DOCTOR_CHECKS: dict[str, tuple[str, ...]] = {
    GOOGLE_DRIVE: ("adapter.google_oauth",),
    GITHUB_ISSUES: ("adapter.github_token",),
    GITHUB_REPO: ("adapter.github_token",),
    CONFLUENCE: ("adapter.office_confluence",),
    JIRA: ("adapter.office_jira",),
    BITBUCKET: ("adapter.office_bitbucket",),
}

ATLASSIAN = frozenset({CONFLUENCE, JIRA})
ATLASSIAN_DEPLOYMENTS = ("CLOUD", "DATA_CENTER", "UNKNOWN")

# Input-only spellings used by earlier builds (portfolio upper-case enum, publisher families).
LEGACY_ALIASES: dict[str, str] = {
    "GOOGLE_DRIVE": GOOGLE_DRIVE,
    "GOOGLE_DOCS": GOOGLE_DRIVE,
    "GOOGLE-DOCS": GOOGLE_DRIVE,
    "GOOGLE": GOOGLE_DRIVE,
    "GITHUB": GITHUB_REPO,
    "GITHUB_REPO": GITHUB_REPO,
    "GITHUB_ISSUES": GITHUB_ISSUES,
    "GITHUB_PROJECTS": GITHUB_PROJECTS,
    "GITHUB_ACTIONS": GITHUB_ACTIONS,
    "CONFLUENCE": CONFLUENCE,
    "JIRA": JIRA,
    "BITBUCKET": BITBUCKET,
    "BITBUCKET_PIPELINES": BITBUCKET_PIPELINES,
}

ALL_PROVIDERS: tuple[str, ...] = tuple(dict.fromkeys(p for ps in INTEGRATION_PROVIDERS.values() for p in ps))


def normalize(provider: str | None) -> str:
    """Canonical id for *provider* (legacy spellings accepted); unknown values are returned lower-cased
    so callers can reject them with their own error (never silently mapped to another provider)."""
    raw = str(provider or "").strip()
    if not raw:
        return ""
    if raw in ALL_PROVIDERS:
        return raw
    alias = LEGACY_ALIASES.get(raw.upper().replace("-", "_")) or LEGACY_ALIASES.get(raw.upper())
    if alias:
        return alias
    return raw.lower()


def is_legacy(provider: str | None) -> bool:
    return bool(provider) and provider not in ALL_PROVIDERS and normalize(provider) in ALL_PROVIDERS


def providers_for(domain: str | Domain) -> dict[str, frozenset[str]]:
    return INTEGRATION_PROVIDERS.get(Domain(domain).value, {})


def write_providers_for(domain: str | Domain) -> tuple[str, ...]:
    return WRITE_PROVIDERS.get(Domain(domain).value, ())


def domain_of(provider: str) -> str | None:
    p = normalize(provider)
    for dom, ps in INTEGRATION_PROVIDERS.items():
        if p in ps:
            return dom
    return None


def publication_provider(domain: str | Domain, purpose: str) -> str | None:
    return PUBLICATION_PROVIDER.get(Domain(domain).value, {}).get(purpose)


def purpose_of_publication_provider(domain: str | Domain, provider: str) -> str | None:
    for purpose, p in PUBLICATION_PROVIDER.get(Domain(domain).value, {}).items():
        if p == normalize(provider):
            return purpose
    return None


def target_identity(provider: str, root_id: str | None) -> dict[str, str]:
    """The exact target an approval binds and an external write names: ``{provider, root_id}``.

    For Google Drive without a configured folder the project folder lives in My Drive (``root``)."""
    p = normalize(provider)
    rid = (root_id or "").strip()
    if not rid and p == GOOGLE_DRIVE:
        rid = MY_DRIVE_ROOT
    return {"provider": p, "root_id": rid}
