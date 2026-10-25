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
from aeos.portfolio import providers as pv
from aeos.publisher.providers import atlassian as atl
from aeos.publisher.providers import github_issues as gh
from aeos.publisher.providers import google as gg
from aeos.secrets import facade as secrets

PURPOSES = ("docs", "issues")
# purpose -> adapter family per domain profile (§04). The family is the adapter key; ``provider`` (the one
# vocabulary of aeos.portfolio.providers) is what integrations, approvals, the interlock and the UI use.
PROFILE: dict[str, dict[str, str]] = {
    "PERSONAL": {"docs": "google", "issues": "github-issues"},
    "OFFICE": {"docs": "confluence", "issues": "jira"},
}
FAMILY_PROVIDER: dict[str, str] = {"google": pv.GOOGLE_DRIVE, "github-issues": pv.GITHUB_ISSUES,
                                   "confluence": pv.CONFLUENCE, "jira": pv.JIRA}
PROVIDER_LABEL = {fam: pv.LABELS[p] for fam, p in FAMILY_PROVIDER.items()}


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
    provider: str = ""  # canonical provider id (aeos.portfolio.providers)
    root_id: str = ""  # exact target identifier an approval binds ({provider, root_id})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def identity(self) -> dict[str, str]:
        return {"provider": self.provider, "root_id": self.root_id}


def _integration(session: Session, project: Project, family: str, purpose: str) -> ProjectIntegration | None:
    provider = FAMILY_PROVIDER[family]
    rows = [r for r in session.scalars(select(ProjectIntegration).where(ProjectIntegration.project_id == project.id))
            if r.domain == project.domain and pv.normalize(r.provider) == provider]  # never cross-domain
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
    if integ is not None and integ.root_id:
        return str(integ.root_id)  # GITHUB_REPOSITORY of the github-issues integration (exact target)
    if integ is not None and (integ.config or {}).get("repo"):
        return str(integ.config["repo"])  # legacy rows only (new config refuses a second target key)
    repos = [r for r in portfolio.repositories(session, project.id) if r.provider.upper() == "GITHUB"]
    repos.sort(key=lambda r: (r.role not in ("project", "platform"), r.full_name))
    return repos[0].full_name if repos else ""


def resolve_target(session: Session, project: Project, purpose: str) -> Target:
    family = PROFILE[project.domain][purpose]
    integ = _integration(session, project, family, purpose)
    t = Target(purpose=purpose, family=family, label=PROVIDER_LABEL[family], status="READY",
               integration_id=integ.id if integ else None, provider=FAMILY_PROVIDER[family])
    if family == "google":
        root = pv.target_identity(pv.GOOGLE_DRIVE, integ.root_id if integ else None)["root_id"]
        t.root_id = root
        t.config = {"root_folder_id": root, "root_label": "My Drive (root)" if root == pv.MY_DRIVE_ROOT else root,
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
        t.root_id = repo
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
        acfg = atl.AtlassianConfig.from_integration(integ.tenant_id if integ else None,
                                                    integ.root_id if integ else None, integ.config if integ else None,
                                                    product=family)
        t.root_id = acfg.parent_id if family == "confluence" else acfg.project_key
        status, reasons, remedy = atl.readiness(acfg, product=family)
        t.config = {"atlassian": asdict(acfg) | {"extra": {}}, "deployment": acfg.deployment, "enabled": acfg.enabled,
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
                gate_ok, gate_reasons = office_gate(session, project)
                if not gate_ok:
                    # §23 OFFICE default deny: enabled=true on the integration is not a company authorisation.
                    # No OFFICE read or write (capability probe, issue sync, publication) runs past this gate.
                    t.status = "BLOCKED"
                    t.reasons += gate_reasons
                    t.remedies.append("Catat otorisasi perusahaan (Decision) dan jalankan Readiness Doctor sampai "
                                      "CAN_USE_OFFICE_INTEGRATIONS READY.")
                else:
                    t.reasons.append("capability (Cloud REST v2, folder support) diperiksa saat eksekusi")
    return t


def office_gate(session: Session, project: Project) -> tuple[bool, list[str]]:
    """(ready, reasons) of the Doctor gate CAN_USE_OFFICE_INTEGRATIONS; unavailable → not ready (fail closed)."""
    try:
        from aeos.core.enums import Capability
        from aeos.doctor import facade as doctor

        state = doctor.capability_state(session, Capability.CAN_USE_OFFICE_INTEGRATIONS, domain="OFFICE",
                                        project_id=project.id)
    except Exception as exc:  # doctor unavailable or failing: never a reason to call an OFFICE system
        return False, [f"CAN_USE_OFFICE_INTEGRATIONS tidak dapat dievaluasi ({exc.__class__.__name__}) — fail closed"]
    if state.status == "READY":
        return True, []
    return False, [f"CAN_USE_OFFICE_INTEGRATIONS {state.status}: " + "; ".join(state.reasons[:3])]


def resolve_targets(session: Session, project: Project, purposes: list[str] | None = None) -> list[Target]:
    wanted = [p for p in (purposes or list(PURPOSES)) if p in PURPOSES]
    return [resolve_target(session, project, p) for p in dict.fromkeys(wanted)]
