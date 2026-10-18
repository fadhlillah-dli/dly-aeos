"""Publication targets per domain profile (master §04).

The domain decides the provider family — never configuration drift or model output:

* PERSONAL → docs: Google Drive/Docs (folder per project) · issues: GitHub Issues
* OFFICE   → docs: Confluence (project folder under CONFLUENCE_PROJECTS_PARENT_ID) · issues: Jira

An integration configured for the other domain's providers is ignored, so OFFICE content can never
be routed to a personal system (§04, §23). Readiness here is static (credential presence,
configuration, enablement). "Stored" is not "verified": the first API call is the real proof.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.errors import Blocked
from aeos.models.portfolio import Project, ProjectIntegration
from aeos.portfolio import facade as portfolio
from aeos.publisher.providers import atlassian as atl
from aeos.publisher.providers import github_issues as gh
from aeos.publisher.providers import google as gg
from aeos.secrets import facade as secrets

PURPOSES = ("docs", "issues")
PROFILE: dict[str, dict[str, str]] = {
    "PERSONAL": {"docs": "google", "issues": "github-issues"},
    "OFFICE": {"docs": "confluence", "issues": "jira"},
}
INTEGRATION_PROVIDERS: dict[str, tuple[str, ...]] = {
    "google": ("google-drive", "google", "google-docs"),
    "github-issues": ("github-issues", "github"),
    "confluence": ("confluence",),
    "jira": ("jira",),
}
PROVIDER_LABEL = {"google": "Google Drive / Google Docs", "github-issues": "GitHub Issues",
                  "confluence": "Confluence", "jira": "Jira"}


@dataclass
class Target:
    purpose: str
    family: str
    label: str
    status: str  # READY | BLOCKED | NOT_SUPPORTED
    reasons: list[str] = field(default_factory=list)
    remedies: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)  # non-secret
    integration_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _integration(session: Session, project: Project, family: str, purpose: str) -> ProjectIntegration | None:
    rows = list(session.scalars(select(ProjectIntegration).where(
        ProjectIntegration.project_id == project.id,
        ProjectIntegration.provider.in_(INTEGRATION_PROVIDERS[family]))))
    rows = [r for r in rows if r.domain == project.domain]  # never cross-domain
    for r in rows:
        if r.purpose in (purpose, "default"):
            return r
    return rows[0] if rows else None


def _secret_exists(ref: str) -> tuple[bool, str]:
    try:
        st = secrets.secret_status(ref)
    except Blocked as exc:
        return False, exc.message
    except Exception as exc:  # pragma: no cover - defensive: unknown provider failure fails closed
        return False, f"secret store error: {exc.__class__.__name__}"
    return st.exists, "" if st.exists else f"credential {ref} belum disimpan"


def _github_repo(session: Session, project: Project, integ: ProjectIntegration | None) -> str:
    if integ is not None and (integ.config or {}).get("repo"):
        return str(integ.config["repo"])
    repos = [r for r in portfolio.repositories(session, project.id) if r.provider.upper() == "GITHUB"]
    repos.sort(key=lambda r: (r.role != "project", r.full_name))
    return repos[0].full_name if repos else ""


def resolve_target(session: Session, project: Project, purpose: str) -> Target:
    family = PROFILE[project.domain][purpose]
    integ = _integration(session, project, family, purpose)
    t = Target(purpose=purpose, family=family, label=PROVIDER_LABEL[family], status="READY",
               integration_id=integ.id if integ else None)
    if family == "google":
        root = (integ.root_id if integ else None) or "root"
        t.config = {"root_folder_id": root, "root_label": "My Drive (root)" if root == "root" else root,
                    "scope": gg.DRIVE_FILE_SCOPE}
        for ref in (gg.CLIENT_REF, gg.TOKEN_REF):
            ok, why = _secret_exists(ref)
            if not ok:
                t.status = "BLOCKED"
                t.reasons.append(why)
        if t.status == "BLOCKED":
            t.remedies.append(gg.CONNECT_REMEDY)
    elif family == "github-issues":
        repo = _github_repo(session, project, integ)
        cfg = dict(integ.config or {}) if integ else {}
        t.config = {"repo": repo, "issue_granularity": cfg.get("issue_granularity", "task"),
                    "milestone_labels": bool(cfg.get("milestone_labels", False)),
                    "capabilities": dict(gh.CAPABILITIES)}
        if not repo:
            t.status = "BLOCKED"
            t.reasons.append("belum ada repository GitHub yang terhubung ke proyek")
            t.remedies.append("Hubungkan repository GitHub proyek (Portofolio → repository).")
        ok, why = _secret_exists(gh.TOKEN_REF)
        if not ok:
            t.status = "BLOCKED"
            t.reasons.append(why)
            t.remedies.append(gh.CONNECT_REMEDY)
    else:
        cfg = atl.AtlassianConfig.from_integration(integ.tenant_id if integ else None,
                                                   integ.root_id if integ else None, integ.config if integ else None)
        status, reasons, remedy = atl.readiness(cfg, product=family)
        t.config = {"atlassian": asdict(cfg) | {"extra": {}}, "deployment": cfg.deployment, "enabled": cfg.enabled,
                    "parent_key": atl.CONFLUENCE_PARENT_KEY}
        if status != "READY_TO_CHECK":
            t.status = "NOT_SUPPORTED" if status == "NOT_SUPPORTED" else "BLOCKED"
            t.reasons += reasons
            t.remedies.append(remedy)
        else:
            ok, why = _secret_exists(atl.ATLASSIAN_REF)
            if not ok:
                t.status = "BLOCKED"
                t.reasons.append(why)
                t.remedies.append(f"aeos secrets set {atl.ATLASSIAN_REF} --stdin (JSON email + api_token)")
            else:
                t.reasons.append("capability (Cloud REST v2, folder support) diperiksa saat eksekusi")
    return t


def resolve_targets(session: Session, project: Project, purposes: list[str] | None = None) -> list[Target]:
    wanted = [p for p in (purposes or list(PURPOSES)) if p in PURPOSES]
    return [resolve_target(session, project, p) for p in dict.fromkeys(wanted)]
