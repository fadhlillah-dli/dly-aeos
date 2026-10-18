"""Run Doctor checks, persist CheckResults, share observations with the inventory (§08).

* No LLM is involved. Functional/network checks run only when explicitly requested.
* A check that raises is recorded as UNKNOWN (``PROBE_ERROR``), never as a pass.
* When a check's fingerprint (binary path/size/mtime, relevant non-secret config) differs from its
  previous result, the previous result is marked STALE and a recheck is recorded.
* Results map to ComponentObservations so Doctor and inventory share component references.
* Works without a database: results are then only returned (the bootstrap CLI prints them).
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aeos.core.config import Settings
from aeos.core.hashing import content_hash
from aeos.core.paths import repo_root
from aeos.core.redaction import redact, redact_text
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.doctor.checks import REGISTRY, load_all, ordered_specs
from aeos.doctor.model import GROUP_IDS, CheckContext, CheckResultData, CheckSpec, Outcome, Remedy, aeos_cmd
from aeos.inventory.probes import ProbeRunner, get_runner
from aeos.models.doctor_ext import DoctorRun
from aeos.models.inventory import CheckResult

DEFAULT_SEVERITY = {
    "VERIFIED": "INFO", "CONFIGURED": "INFO", "CONNECTED": "INFO", "NOT_APPLICABLE": "INFO",
    "UNKNOWN": "LOW", "STALE": "MEDIUM", "EXPIRED": "MEDIUM",
}
# CheckStatus -> inventory dimension status, per dimension (§38 vocabularies).
OBS_MAP: dict[str, dict[str, str]] = {
    "installation": {"VERIFIED": "INSTALLED", "CONFIGURED": "INSTALLED", "CONNECTED": "INSTALLED",
                     "MISSING": "NOT_INSTALLED", "FAILED": "UNKNOWN", "UNKNOWN": "UNKNOWN"},
    # MISSING (no binary / no stored credential / not logged in) means the connection was never tried:
    # NOT_CHECKED, not EXPIRED_AUTH; only a rejected credential (EXPIRED) is EXPIRED_AUTH.
    "connectivity": {"CONNECTED": "CONNECTED", "VERIFIED": "CONNECTED", "FAILED": "UNREACHABLE",
                     "EXPIRED": "EXPIRED_AUTH", "MISSING": "NOT_CHECKED", "UNKNOWN": "UNKNOWN"},
    "configuration": {"CONFIGURED": "CONFIGURED", "VERIFIED": "CONFIGURED", "CONNECTED": "CONFIGURED",
                      "MISSING": "MISSING", "FAILED": "INVALID", "UNKNOWN": "UNKNOWN"},
    "functional": {"VERIFIED": "VERIFIED", "FAILED": "FAILED"},
}


@dataclass
class DoctorReport:
    results: list[CheckResultData]
    run_id: str | None
    persisted: bool
    functional: bool
    groups: list[str]
    capability: str | None
    started_at: str
    finished_at: str
    skipped: list[dict[str, str]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out


def make_context(settings: Settings, session: Session | None, *, runner: ProbeRunner | None = None,
                 functional: bool = False, repo: Path | None = None) -> CheckContext:
    r = runner or get_runner()
    return CheckContext(settings=settings, session=session, runner=r, repo=repo or repo_root(), now=utcnow(),
                        functional=functional, env=dict(r.environ()))


def compute_fingerprint(spec: CheckSpec, ctx: CheckContext) -> str | None:
    if spec.fingerprint is None:
        return None
    try:
        material = spec.fingerprint(ctx)
    except Exception:
        return None
    return content_hash(material) if material is not None else None


def select_specs(groups: list[str] | None = None, capability: str | None = None,
                 functional: bool = False) -> tuple[list[CheckSpec], list[dict[str, str]]]:
    from aeos.doctor.capabilities import checks_for_capability

    specs = ordered_specs()
    if groups:
        wanted = {g.upper() for g in groups}
        unknown = wanted - set(GROUP_IDS)
        if unknown:
            from aeos.core.errors import ValidationFailed

            raise ValidationFailed(f"unknown doctor group(s): {sorted(unknown)}", code="UNKNOWN_GROUP")
        specs = [s for s in specs if s.group in wanted]
    if capability:
        ids = set(checks_for_capability(capability))
        specs = [s for s in specs if s.check_id in ids]
    skipped = []
    selected = []
    for s in specs:
        if s.functional and not functional:
            skipped.append({"check_id": s.check_id, "reason": "uji fungsi; jalankan dengan --functional"})
            continue
        selected.append(s)
    return selected, skipped


def run_check(spec: CheckSpec, ctx: CheckContext) -> CheckResultData:
    if spec.needs_db and ctx.session is None:
        out = Outcome("UNKNOWN", "Database belum diinisialisasi; pemeriksaan ini membutuhkan database.",
                      error_code="DB_NOT_INITIALISED", remedy=Remedy(
                          operation="Buat database lokal lalu jalankan ulang Doctor", command=aeos_cmd("db upgrade"),
                          target="<data>/aeos.sqlite3", evidence_to_collect="pemeriksaan ini tidak UNKNOWN lagi"))
    else:
        try:
            out = spec.fn(ctx)
        except Exception as exc:  # a broken probe is UNKNOWN, never green (§38 step 5)
            out = Outcome("UNKNOWN", f"Pemeriksaan gagal dijalankan ({type(exc).__name__}).",
                          error_code="PROBE_ERROR", evidence={"exception": type(exc).__name__,
                                                              "message": redact_text(str(exc))[:200]})
    severity = out.severity or (spec.severity_fail if out.status in ("FAILED", "MISSING") else
                                DEFAULT_SEVERITY.get(out.status, "LOW"))
    from aeos.doctor.capabilities import capability_index

    # §08 scope: project/domain/capability. V1 checks are platform- or domain-level (no project);
    # the capability part names the gates that read this result.
    scope = dict(spec.scope)
    caps = capability_index().get(spec.check_id)
    if caps:
        scope["capabilities"] = caps
    observations = list(out.observations)
    # A check that reports the dimension itself (e.g. OpenCode INSTALLED but FAILED on pin drift) is not
    # overwritten by the generic status mapping.
    explicit = any(o.get("component_id") == spec.component_id and o.get("dimension") == spec.observation
                   for o in observations)
    if spec.observation and spec.component_id and not explicit:
        mapped = OBS_MAP.get(spec.observation, {}).get(out.status)
        if spec.observation == "connectivity" and out.status == "FAILED" and "UNREACHABLE" not in (out.error_code or ""):
            # e.g. wrong auth mode, sealed vault, repo not visible: the endpoint answered, so it is not
            # "unreachable"; the failure itself is the check result. Do not claim more than was seen.
            mapped = "UNKNOWN"
        if mapped:
            observations.insert(0, {"component_id": spec.component_id, "dimension": spec.observation,
                                    "status": mapped, "observed_version": out.component_version})
    return CheckResultData(
        check_id=spec.check_id, title=spec.title, group=spec.group, component_id=spec.component_id,
        component_version=out.component_version, scope=scope, target_identity=out.target_identity,
        status=out.status, severity=severity, summary=redact_text(out.summary)[:1000],
        observed_evidence=redact(out.evidence), last_checked_at=ctx.now,
        expires_at=ctx.now + timedelta(seconds=spec.ttl_seconds), invalidators=list(spec.invalidators),
        fingerprint=compute_fingerprint(spec, ctx), error_code=out.error_code,
        remediation=redact(out.remedy.as_dict()) if out.remedy else {},
        requires_destructive=out.requires_destructive, requires_paid_write=out.requires_paid_write,
        observations=observations,
    )


def latest_results(session: Session, check_ids: list[str] | None = None) -> dict[str, CheckResult]:
    sub = select(CheckResult.check_id, func.max(CheckResult.last_checked_at).label("m")).group_by(CheckResult.check_id)
    if check_ids is not None:
        sub = sub.where(CheckResult.check_id.in_(check_ids))
    s = sub.subquery()
    q = select(CheckResult).join(s, (CheckResult.check_id == s.c.check_id) & (CheckResult.last_checked_at == s.c.m))
    out: dict[str, CheckResult] = {}
    for r in session.scalars(q):
        prev = out.get(r.check_id)
        if prev is None or r.id > prev.id:
            out[r.check_id] = r
    return out


def _persist(session: Session, results: list[CheckResultData], run: DoctorRun, ctx: CheckContext) -> None:
    from aeos.events import service as events
    from aeos.inventory.observations import record_observation

    previous = latest_results(session, [r.check_id for r in results])
    for r in results:
        prev = previous.get(r.check_id)
        if prev is not None and prev.fingerprint and r.fingerprint and prev.fingerprint != r.fingerprint \
                and prev.status != "STALE":
            prev.observed_evidence = {**(prev.observed_evidence or {}), "status_before_stale": prev.status,
                                      "stale_reason": "fingerprint berubah (binary/versi/konfigurasi); dicek ulang"}
            prev.status = "STALE"
            events.emit(session, source="doctor", kind="doctor.check.stale", entity_type="check", entity_id=r.check_id,
                        domain=r.scope.get("domain"), payload={"check_id": r.check_id, "reason": "fingerprint_changed",
                                                               "previous_result_id": prev.id})
        row = CheckResult(
            check_id=r.check_id, title=r.title, group=r.group, component_id=r.component_id,
            component_version=r.component_version, scope=r.scope, target_identity=r.target_identity, status=r.status,
            severity=r.severity, summary=r.summary, observed_evidence=r.observed_evidence,
            last_checked_at=r.last_checked_at, expires_at=r.expires_at, invalidators=r.invalidators,
            fingerprint=r.fingerprint, error_code=r.error_code, remediation=r.remediation,
            requires_destructive=r.requires_destructive, requires_paid_write=r.requires_paid_write, run_id=run.id,
        )
        session.add(row)
        r.run_id = run.id
        if prev is None or prev.status != r.status:
            events.emit(session, source="doctor", kind="doctor.check", entity_type="check", entity_id=r.check_id,
                        domain=r.scope.get("domain"),
                        payload={"check_id": r.check_id, "status": r.status, "severity": r.severity,
                                 "previous_status": prev.status if prev else None, "component_id": r.component_id,
                                 "run_id": run.id})
        for o in r.observations:
            cid = o.get("component_id")
            if not cid:
                continue
            ttl = int((r.expires_at - r.last_checked_at).total_seconds()) if r.expires_at else 3600
            record_observation(session, component_id=cid, dimension=o["dimension"], status=o["status"],
                               observer="doctor", observed_version=o.get("observed_version"),
                               detail={"check_id": r.check_id, "check_status": r.status,
                                       "operation_ref": f"doctor:{r.check_id}"},
                               ttl_seconds=ttl, scope={"run_id": run.id, **({"domain": r.scope["domain"]}
                                                                           if r.scope.get("domain") else {})})
    session.flush()


def run_doctor(
    settings: Settings, session: Session | None, *, groups: list[str] | None = None, capability: str | None = None,
    functional: bool = False, runner: ProbeRunner | None = None, actor: str = "local-operator", trigger: str = "cli",
    repo: Path | None = None,
) -> DoctorReport:
    """Run the selected checks. Persists (without committing) when a session is given."""
    from aeos.events import service as events

    load_all()
    specs, skipped = select_specs(groups, capability, functional)
    ctx = make_context(settings, session, runner=runner, functional=functional, repo=repo)
    started = ctx.now
    results = [run_check(s, ctx) for s in specs]
    finished = utcnow()
    run_id: str | None = None
    persisted = False
    if session is not None:
        run = DoctorRun(started_at=started, finished_at=finished, trigger=trigger, actor=actor,
                        groups=[g.upper() for g in groups or []], capability=capability, functional=functional,
                        host={"platform": sys.platform, "arch": platform.machine()})
        session.add(run)
        session.flush()
        _persist(session, results, run, ctx)
        counts: dict[str, int] = {}
        for r in results:
            counts[r.status] = counts.get(r.status, 0) + 1
        run.counts = counts
        run_id = run.id
        persisted = True
        # The event has no domain (every session sees it), so its counts cover platform-level checks only;
        # domain-scoped results are announced by their own domain-tagged doctor.check events.
        platform_counts: dict[str, int] = {}
        for r in results:
            if not r.scope.get("domain"):
                platform_counts[r.status] = platform_counts.get(r.status, 0) + 1
        events.emit(session, source="doctor", kind="doctor.run.completed", entity_type="doctor_run", entity_id=run.id,
                    payload={"run_id": run.id, "counts_platform": platform_counts, "functional": functional,
                             "groups": run.groups, "capability": capability})
    return DoctorReport(results=results, run_id=run_id, persisted=persisted, functional=functional,
                        groups=[g.upper() for g in groups or []], capability=capability,
                        started_at=started.isoformat(), finished_at=finished.isoformat(), skipped=skipped)


def history(session: Session, check_id: str, *, limit: int = 50) -> list[CheckResult]:
    load_all()
    return list(session.scalars(select(CheckResult).where(CheckResult.check_id == check_id)
                                .order_by(CheckResult.last_checked_at.desc()).limit(min(limit, 500))))


def latest_run(session: Session) -> DoctorRun | None:
    return session.scalar(select(DoctorRun).order_by(DoctorRun.started_at.desc()).limit(1))


def row_view(row: CheckResult | CheckResultData) -> dict[str, Any]:
    """Uniform dict for a stored or in-memory result."""
    if isinstance(row, CheckResultData):
        d = row.as_dict()
    else:
        d = {
            "check_id": row.check_id, "title": row.title, "group": row.group, "component_id": row.component_id,
            "component_version": row.component_version, "scope": row.scope or {},
            "target_identity": row.target_identity, "status": row.status, "severity": row.severity,
            "summary": row.summary, "observed_evidence": row.observed_evidence or {},
            "last_checked_at": ensure_aware(row.last_checked_at).isoformat() if row.last_checked_at else None,
            "expires_at": ensure_aware(row.expires_at).isoformat() if row.expires_at else None,
            "invalidators": row.invalidators or [], "fingerprint": row.fingerprint, "error_code": row.error_code,
            "remediation": row.remediation or {}, "requires_destructive": row.requires_destructive,
            "requires_paid_write": row.requires_paid_write, "run_id": row.run_id, "id": row.id,
        }
    spec = REGISTRY.get(d["check_id"])
    if spec is not None and not d.get("title"):
        d["title"] = spec.title
    return d
