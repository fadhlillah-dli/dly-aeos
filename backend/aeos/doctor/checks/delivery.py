"""§08 Delivery: provider entitlements, branch/merge/deploy restrictions, status-check identity, trusted CI.

Branch protection on a private GitHub Free repository may be unavailable (S22, §31); when the probe
returns 403/404 the platform records "not enforceable" as a residual risk instead of claiming that
required checks block merges. Human-only merge is kept by the broker having no merge operation.
"""

from __future__ import annotations

import re
from typing import Any

from aeos.doctor.checks import check
from aeos.doctor.checks._common import file_fingerprint, secret_ref_status, set_secret_remedy
from aeos.doctor.checks.adapters import GITHUB_REF, github_get
from aeos.doctor.model import CheckContext, Outcome, Remedy
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


@check("delivery.branch_protection", "DELIVERY", "Ketersediaan branch protection (read-only)", component_id="github",
       ttl_seconds=86400, invalidators=(f"secret:{GITHUB_REF}", "target"), functional=True, network=True,
       severity_fail="HIGH", scope={"domain": "PERSONAL"})
def delivery_branch_protection(ctx: CheckContext) -> Outcome:
    slug = ctx.settings.platform_repo_slug
    state, evidence = secret_ref_status(GITHUB_REF)
    evidence.update({"target": f"github:{slug}", "source": f"S22: {PROTECTED_DOCS}"})
    if state != "PRESENT":
        return Outcome("UNKNOWN", "Token GitHub tidak tersedia; ketersediaan branch protection tidak diketahui.",
                       evidence={**evidence, "residual_risk": RESIDUAL_RISK}, error_code="GITHUB_TOKEN_UNAVAILABLE",
                       remedy=set_secret_remedy(GITHUB_REF, "GitHub (read-only metadata)") if state == "ABSENT" else None)
    try:
        repo = github_get(ctx, f"/repos/{slug}")
        branch = (repo.body or {}).get("default_branch") if repo.status == 200 and isinstance(repo.body, dict) else "main"
        hr = github_get(ctx, f"/repos/{slug}/branches/{branch}/protection")
    except Exception as exc:
        return Outcome("UNKNOWN", f"Probe branch protection gagal ({type(exc).__name__}).", evidence=evidence,
                       error_code="BRANCH_PROTECTION_PROBE_ERROR")
    message = str((hr.body or {}).get("message") or "") if isinstance(hr.body, dict) else ""
    evidence.update({"branch": branch, "http_status": hr.status, "provider_message": message[:120]})
    if hr.status == 200 and isinstance(hr.body, dict):
        checks = ((hr.body.get("required_status_checks") or {}).get("contexts")) or []
        reviews = bool(hr.body.get("required_pull_request_reviews"))
        evidence.update({"required_status_checks": checks, "required_reviews": reviews,
                         "enforce_admins": (hr.body.get("enforce_admins") or {}).get("enabled")})
        return Outcome("VERIFIED", f"Branch protection aktif pada {branch} (required checks: {len(checks)}, review: "
                       f"{'ya' if reviews else 'tidak'}).", evidence=evidence, target_identity=f"github:{slug}@{branch}")
    if hr.status == 404 and re.search(r"not protected", message, re.I):
        return Outcome("MISSING", f"Branch {branch} belum diproteksi; dapat diaktifkan bila paket akun mendukung.",
                       evidence={**evidence, "residual_risk": RESIDUAL_RISK}, error_code="BRANCH_NOT_PROTECTED",
                       remedy=Remedy(operation="Aktifkan branch protection / ruleset untuk main (oleh pemilik repo)",
                                     source=f"S22: {PROTECTED_DOCS}", target=f"github:{slug}@{branch}",
                                     privilege="admin repo (pemilik)", cost="sesuai paket GitHub",
                                     rollback="nonaktifkan rule", evidence_to_collect="delivery.branch_protection VERIFIED"))
    if hr.status in (403, 404):
        return Outcome("MISSING", "Branch protection tidak dapat ditegakkan pada paket/repo ini (403/404 private Free); "
                       "dicatat sebagai residual risk.", evidence={**evidence, "residual_risk": RESIDUAL_RISK,
                                                                   "enforceable": False},
                       error_code="BRANCH_PROTECTION_NOT_ENFORCEABLE", severity="HIGH")
    return Outcome("UNKNOWN", f"Respons tidak konklusif ({hr.status or hr.error}).", evidence=evidence,
                   error_code="BRANCH_PROTECTION_INCONCLUSIVE")


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
