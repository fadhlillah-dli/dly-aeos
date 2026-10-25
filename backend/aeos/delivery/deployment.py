"""Observe-only deployment targets for gates G6 (staging) and G8 (post-release) — §04, §31, §48.

The owner registers a PERSONAL ``deployment-target`` integration (purpose ``staging`` or ``production``)
with its exact base URL (``root_id``) through the Integration Registry. AEOS never deploys: deploying
and releasing stay human actions (U-09). AEOS only *observes* a target with one read-only GET of its
health/version endpoint and records the observation as trusted runtime evidence (E3) for the exact
commit the target reports:

* the target names the expected commit and reports healthy -> PASS;
* the target names the expected commit and reports unhealthy / an HTTP error -> FAIL;
* anything else (another commit, no commit identity, unreachable) -> NOT_PROVEN — never a pass.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.errors import Blocked, NotFound, ValidationFailed
from aeos.core.redaction import redact_text
from aeos.core.timeutil import utcnow
from aeos.models.portfolio import Project, ProjectIntegration

PROVIDER = "deployment-target"
ENVIRONMENTS = ("staging", "production")
DEFAULT_HEALTH_PATH = "/health"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_HEALTHY = frozenset({"ok", "pass", "passed", "healthy", "up", "green"})
_COMMIT_KEYS = ("commit", "sha", "git_sha", "revision", "version")
_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
_PATH = re.compile(r"^/[A-Za-z0-9._~/-]{0,200}$")


def health_path_ok(path: Any) -> bool:
    """The one health-path rule: an absolute URL path of safe characters (the Integration Registry validates the
    owner's ``config.health_path`` with this same rule before it is stored)."""
    return isinstance(path, str) and bool(_PATH.match(path)) and ".." not in path.split("/")


def target_for(session: Session, project: Project, environment: str) -> ProjectIntegration | None:
    return session.scalar(select(ProjectIntegration).where(
        ProjectIntegration.project_id == project.id, ProjectIntegration.purpose == environment,
        ProjectIntegration.provider == PROVIDER).order_by(ProjectIntegration.created_at.desc()).limit(1))


def classify(sha: str, http_status: int | None, body: Any) -> tuple[str, str, str | None]:
    """(status, reason, reported commit) of one health observation."""
    reported: str | None = None
    health: str | None = None
    if isinstance(body, dict):
        for key in _COMMIT_KEYS:
            v = body.get(key)
            if isinstance(v, str) and _SHA.match(v.strip()):
                reported = v.strip().lower()
                break
        st = body.get("status")
        health = str(st).strip().lower() if st is not None else None
    if reported is None:
        return "NOT_PROVEN", "target does not report a commit identity; the observation is not attributable", None
    want = sha.lower()
    same = want.startswith(reported) or reported.startswith(want)
    if not same:
        return "NOT_PROVEN", f"target runs another commit ({reported[:12]})", reported
    if http_status != 200:
        return "FAIL", f"target reports commit {reported[:12]} with HTTP {http_status}", reported
    if health is not None and health not in _HEALTHY:
        return "FAIL", f"target reports commit {reported[:12]} as {health!r}", reported
    return "PASS", f"target serves commit {reported[:12]} and reports healthy", reported


def observe(session: Session, project_id: str, environment: str, sha: str, *, actor: str,
            client: httpx.Client | None = None) -> dict[str, Any]:
    """One read-only GET of the registered target's health endpoint -> RUNTIME_OBSERVATION evidence."""
    from aeos.verification import facade as verification

    if environment not in ENVIRONMENTS:
        raise ValidationFailed(f"environment must be one of {list(ENVIRONMENTS)}", code="ENVIRONMENT_INVALID")
    if not _SHA.match(sha or ""):
        raise ValidationFailed("commit sha required", code="SHA_INVALID")
    project = session.get(Project, project_id)
    if project is None:
        raise NotFound("project not found")
    target = target_for(session, project, environment)
    if target is None or not target.root_id:
        raise Blocked(f"no {environment} deployment target is registered for this project: register it in the "
                      f"Control Center (Portofolio -> project -> Integrasi -> Deployment target {environment}; "
                      f"API: PUT /api/projects/{project.id}/integrations provider={PROVIDER} purpose={environment})",
                      code="NO_DEPLOYMENT_TARGET")
    path = str((target.config or {}).get("health_path") or DEFAULT_HEALTH_PATH)
    if not health_path_ok(path):
        raise ValidationFailed("health_path must be an absolute URL path", code="HEALTH_PATH_INVALID")
    url = target.root_id.rstrip("/") + path
    http_status: int | None = None
    body: Any = None
    error: str | None = None
    owns = client is None
    http = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)
    try:
        resp = http.get(url, headers={"Accept": "application/json", "User-Agent": "dly-aeos-observer"})
        http_status = resp.status_code
        try:
            body = resp.json()
        except ValueError:
            body = None
    except httpx.HTTPError as exc:
        error = f"unreachable: {type(exc).__name__}"
    finally:
        if owns:
            http.close()
    if error:
        status, reason, reported = "NOT_PROVEN", error, None
    else:
        status, reason, reported = classify(sha, http_status, body)
    content = {"status": status, "reason": reason, "environment": environment, "target": url,
               "expected_commit": sha, "reported_commit": reported, "http_status": http_status,
               "observed_at": utcnow().isoformat(), "method": "GET (observe-only; AEOS never deploys)"}
    ev = verification.collect_evidence(
        session, kind="RUNTIME_OBSERVATION", origin="collector", collector_identity=f"deployment-observer:{environment}",
        claim=redact_text(f"{environment} observation of {sha[:12]} at {url}: {status}")[:400], content=content,
        evidence_level="E3", subject_type="commit", subject_id=sha, project_id=project.id, domain=project.domain,
        environment={"commit": sha, "deployment_environment": environment, "target": url,
                     "integration_id": target.id},
    )
    audit.record(session, actor_type="user", actor_id=actor, action="delivery.target.observe",
                 result="SUCCESS", target_type="integration", target_id=target.id, project_id=project.id,
                 domain=project.domain, reason=reason[:500],
                 details={"environment": environment, "status": status, "evidence_id": ev.id, "commit": sha})
    return {"status": status, "reason": reason, "evidence_id": ev.id, "target": url, "environment": environment}
