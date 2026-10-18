"""§08 Delivery: provider entitlements, branch/merge/deploy restrictions, status-check identity, trusted CI.

Branch protection on a private GitHub Free repository may be unavailable (S22, §31); when the probe
returns 403/404 the platform records "not enforceable" as a residual risk instead of claiming that
required checks block merges. Human-only merge is kept by the broker having no merge operation.
"""

from __future__ import annotations

from typing import Any

from aeos.doctor.checks import check
from aeos.doctor.checks._common import file_fingerprint, secret_ref_status, set_secret_remedy
from aeos.doctor.checks.adapters import GITHUB_REF, github_token
from aeos.doctor.model import CheckContext, Outcome, Remedy, aeos_cmd
from aeos.inventory import scope as scope_reader

PROTECTED_DOCS = ("https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/"
                  "managing-protected-branches/about-protected-branches")
RESIDUAL_RISK = ("Merge ke main tidak ditegakkan provider; human-only merge bergantung pada broker tanpa operasi merge "
                 "dan owner credential di luar runtime agent (§31).")


@check("delivery.ci_workflow", "DELIVERY", "Workflow CI tepercaya (action dipin SHA)", component_id="github-actions",
       ttl_seconds=86400, severity_fail="HIGH", fingerprint=file_fingerprint(".github/workflows/ci.yml"),
       scope={"domain": "PERSONAL"})
def delivery_ci_workflow(ctx: CheckContext) -> Outcome:
    read, actions = scope_reader.read_workflows(ctx.repo)
    if read.status != "READ":
        return Outcome("MISSING", "Tidak ada workflow CI di .github/workflows.", error_code="NO_CI_WORKFLOW")
    unpinned = sorted({a["uses"] + "@" + str(a["ref"]) for a in actions if not a["pinned_sha"]})
    evidence: dict[str, Any] = {"files": (read.detail or {}).get("files"), "actions": len(actions), "unpinned": unpinned,
                                "evidence_level": "E1 (statis)"}
    if unpinned:
        return Outcome("FAILED", f"{len(unpinned)} action tidak dipin ke commit SHA: {', '.join(unpinned)}.",
                       evidence=evidence, error_code="UNPINNED_ACTIONS", remedy=Remedy(
                           operation="Pin action ke full commit SHA lewat PR (protected path)",
                           target=".github/workflows", privilege="reviewer manusia",
                           evidence_to_collect="delivery.ci_workflow VERIFIED"))
    return Outcome("VERIFIED", f"{len(actions)} action di workflow CI dipin ke commit SHA (bukti statis E1).",
                   evidence=evidence)


def _capability_view(caps: dict[str, Any]) -> dict[str, Any]:
    keep = ("repo", "branch", "checked_at", "credential", "visibility", "plan", "merge_gate", "enforcement_tested",
            "required_status_checks", "residual_risks", "source", "mode")
    return {k: caps.get(k) for k in keep if k in caps}


def _merge_gate_outcome(caps: dict[str, Any], *, origin: str) -> Outcome:
    """Map ``aeos.delivery.provider.ProviderCapabilities`` onto a CheckResult. A CONFIGURED provider rule is
    still not proven enforcement (bypass tests NOT-PROVEN); NOT_ENFORCEABLE is a residual risk, never a pass."""
    from aeos.delivery.provider import ENFORCEMENT_UNTESTED, NOT_CONFIGURED_MESSAGE, NOT_ENFORCEABLE_MESSAGE

    view = _capability_view(caps)
    gate = str(caps.get("merge_gate") or "UNKNOWN").upper()
    risks = [str(r) for r in caps.get("residual_risks") or []]
    if gate == "NOT_ENFORCEABLE" and NOT_ENFORCEABLE_MESSAGE not in risks:
        risks.append(NOT_ENFORCEABLE_MESSAGE)
    if gate == "NOT_CONFIGURED" and NOT_CONFIGURED_MESSAGE not in risks:
        risks.append(NOT_CONFIGURED_MESSAGE)
    if ENFORCEMENT_UNTESTED not in risks:
        risks.append(ENFORCEMENT_UNTESTED)
    if RESIDUAL_RISK not in risks and gate != "CONFIGURED":
        risks.append(RESIDUAL_RISK)
    evidence = {**view, "residual_risks": risks, "origin": origin, "enforceable": gate == "CONFIGURED"}
    target = f"github:{caps.get('repo')}@{caps.get('branch')}"
    if gate == "CONFIGURED":
        return Outcome("CONFIGURED", f"Merge gate provider terkonfigurasi pada {caps.get('branch')} (required checks: "
                       f"{len(caps.get('required_status_checks') or [])}); penegakan NOT-PROVEN sampai uji bypass.",
                       evidence=evidence, target_identity=target)
    if gate == "NOT_ENFORCEABLE":
        return Outcome("MISSING", "Branch protection tidak dapat ditegakkan pada paket/repo ini (residual risk, bukan "
                       "penegakan): merge tetap tindakan manusia.", evidence=evidence, target_identity=target,
                       error_code="BRANCH_PROTECTION_NOT_ENFORCEABLE", severity="HIGH")
    if gate == "NOT_CONFIGURED":
        return Outcome("MISSING", f"Branch protection/ruleset tersedia tetapi belum dikonfigurasi untuk "
                       f"{caps.get('branch')}.", evidence=evidence, target_identity=target, error_code="BRANCH_NOT_PROTECTED",
                       remedy=Remedy(operation="Aktifkan branch protection / ruleset untuk branch default (oleh pemilik "
                                     "repo, di GitHub)", source=f"S22: {PROTECTED_DOCS}", target=target,
                                     privilege="admin repo (pemilik)", cost="sesuai paket GitHub",
                                     rollback="nonaktifkan rule", evidence_to_collect="delivery.branch_protection CONFIGURED"))
    return Outcome("UNKNOWN", f"Kemampuan merge gate provider UNKNOWN ({caps.get('credential') or 'tanpa detail'}); "
                   "merge tetap tindakan manusia.", evidence=evidence, target_identity=target,
                   error_code="PROVIDER_CAPABILITY_UNKNOWN")


def _latest_capability_evidence(ctx: CheckContext, slug: str) -> Any:
    from sqlalchemy import or_, select

    from aeos.models.evidence import EvidenceArtifact

    assert ctx.session is not None
    return ctx.session.scalar(select(EvidenceArtifact).where(
        EvidenceArtifact.kind == "PROVIDER_CAPABILITY", EvidenceArtifact.subject_id == slug,
        EvidenceArtifact.trusted.is_(True), EvidenceArtifact.mode == "LIVE",
        or_(EvidenceArtifact.domain.is_(None), EvidenceArtifact.domain != "OFFICE"))
        .order_by(EvidenceArtifact.created_at.desc()).limit(1))


@check("delivery.branch_protection", "DELIVERY", "Merge gate provider / branch protection (read-only)", component_id="github",
       ttl_seconds=86400, invalidators=(f"secret:{GITHUB_REF}", "target", "repo:origin"), severity_fail="HIGH",
       scope={"domain": "PERSONAL"}, ok={"CONFIGURED", "VERIFIED"}, network=True,
       description="Tanpa --functional: bukti PROVIDER_CAPABILITY terakhir (aeos delivery capabilities --record). "
                   "Dengan --functional: aeos.delivery.provider.check_capabilities (GET saja, jaringan).")
def delivery_branch_protection(ctx: CheckContext) -> Outcome:
    slug = ctx.settings.platform_repo_slug
    if not ctx.functional:
        if ctx.session is None:
            return Outcome("UNKNOWN", "Database belum ada; bukti kemampuan provider tidak dapat dibaca.",
                           error_code="DB_NOT_INITIALISED", evidence={"residual_risks": [RESIDUAL_RISK]})
        ev = _latest_capability_evidence(ctx, slug)
        if ev is None:
            return Outcome("UNKNOWN", f"Kemampuan merge gate github:{slug} belum pernah diprobe (NOT-PROVEN); merge tetap "
                           "tindakan manusia.", evidence={"target": f"github:{slug}", "residual_risks": [RESIDUAL_RISK]},
                           error_code="PROVIDER_CAPABILITY_NOT_CHECKED", remedy=Remedy(
                               operation="Probe read-only kemampuan provider dan simpan sebagai bukti",
                               command=aeos_cmd("delivery capabilities --record"), source=f"S22: {PROTECTED_DOCS}",
                               target=f"github:{slug}", privilege="pengguna lokal (token read-only)",
                               cost="tidak ada (GET saja)",
                               evidence_to_collect="EvidenceArtifact PROVIDER_CAPABILITY"))
        inline = ev.inline or {}
        caps = inline.get("content") if isinstance(inline.get("content"), dict) else (inline.get("summary") or {})
        out = _merge_gate_outcome(caps, origin=f"evidence {ev.id[:8]} ({ev.created_at.isoformat()})")
        out.evidence["evidence_id"] = ev.id
        return out
    state, evidence = secret_ref_status(GITHUB_REF)
    evidence.update({"target": f"github:{slug}", "source": f"S22: {PROTECTED_DOCS}"})
    if state != "PRESENT":
        return Outcome("UNKNOWN", "Token GitHub tidak tersedia; kemampuan merge gate tidak diketahui (tidak ada panggilan).",
                       evidence={**evidence, "residual_risks": [RESIDUAL_RISK]}, error_code="GITHUB_TOKEN_UNAVAILABLE",
                       remedy=set_secret_remedy(GITHUB_REF, "GitHub (read-only metadata)") if state == "ABSENT" else None)
    try:
        from aeos.delivery import provider

        caps = provider.check_capabilities(slug, token=github_token(), credential="AVAILABLE").to_dict()
    except Exception as exc:  # store/network/validation failure: never green
        return Outcome("UNKNOWN", f"Probe kemampuan provider gagal ({type(exc).__name__}).",
                       evidence={**evidence, "residual_risks": [RESIDUAL_RISK]}, error_code="PROVIDER_PROBE_ERROR")
    return _merge_gate_outcome(caps, origin="aeos.delivery.provider.check_capabilities (live, GET only)")


@check("delivery.staging_target", "DELIVERY", "Target staging terkonfigurasi", component_id="delivery-ci",
       ttl_seconds=86400, needs_db=True, ok={"CONFIGURED", "VERIFIED"}, severity_fail="LOW")
def delivery_staging_target(ctx: CheckContext) -> Outcome:
    from sqlalchemy import select

    from aeos.models.portfolio import ProjectIntegration

    assert ctx.session is not None
    rows = list(ctx.session.scalars(select(ProjectIntegration).where(ProjectIntegration.purpose == "staging")))
    if not rows:
        return Outcome("MISSING", "Belum ada target staging yang dikonfigurasi; validasi staging tidak tersedia.",
                       error_code="NO_STAGING_TARGET", severity="LOW")
    return Outcome("CONFIGURED", f"{len(rows)} target staging tercatat (deploy staging tetap butuh otorisasi).",
                   evidence={"targets": [f"{r.provider}:{r.domain}" for r in rows]})
