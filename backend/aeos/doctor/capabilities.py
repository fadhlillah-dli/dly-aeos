"""Capability readiness gates (§08). Implements ``aeos.doctor.facade``.

Readiness is evaluated per capability and target from the latest check results — never from
narration. Each capability has three tiers:

* **critical** — any failing/missing check -> BLOCKED; never-run, STALE or EXPIRED -> UNKNOWN;
* **required** — a FAILED check -> BLOCKED (e.g. CONNECTED + functional FAILED is not READY);
  otherwise not-ok -> DEGRADED (e.g. bridge NOT-PROVEN, manual fallback available);
* **advisory** — shown as reasons only (e.g. MCI UNVERIFIED, quota UNKNOWN, residual risks).

OFFICE capabilities are BLOCKED by default (§23) until a recorded company authorisation sets the
relevant policy flag. Human-only merge and production deployment are never capabilities.
Optional components only appear in the tiers of capabilities that depend on them, so a missing
optional tool never blocks unrelated capabilities (§07, §50).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from aeos.core.enums import Capability
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.doctor.facade import CapabilityState
from aeos.doctor.office import office_gate

BASE = ("host.data_dir", "runtime.python", "runtime.backend_deps", "runtime.db", "runtime.storage", "auth.bootstrap",
        "auth.default_credentials")
STRICT_CONNECTED = frozenset({"CONNECTED", "VERIFIED"})
HUMAN_ONLY = (
    "Merge ke main", "Deployment produksi", "Persetujuan PlanVersion", "Persetujuan publikasi eksternal",
    "Penerimaan risiko", "Kenaikan budget / route berbayar",
)


@dataclass(frozen=True)
class CapRule:
    label: str
    critical: tuple[str, ...]
    required: tuple[str, ...] = ()
    advisory: tuple[str, ...] = ()
    domain: str | None = None  # capability that only exists for one domain
    office_gate: bool = False
    required_ok: dict[str, frozenset[str]] = field(default_factory=dict)  # stricter ok-set in the required tier
    description: str = ""


RULES: dict[Capability, CapRule] = {
    Capability.CAN_OPEN_DASHBOARD: CapRule(
        "Membuka dashboard lokal", (*BASE, "host.port", "runtime.frontend_dist"), required=("auth.admins",),
        advisory=("host.platform", "host.disk", "host.clock", "runtime.node", "repo.root", "repo.install_manifest",
                  "secret.no_raw_exposure"),
        description="Server lokal, database, auth bootstrap dan build Control Center."),
    Capability.CAN_PLAN_PERSONAL: CapRule(
        "Merencanakan proyek PERSONAL", (*BASE, "claude.env"),
        required=("claude.binary", "claude.auth", "claude.capability_test", "policy.snapshot"),
        advisory=("policy.mci", "claude.extra_billing", "claude.quota", "secret.no_raw_exposure"), domain="PERSONAL",
        description="Planning Opus/Fable lewat Claude Code resmi (Max); tanpa bridge terbukti hanya paket manual."),
    Capability.CAN_PLAN_OFFICE: CapRule(
        "Merencanakan proyek OFFICE", (*BASE, "claude.env", "policy.office_defaults"),
        required=("claude.binary", "claude.auth", "claude.capability_test", "policy.snapshot"),
        advisory=("policy.mci", "claude.extra_billing", "claude.quota"), domain="OFFICE", office_gate=True,
        description="Butuh otorisasi perusahaan atas data/provider (§23, §51 P1)."),
    Capability.CAN_PUBLISH_PERSONAL: CapRule(
        "Publikasi dokumen/tiket PERSONAL",
        (*BASE, "auth.interlock", "secret.status", "adapter.github_token", "adapter.google_oauth", "secret.no_raw_exposure"),
        required=("adapter.github_token",), advisory=("recovery.publication_idempotency", "policy.mci"),
        domain="PERSONAL", required_ok={"adapter.github_token": STRICT_CONNECTED},
        description="Interlock external-write, credential adapter dan akses target yang terbukti."),
    Capability.CAN_RUN_WORKER: CapRule(
        "Menjalankan worker dalam sandbox",
        (*BASE, "worker.opencode", "containment.sandbox", "containment.report", "route.registry", "route.worker_key", "secret.status", "evidence.verifier", "secret.no_raw_exposure"),
        required=("worker.contract", "route.catalog_freshness", "containment.protected_paths"),
        advisory=("containment.docker", "recovery.leases"),
        description="Engine, isolasi yang teruji, route disetujui, kunci via broker dan verifier independen."),
    Capability.CAN_REVIEW_WITH_MAX: CapRule(
        "Review checkpoint dengan Claude Max", (*BASE, "claude.binary", "claude.auth", "claude.env"),
        required=("claude.capability_test",), advisory=("claude.extra_billing", "claude.quota"),
        description="Pengawas native Max; kuota habis berarti menunggu, bukan fallback."),
    Capability.CAN_PREPARE_PR: CapRule(
        "Menyiapkan PR (merge tetap manusia)",
        (*BASE, "repo.origin", "adapter.github_token", "evidence.verifier", "delivery.ci_workflow"),
        required=("repo.state", "adapter.github_token", "evidence.acceptance_suite"),
        advisory=("delivery.branch_protection", "repo.hooks"), domain="PERSONAL",
        required_ok={"adapter.github_token": STRICT_CONNECTED},
        description="Broker membuat PR dari branch aeos/*; tidak ada operasi merge."),
    Capability.CAN_USE_OFFICE_INTEGRATIONS: CapRule(
        "Integrasi OFFICE (Bitbucket/Jira/Confluence)",
        (*BASE, "policy.office_defaults", "adapter.office_bitbucket", "adapter.office_jira", "adapter.office_confluence"),
        domain="OFFICE", office_gate=True, description="Diblokir default sampai otorisasi perusahaan (§23)."),
    Capability.CAN_VALIDATE_STAGING: CapRule(
        "Validasi staging", (*BASE, "delivery.staging_target", "evidence.verifier"), required=("recovery.restore_test",),
        description="Target staging berizin, verifier, recovery teruji."),
}


def checks_for_capability(capability: str | Capability) -> list[str]:
    rule = RULES[Capability(capability)]
    seen: list[str] = []
    for cid in (*rule.critical, *rule.required, *rule.advisory):
        if cid not in seen:
            seen.append(cid)
    return seen


def capability_index() -> dict[str, list[str]]:
    """check_id -> capabilities whose gate reads it (the §08 'capability scope' of a CheckResult)."""
    out: dict[str, list[str]] = {}
    for cap in RULES:
        for cid in checks_for_capability(cap):
            out.setdefault(cid, []).append(cap.value)
    return out


def _parse(ts: Any) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ensure_aware(ts)
    try:
        return ensure_aware(datetime.fromisoformat(str(ts)))
    except ValueError:
        return None


def effective_status(row: dict[str, Any] | None, now: datetime, live_fp: str | None) -> tuple[str, str | None]:
    """Stored status adjusted for STALE (invalidated / fingerprint changed) and EXPIRED (TTL)."""
    if row is None:
        return "UNKNOWN", "belum pernah diperiksa"
    status = row["status"]
    if status == "STALE":
        return "STALE", (row.get("observed_evidence") or {}).get("stale_reason") or "ditandai usang; perlu dicek ulang"
    if live_fp is not None and row.get("fingerprint") and live_fp != row["fingerprint"]:
        return "STALE", "binary/versi/konfigurasi berubah sejak pemeriksaan terakhir; perlu dicek ulang"
    exp = _parse(row.get("expires_at"))
    if exp is not None and exp <= now:
        return "EXPIRED", "hasil pemeriksaan kedaluwarsa; perlu dicek ulang"
    return status, None


def live_fingerprints(session: Session | None, check_ids: list[str]) -> dict[str, str | None]:
    """Cheap current fingerprints (no subprocess) used to detect STALE results without re-running."""
    from aeos.core.config import get_settings
    from aeos.doctor.checks import REGISTRY, load_all
    from aeos.doctor.runner import compute_fingerprint, make_context

    load_all()
    specs = [REGISTRY[c] for c in check_ids if c in REGISTRY and REGISTRY[c].fingerprint is not None]
    if not specs:
        return {}
    ctx = make_context(get_settings(), session)
    return {s.check_id: compute_fingerprint(s, ctx) for s in specs}


def _office_gate_key(capability: Capability, rule: CapRule, domain: str | None) -> str | None:
    """Which OFFICE policy gate applies (None = not an OFFICE evaluation)."""
    if rule.office_gate:
        return capability.value
    if domain == "OFFICE" and capability == Capability.CAN_RUN_WORKER:
        return "CAN_RUN_WORKER"  # external_worker_allowed
    if domain == "OFFICE" and capability == Capability.CAN_REVIEW_WITH_MAX:
        return "CAN_PLAN_OFFICE"  # Claude Max on Office data needs company approval (§51 P1)
    if domain == "OFFICE" and capability == Capability.CAN_VALIDATE_STAGING:
        return "CAN_USE_OFFICE_INTEGRATIONS"  # Office execution stays off until authorised (§23, §49 M5)
    return None


def evaluate(capability: Capability, rows: dict[str, dict[str, Any]], *, now: datetime,
             fps: dict[str, str | None], domain: str | None, session: Session | None) -> CapabilityState:
    from aeos.doctor.checks import REGISTRY, load_all

    load_all()
    rule = RULES[capability]
    if rule.domain is not None and domain is not None and domain != rule.domain:
        return CapabilityState(capability, "NOT_APPLICABLE", [f"Capability ini hanya untuk domain {rule.domain}."])
    eff_domain = rule.domain or domain
    blocked: list[str] = []
    unknown: list[str] = []
    degraded: list[str] = []
    notes: list[str] = []
    checks: list[dict[str, Any]] = []
    remedies: list[dict[str, Any]] = []
    for tier, ids in (("critical", rule.critical), ("required", rule.required), ("advisory", rule.advisory)):
        for cid in ids:
            spec = REGISTRY.get(cid)
            row = rows.get(cid)
            eff, why = effective_status(row, now, fps.get(cid))
            ok_set = rule.required_ok.get(cid) if tier == "required" and cid in rule.required_ok else (
                spec.ok_statuses if spec else frozenset({"VERIFIED"}))
            ok = eff in ok_set
            title = spec.title if spec else cid
            checks.append({"check_id": cid, "title": title, "tier": tier, "status": row["status"] if row else None,
                           "effective_status": eff, "ok": ok, "last_checked_at": row.get("last_checked_at") if row else None,
                           "component_id": spec.component_id if spec else None})
            if ok:
                continue
            summary = (row or {}).get("summary") or why or ""
            reason = f"{cid}: {eff} - {summary}" if summary else f"{cid}: {eff}"
            if why and why not in reason:
                reason += f" ({why})"
            if row and row.get("remediation") and tier != "advisory":
                remedies.append({"check_id": cid, **row["remediation"]})
            if tier == "critical":
                (unknown if eff in ("UNKNOWN", "STALE", "EXPIRED") else blocked).append(reason)
            elif tier == "required":
                (blocked if eff == "FAILED" else degraded).append(reason)
            else:
                notes.append(f"catatan: {reason}")
    gate_reason = None
    gate_key = _office_gate_key(capability, rule, eff_domain)
    if gate_key is not None:
        allowed, gate_reason = office_gate(gate_key, session)
        if not allowed:
            blocked.insert(0, gate_reason)
    if blocked:
        status = "BLOCKED"
    elif unknown:
        status = "UNKNOWN"
    elif degraded:
        status = "DEGRADED"
    else:
        status = "READY"
    reasons = [*blocked, *unknown, *degraded, *notes]
    if status == "READY" and gate_reason:
        reasons.insert(0, gate_reason)
    return CapabilityState(capability=capability, status=status, reasons=reasons, checks=checks, remedies=remedies)


def _rows(session: Session | None, check_ids: list[str]) -> dict[str, dict[str, Any]]:
    if session is None:
        return {}
    from aeos.doctor.runner import latest_results, row_view

    return {cid: row_view(r) for cid, r in latest_results(session, check_ids).items()}


def capability_state(session: Session, capability: Capability, *, domain: str | None = None,
                     project_id: str | None = None) -> CapabilityState:
    """Facade implementation. ``project_id`` is accepted for scoping; V1 checks are platform/domain-level."""
    ids = checks_for_capability(capability)
    return evaluate(Capability(capability), _rows(session, ids), now=utcnow(), fps=live_fingerprints(session, ids),
                    domain=domain, session=session)


def capability_matrix(session: Session | None, *, domain: str | None = None,
                      rows: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    all_ids = sorted({c for cap in RULES for c in checks_for_capability(cap)})
    rows = rows if rows is not None else _rows(session, all_ids)
    fps = live_fingerprints(session, all_ids)
    now = utcnow()
    out = []
    for cap, rule in RULES.items():
        st = evaluate(cap, rows, now=now, fps=fps, domain=domain, session=session)
        out.append({"capability": cap.value, "label": rule.label, "description": rule.description,
                    "domain": rule.domain, "status": st.status, "reasons": st.reasons, "checks": st.checks,
                    "remedies": st.remedies})
    return out


def _matches(invalidator: str, declared: list[str]) -> bool:
    for d in declared or []:
        if d == invalidator:
            return True
        if d.endswith("*") and invalidator.startswith(d[:-1]):
            return True
        if invalidator.endswith("*") and d.startswith(invalidator[:-1]):
            return True
    return False


COMPONENT_INVALIDATOR = "component:"


def _depends(invalidator: str, row: Any) -> bool:
    """A result depends on *invalidator* when it declares it (exact or wildcard), or - for
    ``component:<id>`` (the observed identity/version of that component changed) - when the check is
    about that component."""
    if invalidator.startswith(COMPONENT_INVALIDATOR):
        return bool(row.component_id) and row.component_id == invalidator[len(COMPONENT_INVALIDATOR):]
    return _matches(invalidator, row.invalidators or [])


def invalidate(session: Session, *, invalidator: str, reason: str = "") -> int:
    """Mark the latest results that depend on *invalidator* as STALE (recheck required) and expire the
    related component observations. Returns the number of results marked STALE.

    Invalidators are declared per check (``binary:claude``, ``config:routes``, ``secret:<ref>``,
    ``manifest`` ...); ``component:<id>`` marks every check about that component (used by inventory
    reconciliation when an observed version/identity changes, §38 "Drift dan kegagalan")."""
    from aeos.doctor.runner import latest_results
    from aeos.events import service as events
    from aeos.inventory.observations import expire_component_observations

    n = 0
    components: set[str] = set()
    why = reason or f"invalidated by {invalidator}"
    for cid, row in latest_results(session).items():
        if row.status == "STALE" or not _depends(invalidator, row):
            continue
        row.observed_evidence = {**(row.observed_evidence or {}), "status_before_stale": row.status,
                                 "stale_reason": f"{why} ({invalidator})"[:300]}
        row.status = "STALE"
        n += 1
        if row.component_id:
            components.add(row.component_id)
        events.emit(session, source="doctor", kind="doctor.check.stale", entity_type="check", entity_id=cid,
                    domain=(row.scope or {}).get("domain"),
                    payload={"check_id": cid, "invalidator": invalidator, "reason": why[:200]})
    for comp in components:
        expire_component_observations(session, comp, reason=f"{invalidator}: {why}")
    session.flush()
    return n
