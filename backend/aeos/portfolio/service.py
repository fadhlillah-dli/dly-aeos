"""Gateway & Portfolio service (§04, §05 steps 2-4, §10, §13).

Domain-first (U-05): a project is created only with an explicit, human-chosen domain that equals
the session's active domain. The domain is never inferred from names, prompts, attachments or
model output and never changes afterwards (PATCH rejects it; a DB trigger blocks it as well).
Identity is a UUID; ``project_key`` is a unique display slug.
"""

from __future__ import annotations

import re
from typing import Any, NoReturn

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.enums import Domain
from aeos.core.errors import AeosError, Conflict, NotFound, PolicyDenied, ValidationFailed
from aeos.core.ids import slugify
from aeos.core.paths import safe_segment
from aeos.core.redaction import redact_text
from aeos.events import service as events
from aeos.identity.service import Principal
from aeos.intake.scan import scan_text
from aeos.models.portfolio import Project, ProjectIntegration, RepositoryReference
from aeos.portfolio import classification as cls
from aeos.portfolio.facade import PLATFORM_PROJECT_KEY

RESERVED_KEYS = frozenset({PLATFORM_PROJECT_KEY})
LIFECYCLES = ("ACTIVE", "ARCHIVED", "RETIRING", "RETIRED")
LIFECYCLE_TRANSITIONS: dict[str, frozenset[str]] = {
    "ACTIVE": frozenset({"ARCHIVED", "RETIRING"}),
    "ARCHIVED": frozenset({"ACTIVE", "RETIRING"}),
    "RETIRING": frozenset({"ACTIVE", "RETIRED"}),
    "RETIRED": frozenset(),  # terminal: history is kept, nothing is deleted
}
# ARCHIVED and RETIRED projects are read-only: history stays visible, nothing new is attached to them
# (repositories, integrations, attachments, context packs, memory). ARCHIVED can be reactivated;
# RETIRED is terminal. RETIRING still accepts work so the wind-down can be completed.
READ_ONLY_LIFECYCLES = frozenset({"ARCHIVED", "RETIRED"})
LIFECYCLE_FIELDS = frozenset({"lifecycle", "lifecycle_reason"})
PROJECT_KINDS = ("NEW", "EXISTING")
EDITABLE_FIELDS = frozenset({"name", "description", "idea", "lifecycle", "lifecycle_reason"})
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_NAME, MAX_IDEA, MAX_DESCRIPTION = 200, 20_000, 5_000


def _actor(principal: Principal) -> str:
    return principal.user.id


def require_writable(project: Project) -> None:
    """Refuse changes to an ARCHIVED/RETIRED project (lifecycle changes go through ``update_project``)."""
    if project.lifecycle in READ_ONLY_LIFECYCLES:
        hint = ("reactivate it (lifecycle ACTIVE) first" if project.lifecycle == "ARCHIVED"
                else "a retired project keeps its history read-only")
        raise Conflict(f"project is {project.lifecycle} and read-only; {hint}", code="PROJECT_READ_ONLY",
                       details={"lifecycle": project.lifecycle})


def clean_text(value: Any, *, field: str, max_len: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValidationFailed(f"{field} must be a string", code="INVALID_FIELD", details={"field": field})
    v = _CTRL.sub("", value).strip()
    if required and not v:
        raise ValidationFailed(f"{field} is required", code="FIELD_REQUIRED", details={"field": field})
    if len(v) > max_len:
        raise ValidationFailed(f"{field} exceeds {max_len} characters", code="FIELD_TOO_LONG", details={"field": field})
    return v


def deny(session: Session, principal: Principal, *, action: str, reason: str, code: str,
         exc_cls: type[AeosError] = PolicyDenied, project: Project | None = None,
         target_type: str = "project", target_id: str | None = None,
         details: dict[str, Any] | None = None) -> NoReturn:
    """Abort the unit of work, record the denial durably, then raise (fail closed)."""
    actor = _actor(principal)
    pid = project.id if project is not None else None
    pdomain = project.domain if project is not None else None
    if pdomain is None and principal.active_domain is not None:
        pdomain = principal.active_domain.value
    session.rollback()  # nothing from the denied request is persisted
    if pid is not None and session.get(Project, pid) is None:
        pid = None  # the project only existed inside the aborted unit of work
    audit.record(session, actor_type="user", actor_id=actor, action=action, result="DENIED",
                 target_type=target_type, target_id=target_id or pid or "", project_id=pid, domain=pdomain,
                 reason=reason, details={"code": code, **(details or {})})
    session.commit()  # denials stay in the audit trail although the request fails
    raise exc_cls(reason, code=code, details=details or {})


_deny = deny


def _key_taken(session: Session, key: str) -> bool:
    # The platform key is reserved for the §09 self-build project registered by the seed, so a
    # human-created "DLY AEOS" project can never occupy it (the seed would then refuse to register).
    if key in RESERVED_KEYS:
        return True
    return bool(session.scalar(select(func.count()).select_from(Project).where(Project.project_key == key)))


def unique_project_key(session: Session, name: str) -> str:
    base = slugify(name)
    if not base[0].isalnum():  # pragma: no cover - slugify strips leading dashes
        base = "p-" + base
    key = safe_segment(base)
    n = 2
    while _key_taken(session, key):
        suffix = f"-{n}"
        key = safe_segment(base[: 48 - len(suffix)].rstrip("-") + suffix)
        n += 1
    return key


def require_explicit_domain(session: Session, principal: Principal, active: Domain, body_domain: Any,
                            *, action: str) -> Domain:
    """U-05 / §50 "Submit prompt tanpa domain": the body must name the domain, and it must be the
    domain the human selected for this session. Nothing is inferred or defaulted."""
    if body_domain in (None, ""):
        _deny(session, principal, action=action, code="DOMAIN_REQUIRED", exc_cls=ValidationFailed,
              reason="domain is required: choose PERSONAL or OFFICE explicitly (it is never inferred)")
    if body_domain not in (Domain.PERSONAL.value, Domain.OFFICE.value):
        _deny(session, principal, action=action, code="DOMAIN_INVALID", exc_cls=ValidationFailed,
              reason="domain must be exactly PERSONAL or OFFICE")
    requested = Domain(body_domain)
    if requested != active:
        _deny(session, principal, action=action, code="DOMAIN_MISMATCH", exc_cls=Conflict,
              reason=f"request domain {requested.value} differs from the session domain {active.value}; "
                     "switch domain explicitly first",
              details={"session_domain": active.value, "requested_domain": requested.value})
    if not principal.can_access_domain(requested):
        _deny(session, principal, action=action, code="DOMAIN_FORBIDDEN", exc_cls=PolicyDenied,
              reason="domain is outside your scope")
    return requested


def create_project(
    session: Session,
    principal: Principal,
    *,
    active_domain: Domain,
    domain: Any,
    name: Any,
    idea: Any = "",
    description: Any = "",
    project_kind: Any = "NEW",
    data_classification: Any = None,
    repository: dict[str, Any] | None = None,
    is_platform: bool = False,
) -> Project:
    chosen = require_explicit_domain(session, principal, active_domain, domain, action="portfolio.project.create")
    name_v = clean_text(name, field="name", max_len=MAX_NAME, required=True)
    idea_v = clean_text(idea, field="idea", max_len=MAX_IDEA)
    desc_v = clean_text(description, field="description", max_len=MAX_DESCRIPTION)
    kind = str(project_kind or "NEW").strip().upper()
    if kind not in PROJECT_KINDS:
        raise ValidationFailed("project_kind must be NEW or EXISTING", code="INVALID_PROJECT_KIND")
    if kind == "EXISTING" and not repository:
        # §05 step 3: an existing project's repository/workspace is chosen explicitly, never guessed.
        raise ValidationFailed("EXISTING projects need an explicitly chosen repository", code="REPOSITORY_REQUIRED")
    level = cls.normalize(data_classification)

    findings: list[dict[str, Any]] = []
    for fname, text in (("idea", idea_v), ("description", desc_v), ("name", name_v)):
        if text:
            res = scan_text(text, source=fname)
            findings.extend(res.findings)
    safe_name = redact_text(name_v)
    project = Project(
        project_key=unique_project_key(session, safe_name),  # never derived from an unredacted secret
        name=safe_name,
        domain=chosen.value,  # the human's explicit choice; findings above never influence this
        owner_user_id=principal.user.id,
        lifecycle="ACTIVE",
        data_classification=level,
        description=redact_text(desc_v),
        idea=redact_text(idea_v),
        is_platform=is_platform,
        project_kind=kind,
        mode="LIVE",
        input_findings=findings,
    )
    session.add(project)
    session.flush()
    audit.record(session, actor_type="user", actor_id=_actor(principal), action="portfolio.project.create",
                 target_type="project", target_id=project.id, project_id=project.id, domain=project.domain,
                 details={"project_key": project.project_key, "kind": kind, "classification": level,
                          "input_findings": [f.get("marker") or f.get("kind") for f in findings]})
    if findings:
        audit.record(session, actor_type="user", actor_id=_actor(principal), action="portfolio.project.untrusted_input",
                     target_type="project", target_id=project.id, project_id=project.id, domain=project.domain,
                     reason="instruction-like or secret-like text recorded as DATA; domain unchanged",
                     details={"findings": findings})
    if repository:
        from aeos.portfolio import repositories

        repositories.add_repository(session, principal, project, **repository)
    events.emit(session, source="portfolio", kind="portfolio.project.created", entity_type="project",
                entity_id=project.id, project_id=project.id, domain=project.domain,
                payload={"name": project.name, "project_key": project.project_key, "kind": kind})
    return project


def update_project(session: Session, principal: Principal, project: Project, body: dict[str, Any]) -> Project:
    if not isinstance(body, dict) or not body:
        raise ValidationFailed("request body must be a non-empty object", code="INVALID_BODY")
    domain_keys = sorted(k for k in body if "domain" in str(k).lower())
    if domain_keys:
        # §04: "Domain tidak dapat diganti"; a domain move is a migration with an owner decision.
        _deny(session, principal, action="portfolio.project.update", code="DOMAIN_IMMUTABLE", project=project,
              reason="project domain is immutable; moving a project between domains is a migration that "
                     "needs an owner decision", details={"fields": domain_keys})
    unknown = sorted(k for k in body if k not in EDITABLE_FIELDS)
    if unknown:
        raise ValidationFailed(f"fields not editable: {', '.join(unknown)}", code="FIELD_NOT_EDITABLE",
                               details={"fields": unknown, "editable": sorted(EDITABLE_FIELDS)})
    if set(body) - LIFECYCLE_FIELDS:
        require_writable(project)  # an ARCHIVED/RETIRED project only accepts a lifecycle change
    changes: dict[str, Any] = {}
    findings: list[dict[str, Any]] = []
    retirement_decision_id: str | None = None
    if "name" in body:
        v = clean_text(body["name"], field="name", max_len=MAX_NAME, required=True)
        findings += scan_text(v, source="name").findings
        changes["name"] = redact_text(v)
    for f, mx in (("description", MAX_DESCRIPTION), ("idea", MAX_IDEA)):
        if f in body:
            v = clean_text(body[f], field=f, max_len=mx)
            findings += scan_text(v, source=f).findings
            changes[f] = redact_text(v)
    if "lifecycle" in body:
        target = str(body["lifecycle"] or "").strip().upper()
        if target not in LIFECYCLES:
            raise ValidationFailed("lifecycle must be ACTIVE, ARCHIVED, RETIRING or RETIRED", code="INVALID_LIFECYCLE")
        if target != project.lifecycle:
            if target not in LIFECYCLE_TRANSITIONS.get(project.lifecycle, frozenset()):
                raise Conflict(f"lifecycle {project.lifecycle} -> {target} is not allowed", code="INVALID_TRANSITION",
                               details={"allowed": sorted(LIFECYCLE_TRANSITIONS.get(project.lifecycle, ()))})
            if project.is_platform and target in {"RETIRING", "RETIRED"}:
                _deny(session, principal, action="portfolio.project.update", code="PLATFORM_RETIREMENT_DENIED",
                      project=project, reason="retiring the DLY AEOS platform project needs a migration decision")
            if target == "RETIRED":
                # §10/§33: RETIRED needs the owner's retirement decision bound to the current checklist hash.
                from aeos.core.config import get_settings
                from aeos.operations import facade as operations

                retirement_decision_id = operations.require_retirement_decision(session, get_settings(), project).id
            changes["lifecycle"] = target
    if "lifecycle_reason" in body:
        changes["lifecycle_reason"] = redact_text(clean_text(body["lifecycle_reason"], field="lifecycle_reason",
                                                             max_len=MAX_DESCRIPTION))
    before = {k: getattr(project, k) for k in changes}
    for k, v in changes.items():
        setattr(project, k, v)
    if findings:
        project.input_findings = [*(project.input_findings or []), *findings][-100:]
    session.flush()
    audit.record(session, actor_type="user", actor_id=_actor(principal), action="portfolio.project.update",
                 target_type="project", target_id=project.id, project_id=project.id, domain=project.domain,
                 details={"changed": sorted(changes), "lifecycle_before": before.get("lifecycle"),
                          "lifecycle_after": changes.get("lifecycle"),
                          **({"retirement_decision_id": retirement_decision_id} if retirement_decision_id else {})})
    events.emit(session, source="portfolio", kind="portfolio.project.updated", entity_type="project",
                entity_id=project.id, project_id=project.id, domain=project.domain,
                payload={"changed": sorted(changes), "lifecycle": project.lifecycle})
    return project


def visible_projects(session: Session, principal: Principal, domain: Domain, *, lifecycle: str | None = None) -> list[Project]:
    q = select(Project).where(Project.domain == domain.value).order_by(Project.is_platform.desc(), Project.name)
    if principal.allowed_project_ids is not None:
        q = q.where(Project.id.in_(principal.allowed_project_ids))
    if lifecycle:
        q = q.where(Project.lifecycle == lifecycle.upper())
    return list(session.scalars(q))


def find_by_key(session: Session, project_key: str) -> Project | None:
    return session.scalar(select(Project).where(Project.project_key == project_key))


def require_project(session: Session, project_id: str) -> Project:
    p = session.get(Project, project_id)
    if p is None:
        raise NotFound("project not found")
    return p


# ---------------------------------------------------------------- views


def links(project: Project) -> dict[str, str]:
    return {
        "plans": f"/plans?project={project.id}",
        "publications": f"/publications?project={project.id}",
        "dag": f"/dag?project={project.id}",
        "detail": f"/projects/{project.id}",
    }


def project_view(session: Session, project: Project, *, detail: bool = False) -> dict[str, Any]:
    from aeos.portfolio import integrations as integ
    from aeos.portfolio import repositories

    repos = list(session.scalars(select(RepositoryReference).where(RepositoryReference.project_id == project.id)
                                 .order_by(RepositoryReference.role, RepositoryReference.full_name)))  # platform first
    ints = list(session.scalars(select(ProjectIntegration).where(ProjectIntegration.project_id == project.id)
                                .order_by(ProjectIntegration.purpose, ProjectIntegration.provider)))
    int_views = [integ.live_integration_view(i) for i in ints]  # credential/root state as of now
    statuses = {v["id"]: v["capability_status"] for v in int_views}
    view: dict[str, Any] = {
        "id": project.id,
        "project_key": project.project_key,
        "name": project.name,
        "domain": project.domain,
        "lifecycle": project.lifecycle,
        "lifecycle_reason": project.lifecycle_reason,
        "data_classification": project.data_classification,
        "project_kind": project.project_kind,
        "is_platform": project.is_platform,
        "mode": project.mode,
        "owner_user_id": project.owner_user_id,
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
        "repositories": [repositories.repository_view(r, summary=not detail) for r in repos],
        "integrations": int_views,
        "integration_state": integ.integration_state(ints, statuses),
        "environment": integ.environment_state(project, ints),
        "links": links(project),
    }
    if detail:
        from aeos.intake.service import attachment_summary

        view.update({
            "description": project.description,
            "idea": project.idea,
            "input_findings": project.input_findings or [],
            "attachments": attachment_summary(session, project.id),
        })
    return view
