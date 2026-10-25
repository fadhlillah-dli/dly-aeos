"""Capability readiness gates (§08). Implements ``aeos.doctor.facade``.

Readiness is evaluated per capability and target from the latest check results — never from
narration. Each capability has three tiers:

* **critical** — any failing/missing check -> BLOCKED; never-run, STALE or EXPIRED -> UNKNOWN;
* **required** — a FAILED check -> BLOCKED (e.g. CONNECTED + functional FAILED is not READY);
  otherwise not-ok -> DEGRADED (e.g. bridge NOT-PROVEN, manual fallback available);
* **guard** — FAILED -> BLOCKED, anything else is a note (e.g. WEAK process-sandbox containment,
  worker contract NOT-PROVEN: the run still ends in REVIEW behind the independent verifier);
* **advisory** — shown as reasons only (e.g. MCI UNVERIFIED, quota UNKNOWN, residual risks).

``CAN_RUN_WORKER`` additionally asks ``aeos.worker_runtime.facade.sandbox_readiness`` live on every read
(ADR-0051: READY only with a proven container or an ACTIVE owner exception for the WEAK process sandbox).
Residual risks reported by the checks (WEAK sandbox, branch protection NOT_ENFORCEABLE, interim keyring)
are returned separately and never turn a gate green.

OFFICE capabilities are BLOCKED by default (§23) until a recorded company authorisation sets the
relevant policy flag. Human-only merge and production deployment are never capabilities.
Optional components only appear in the tiers of capabilities that depend on them, so a missing
optional tool never blocks unrelated capabilities (§07, §50).
"""

from __future__ import annotations

import threading
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
    # checks that only matter when they FAILED (-> BLOCKED); otherwise a note + residual risk, e.g. a WEAK
    # process-sandbox containment report or an unproven worker contract (the run still ends in REVIEW).
    guard: tuple[str, ...] = ()
    live_sandbox: bool = False  # also ask worker_runtime.facade.sandbox_readiness(session, domain) live
    # §08 "Readiness dievaluasi menurut capability dan target": project-scoped checks evaluated for the project
    # the caller names (FAILED/MISSING -> BLOCKED, never-run/STALE/EXPIRED/UNKNOWN -> UNKNOWN, other -> DEGRADED).
    project: tuple[str, ...] = ()
    project_ok: dict[str, frozenset[str]] = field(default_factory=dict)


RULES: dict[Capability, CapRule] = {
    Capability.CAN_OPEN_DASHBOARD: CapRule(
        "Membuka dashboard lokal", (*BASE, "host.port", "runtime.frontend_dist"), required=("auth.admins",),
        guard=("auth.http_guard",),
        advisory=("host.platform", "host.disk", "host.clock", "runtime.node", "repo.root", "repo.install_manifest",
                  "secret.no_raw_exposure", "host.vscode", "host.resource_budget"),
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
        (*BASE, "auth.interlock", "secret.status", "adapter.github_token", "adapter.google_oauth",
         "adapter.write_enablement", "secret.no_raw_exposure"),
        required=("adapter.github_token",), guard=("auth.http_guard", "secret.openbao_auth"),
        advisory=("recovery.publication_idempotency", "policy.mci"),
        domain="PERSONAL", required_ok={"adapter.github_token": STRICT_CONNECTED},
        project=("adapter.project_drive_root",),
        description="Interlock external-write (bootstrap, write enablement manusia), token Google + GitHub dan akses "
                    "target yang terbukti; setiap write tetap butuh approval versi/hash. Per proyek: root Drive "
                    "proyek dan scope token."),
    Capability.CAN_RUN_WORKER: CapRule(
        "Menjalankan worker dalam sandbox",
        (*BASE, "worker.opencode", "worker.agent_definition", "containment.sandbox", "route.registry",
         "route.worker_key", "route.budget_admission", "secret.status", "evidence.verifier", "secret.no_raw_exposure"),
        required=("containment.protected_paths",),
        guard=("containment.report", "worker.contract", "secret.openbao_auth"),
        advisory=("route.catalog_freshness", "containment.docker", "recovery.leases"),
        live_sandbox=True,
        description="OpenCode terpasang, agent opencode-implementer aktif, sandbox memenuhi ADR-0051 (live), route "
                    "APPROVED/TRIAL eligible, budget admission sehat, kunci via broker dan verifier independen."),
    Capability.CAN_REVIEW_WITH_MAX: CapRule(
        "Review checkpoint dengan Claude Max", (*BASE, "claude.binary", "claude.auth", "claude.env"),
        required=("claude.capability_test",), advisory=("claude.extra_billing", "claude.quota"),
        description="Pengawas native Max; kuota habis berarti menunggu, bukan fallback."),
    Capability.CAN_PREPARE_PR: CapRule(
        "Menyiapkan PR (merge tetap manusia)",
        (*BASE, "repo.origin", "adapter.github_token", "evidence.verifier", "delivery.ci_workflow"),
        required=("repo.state", "adapter.github_token", "evidence.acceptance_suite"),
        guard=("evidence.acceptance_invalidation", "repo.domain"),
        advisory=("delivery.branch_protection", "repo.hooks"), domain="PERSONAL",
        required_ok={"adapter.github_token": STRICT_CONNECTED},
        project=("adapter.project_repo",), project_ok={"adapter.project_repo": STRICT_CONNECTED},
        description="Broker membuat PR dari branch aeos/*; tidak ada operasi merge. Per proyek: akses repo "
                    "proyek (ID repo persis) terbukti."),
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
    for cid in (*rule.critical, *rule.required, *rule.guard, *rule.advisory, *rule.project):
        if cid not in seen:
            seen.append(cid)
    return seen


def _risks_of(row: dict[str, Any] | None) -> list[str]:
    ev = (row or {}).get("observed_evidence") or {}
    out: list[str] = []
    for key in ("residual_risks", "residual_risk"):
        v = ev.get(key)
        if isinstance(v, str) and v:
            out.append(v)
        elif isinstance(v, list):
            out += [str(x) for x in v if x]
    return out


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


LIVE_SANDBOX_ID = "worker_runtime.sandbox_readiness"


def _live_sandbox(session: Session | None, domain: str | None) -> tuple[str, list[str], list[dict[str, Any]],
                                                                         list[str], dict[str, Any]]:
    """(tier outcome, reasons, remedies, residual risks, check entry) of the live ADR-0051 requirement.

    Evaluated on every gate read (not only from a stored CheckResult): an exception decision can expire by
    time and Docker can disappear between Doctor runs. A domain-less view is evaluated for PERSONAL and says
    so; OFFICE is always BLOCKED by the worker runtime."""
    eval_domain = domain or "PERSONAL"
    entry: dict[str, Any] = {"check_id": LIVE_SANDBOX_ID, "title": "Syarat sandbox worker (live, ADR-0051)",
                             "tier": "critical", "live": True, "component_id": "worker-runtime",
                             "last_checked_at": utcnow().isoformat()}
    if session is None:
        entry.update(status=None, effective_status="UNKNOWN", ok=False)
        return "unknown", [f"{LIVE_SANDBOX_ID}: UNKNOWN - syarat sandbox (keputusan pengecualian) tidak dapat dinilai "
                           "tanpa database"], [], [], entry
    from aeos.doctor.checks.containment import sandbox_readiness_view

    sb = sandbox_readiness_view(session, eval_domain)
    status = str(sb.get("status") or "BLOCKED")
    risks = [str(r) for r in sb.get("residual_risks") or []] if status == "READY" else []
    # Honest label: only a container whose containment report passed is VERIFIED. The WEAK process sandbox
    # allowed by an owner exception satisfies the policy (ok) but is CONFIGURED, never VERIFIED (§08, §25).
    if status == "READY":
        label = "VERIFIED" if sb.get("kind") == "container" else "CONFIGURED"
    else:
        label = "MISSING"
    entry.update(status=status, effective_status=label, ok=status == "READY", kind=sb.get("kind"),
                 evaluated_domain=eval_domain)
    if status == "READY":
        entry["residual_risks"] = risks
        notes = [] if domain else [f"catatan: {LIVE_SANDBOX_ID}: dievaluasi untuk PERSONAL (OFFICE selalu BLOCKED "
                                   "tanpa isolasi terverifikasi)"]
        if sb.get("kind") == "process":
            notes.append(f"catatan: {LIVE_SANDBOX_ID}: process sandbox WEAK diizinkan keputusan pengecualian "
                         f"{str(sb.get('decision_id') or '')[:8]}; residual risk berlaku")
        return "ok", notes, [], risks, entry
    why = "; ".join(str(r) for r in sb.get("reasons") or []) or "syarat tidak terpenuhi"
    remedies = [{"check_id": LIVE_SANDBOX_ID, "operation": str(r), "executed_by": "MANUSIA (salin dan jalankan sendiri)"}
                for r in sb.get("remedies") or []]
    # Not accepted yet: these are the risks an owner exception for the WEAK process sandbox would accept.
    pending = [f"bila process sandbox diizinkan: {r}" for r in sb.get("residual_risks") or []]
    entry["residual_risks"] = pending
    return "blocked", [f"{LIVE_SANDBOX_ID}: BLOCKED ({eval_domain}) - {why}"], remedies, pending, entry


PLATFORM_COVER = {"adapter.project_repo": "adapter.github_token"}
# Optional per-project targets: a project that has no such target (e.g. publishes tickets only, no Drive root) is
# NOT_APPLICABLE for that check instead of UNKNOWN; the target itself is never assumed.
OPTIONAL_PROJECT_TARGETS = frozenset({"adapter.project_drive_root"})


def _project_tier(rule: CapRule, project: dict[str, Any] | None, project_rows: dict[str, dict[str, Any]],
                  rows: dict[str, dict[str, Any]], *, now: datetime, excluded: set[str]
                  ) -> tuple[list[str], list[str], list[str], list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """(blocked, unknown, degraded, notes, checks, remedies) of the project tier for one project.

    A project whose GitHub repository *is* the platform repository is covered by the platform-level
    ``adapter.github_token`` probe of exactly that repo (``PLATFORM_COVER``); any other target needs its own
    project-scoped result — readiness of one project is never inferred from another target (§08)."""
    from aeos.doctor.checks import REGISTRY

    blocked: list[str] = []
    unknown: list[str] = []
    degraded: list[str] = []
    notes: list[str] = []
    checks: list[dict[str, Any]] = []
    remedies: list[dict[str, Any]] = []
    if not rule.project:
        return blocked, unknown, degraded, notes, checks, remedies
    if project is None:
        notes.append(f"catatan: {', '.join(rule.project)}: target proyek dievaluasi per proyek (pilih proyek)")
        return blocked, unknown, degraded, notes, checks, remedies
    if not project.get("known", True):
        unknown.append(f"proyek {str(project.get('id'))[:8]}: tidak ditemukan pada domain ini; target proyek "
                       "tidak dievaluasi (fail closed)")
        return blocked, unknown, degraded, notes, checks, remedies
    for cid in rule.project:
        if cid in excluded:
            continue
        spec = REGISTRY.get(cid)
        row = project_rows.get(cid)
        covered = None
        if row is None and cid in PLATFORM_COVER and project.get("is_platform_repo"):
            covered = PLATFORM_COVER[cid]
            row = rows.get(covered)
        eff, why = effective_status(row, now, None)
        if row is None and cid in OPTIONAL_PROJECT_TARGETS and not _current_target_set(cid, project):
            eff, why = "NOT_APPLICABLE", "proyek ini tidak memiliki target tersebut"
            checks.append({"check_id": cid, "title": spec.title if spec else cid, "tier": "project", "status": None,
                           "effective_status": eff, "ok": True, "last_checked_at": None,
                           "component_id": spec.component_id if spec else None, "project_id": project.get("id"),
                           "covered_by": None, "residual_risks": []})
            notes.append(f"catatan: {cid} (proyek {str(project.get('id'))[:8]}): NOT_APPLICABLE - {why}")
            continue
        if row is not None and covered is None and eff not in ("STALE", "EXPIRED"):
            probed = _target_set(cid, row.get("observed_evidence") or {})
            current = _current_target_set(cid, project)
            if probed is not None and current is not None and probed != current:
                eff, why = "STALE", "target proyek berubah sejak pemeriksaan terakhir; perlu dicek ulang"
        ok_set = rule.project_ok.get(cid) or (spec.ok_statuses if spec else frozenset({"VERIFIED"}))
        ok = eff in ok_set
        checks.append({"check_id": cid, "title": spec.title if spec else cid, "tier": "project",
                       "status": row["status"] if row else None, "effective_status": eff, "ok": ok,
                       "last_checked_at": row.get("last_checked_at") if row else None,
                       "component_id": spec.component_id if spec else None, "project_id": project.get("id"),
                       "covered_by": covered, "residual_risks": []})
        if ok:
            if covered:
                notes.append(f"catatan: {cid}: repo proyek = repo platform; dibuktikan oleh {covered}")
            continue
        summary = (row or {}).get("summary") or why or ""
        reason = f"{cid} (proyek {str(project.get('id'))[:8]}): {eff}" + (f" - {summary}" if summary else "")
        if row and row.get("remediation"):
            remedies.append({"check_id": cid, **row["remediation"]})
        if eff in ("FAILED", "MISSING"):
            blocked.append(reason)
        elif eff in ("UNKNOWN", "STALE", "EXPIRED"):
            unknown.append(reason)
        else:
            degraded.append(reason)
    return blocked, unknown, degraded, notes, checks, remedies


def evaluate(capability: Capability, rows: dict[str, dict[str, Any]], *, now: datetime,
             fps: dict[str, str | None], domain: str | None, session: Session | None,
             exclude_checks: tuple[str, ...] = (), project: dict[str, Any] | None = None,
             project_rows: dict[str, dict[str, Any]] | None = None) -> CapabilityState:
    from aeos.doctor.checks import REGISTRY, load_all

    load_all()
    rule = RULES[capability]
    if rule.domain is not None and domain is not None and domain != rule.domain:
        return CapabilityState(capability, "NOT_APPLICABLE", [f"Capability ini hanya untuk domain {rule.domain}."])
    eff_domain = rule.domain or domain
    excluded = set(exclude_checks)
    blocked: list[str] = []
    unknown: list[str] = []
    degraded: list[str] = []
    notes: list[str] = []
    checks: list[dict[str, Any]] = []
    remedies: list[dict[str, Any]] = []
    risks: list[str] = []
    tiers = (("critical", rule.critical), ("required", rule.required), ("guard", rule.guard),
             ("advisory", rule.advisory))
    for tier, ids in tiers:
        for cid in ids:
            if cid in excluded:
                continue
            spec = REGISTRY.get(cid)
            row = rows.get(cid)
            eff, why = effective_status(row, now, fps.get(cid))
            ok_set: frozenset[str] = rule.required_ok[cid] if tier == "required" and cid in rule.required_ok else (
                spec.ok_statuses if spec else frozenset({"VERIFIED"}))
            ok = eff in ok_set
            title = spec.title if spec else cid
            # a stale/expired result no longer describes the system; risks stay attached to their check so a
            # view can drop those of checks scoped to another domain (views.capabilities_view, §50)
            row_risks = _risks_of(row) if eff not in ("STALE", "EXPIRED") else []
            checks.append({"check_id": cid, "title": title, "tier": tier, "status": row["status"] if row else None,
                           "effective_status": eff, "ok": ok, "last_checked_at": row.get("last_checked_at") if row else None,
                           "component_id": spec.component_id if spec else None, "residual_risks": row_risks})
            risks += row_risks
            if ok:
                continue
            summary = (row or {}).get("summary") or why or ""
            reason = f"{cid}: {eff} - {summary}" if summary else f"{cid}: {eff}"
            if why and why not in reason:
                reason += f" ({why})"
            gating = tier in ("critical", "required") or (tier == "guard" and eff == "FAILED")
            if row and row.get("remediation") and gating:
                remedies.append({"check_id": cid, **row["remediation"]})
            if tier == "critical":
                (unknown if eff in ("UNKNOWN", "STALE", "EXPIRED") else blocked).append(reason)
            elif tier == "required":
                (blocked if eff == "FAILED" else degraded).append(reason)
            elif tier == "guard" and eff == "FAILED":
                blocked.append(reason)
            else:
                notes.append(f"catatan: {reason}")
    pb, pu, pd, pn, pchecks, premedies = _project_tier(rule, project, project_rows or {}, rows, now=now,
                                                       excluded=excluded)
    blocked += pb
    unknown += pu
    degraded += pd
    notes += pn
    checks += pchecks
    remedies += premedies
    if rule.live_sandbox and "containment.sandbox" not in excluded:
        outcome, live_reasons, live_remedies, live_risks, entry = _live_sandbox(session, eff_domain)
        checks.append(entry)
        remedies += live_remedies
        risks += live_risks
        if outcome == "blocked":
            blocked += live_reasons
        elif outcome == "unknown":
            unknown += live_reasons
        else:
            notes += live_reasons
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
    risks = list(dict.fromkeys(risks))
    return CapabilityState(capability=capability, status=status, reasons=reasons, checks=checks, remedies=remedies,
                           residual_risks=risks)


def _rows(session: Session | None, check_ids: list[str]) -> dict[str, dict[str, Any]]:
    if session is None:
        return {}
    from aeos.doctor.runner import latest_results, row_view

    return {cid: row_view(r) for cid, r in latest_results(session, check_ids).items()}


def project_target(session: Session | None, project_id: str | None, domain: str | None = None
                   ) -> dict[str, Any] | None:
    """The project a gate is evaluated for: id, domain, its current targets (GitHub repos, Drive roots) and whether
    its GitHub repo is the platform repo. A project of another domain than the gate's is never evaluated as if it
    were this domain's (its targets are not loaded; the project tier stays UNKNOWN, fail closed)."""
    if session is None or not project_id:
        return None
    from sqlalchemy import select

    from aeos.core.config import get_settings
    from aeos.models.portfolio import Project, ProjectIntegration, RepositoryReference

    p = session.get(Project, project_id)
    if p is None or (domain is not None and p.domain != domain):
        return {"id": project_id, "domain": p.domain if p else None, "is_platform_repo": False, "known": False}
    slug = get_settings().platform_repo_slug.lower()
    repos = [r.full_name.lower() for r in session.scalars(select(RepositoryReference).where(
        RepositoryReference.project_id == project_id)) if (r.provider or "").upper() == "GITHUB"]
    roots = [str(i.root_id or "") for i in session.scalars(select(ProjectIntegration).where(
        ProjectIntegration.project_id == project_id, ProjectIntegration.provider == "google-drive"))]
    return {"id": p.id, "domain": p.domain, "known": True, "repos": repos, "drive_roots": roots,
            "is_platform_repo": bool(repos) and all(r == slug for r in repos)}


# §08 "Perubahan target memicu invalidation": a project-scoped result only describes the targets it probed. When
# the project's current targets differ (repository added/replaced, Drive root added) the result is STALE.
def _target_set(check_id: str, evidence: dict[str, Any]) -> list[str] | None:
    if check_id == "adapter.project_repo":
        return sorted(str(x).lower() for x in evidence.get("repos") or [])
    if check_id == "adapter.project_drive_root":
        return sorted(str((x or {}).get("root_id") or "") for x in evidence.get("roots") or [])
    return None


def _current_target_set(check_id: str, project: dict[str, Any]) -> list[str] | None:
    if check_id == "adapter.project_repo":
        return sorted(project.get("repos") or [])
    if check_id == "adapter.project_drive_root":
        return sorted(project.get("drive_roots") or [])
    return None


def _project_rows(session: Session | None, check_ids: list[str], project_id: str | None) -> dict[str, dict[str, Any]]:
    if session is None or not project_id or not check_ids:
        return {}
    from aeos.doctor.runner import latest_project_results, row_view

    return {cid: row_view(r) for (cid, _p), r in latest_project_results(session, check_ids,
                                                                        project_id=project_id).items()}


def capability_state(session: Session, capability: Capability, *, domain: str | None = None,
                     project_id: str | None = None, exclude_checks: tuple[str, ...] = ()) -> CapabilityState:
    """Facade implementation. With ``project_id`` the rule's project tier is evaluated for that project's own
    targets (repository / Drive root); without it the gate is platform/domain-level and says so."""
    cap = Capability(capability)
    ids = checks_for_capability(cap)
    rule = RULES[cap]
    return evaluate(cap, _rows(session, ids), now=utcnow(), fps=live_fingerprints(session, ids),
                    domain=domain, session=session, exclude_checks=tuple(exclude_checks or ()),
                    project=project_target(session, project_id, domain=RULES[cap].domain or domain),
                    project_rows=_project_rows(session, list(rule.project), project_id))


def capability_matrix(session: Session | None, *, domain: str | None = None,
                      rows: dict[str, dict[str, Any]] | None = None, project_id: str | None = None
                      ) -> list[dict[str, Any]]:
    all_ids = sorted({c for cap in RULES for c in checks_for_capability(cap)})
    rows = rows if rows is not None else _rows(session, all_ids)
    fps = live_fingerprints(session, all_ids)
    now = utcnow()
    project = project_target(session, project_id, domain=domain)
    project_rows = _project_rows(session, sorted({c for r in RULES.values() for c in r.project}), project_id)
    out = []
    for cap, rule in RULES.items():
        st = evaluate(cap, rows, now=now, fps=fps, domain=domain, session=session, project=project,
                      project_rows=project_rows)
        out.append({"capability": cap.value, "label": rule.label, "description": rule.description,
                    "domain": rule.domain, "status": st.status, "reasons": st.reasons, "checks": st.checks,
                    "remedies": st.remedies, "residual_risks": st.residual_risks})
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


def invalidator_label(invalidator: str) -> str:
    """Display form of an invalidator for stored reasons/events. ``secret:<ref>`` is shown as
    ``secret-ref <ref>``: the raw form reads like a ``secret: value`` assignment to the redaction scanner
    (it would be redacted to ``[REDACTED]`` and flagged by ``secret.no_raw_exposure``). Matching always
    uses the raw invalidator."""
    kind, sep, target = invalidator.partition(":")
    if sep and kind.lower() in ("secret", "token", "password", "passwd", "api_key", "apikey"):
        return f"{kind}-ref {target}"
    return invalidator


# Secret namespaces whose reference names are domain metadata (same rule as the platform-level
# ``secret.status`` row): a result that is not scoped to that domain never quotes them (§35, §50).
PRIVATE_SECRET_NAMESPACES: dict[str, str | None] = {"office": "OFFICE", "projects": None}
PRIVATE_LABEL = "secret-ref <ber-scope domain>"
PRIVATE_REASON = "credential ber-scope domain berubah; detail hanya terlihat di domain pemiliknya"


def _private_owner(invalidator: str) -> tuple[bool, str | None]:
    """(domain-private?, owning domain or None) of an invalidator target. Only ``secret:office/...`` and
    ``secret:projects/...`` references are private; ``projects`` refs may belong to either domain."""
    kind, sep, target = invalidator.partition(":")
    if sep and kind.lower() == "secret":
        ns = target.partition("/")[0]
        if ns in PRIVATE_SECRET_NAMESPACES:
            return True, PRIVATE_SECRET_NAMESPACES[ns]
    return False, None


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
    stale_ids: list[str] = []
    components: dict[str, bool] = {}  # component -> may quote the invalidator target
    label = invalidator_label(invalidator)
    why = reason or f"invalidated by {label}"
    kind, _, target = invalidator.partition(":")
    private, owner = _private_owner(invalidator)
    from aeos.doctor.checks import REGISTRY, load_all
    from aeos.doctor.runner import latest_project_results

    load_all()
    candidates = {r.id: r for r in latest_results(session).values()}
    # project-scoped checks keep one latest result per project: every project's row may depend on it
    candidates.update({r.id: r for r in latest_project_results(
        session, [c for c, s in REGISTRY.items() if s.targets is not None]).values()})
    for row in candidates.values():
        cid = row.check_id
        if row.status == "STALE" or not _depends(invalidator, row):
            continue
        # A platform-level row (or another domain's row) is visible to every/that session, and so is its
        # domain-less event: it never names an OFFICE/per-project credential reference (§50 "counts are
        # metadata too"). Matching above still used the raw invalidator.
        row_domain = (row.scope or {}).get("domain")
        quote = not private or (owner is not None and row_domain == owner)
        row_label, row_why = (label, why) if quote else (PRIVATE_LABEL, PRIVATE_REASON)
        row.observed_evidence = {**(row.observed_evidence or {}), "status_before_stale": row.status,
                                 "stale_reason": f"{row_why} [{row_label}]"[:300]}
        row.status = "STALE"
        n += 1
        stale_ids.append(row.check_id)
        if row.component_id:
            components[row.component_id] = components.get(row.component_id, True) and quote
        events.emit(session, source="doctor", kind="doctor.check.stale", entity_type="check", entity_id=cid,
                    domain=row_domain,
                    payload={"check_id": cid, "invalidator": row_label, "invalidator_kind": kind,
                             "invalidator_target": target if quote else None, "reason": row_why[:200]})
    for comp, quote in components.items():
        expire_component_observations(session, comp, reason=f"{label}: {why}" if quote
                                      else f"{PRIVATE_LABEL}: {PRIVATE_REASON}")
    session.flush()
    _schedule_recheck(session, stale_ids)
    return n


# ---------------------------------------------------------------- targeted recheck after invalidation (§08)
#
# "Perubahan ... membuat pemeriksaan terdampak STALE dan memicu recheck sebelum capability tersebut digunakan
# kembali": while the server runs (enabled by the doctor lifecycle hook), the checks an invalidation marked STALE
# are re-run in the background right after the invalidating transaction commits: non-functional, loopback-only
# checks as they are, and the read-only, zero-cost remote identity probes (REMOTE_RECHECK: one GET each, no
# quota, no write) functionally. Until that recheck lands the gate stays UNKNOWN (fail closed). Checks that cost
# quota or run heavy self-tests (Claude capability test, containment) are never re-run implicitly; they keep
# waiting for an explicit --functional run (the gate stays UNKNOWN/STALE meanwhile).

REMOTE_RECHECK: frozenset[str] = frozenset({
    "adapter.github_token", "adapter.project_repo", "adapter.project_drive_root", "adapter.project_office_targets",
    "delivery.branch_protection",
})

class _RecheckState:
    """Process-wide recheck state; every field is read/written under ``_recheck_lock``."""

    def __init__(self) -> None:
        self.enabled = False
        self.settings: Any = None
        self.pending: set[str] = set()
        self.thread: threading.Thread | None = None


_recheck = _RecheckState()
_recheck_lock = threading.Lock()
RECHECK_DELAY_SECONDS = 1.0


def enable_auto_recheck(settings: Any) -> None:
    with _recheck_lock:
        _recheck.enabled = True
        _recheck.settings = settings


def disable_auto_recheck() -> None:
    with _recheck_lock:
        _recheck.enabled = False
        _recheck.settings = None
        _recheck.pending = set()


def auto_recheck_enabled() -> bool:
    return _recheck.enabled


def recheckable(check_ids: list[str]) -> list[str]:
    """Checks re-run as they are (non-functional, loopback-only), excluding the remote identity probes."""
    from aeos.doctor.checks import REGISTRY, load_all

    load_all()
    return [c for c in check_ids if c in REGISTRY and not REGISTRY[c].functional and not REGISTRY[c].network
            and c not in REMOTE_RECHECK]


def remote_recheckable(check_ids: list[str]) -> list[str]:
    """Read-only, zero-cost remote probes re-run functionally after an invalidation."""
    from aeos.doctor.checks import REGISTRY, load_all

    load_all()
    return [c for c in check_ids if c in REGISTRY and c in REMOTE_RECHECK and not REGISTRY[c].functional]


def _schedule_recheck(session: Session, stale_ids: list[str]) -> None:
    skip = session.info.get("aeos_doctor_recheck_skip") or set()  # checks the current Doctor run records itself
    ids = [c for c in [*recheckable(stale_ids), *remote_recheckable(stale_ids)] if c not in skip]
    if not ids or not auto_recheck_enabled():
        return
    from sqlalchemy import event

    pending = session.info.setdefault("aeos_doctor_recheck", set())
    first = not pending
    pending.update(ids)
    if first:
        def _after_commit(sess: Session) -> None:
            queued = sess.info.pop("aeos_doctor_recheck", set())
            if queued:
                request_recheck(sorted(queued))

        event.listen(session, "after_commit", _after_commit, once=True)


def request_recheck(check_ids: list[str]) -> None:
    """Queue a background recheck of *check_ids* (debounced; one worker thread at a time)."""
    with _recheck_lock:
        if not _recheck.enabled:
            return
        _recheck.pending |= set(check_ids)
        t = _recheck.thread
        if t is not None and t.is_alive():
            return
        t = threading.Thread(target=_recheck_worker, name="aeos-doctor-recheck", daemon=True)
        _recheck.thread = t
        t.start()


def _recheck_worker() -> None:
    import logging
    import time

    log = logging.getLogger("aeos.doctor")
    while True:
        time.sleep(RECHECK_DELAY_SECONDS)
        with _recheck_lock:
            ids = sorted(_recheck.pending)
            _recheck.pending = set()
            settings = _recheck.settings
            if not ids or not _recheck.enabled or settings is None:
                _recheck.thread = None
                return
        try:
            from aeos.core.db import session_scope
            from aeos.doctor.runner import run_doctor

            local, remote = recheckable(ids), remote_recheckable(ids)
            if local:
                with session_scope() as s:
                    run_doctor(settings, s, checks=local, functional=False, actor="system", trigger="recheck")
            if remote:
                with session_scope() as s:
                    run_doctor(settings, s, checks=remote, functional=True, actor="system", trigger="recheck")
        except Exception:  # visible as the still-STALE result; never fatal
            log.exception("targeted doctor recheck failed")
