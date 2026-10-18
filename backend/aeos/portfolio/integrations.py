"""Integration Registry (§04, §10, §13 ProjectIntegration, §35 Integrations & Access).

Records provider + purpose + exact tenant/root identifiers + a credential *reference* (never a
value). Capability status here is configuration-level only: connectivity and functional proof
come from the Doctor/publisher and are never claimed by this module (CONNECTED != VERIFIED).
OFFICE providers are accepted only in OFFICE projects and stay deny-by-default (§23).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.enums import Domain
from aeos.core.errors import AeosError, ValidationFailed
from aeos.core.redaction import contains_secret
from aeos.core.timeutil import utcnow
from aeos.events import service as events
from aeos.identity.service import Principal
from aeos.models.portfolio import Project, ProjectIntegration
from aeos.portfolio.service import deny, require_writable

PURPOSES = ("docs", "issues", "code", "ci", "roadmap")
# domain -> provider -> allowed purposes (U-02..U-04, §04 table)
PROVIDERS: dict[str, dict[str, frozenset[str]]] = {
    Domain.PERSONAL.value: {
        "GITHUB": frozenset({"code"}),
        "GITHUB_ISSUES": frozenset({"issues"}),
        "GITHUB_PROJECTS": frozenset({"roadmap"}),
        "GITHUB_ACTIONS": frozenset({"ci"}),
        "GOOGLE_DRIVE": frozenset({"docs"}),
        "GOOGLE_DOCS": frozenset({"docs"}),
    },
    Domain.OFFICE.value: {
        "BITBUCKET": frozenset({"code"}),
        "BITBUCKET_PIPELINES": frozenset({"ci"}),
        "JIRA": frozenset({"issues", "roadmap"}),
        "CONFLUENCE": frozenset({"docs"}),
    },
}
OFFICE_PROVIDERS = frozenset(PROVIDERS[Domain.OFFICE.value])
ATLASSIAN_PROVIDERS = frozenset({"JIRA", "CONFLUENCE"})
CREDENTIAL_NAMESPACES = {Domain.PERSONAL.value: ("personal", "projects"), Domain.OFFICE.value: ("office", "projects")}
ROOT_ID_NAMES = {"GOOGLE_DRIVE": "GOOGLE_DRIVE_FOLDER_ID", "GOOGLE_DOCS": "GOOGLE_DRIVE_FOLDER_ID",
                 "CONFLUENCE": "CONFLUENCE_PROJECTS_PARENT_ID", "JIRA": "JIRA_PROJECT_KEY",
                 "GITHUB_ISSUES": "GITHUB_REPOSITORY", "GITHUB": "GITHUB_REPOSITORY",
                 "BITBUCKET": "BITBUCKET_REPOSITORY", "GITHUB_PROJECTS": "GITHUB_PROJECT_ID"}
ROOT_REQUIRED = frozenset({"GOOGLE_DRIVE", "GOOGLE_DOCS", "CONFLUENCE", "JIRA", "GITHUB_ISSUES"})
_ROOT_PATTERNS = {
    "GOOGLE_DRIVE": re.compile(r"^[A-Za-z0-9_-]{10,128}$"),
    "GOOGLE_DOCS": re.compile(r"^[A-Za-z0-9_-]{10,128}$"),
    "CONFLUENCE": re.compile(r"^\d{1,20}$"),
    "JIRA": re.compile(r"^[A-Z][A-Z0-9_]{1,19}$"),
    "GITHUB_ISSUES": re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$"),
    "GITHUB": re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$"),
    "BITBUCKET": re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,61}/[A-Za-z0-9._-]{1,100}$"),
}
_GENERIC_ID = re.compile(r"^[A-Za-z0-9._:/@-]{1,256}$")
_TENANT = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_SENSITIVE_KEY = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|credential|cookie|private[_-]?key)")
_TARGET_KEY = re.compile(r"(?i)(parent|root|folder|target|alias|page[_-]?id)")
REMEDIES = {
    ("GOOGLE_DRIVE", "root"): "aeos publish google-connect",
    ("GOOGLE_DOCS", "root"): "aeos publish google-connect",
    ("GOOGLE_DRIVE", "credential"): "aeos publish google-connect",
    ("GOOGLE_DOCS", "credential"): "aeos publish google-connect",
}


def _remedy(provider: str, what: str, credential_ref: str | None) -> str:
    if (provider, what) in REMEDIES:
        return REMEDIES[(provider, what)]
    if what == "credential":
        return f"aeos secrets set {credential_ref}" if credential_ref else "set credential_ref, then aeos secrets set <ref>"
    return f"PUT /api/projects/<id>/integrations with {ROOT_ID_NAMES.get(provider, 'root_id')}"


def _validate_id(value: Any, *, field: str, pattern: re.Pattern[str] | None) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValidationFailed(f"{field} must be a string", code="INVALID_FIELD", details={"field": field})
    v = value.strip()
    if contains_secret(v):
        raise ValidationFailed(f"{field} looks like a credential; store secrets with `aeos secrets set`",
                               code="SECRET_IN_FIELD", details={"field": field})
    if not (pattern or _GENERIC_ID).match(v):
        raise ValidationFailed(f"{field} has an invalid format", code="INVALID_FIELD", details={"field": field})
    return v


def _validate_credential_ref(ref: Any, project: Project) -> str | None:
    if ref in (None, ""):
        return None
    if not isinstance(ref, str):
        raise ValidationFailed("credential_ref must be a string", code="INVALID_CREDENTIAL_REF")
    from aeos.secrets.facade import validate_ref

    value = ref.strip()
    try:
        ns, name = validate_ref(value)
    except ValueError as exc:
        raise ValidationFailed(str(exc), code="INVALID_CREDENTIAL_REF") from exc
    if contains_secret(value):
        raise ValidationFailed("credential_ref must be a reference such as personal/github, never a value",
                               code="SECRET_IN_FIELD")
    domain = project.domain
    if ns not in CREDENTIAL_NAMESPACES[domain]:
        raise ValidationFailed(f"{domain} integrations use credential namespaces {CREDENTIAL_NAMESPACES[domain]}",
                               code="CREDENTIAL_NAMESPACE_MISMATCH")
    if ns == "projects" and name.split("/", 1)[0] not in {project.id, project.project_key}:
        # per-project credentials are bound to their own project: projects/<project_key>/<name>
        raise ValidationFailed(f"project credentials must be under projects/{project.project_key}/...",
                               code="CREDENTIAL_PROJECT_MISMATCH")
    return value


def _validate_config(config: Any) -> dict[str, Any]:
    if config in (None, {}):
        return {}
    if not isinstance(config, dict):
        raise ValidationFailed("config must be an object", code="INVALID_CONFIG")
    out: dict[str, Any] = {}
    for k, v in config.items():
        if not isinstance(k, str) or _SENSITIVE_KEY.search(k):
            raise ValidationFailed(f"config key {k!r} is not allowed (credentials go to the secret store)",
                                   code="SECRET_IN_FIELD")
        if _TARGET_KEY.search(k):
            # §04: the target lives only in root_id (e.g. CONFLUENCE_PROJECTS_PARENT_ID); a legacy alias in
            # config must never become a second target.
            raise ValidationFailed(f"config key {k!r} looks like a target id; set it as root_id instead",
                                   code="TARGET_IN_CONFIG")
        if isinstance(v, (dict, list)):
            raise ValidationFailed("config values must be scalars", code="INVALID_CONFIG")
        if isinstance(v, str) and (contains_secret(v) or len(v) > 512):
            raise ValidationFailed(f"config value for {k!r} is not allowed", code="SECRET_IN_FIELD")
        out[k] = v
    return out


def _credential_state(ref: str | None) -> dict[str, Any]:
    if not ref:
        return {"status": "MISSING", "reason": "no credential reference configured"}
    try:
        from aeos.secrets import facade as secrets

        st = secrets.secret_status(ref)
    except (ImportError, ModuleNotFoundError):
        return {"status": "UNKNOWN", "reason": "secret store module unavailable"}
    except AeosError as exc:
        return {"status": "UNKNOWN", "reason": f"secret store unavailable ({exc.code})"}
    return {"status": "CONFIGURED" if st.exists else "MISSING", "backend": st.backend,
            "expires_at": st.expires_at.isoformat() if st.expires_at else None}


def compute_capability_status(integration: ProjectIntegration) -> dict[str, Any]:
    """Configuration-level state only; never VERIFIED here (that needs a functional probe)."""
    root_needed = integration.provider in ROOT_REQUIRED
    root_state = "CONFIGURED" if integration.root_id else ("MISSING" if root_needed else "NOT_APPLICABLE")
    cred = _credential_state(integration.credential_ref)
    remedies: list[dict[str, str]] = []
    if root_state == "MISSING":
        remedies.append({"issue": f"{ROOT_ID_NAMES.get(integration.provider, 'root_id')} belum diatur",
                         "command": _remedy(integration.provider, "root", integration.credential_ref)})
    if cred["status"] != "CONFIGURED":
        remedies.append({"issue": f"credential {integration.credential_ref or '(none)'}: {cred['status']}",
                         "command": _remedy(integration.provider, "credential", integration.credential_ref)})
    office = integration.domain == Domain.OFFICE.value
    if office:
        remedies.append({"issue": "OFFICE default deny (§23): needs a recorded company authorisation",
                         "command": "aeos policy show office"})
    extra: dict[str, Any] = {}
    if integration.provider in ATLASSIAN_PROVIDERS:
        # §04: Atlassian Cloud is a blueprint assumption, not a fact about the tenant.
        deployment = str((integration.config or {}).get("deployment") or "UNKNOWN").upper()
        extra["atlassian_deployment"] = deployment if deployment in {"CLOUD", "DATA_CENTER"} else "UNKNOWN"
        if extra["atlassian_deployment"] == "UNKNOWN":
            remedies.append({"issue": "Atlassian Cloud vs Data Center belum diverifikasi untuk tenant ini",
                             "command": "Doctor: Project adapters capability check (tenant/space/parent)"})
    configured = root_state != "MISSING" and cred["status"] == "CONFIGURED"
    return {
        **extra,
        "configuration": "CONFIGURED" if configured else "MISSING",
        "root": root_state,
        "root_id_name": ROOT_ID_NAMES.get(integration.provider),
        "credential": cred,
        "connectivity": "NOT_CHECKED",
        "functional": "NOT_TESTED",
        "write_enabled": False,
        "office_default_deny": office,
        "remedies": remedies,
        "computed_at": utcnow().isoformat(),
        "source": "integration-registry (configuration only)",
    }


def upsert_integration(session: Session, principal: Principal, project: Project, body: dict[str, Any]) -> ProjectIntegration:
    if not isinstance(body, dict):
        raise ValidationFailed("request body must be an object", code="INVALID_BODY")
    require_writable(project)
    if any("domain" in str(k).lower() for k in body):
        deny(session, principal, action="portfolio.integration.update", code="DOMAIN_IMMUTABLE", project=project,
             reason="integration domain follows the project domain and cannot be set")
    provider = str(body.get("provider") or "").strip().upper()
    purpose = str(body.get("purpose") or "").strip().lower()
    allowed = PROVIDERS[project.domain]
    if provider not in allowed:
        if provider in OFFICE_PROVIDERS and project.domain != Domain.OFFICE.value:
            deny(session, principal, action="portfolio.integration.update", code="OFFICE_PROVIDER_IN_PERSONAL",
                 project=project, reason=f"{provider} is an OFFICE provider; it is not allowed in a PERSONAL project")
        deny(session, principal, action="portfolio.integration.update", code="PROVIDER_DOMAIN_MISMATCH",
             project=project, reason=f"provider {provider or '(none)'} is not allowed for {project.domain} projects",
             details={"allowed": sorted(allowed)})
    if purpose not in PURPOSES or purpose not in allowed[provider]:
        raise ValidationFailed(f"purpose {purpose or '(none)'} is not valid for {provider}", code="INVALID_PURPOSE",
                               details={"allowed": sorted(allowed[provider])})
    unknown = sorted(set(body) - {"provider", "purpose", "tenant_id", "root_id", "credential_ref", "config"})
    if unknown:
        raise ValidationFailed(f"unknown fields: {', '.join(unknown)}", code="FIELD_NOT_EDITABLE")
    tenant = _validate_id(body.get("tenant_id"), field="tenant_id", pattern=_TENANT)
    root = _validate_id(body.get("root_id"), field="root_id", pattern=_ROOT_PATTERNS.get(provider))
    cred_ref = _validate_credential_ref(body.get("credential_ref"), project)
    config = _validate_config(body.get("config"))

    integ = session.scalar(select(ProjectIntegration).where(
        ProjectIntegration.project_id == project.id, ProjectIntegration.provider == provider,
        ProjectIntegration.purpose == purpose))
    created = integ is None
    if integ is None:
        integ = ProjectIntegration(project_id=project.id, domain=project.domain, provider=provider, purpose=purpose)
        session.add(integ)
    before = {"tenant_id": integ.tenant_id, "root_id": integ.root_id, "credential_ref": integ.credential_ref}
    integ.tenant_id = tenant
    integ.root_id = root
    integ.credential_ref = cred_ref
    integ.config = {**config, "root_id_name": ROOT_ID_NAMES.get(provider)}
    target_changed = not created and before != {"tenant_id": tenant, "root_id": root, "credential_ref": cred_ref}
    if target_changed:
        integ.last_verified_at = None  # a new target/identity invalidates earlier proof (§08)
    integ.capability_status = compute_capability_status(integ)
    session.flush()
    invalidation = "NOT_NEEDED"
    if target_changed:
        invalidation = _invalidate_checks(session, integ)
    audit.record(session, actor_type="user", actor_id=principal.user.id, action="portfolio.integration.update",
                 target_type="integration", target_id=integ.id, project_id=project.id, domain=project.domain,
                 details={"provider": provider, "purpose": purpose, "created": created, "root_set": bool(root),
                          "tenant_set": bool(tenant), "credential_ref": cred_ref, "target_changed": target_changed,
                          "doctor_invalidation": invalidation})
    events.emit(session, source="portfolio", kind="portfolio.integration.updated", entity_type="integration",
                entity_id=integ.id, project_id=project.id, domain=project.domain,
                payload={"provider": provider, "purpose": purpose,
                         "configuration": integ.capability_status.get("configuration")})
    return integ


def _invalidate_checks(session: Session, integ: ProjectIntegration) -> str:
    try:
        from aeos.doctor import facade as doctor

        n = doctor.invalidate(session, invalidator=f"integration:{integ.id}", reason="integration target changed")
        return f"INVALIDATED:{n}"
    except (ImportError, ModuleNotFoundError):
        return "UNAVAILABLE"  # doctor module absent: nothing was VERIFIED by it either
    except AeosError as exc:
        return f"FAILED:{exc.code}"


def ensure_integration(session: Session, project: Project, *, provider: str, purpose: str, root_id: str | None = None,
                       tenant_id: str | None = None, credential_ref: str | None = None,
                       config: dict[str, Any] | None = None) -> tuple[ProjectIntegration, bool]:
    """Idempotent registration used by seeds (no principal, no overwrite of existing target ids)."""
    integ = session.scalar(select(ProjectIntegration).where(
        ProjectIntegration.project_id == project.id, ProjectIntegration.provider == provider,
        ProjectIntegration.purpose == purpose))
    if integ is not None:
        integ.capability_status = compute_capability_status(integ)
        return integ, False
    integ = ProjectIntegration(project_id=project.id, domain=project.domain, provider=provider, purpose=purpose,
                               root_id=root_id, tenant_id=tenant_id, credential_ref=credential_ref,
                               config={**(config or {}), "root_id_name": ROOT_ID_NAMES.get(provider)})
    session.add(integ)
    integ.capability_status = compute_capability_status(integ)
    session.flush()
    return integ, True


def target_link(integ: ProjectIntegration) -> str | None:
    """Official folder/page/tracker link built from the exact stored identifiers (§37); never guessed."""
    root, tenant = integ.root_id, integ.tenant_id
    if not root:
        return None
    if integ.provider in {"GOOGLE_DRIVE", "GOOGLE_DOCS"}:
        return f"https://drive.google.com/drive/folders/{quote(root, safe='')}"
    if integ.provider == "GITHUB_ISSUES":
        return f"https://github.com/{root}/issues"
    if integ.provider == "GITHUB":
        return f"https://github.com/{root}"
    if integ.provider == "BITBUCKET":
        return f"https://bitbucket.org/{root}"
    if integ.provider == "CONFLUENCE" and tenant:
        return f"https://{tenant}/wiki/pages/viewpage.action?pageId={quote(root, safe='')}"
    if integ.provider == "JIRA" and tenant:
        return f"https://{tenant}/browse/{quote(root, safe='')}"
    return None


def integration_view(integ: ProjectIntegration) -> dict[str, Any]:
    return {
        "target_url": target_link(integ),
        "id": integ.id,
        "project_id": integ.project_id,
        "domain": integ.domain,
        "provider": integ.provider,
        "purpose": integ.purpose,
        "tenant_id": integ.tenant_id,
        "root_id": integ.root_id,
        "root_id_name": (integ.config or {}).get("root_id_name") or ROOT_ID_NAMES.get(integ.provider),
        "credential_ref": integ.credential_ref,
        "config": {k: v for k, v in (integ.config or {}).items() if k != "root_id_name"},
        "capability_status": integ.capability_status or {},
        "last_verified_at": integ.last_verified_at.isoformat() if integ.last_verified_at else None,
        "updated_at": integ.updated_at.isoformat() if integ.updated_at else None,
    }


def live_integration_view(integ: ProjectIntegration) -> dict[str, Any]:
    """View with the configuration state re-evaluated now (credential/root), never persisted."""
    view = integration_view(integ)
    view["capability_status"] = compute_capability_status(integ)
    return view


def integration_state(integrations: list[ProjectIntegration],
                      statuses: dict[str, dict[str, Any]] | None = None) -> dict[str, str]:
    """Per-purpose summary for list views: NOT_CONFIGURED | MISSING | CONFIGURED | VERIFIED.

    *statuses* (integration id -> capability status computed on read) takes precedence over the
    value stored at the last PUT, so a credential stored later is reflected without another PUT."""
    out: dict[str, str] = {}
    for purpose in ("docs", "issues"):
        rows = [i for i in integrations if i.purpose == purpose]
        if not rows:
            out[purpose] = "NOT_CONFIGURED"
        elif any(i.last_verified_at for i in rows):
            out[purpose] = "VERIFIED"
        elif any(((statuses or {}).get(i.id) or i.capability_status or {}).get("configuration") == "CONFIGURED"
                 for i in rows):
            out[purpose] = "CONFIGURED"
        else:
            out[purpose] = "MISSING"
    return out
