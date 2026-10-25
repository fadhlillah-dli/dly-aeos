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
    observed = _provider_ci_observation(ctx)
    evidence["provider_ci"] = observed
    note = (f" CI provider teramati untuk {str(observed.get('commit'))[:12]}: {observed.get('overall')} (E3)."
            if observed.get("status") == "OBSERVED" else " Belum ada observasi CI provider (provider-ci) yang tercatat.")
    return Outcome("VERIFIED", f"{len(actions)} action di workflow CI dipin ke commit SHA (bukti statis E1).{note}",
                   evidence=evidence)


def _provider_ci_observation(ctx: CheckContext) -> dict[str, Any]:
    """The newest trusted provider-ci evidence of a PERSONAL project (what the provider actually ran), shown next
    to the static pin check so "trusted CI" is not only a statement about the workflow file."""
    if ctx.session is None:
        return {"status": "UNKNOWN", "reason": "database not available"}
    try:
        from sqlalchemy import select

        from aeos.models.evidence import EvidenceArtifact
        from aeos.verification import facade as collector

        ev = ctx.session.scalar(select(EvidenceArtifact).where(
            EvidenceArtifact.kind == "CI_STATUS", EvidenceArtifact.origin == "provider-ci",
            EvidenceArtifact.trusted.is_(True), EvidenceArtifact.domain == "PERSONAL", EvidenceArtifact.mode == "LIVE")
            .order_by(EvidenceArtifact.created_at.desc()).limit(1))
        if ev is None:
            return {"status": "NOT_OBSERVED"}
        data = collector.read_json(ev) or {}
        return {"status": "OBSERVED", "evidence_id": ev.id, "commit": ev.subject_id,
                "overall": data.get("overall"), "gate_mapping": data.get("gate_mapping"),
                "checked_at": ev.created_at.isoformat() if ev.created_at else None}
    except Exception as exc:  # never green because of this informational read
        return {"status": "UNKNOWN", "reason": type(exc).__name__}


def _capability_view(caps: dict[str, Any]) -> dict[str, Any]:
    keep = ("provider", "repo", "branch", "checked_at", "credential", "visibility", "plan", "merge_gate",
            "enforcement_tested", "enforcement_test", "required_status_checks", "status_check_identity",
            "deploy_restrictions",
            "entitlement", "residual_risks", "source", "mode")
    return {k: caps.get(k) for k in keep if k in caps}


IDENTITY_RISK = ("Required status check tidak terikat ke identitas pelapor tertentu (app/integrasi): status dapat "
                 "dilaporkan oleh akun lain dengan izin menulis status - check tidak membuktikan CI tepercaya.")
DEPLOY_RISK = ("Pembatasan deploy/environment provider belum terkonfigurasi atau tidak diketahui; AEOS tidak memiliki "
               "operasi deploy dan produksi tetap tindakan manusia.")


def _restrictions(caps: dict[str, Any], risks: list[str]) -> dict[str, str]:
    """§08 "branch/merge/deploy restrictions, status-check identity": evaluated, not only passed through.
    An unbound status check or missing deploy restriction is a residual risk on every outcome (never a pass)."""
    identity = str(caps.get("status_check_identity") or "UNKNOWN").upper()
    deploy = str(caps.get("deploy_restrictions") or "UNKNOWN").upper()
    if (identity in ("NOT_CONFIGURED", "UNKNOWN") and caps.get("required_status_checks")
            and not any("tidak terikat" in r for r in risks)):
        risks.append(IDENTITY_RISK)
    if deploy != "CONFIGURED" and not any("deploy" in r.lower() for r in risks):
        risks.append(DEPLOY_RISK)
    return {"status_check_identity": identity, "deploy_restrictions": deploy,
            "entitlement": str(caps.get("entitlement") or "UNKNOWN").upper()}


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
    # VERIFIED only from a provider refusal bound to this repo/branch and the still-unchanged active ruleset
    # (aeos.delivery.provider.apply_enforcement_evidence); CLAIMED (owner attestation) or STALE never count.
    tested = str(caps.get("enforcement_tested") or "NOT-PROVEN").upper()
    raw_test = caps.get("enforcement_test")
    test: dict[str, Any] = raw_test if isinstance(raw_test, dict) else {}
    proven = gate == "CONFIGURED" and tested == "VERIFIED" and bool(test.get("evidence_id"))
    if proven:
        risks = [r for r in risks if r != ENFORCEMENT_UNTESTED]
    elif ENFORCEMENT_UNTESTED not in risks:
        risks.append(ENFORCEMENT_UNTESTED)
    if RESIDUAL_RISK not in risks and gate != "CONFIGURED":
        risks.append(RESIDUAL_RISK)
    restrictions = _restrictions(caps, risks)
    evidence = {**view, "residual_risks": risks, "origin": origin, "enforceable": gate == "CONFIGURED",
                "restrictions": restrictions, "enforcement_test": test}
    target = f"{str(caps.get('provider') or 'github').lower()}:{caps.get('repo')}@{caps.get('branch')}"
    if proven:
        rulesets = ", ".join(str(r.get("id")) for r in test.get("rulesets") or [])
        return Outcome("VERIFIED", f"Merge gate provider pada {caps.get('branch')} terbukti menolak perubahan langsung: "
                       f"penolakan oleh ruleset aktif {rulesets} teramati {test.get('observed_at')} (bukti "
                       f"{str(test.get('evidence_id'))[:8]}); berlaku selama ruleset itu tidak berubah.",
                       evidence=evidence, target_identity=target)
    if gate == "CONFIGURED":
        return Outcome("CONFIGURED", f"Merge gate provider terkonfigurasi pada {caps.get('branch')} (required checks: "
                       f"{len(caps.get('required_status_checks') or [])}; identitas pelapor: "
                       f"{restrictions['status_check_identity']}; pembatasan deploy: "
                       f"{restrictions['deploy_restrictions']}; entitlement: {restrictions['entitlement']}); "
                       "penegakan NOT-PROVEN sampai uji bypass.",
                       evidence=evidence, target_identity=target)
    if gate == "NOT_ENFORCEABLE":
        return Outcome("MISSING", "Branch protection tidak dapat ditegakkan pada paket/repo ini (entitlement: "
                       f"{restrictions['entitlement']}; residual risk, bukan penegakan): merge tetap tindakan manusia.",
                       evidence=evidence, target_identity=target,
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
       ttl_seconds=86400, invalidators=(f"secret:{GITHUB_REF}", "integration:*", "repo:origin"), severity_fail="HIGH",
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
        raw = inline.get("content") if isinstance(inline.get("content"), dict) else inline.get("summary")
        caps: dict[str, Any] = raw if isinstance(raw, dict) else {}
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

        live = provider.check_capabilities(slug, token=github_token(), credential="AVAILABLE")
        if ctx.session is not None:
            live = provider.apply_enforcement_evidence(ctx.session, live)
        caps = live.to_dict()
    except Exception as exc:  # store/network/validation failure: never green
        return Outcome("UNKNOWN", f"Probe kemampuan provider gagal ({type(exc).__name__}).",
                       evidence={**evidence, "residual_risks": [RESIDUAL_RISK]}, error_code="PROVIDER_PROBE_ERROR")
    return _merge_gate_outcome(caps, origin="aeos.delivery.provider.check_capabilities (live, GET only)")


def staging_targets(ctx: CheckContext) -> list[dict[str, Any]]:
    """One target per (non-retired) project: its staging registrations, split into the observable
    ``deployment-target`` (the rule of aeos.delivery.deployment.target_for, which gate G6 uses) and the
    ``local-target`` identities that cannot be observed with a GET."""
    from sqlalchemy import select

    from aeos.doctor.checks.adapters import _projects
    from aeos.models.portfolio import ProjectIntegration

    assert ctx.session is not None
    projects = _projects(ctx)
    rows: dict[str, list[Any]] = {}
    for integ in ctx.session.scalars(select(ProjectIntegration).where(ProjectIntegration.purpose == "staging")
                                     .order_by(ProjectIntegration.created_at.desc())):
        rows.setdefault(integ.project_id, []).append(integ)
    out = []
    for pid, p in sorted(projects.items()):
        mine = rows.get(pid, [])
        out.append({"project_id": pid, "domain": p.domain, "project_key": p.project_key,
                    "targets": [{"integration_id": r.id, "root_id": r.root_id} for r in mine
                                if r.provider == "deployment-target" and r.root_id][:1],
                    "local_targets": [{"integration_id": r.id, "root_id": r.root_id} for r in mine
                                      if r.provider == "local-target"],
                    "invalidators": [f"integration:{r.id}" for r in mine]})
    return out


@check("delivery.staging_target", "DELIVERY", "Target staging proyek terdaftar dan dapat diobservasi (per proyek)",
       component_id="delivery-ci", ttl_seconds=86400, needs_db=True, ok={"CONFIGURED", "VERIFIED"},
       severity_fail="LOW", invalidators=("integration:*",), targets=staging_targets,
       description="Per proyek, aturan yang sama dengan gate G6 (aeos.delivery.deployment.target_for): hanya integrasi "
                   "deployment-target purpose=staging dengan URL dasar yang dapat diobservasi; local-target dicatat "
                   "tetapi tidak dapat divalidasi lewat GET.")
def delivery_staging_target(ctx: CheckContext) -> Outcome:
    target = ctx.project or {}
    pid = str(target.get("project_id") or "")
    evidence: dict[str, Any] = {"project_key": target.get("project_key"), "targets": target.get("targets") or [],
                                "local_targets": target.get("local_targets") or []}
    remedy = Remedy(operation="Daftarkan target staging observe-only (URL dasar) untuk proyek ini",
                    command=f"PUT /api/projects/{pid or '<project_id>'}/integrations provider=deployment-target "
                            "purpose=staging root_id=https://<staging host>",
                    target=f"project:{pid or '<project_id>'}", privilege="SUPER_ADMIN",
                    evidence_to_collect="delivery.staging_target CONFIGURED untuk proyek ini")
    if not pid:
        return Outcome("UNKNOWN", "Target staging dievaluasi per proyek; tidak ada proyek yang dipilih.",
                       error_code="PROJECT_REQUIRED")
    if target.get("domain") == "OFFICE":
        return Outcome("MISSING", "OFFICE belum memiliki target environment (M5, otorisasi perusahaan); validasi "
                       "staging tidak tersedia.", evidence=evidence, error_code="OFFICE_DEFAULT_DENY", severity="LOW")
    targets = evidence["targets"]
    if targets:
        return Outcome("CONFIGURED", f"Target staging deployment-target terdaftar ({targets[0]['root_id']}); hanya "
                       "diobservasi dengan GET (deploy staging tetap tindakan manusia berizin).", evidence=evidence,
                       target_identity=f"deployment-target:{targets[0]['root_id']}")
    if evidence["local_targets"]:
        return Outcome("MISSING", "Target staging lokal (local-target "
                       + ", ".join(str(t["root_id"]) for t in evidence["local_targets"])
                       + ") tercatat tetapi tidak dapat diobservasi lewat GET; daftarkan deployment-target dengan URL.",
                       evidence=evidence, error_code="STAGING_TARGET_NOT_OBSERVABLE", severity="LOW", remedy=remedy)
    return Outcome("MISSING", "Belum ada target staging yang terdaftar untuk proyek ini; validasi staging tidak "
                   "tersedia.", evidence=evidence, error_code="NO_STAGING_TARGET", severity="LOW", remedy=remedy)
