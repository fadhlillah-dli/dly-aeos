"""Observe-only deployment targets for gates G6 (staging) and G8 (post-release) — §04, §31, §33, §48.

The owner registers a PERSONAL ``deployment-target`` integration (purpose ``staging`` or ``production``)
with its exact base URL (``root_id``) through the Integration Registry. AEOS never deploys, releases or
rolls back: those stay human actions (U-09, §40). AEOS only *observes* a target with read-only GETs and
records each observation as trusted runtime evidence (E3) for the exact commit the target reports:

* the health endpoint (``config.health_path``, default ``/health``) names the expected commit and reports
  healthy -> the health part passes; the platform's own ``/api/health`` shape is accepted too, but only
  while ``controller`` runs the ACTIVE, clean release (``mode=release``, ``status=ACTIVE_RELEASE``,
  ``matches_active=true``, ``dirty=false``) — a checkout's commit is not a deployment identity;
* every owner-configured smoke / critical-flow GET check (``config.smoke_checks``) must answer its expected
  status and JSON keys; each one is recorded as its own ``SMOKE_TEST`` evidence;
* every owner-configured product SLI (``config.sli_checks``) is measured from a JSON number; it can fail the
  observation only against a threshold the owner set (no threshold -> ``NO_TARGET``, report only);
* a reported artifact digest is compared with the provider-CI ``ARTIFACT_DIGEST`` evidence of the commit; it
  decides the observation only when the owner set ``require_artifact_digest``.

The aggregate is one ``RUNTIME_OBSERVATION`` (PASS only when every part passed; FAIL when the target names
the expected commit and a part failed; NOT_PROVEN otherwise — never a pass). It carries the hash of the
target configuration it ran with, so a later change of the checks makes it STALE for the gates.

Staging observations are an operation of the ``CAN_VALIDATE_STAGING`` capability (§08): they run only while
that gate is READY for the project. A FAIL of a production observation raises the ``post_release_failure``
alert, opens/links an incident with a HUMAN rollback recommendation and files a decision request (§31 G8,
§33, §40, §50 1.35) through the operations facade.
"""

from __future__ import annotations

import math
import re
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.errors import AeosError, Blocked, Conflict, NotFound, ValidationFailed
from aeos.core.hashing import content_hash
from aeos.core.redaction import redact_text
from aeos.core.timeutil import utcnow
from aeos.models.portfolio import Project, ProjectIntegration

PROVIDER = "deployment-target"
LOCAL_PROVIDER = "local-target"
ENVIRONMENTS = ("staging", "production")
DEFAULT_HEALTH_PATH = "/health"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
OBSERVER = "deployment-observer"
_HEALTHY = frozenset({"ok", "pass", "passed", "healthy", "up", "green"})
_COMMIT_KEYS = ("commit", "sha", "git_sha", "revision", "version")
_DIGEST_KEYS = ("artifact_digest", "image_digest", "digest")
_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
# an absolute path on the target itself: no host part ('@other.host', '//other.host'), no query/fragment
_PATH = re.compile(r"^/(?!/)[A-Za-z0-9._~/-]{0,200}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
_JSON_KEY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}(?:\.[A-Za-z0-9_][A-Za-z0-9_-]{0,63}){0,4}$")

CHECK_KINDS = ("smoke", "critical_flow")
MAX_CHECKS = 20
MAX_JSON_KEYS = 10
# Failure conditions an observation can report; the owner names the ones that call for a rollback.
ROLLBACK_CONDITIONS = ("health_fail", "smoke_fail", "critical_flow_fail", "sli_breach", "artifact_digest_mismatch")
# deployment-target config keys owned by this module (validated here; the Integration Registry stores them).
CONFIG_KEYS = ("health_path", "smoke_checks", "sli_checks", "rollback_on", "require_artifact_digest")
# keys settable by PUT /api/delivery/targets/{id}/checks (and ``aeos delivery target-checks``) and kept by a generic
# integration PUT that omits them (the Integrasi form may not send them; an explicit value replaces them)
CHECK_CONFIG_KEYS = ("health_path", "smoke_checks", "sli_checks", "rollback_on", "require_artifact_digest")
READ_ONLY_LIFECYCLES = frozenset({"ARCHIVED", "RETIRED"})  # the Integration Registry's rule (no change then)
_SMOKE_FIELDS = frozenset({"name", "path", "expect_status", "json_keys", "kind"})
_SLI_FIELDS = frozenset({"name", "path", "json_key", "max", "min"})


# ----------------------------------------------------------------------------------------- configuration


def _bad(code: str, message: str, **details: Any) -> ValidationFailed:
    return ValidationFailed(message, code=code, details=details or None)


def _path(value: Any, *, code: str, field: str) -> str:
    if not isinstance(value, str) or not _PATH.match(value.strip()):
        raise _bad(code, f"{field} must be an absolute URL path such as /health (no host, query or '@')",
                   field=field)
    return value.strip()


def _number(value: Any, *, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise _bad("SLI_CHECKS_INVALID", f"{field} must be a number (or null)", field=field)
    return float(value)


def _default_name(path: str) -> str:
    return path.strip("/").replace("/", "-")[:64] or "root"


def _smoke_check(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _bad("SMOKE_CHECKS_INVALID", f"smoke_checks[{index}] must be an object", index=index)
    unknown = sorted(set(raw) - _SMOKE_FIELDS)
    if unknown:
        raise _bad("SMOKE_CHECKS_INVALID", f"smoke_checks[{index}] has unknown fields {unknown}", index=index,
                   allowed=sorted(_SMOKE_FIELDS))
    path = _path(raw.get("path"), code="SMOKE_CHECKS_INVALID", field=f"smoke_checks[{index}].path")
    name = str(raw.get("name") or _default_name(path)).strip()[:64]
    if not _NAME.match(name):
        raise _bad("SMOKE_CHECKS_INVALID", f"smoke_checks[{index}].name has an invalid format", index=index)
    status = raw.get("expect_status", 200)
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        raise _bad("SMOKE_CHECKS_INVALID", f"smoke_checks[{index}].expect_status must be an HTTP status 100-599",
                   index=index)
    keys = raw.get("json_keys") or []
    if not isinstance(keys, list) or len(keys) > MAX_JSON_KEYS or not all(
            isinstance(k, str) and _JSON_KEY.match(k) for k in keys):
        raise _bad("SMOKE_CHECKS_INVALID", f"smoke_checks[{index}].json_keys must be up to {MAX_JSON_KEYS} key "
                   "names (dotted paths allowed)", index=index)
    kind = str(raw.get("kind") or "smoke").strip().lower()
    if kind not in CHECK_KINDS:
        raise _bad("SMOKE_CHECKS_INVALID", f"smoke_checks[{index}].kind must be one of {list(CHECK_KINDS)}",
                   index=index)
    return {"name": name, "path": path, "expect_status": status, "json_keys": list(dict.fromkeys(keys)),
            "kind": kind}


def _sli_check(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _bad("SLI_CHECKS_INVALID", f"sli_checks[{index}] must be an object", index=index)
    unknown = sorted(set(raw) - _SLI_FIELDS)
    if unknown:
        raise _bad("SLI_CHECKS_INVALID", f"sli_checks[{index}] has unknown fields {unknown}", index=index,
                   allowed=sorted(_SLI_FIELDS))
    path = _path(raw.get("path"), code="SLI_CHECKS_INVALID", field=f"sli_checks[{index}].path")
    key = raw.get("json_key")
    if not isinstance(key, str) or not _JSON_KEY.match(key):
        raise _bad("SLI_CHECKS_INVALID", f"sli_checks[{index}].json_key must name a JSON number (dotted path)",
                   index=index)
    name = str(raw.get("name") or key).strip()[:64]
    if not _NAME.match(name):
        raise _bad("SLI_CHECKS_INVALID", f"sli_checks[{index}].name has an invalid format", index=index)
    maximum = _number(raw.get("max"), field=f"sli_checks[{index}].max")
    minimum = _number(raw.get("min"), field=f"sli_checks[{index}].min")
    if maximum is not None and minimum is not None and minimum > maximum:
        raise _bad("SLI_CHECKS_INVALID", f"sli_checks[{index}]: min is above max", index=index)
    return {"name": name, "path": path, "json_key": key, "max": maximum, "min": minimum}


def _check_list(value: Any, parse: Any, *, key: str) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    code = "SMOKE_CHECKS_INVALID" if key == "smoke_checks" else "SLI_CHECKS_INVALID"
    if not isinstance(value, list) or len(value) > MAX_CHECKS:
        raise _bad(code, f"{key} must be a list of at most {MAX_CHECKS} checks")
    out = [parse(item, i) for i, item in enumerate(value)]
    names = [c["name"] for c in out]
    if len(set(names)) != len(names):
        raise _bad(code, f"{key} names must be unique")
    return out


def validate_target_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Validate the deployment-target keys of an integration config (``CONFIG_KEYS``) and return them
    normalised. Unknown keys are not this module's business (the Integration Registry checks them)."""
    cfg = dict(config or {})
    out: dict[str, Any] = {}
    if cfg.get("health_path") not in (None, ""):
        out["health_path"] = _path(cfg["health_path"], code="HEALTH_PATH_INVALID", field="health_path")
    if "smoke_checks" in cfg:
        out["smoke_checks"] = _check_list(cfg["smoke_checks"], _smoke_check, key="smoke_checks")
    if "sli_checks" in cfg:
        out["sli_checks"] = _check_list(cfg["sli_checks"], _sli_check, key="sli_checks")
    if "rollback_on" in cfg:
        raw = cfg["rollback_on"]
        if raw in (None, ""):
            out["rollback_on"] = []
        elif not isinstance(raw, list) or not all(isinstance(c, str) and c in ROLLBACK_CONDITIONS for c in raw):
            raise _bad("ROLLBACK_CRITERIA_INVALID", f"rollback_on must list conditions from {list(ROLLBACK_CONDITIONS)}",
                       allowed=list(ROLLBACK_CONDITIONS))
        else:
            out["rollback_on"] = list(dict.fromkeys(raw))
    if "require_artifact_digest" in cfg:
        if not isinstance(cfg["require_artifact_digest"], bool):
            raise _bad("INVALID_CONFIG", "require_artifact_digest must be true/false")
        out["require_artifact_digest"] = cfg["require_artifact_digest"]
    return out


def health_path_ok(path: Any) -> bool:
    """The one health-path rule: an absolute URL path of safe characters (the Integration Registry validates the
    owner's ``config.health_path`` with this same rule before it is stored)."""
    return isinstance(path, str) and bool(_PATH.match(path)) and ".." not in path.split("/")


def target_for(session: Session, project: Project, environment: str) -> ProjectIntegration | None:
    return session.scalar(select(ProjectIntegration).where(
        ProjectIntegration.project_id == project.id, ProjectIntegration.purpose == environment,
        ProjectIntegration.provider == PROVIDER).order_by(ProjectIntegration.created_at.desc()).limit(1))


def local_targets_for(session: Session, project: Project, environment: str) -> list[ProjectIntegration]:
    """``local-target`` registrations (container:/host:/vm: identities) of *environment*: recorded, but not
    observable with a GET — the gates and the Doctor report them as such, never as a validated target."""
    return list(session.scalars(select(ProjectIntegration).where(
        ProjectIntegration.project_id == project.id, ProjectIntegration.purpose == environment,
        ProjectIntegration.provider == LOCAL_PROVIDER)))


def target_settings(target: ProjectIntegration) -> dict[str, Any]:
    """The checked observation settings of a registered target (raises ValidationFailed when the stored config
    is invalid, e.g. a ``health_path`` that would send the GET to another host)."""
    stored = target.config or {}
    cfg = validate_target_config({k: stored.get(k) for k in CONFIG_KEYS if k in stored})
    base = (target.root_id or "").rstrip("/")
    return {"integration_id": target.id, "base_url": base,
            "health_path": cfg.get("health_path") or DEFAULT_HEALTH_PATH,
            "smoke_checks": cfg.get("smoke_checks") or [], "sli_checks": cfg.get("sli_checks") or [],
            "rollback_on": cfg.get("rollback_on") or [],
            "rollback_criteria": "RECORDED" if cfg.get("rollback_on") else "UNDECIDED",
            "require_artifact_digest": bool(cfg.get("require_artifact_digest"))}


def set_target_checks(session: Session, integration: ProjectIntegration, body: dict[str, Any], *, actor: str,
                      actor_type: str = "user") -> dict[str, Any]:
    """Owner configuration of a target's observation settings (``CHECK_CONFIG_KEYS``: health_path and the checks):
    only the given keys change (an empty ``health_path`` falls back to the default ``/health``); the target identity
    (root_id) stays with the Integration Registry. Audited; does not commit."""
    from aeos.events import service as events

    if integration.provider != PROVIDER or integration.purpose not in ENVIRONMENTS:
        raise ValidationFailed("checks apply to deployment-target integrations (staging|production) only",
                               code="NOT_A_DEPLOYMENT_TARGET")
    project = session.get(Project, integration.project_id)
    if project is not None and project.lifecycle in READ_ONLY_LIFECYCLES:
        raise Conflict(f"project is {project.lifecycle} and read-only", code="PROJECT_READ_ONLY",
                       details={"lifecycle": project.lifecycle})
    unknown = sorted(set(body) - set(CHECK_CONFIG_KEYS))
    if unknown:
        raise ValidationFailed(f"unknown fields: {', '.join(unknown)}", code="FIELD_NOT_EDITABLE",
                               details={"allowed": list(CHECK_CONFIG_KEYS)})
    clean = validate_target_config(body)
    before = {k: (integration.config or {}).get(k) for k in CHECK_CONFIG_KEYS}
    merged = {**(integration.config or {}), **clean}
    if "health_path" in body and "health_path" not in clean:  # explicitly cleared -> default /health
        merged.pop("health_path", None)
        clean["health_path"] = None
    integration.config = merged
    session.flush()
    settings = target_settings(integration)
    audit.record(session, actor_type=actor_type, actor_id=actor, action="delivery.target.checks",
                 target_type="integration", target_id=integration.id, project_id=integration.project_id,
                 domain=integration.domain,
                 details={"changed": sorted(k for k in clean if before.get(k) != clean[k]),
                          "smoke_checks": len(settings["smoke_checks"]), "sli_checks": len(settings["sli_checks"]),
                          "rollback_on": settings["rollback_on"],
                          "require_artifact_digest": settings["require_artifact_digest"],
                          "target_config_hash": target_config_hash(settings)})
    events.emit(session, source="delivery", kind="delivery.target.checks_updated", entity_type="integration",
                entity_id=integration.id, project_id=integration.project_id, domain=integration.domain,
                payload={"environment": integration.purpose, "target_config_hash": target_config_hash(settings)})
    return target_view(session, integration)


def target_view(session: Session, integration: ProjectIntegration) -> dict[str, Any]:
    """Settings of one target plus the status of its newest observation for the CURRENT configuration."""
    from aeos.models.evidence import EvidenceArtifact

    try:
        settings: dict[str, Any] = target_settings(integration)
        problem = None
    except ValidationFailed as exc:
        settings, problem = {"integration_id": integration.id, "base_url": (integration.root_id or "").rstrip("/")}, \
            f"{exc.code}: {exc.message}"
    ev = session.scalar(select(EvidenceArtifact).where(
        EvidenceArtifact.project_id == integration.project_id, EvidenceArtifact.kind == "RUNTIME_OBSERVATION",
        EvidenceArtifact.collector_identity == f"{OBSERVER}:{integration.purpose}", EvidenceArtifact.trusted.is_(True))
        .order_by(EvidenceArtifact.created_at.desc()).limit(1))
    latest = observation_view(ev, integration) if ev is not None else {"status": "NO_EVIDENCE", "evidence_id": None}
    return {"integration_id": integration.id, "project_id": integration.project_id, "domain": integration.domain,
            "environment": integration.purpose, "settings": settings, "config_problem": problem,
            "target_config_hash": target_config_hash(settings) if problem is None else None,
            "latest_observation": latest, "observe_only": True,
            "note": "AEOS hanya membaca target ini (GET); deploy, rilis dan rollback adalah tindakan manusia.",
            "source": "project_integrations (deployment-target) + evidence_artifacts",
            "checked_at": latest.get("observed_at"), "generated_at": utcnow().isoformat()}


def target_config_hash(settings: dict[str, Any]) -> str:
    """Identity of what an observation checked: the exact target URL, health path and every check that
    decides PASS. A later change makes an earlier observation STALE."""
    return content_hash({k: settings.get(k) for k in ("base_url", "health_path", "smoke_checks", "sli_checks",
                                                       "require_artifact_digest")})


# ----------------------------------------------------------------------------------------- classification


def _controller_commit(body: dict[str, Any]) -> tuple[str | None, str | None]:
    """(commit, refusal reason) of the DLY AEOS ``/api/health`` shape ``{"controller": {...}}`` (§09 zones)."""
    ctl = body.get("controller")
    if not isinstance(ctl, dict):
        return None, None
    commit = ctl.get("commit")
    if not isinstance(commit, str) or not _SHA.match(commit.strip()):
        return None, "controller reports no commit identity; the observation is not attributable"
    if ctl.get("mode") != "release" or ctl.get("status") != "ACTIVE_RELEASE" or ctl.get("matches_active") is not True:
        return None, (f"controller runs {ctl.get('mode') or 'unknown'} code ({ctl.get('status') or 'UNKNOWN'}), not "
                      "the ACTIVE release; a checkout commit is not a deployment identity")
    if ctl.get("dirty") is not False:
        return None, "controller code is not a clean release (dirty/unknown); the observation is not attributable"
    return commit.strip().lower(), None


def classify(sha: str, http_status: int | None, body: Any) -> tuple[str, str, str | None]:
    """(status, reason, reported commit) of one health observation."""
    reported: str | None = None
    health: str | None = None
    refusal: str | None = None
    if isinstance(body, dict):
        for key in _COMMIT_KEYS:
            v = body.get(key)
            if isinstance(v, str) and _SHA.match(v.strip()):
                reported = v.strip().lower()
                break
        if reported is None:
            reported, refusal = _controller_commit(body)
        st = body.get("status")
        health = str(st).strip().lower() if st is not None else None
    if reported is None:
        return "NOT_PROVEN", refusal or "target does not report a commit identity; the observation is not attributable", None
    want = sha.lower()
    same = want.startswith(reported) or reported.startswith(want)
    if not same:
        return "NOT_PROVEN", f"target runs another commit ({reported[:12]})", reported
    if http_status != 200:
        return "FAIL", f"target reports commit {reported[:12]} with HTTP {http_status}", reported
    if health is not None and health not in _HEALTHY:
        return "FAIL", f"target reports commit {reported[:12]} as {health!r}", reported
    return "PASS", f"target serves commit {reported[:12]} and reports healthy", reported


def reported_digest(body: Any) -> str | None:
    """The artifact digest a health response reports (``sha256:<64 hex>``), or None."""
    if not isinstance(body, dict):
        return None
    for key in _DIGEST_KEYS:
        v = body.get(key)
        if isinstance(v, str) and _DIGEST.match(v.strip().lower()):
            return v.strip().lower()
    return None


def _json_value(body: Any, dotted: str) -> tuple[bool, Any]:
    cur = body
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _get_json(http: httpx.Client, url: str) -> tuple[int | None, Any, str | None]:
    try:
        resp = http.get(url, headers={"Accept": "application/json", "User-Agent": "dly-aeos-observer"})
    except httpx.HTTPError as exc:
        return None, None, f"unreachable: {type(exc).__name__}"
    try:
        body = resp.json()
    except ValueError:
        body = None
    return resp.status_code, body, None


def run_smoke_check(http: httpx.Client, base_url: str, check: dict[str, Any]) -> dict[str, Any]:
    """One read-only GET; PASS only with the expected status and every expected JSON key. The response body is
    never stored (it may carry personal data); only status and key presence are."""
    url = base_url + check["path"]
    out = {**check, "url": url, "http_status": None, "status": "NOT_PROVEN", "reason": ""}
    code, body, error = _get_json(http, url)
    if error:
        out["reason"] = error
        return out
    out["http_status"] = code
    if code != check["expect_status"]:
        out.update(status="FAIL", reason=f"HTTP {code}, expected {check['expect_status']}")
        return out
    missing = [k for k in check["json_keys"] if not _json_value(body, k)[0]]
    if missing:
        out.update(status="FAIL", reason=f"JSON key(s) missing: {', '.join(missing)}")
        return out
    out.update(status="PASS", reason=f"HTTP {code}" + (f"; keys present: {', '.join(check['json_keys'])}"
                                                       if check["json_keys"] else ""))
    return out


def run_sli_check(http: httpx.Client, base_url: str, sli: dict[str, Any]) -> dict[str, Any]:
    """Measure one product SLI (a JSON number). Without an owner threshold it is NO_TARGET (report only)."""
    url = base_url + sli["path"]
    out = {**sli, "url": url, "http_status": None, "value": None, "status": "UNKNOWN", "reason": ""}
    code, body, error = _get_json(http, url)
    if error:
        out["reason"] = error
        return out
    out["http_status"] = code
    if code != 200:
        out["reason"] = f"HTTP {code}; no measurement"
        return out
    found, value = _json_value(body, sli["json_key"])
    if not found or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        out["reason"] = f"{sli['json_key']} is not a number in the response"
        return out
    out["value"] = float(value)
    if sli["max"] is None and sli["min"] is None:
        out.update(status="NO_TARGET", reason="no owner threshold recorded (report only, never 'within target')")
    elif (sli["max"] is not None and value > sli["max"]) or (sli["min"] is not None and value < sli["min"]):
        out.update(status="BREACH", reason=f"{value} outside the owner threshold (min {sli['min']}, max {sli['max']})")
    else:
        out.update(status="WITHIN_TARGET", reason=f"{value} within the owner threshold")
    return out


def artifact_status(session: Session, project: Project, sha: str, digest: str | None) -> dict[str, Any]:
    """Reported artifact digest vs the provider-CI ARTIFACT_DIGEST evidence of the commit (build once, §31)."""
    from aeos.delivery import provider

    recorded, evidence_id = provider.recorded_artifact_digests(session, project.id, sha)
    if digest is None:
        status = "NOT_REPORTED"
    elif not recorded:
        status = "NOT_RECORDED"
    else:
        status = "MATCH" if digest in recorded else "MISMATCH"
    return {"status": status, "reported_digest": digest, "recorded_digests": sorted(recorded),
            "digest_evidence_id": evidence_id}


# ----------------------------------------------------------------------------------------- observation


def _require_staging_gate(session: Session, project: Project) -> None:
    """§08 "operasi dengan dependency critical gagal tidak dijalankan": a staging observation is the operation of
    CAN_VALIDATE_STAGING; it runs only while the gate is READY for this project (fail closed)."""
    from aeos.core.enums import Capability
    from aeos.doctor import facade as doctor

    doctor.require_capability(session, Capability.CAN_VALIDATE_STAGING, domain=project.domain, project_id=project.id)


def require_staging_gate(session: Session, project: Project) -> None:
    """Public form for the other staging operations (the docs screenshot collector)."""
    _require_staging_gate(session, project)


def checked_target(session: Session, project: Project, environment: str) -> dict[str, Any] | None:
    """The registered target of *environment* with checked settings, or None when none is registered."""
    if environment not in ENVIRONMENTS:
        raise ValidationFailed(f"environment must be one of {list(ENVIRONMENTS)}", code="ENVIRONMENT_INVALID")
    target = target_for(session, project, environment)
    if target is None or not target.root_id:
        return None
    return target_settings(target)


def _record_part(session: Session, project: Project, *, kind: str, identity: str, environment: str, sha: str,
                 result: dict[str, Any], target_id: str) -> str:
    from aeos.verification import facade as verification

    ev = verification.collect_evidence(
        session, kind=kind, origin="collector", collector_identity=identity,
        claim=redact_text(f"{environment} {result.get('kind') or 'sli'} check {result['name']} of {sha[:12]}: "
                          f"{result['status']}")[:400],
        content={**result, "observed_at": utcnow().isoformat()},
        evidence_level="E3", subject_type="commit", subject_id=sha, project_id=project.id, domain=project.domain,
        environment={"commit": sha, "deployment_environment": environment, "target": result.get("url"),
                     "integration_id": target_id, "check_name": result["name"],
                     "check_kind": result.get("kind") or "sli"},
    )
    return ev.id


def _overall(health: str, checks: list[dict[str, Any]], slis: list[dict[str, Any]], artifact: dict[str, Any],
             require_digest: bool) -> tuple[str, list[str]]:
    conditions: list[str] = []
    if health == "FAIL":
        conditions.append("health_fail")
    if any(c["status"] == "FAIL" and c["kind"] == "smoke" for c in checks):
        conditions.append("smoke_fail")
    if any(c["status"] == "FAIL" and c["kind"] == "critical_flow" for c in checks):
        conditions.append("critical_flow_fail")
    if any(s["status"] == "BREACH" for s in slis):
        conditions.append("sli_breach")
    if require_digest and artifact["status"] == "MISMATCH":
        conditions.append("artifact_digest_mismatch")
    if health == "NOT_PROVEN":
        return "NOT_PROVEN", conditions
    if conditions:
        return "FAIL", conditions
    unproven = (any(c["status"] != "PASS" for c in checks) or any(s["status"] == "UNKNOWN" for s in slis)
                or (require_digest and artifact["status"] != "MATCH"))
    return ("NOT_PROVEN" if unproven else "PASS"), conditions


def observe(session: Session, project_id: str, environment: str, sha: str, *, actor: str,
            client: httpx.Client | None = None) -> dict[str, Any]:
    """Read-only GETs of the registered target (health + configured checks) -> RUNTIME_OBSERVATION evidence."""
    from aeos.verification import facade as verification

    if environment not in ENVIRONMENTS:
        raise ValidationFailed(f"environment must be one of {list(ENVIRONMENTS)}", code="ENVIRONMENT_INVALID")
    if not _SHA.match(sha or ""):
        raise ValidationFailed("commit sha required", code="SHA_INVALID")
    sha = sha.lower()
    project = session.get(Project, project_id)
    if project is None:
        raise NotFound("project not found")
    target = target_for(session, project, environment)
    if target is None or not target.root_id:
        local = local_targets_for(session, project, environment)
        hint = (f"; the local-target {', '.join(str(t.root_id) for t in local)} is recorded but cannot be observed "
                "with a GET" if local else "")
        raise Blocked(f"no {environment} deployment target is registered for this project: register it in the "
                      f"Control Center (Portofolio -> project -> Integrasi -> Deployment target {environment}; "
                      f"API: PUT /api/projects/{project.id}/integrations provider={PROVIDER} purpose={environment})"
                      f"{hint}", code="NO_DEPLOYMENT_TARGET")
    # target_settings validates health_path with the same rule as health_path_ok (HEALTH_PATH_INVALID)
    settings = target_settings(target)
    gate: dict[str, Any] | None = None
    if environment == "staging":
        _require_staging_gate(session, project)  # Blocked CAPABILITY_NOT_READY before any request is made
        gate = {"capability": "CAN_VALIDATE_STAGING", "status": "READY", "checked_at": utcnow().isoformat()}
    url = settings["base_url"] + settings["health_path"]
    http_status: int | None = None
    body: Any = None
    owns = client is None
    http = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)
    checks: list[dict[str, Any]] = []
    slis: list[dict[str, Any]] = []
    try:
        http_status, body, error = _get_json(http, url)
        if error:
            health, health_reason, reported = "NOT_PROVEN", error, None
        else:
            health, health_reason, reported = classify(sha, http_status, body)
        if health != "NOT_PROVEN":  # only checks of the attributable commit say anything about it
            checks = [run_smoke_check(http, settings["base_url"], c) for c in settings["smoke_checks"]]
            slis = [run_sli_check(http, settings["base_url"], s) for s in settings["sli_checks"]]
    finally:
        if owns:
            http.close()
    artifact = artifact_status(session, project, sha, reported_digest(body))
    status, conditions = _overall(health, checks, slis, artifact, settings["require_artifact_digest"])
    skipped = [c["name"] for c in settings["smoke_checks"] + settings["sli_checks"]] if health == "NOT_PROVEN" else []
    for c in checks:
        c["evidence_id"] = _record_part(session, project, kind="SMOKE_TEST", identity=f"deployment-smoke:{environment}",
                                        environment=environment, sha=sha, result=c, target_id=target.id)
    for s in slis:
        s["evidence_id"] = _record_part(session, project, kind="PROBE", identity=f"deployment-sli:{environment}",
                                        environment=environment, sha=sha, result=s, target_id=target.id)
    failed = [c["name"] for c in checks if c["status"] != "PASS"] + [s["name"] for s in slis
                                                                      if s["status"] in ("BREACH", "UNKNOWN")]
    reason = health_reason + (f"; checks not passing: {', '.join(failed)}" if failed else "") + (
        f"; checks skipped (commit not proven): {', '.join(skipped)}" if skipped else "") + (
        f"; artifact digest {artifact['status']}" if settings["require_artifact_digest"] else "")
    content = {"status": status, "reason": reason, "environment": environment, "target": url,
               "expected_commit": sha, "reported_commit": reported, "http_status": http_status,
               "health": {"status": health, "reason": health_reason, "url": url},
               "checks": checks, "slis": slis, "checks_skipped": skipped, "artifact": artifact,
               "failure_conditions": conditions, "integration_id": target.id,
               "target_config_hash": target_config_hash(settings), "capability_gate": gate,
               "rollback_criteria": settings["rollback_criteria"],
               "observed_at": utcnow().isoformat(), "method": "GET (observe-only; AEOS never deploys)"}
    ev = verification.collect_evidence(
        session, kind="RUNTIME_OBSERVATION", origin="collector", collector_identity=f"{OBSERVER}:{environment}",
        claim=redact_text(f"{environment} observation of {sha[:12]} at {url}: {status}")[:400], content=content,
        evidence_level="E3", subject_type="commit", subject_id=sha, project_id=project.id, domain=project.domain,
        environment={"commit": sha, "deployment_environment": environment, "target": url,
                     "integration_id": target.id},
    )
    part_ids = [c["evidence_id"] for c in checks] + [s["evidence_id"] for s in slis]
    audit.record(session, actor_type="user", actor_id=actor, action="delivery.target.observe",
                 result="SUCCESS", target_type="integration", target_id=target.id, project_id=project.id,
                 domain=project.domain, reason=reason[:500],
                 details={"environment": environment, "status": status, "evidence_id": ev.id, "commit": sha,
                          "check_evidence_ids": part_ids, "failure_conditions": conditions})
    out: dict[str, Any] = {"status": status, "reason": reason, "evidence_id": ev.id, "target": url,
                           "environment": environment, "checks": [_brief(c) for c in checks],
                           "slis": [_brief(s) for s in slis], "artifact": artifact,
                           "failure_conditions": conditions, "post_release": None}
    if environment == "production":
        from aeos.operations import facade as operations

        if status == "FAIL":
            out["post_release"] = operations.post_release_failure(
                session, project=project, commit=sha, target=url, observation_id=ev.id,
                evidence_ids=[ev.id, *part_ids], failure_conditions=conditions,
                rollback_on=settings["rollback_on"], actor=actor)
        elif status == "PASS":
            out["post_release"] = operations.post_release_recovered(session, project=project, commit=sha,
                                                                    observation_id=ev.id, actor=actor)
    return out


def _brief(result: dict[str, Any]) -> dict[str, Any]:
    keep = ("name", "kind", "path", "status", "reason", "http_status", "value", "evidence_id")
    return {k: result[k] for k in keep if k in result}


def audit_refusal(session: Session, project_id: str, environment: str, exc: AeosError, *, actor: str,
                  actor_type: str = "cli", action: str = "delivery.target.observe") -> None:
    """Record a refused observation (e.g. CAN_VALIDATE_STAGING not READY) after the caller rolled back."""
    project = session.get(Project, project_id)
    audit.record(session, actor_type=actor_type, actor_id=actor, action=action,
                 result="DENIED" if exc.status_code in (403, 409) else "FAILED", target_type="project",
                 target_id=project_id, project_id=project.id if project else None,
                 domain=project.domain if project else None, reason=f"{exc.code}: {exc.message}"[:500],
                 details={"environment": environment})


# ----------------------------------------------------------------------------------------- read side


def latest_observation(session: Session, project: Project, shas: list[str | None],
                       environment: str) -> Any:
    """Newest trusted LIVE aggregate observation of *environment* for one of *shas* (or None)."""
    from aeos.models.evidence import EvidenceArtifact

    wanted = [s.lower() for s in shas if s]
    if not wanted:
        return None
    rows = session.scalars(select(EvidenceArtifact).where(
        EvidenceArtifact.project_id == project.id, EvidenceArtifact.kind == "RUNTIME_OBSERVATION",
        EvidenceArtifact.origin == "collector", EvidenceArtifact.collector_identity == f"{OBSERVER}:{environment}",
        EvidenceArtifact.trusted.is_(True), EvidenceArtifact.subject_type == "commit",
        EvidenceArtifact.subject_id.in_(wanted)).order_by(EvidenceArtifact.created_at.desc()))
    for ev in rows:
        if (ev.environment or {}).get("deployment_environment") == environment and ev.mode == "LIVE":
            return ev
    return None


def observation_view(ev: Any, target: ProjectIntegration | None) -> dict[str, Any]:
    """Status of a stored observation for the CURRENT target configuration: STALE when the target or its checks
    changed since, NOT_PROVEN when its content cannot be read back intact."""
    from aeos.verification import facade as verification

    data = verification.read_json(ev) if ev is not None else None
    if ev is None:
        return {"status": "NO_EVIDENCE", "evidence_id": None}
    if not isinstance(data, dict):
        return {"status": "NOT_PROVEN", "evidence_id": ev.id, "reason": "observation content unreadable"}
    status = str(data.get("status") or "UNKNOWN").upper()
    view = {"status": status if status in ("PASS", "FAIL") else "NOT_PROVEN", "evidence_id": ev.id,
            "reason": data.get("reason"), "commit": ev.subject_id, "failure_conditions": data.get("failure_conditions")
            or [], "check_evidence_ids": [c.get("evidence_id") for c in (data.get("checks") or []) + (
                data.get("slis") or []) if isinstance(c, dict) and c.get("evidence_id")],
            "artifact": data.get("artifact") or {}, "observed_at": data.get("observed_at")}
    if target is not None:
        try:
            current = target_config_hash(target_settings(target))
        except ValidationFailed:
            current = None
        if data.get("integration_id") != target.id or data.get("target_config_hash") != current:
            view.update(status="STALE", reason="target or its checks changed since this observation; observe again")
    return view
