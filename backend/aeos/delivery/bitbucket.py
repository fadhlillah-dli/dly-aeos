"""Bitbucket Cloud delivery for OFFICE projects - the **read** side (§04, §30, §31, §45; ADR-0111).

OFFICE code lives in the selected Bitbucket repository and its CI is Bitbucket Pipelines (or an approved
office CI that reports Bitbucket commit statuses). This module only observes the provider (GET):

* ``office_delivery_readiness`` - disabled by default. Delivery is AVAILABLE only with an ACTIVE company
  authorisation that lists ``bitbucket`` (POLICY decision, §23), an enabled ``bitbucket/code`` integration
  (ADR-0073), a verified Bitbucket **Cloud** deployment (Data Center -> NOT_SUPPORTED, §04), the stored
  ``office/bitbucket`` credential and the registered repository whose exact identity was verified.
* ``fetch_ci_status`` - commit statuses + Pipelines steps of one exact commit -> ``CIStatus`` that
  ``provider.record_ci_evidence`` stores as ``provider-ci`` evidence for the gates (G1/G3/G5).
* ``check_capabilities`` - branch restrictions and merge checks. Bitbucket only *blocks* a merge on failed
  checks with ``enforce_merge_checks`` (a Premium feature, S23): without it the merge gate is
  NOT_ENFORCEABLE, never "required checks block merge" (§31).
* ``fetch_pull`` / ``fetch_commit_files`` - PR state, full head/base/merge-commit hashes and ``closed_by``
  for the PR observation (sync).

Writes (sha-pinned ``aeos/*`` push, PR creation) exist only in the trusted broker. There is no merge,
decline or deploy call anywhere (U-09). Exercised with mocked HTTP (respx) only: live tenant behaviour is
NOT-PROVEN until the owner obtains the company authorisation and a tenant (M5).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.errors import AeosError, ValidationFailed
from aeos.core.redaction import redact_text
from aeos.core.timeutil import utcnow
from aeos.delivery.provider import (
    ENFORCEMENT_UNTESTED,
    CapabilityCheck,
    CheckRunView,
    CIStatus,
    ProviderCapabilities,
    classify_check,
    parse_check_runs_summary,
)
from aeos.models.portfolio import Project, ProjectIntegration, RepositoryReference

PROVIDER = "bitbucket"
CI_PROVIDER = "bitbucket-pipelines"
SECRET_REF = "office/bitbucket"
READ_USE = "delivery.provider-read"
WEB = "https://bitbucket.org"
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,61}/[A-Za-z0-9._-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
LIVE_PROOF = ("NOT-PROVEN: adapter Bitbucket Cloud hanya diuji dengan HTTP tiruan (respx); bukti live butuh "
              "otorisasi perusahaan dan tenant (M5)")
MAX_PIPELINES = 10
NOT_ENFORCEABLE_MESSAGE = ("Merge checks Bitbucket hanya 'recommended' tanpa 'enforce merge checks' (fitur "
                           "Premium) - tidak memblokir merge; merge tetap tindakan manusia")
DISABLED_REMEDY = ("owner: otorisasi perusahaan (keputusan POLICY office_policy yang mencantumkan bitbucket dan "
                   "atlassian_deployment=CLOUD), integrasi bitbucket/code dengan config.enabled=true, repository "
                   "terverifikasi, credential office/bitbucket (M5)")


def validate_repo(full_name: str) -> str:
    if not isinstance(full_name, str) or not REPO_RE.match(full_name) or ".." in full_name:
        raise ValidationFailed("Bitbucket repository must be 'workspace/repo_slug'", code="REPO_INVALID")
    return full_name


# ---------------------------------------------------------------------------------------------- readiness


@dataclass
class OfficeDeliveryReadiness:
    status: str  # AVAILABLE | DISABLED | NOT_SUPPORTED | BLOCKED
    reasons: list[str]
    remedy: str
    deployment: str
    repo: str | None = None
    default_branch: str | None = None
    authorization_decision_id: str | None = None
    integration_id: str | None = None
    ci_integration_id: str | None = None
    provider: str = PROVIDER
    ci: str = CI_PROVIDER
    live_proof: str = LIVE_PROOF

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _code_integration(session: Session, project: Project, purpose: str, provider: str) -> ProjectIntegration | None:
    return session.scalar(select(ProjectIntegration).where(
        ProjectIntegration.project_id == project.id, ProjectIntegration.provider == provider,
        ProjectIntegration.purpose == purpose).order_by(ProjectIntegration.created_at.desc()).limit(1))


def office_delivery_readiness(session: Session, project: Project) -> OfficeDeliveryReadiness:
    """Whether OFFICE delivery through Bitbucket Cloud may run for *project* (no network, fail closed)."""
    if project.domain != "OFFICE":
        return OfficeDeliveryReadiness("NOT_APPLICABLE", ["PERSONAL projects deliver through GitHub"], "", "")
    reasons: list[str] = []
    try:
        from aeos.policy import facade as policy

        pol = policy.office_policy(session)
    except ImportError:
        pol = {}
        reasons.append("policy module unavailable: company authorisation cannot be checked (fail closed)")
    authorised = pol.get("office_authorization") is True and PROVIDER in (pol.get("authorized_providers") or [])
    if not authorised:
        reasons.append("tidak ada otorisasi perusahaan ACTIVE yang mencantumkan bitbucket (OFFICE default deny, §23)")
    integ = _code_integration(session, project, "code", PROVIDER)
    cfg = dict(integ.config or {}) if integ is not None else {}
    enabled = integ is not None and cfg.get("enabled") is True
    if not enabled:
        reasons.append("adapter Bitbucket nonaktif untuk proyek ini (integrasi bitbucket/code dengan "
                       "config.enabled=true diperlukan, ADR-0073)")
    deployment = str(cfg.get("deployment") or "").upper()
    policy_deployment = str(pol.get("atlassian_deployment") or "").upper()
    dc = "DATA_CENTER" in (deployment, policy_deployment)
    effective = "DATA_CENTER" if dc else (deployment or policy_deployment or "UNKNOWN")
    if dc:
        reasons.append("Bitbucket Data Center NOT SUPPORTED: adapter ini hanya untuk Bitbucket Cloud (§04 - Data "
                       "Center butuh adapter dan capability check tersendiri)")
    elif effective != "CLOUD":
        reasons.append("deployment Bitbucket (Cloud vs Data Center) belum diverifikasi; catat "
                       "office_policy.atlassian_deployment=CLOUD pada keputusan otorisasi perusahaan setelah tenant "
                       "diperiksa")
    repos = [r for r in session.scalars(select(RepositoryReference).where(
        RepositoryReference.project_id == project.id)) if (r.provider or "").upper() == "BITBUCKET"]
    wanted = str(integ.root_id or "").strip() if integ is not None else ""
    # the exact target is the integration's root (BITBUCKET_REPOSITORY) AND a registered repository: never guessed
    repo = next((r for r in repos if wanted and r.full_name.lower() == wanted.lower()), None)
    if repo is None:
        reasons.append("repository Bitbucket proyek tidak terdaftar atau tidak sama dengan root integrasi "
                       f"({wanted or 'kosong'}) - target tidak pernah ditebak")
    elif repo.verification_status != "VERIFIED":
        reasons.append(f"identitas repository {repo.full_name} belum VERIFIED lewat Bitbucket API "
                       "(POST /api/repositories/<id>/verify)")
    if authorised and wanted:
        # §01 U-03 / §07 step 10: the exact repository must be a company-approved target (approved_targets)
        try:
            from aeos.policy import facade as policy

            ok, why = policy.office_target_authorised(session, PROVIDER, root_id=wanted, policy=pol)
        except ImportError:
            ok, why = False, "policy module unavailable (fail closed)"
        if not ok:
            reasons.append(f"repository {wanted} bukan target yang disetujui perusahaan: {why}")
    try:
        from aeos.secrets import facade as secrets

        if not secrets.secret_status(SECRET_REF).exists:
            reasons.append(f"credential {SECRET_REF} belum disimpan")
    except (ImportError, AeosError, ValueError):
        reasons.append(f"status credential {SECRET_REF} tidak dapat dibaca (fail closed)")
    ci = _code_integration(session, project, "ci", CI_PROVIDER)
    if dc:
        status = "NOT_SUPPORTED"
    elif not (authorised and enabled):
        status = "DISABLED"
    else:
        status = "BLOCKED" if reasons else "AVAILABLE"
    return OfficeDeliveryReadiness(
        status=status, reasons=reasons, remedy="" if status == "AVAILABLE" else DISABLED_REMEDY, deployment=effective,
        repo=repo.full_name if repo is not None else None,
        default_branch=(repo.default_branch if repo is not None else None),
        authorization_decision_id=pol.get("authorization_decision_id"), integration_id=integ.id if integ else None,
        ci_integration_id=ci.id if ci is not None else None)


# ---------------------------------------------------------------------------------------------- credential


def read_auth(domain: str) -> tuple[str | None, str]:
    """(API Authorization value, credential status) for an OFFICE read. Never returned to UI/logs."""
    if domain != "OFFICE":
        return None, "NOT_APPLICABLE: Bitbucket is the OFFICE code host"
    try:
        from aeos.publisher import facade as publisher
    except ImportError:
        return None, "UNAVAILABLE: publisher adapters not available"
    try:
        return publisher.bitbucket_read_auth(use=READ_USE, actor="delivery"), "AVAILABLE"
    except publisher.ProviderError as exc:
        return None, f"INVALID: {getattr(exc, 'code', type(exc).__name__)}"
    except AeosError as exc:
        return None, f"MISSING: {exc.code}"


def _reader(repo: str, auth_header: str, client: httpx.Client | None) -> Any:
    from aeos.publisher import facade as publisher

    return publisher.bitbucket_reader(repo, auth_header=auth_header, client=client)


def _error_text(exc: Exception) -> str:
    return redact_text(str(getattr(exc, "message", "") or exc))[:300]


# ---------------------------------------------------------------------------------------------- capabilities


def check_capabilities(repo: str, branch: str | None = None, *, auth_header: str | None, credential: str = "AVAILABLE",
                       client: httpx.Client | None = None) -> ProviderCapabilities:
    """Read-only Bitbucket Cloud capability probe (branch restrictions, merge checks, environments)."""
    from aeos.publisher.facade import ProviderError

    validate_repo(repo)
    caps = ProviderCapabilities(provider="BITBUCKET", repo=repo, branch=branch, checked_at=utcnow().isoformat(),
                                credential=credential, source="Bitbucket Cloud REST API 2.0 (GET only)")
    caps.residual_risks.append(ENFORCEMENT_UNTESTED)
    if not auth_header:
        caps.checks.append(CapabilityCheck("credential", "UNKNOWN",
                                           f"Credential Bitbucket tidak tersedia ({credential}); tidak ada panggilan API"))
        caps.residual_risks.append("Kemampuan provider UNKNOWN - merge tetap tindakan manusia")
        return caps
    bb = _reader(repo, auth_header, client)
    try:
        try:
            info = bb.repository()
        except ProviderError as exc:
            caps.checks.append(CapabilityCheck("repository", "UNKNOWN", f"Repository tidak terbaca: {_error_text(exc)}",
                                               getattr(exc, "status_code", None)))
            caps.residual_risks.append("Kemampuan provider UNKNOWN - merge tetap tindakan manusia")
            return caps
        caps.visibility = "private" if info.get("is_private") else "public"
        main = (info.get("mainbranch") or {}).get("name") if isinstance(info.get("mainbranch"), dict) else None
        branch = branch or main or "main"
        caps.branch = branch
        caps.checks.append(CapabilityCheck("repository", "AVAILABLE", f"{caps.visibility}; main branch {main}", 200,
                                           {"uuid": info.get("uuid"), "full_name": info.get("full_name")}))
        caps.checks.append(CapabilityCheck(
            "plan", "UNKNOWN", "Paket workspace Bitbucket (Free/Standard/Premium) tidak terlihat lewat API; "
                               "entitlement 'enforce merge checks' (Premium) hanya terlihat dari aturan yang ada"))
        try:
            summary = bb.restriction_summary(branch)
        except ProviderError as exc:
            code = getattr(exc, "status_code", None)
            why = ("butuh izin admin repository" if code in (401, 403) else _error_text(exc))
            caps.checks.append(CapabilityCheck("branch_restrictions", "UNKNOWN",
                                               f"Branch restrictions tidak terbaca ({why})", code))
            caps.checks.append(CapabilityCheck("merge_check_enforcement", "UNKNOWN",
                                               "Entitlement enforce merge checks UNKNOWN (aturan tidak terbaca)"))
            caps.merge_gate = "UNKNOWN"
            caps.entitlement = "UNKNOWN"
            caps.residual_risks.append("Kemampuan penegakan merge UNKNOWN - merge tetap tindakan manusia")
            return caps
        kinds = summary["restriction_kinds"]
        caps.checks.append(CapabilityCheck(
            "branch_restrictions", "CONFIGURED" if kinds else "NOT_CONFIGURED",
            (f"{len(kinds)} jenis aturan berlaku pada {branch}: " + ", ".join(kinds)) if kinds
            else f"Tidak ada branch restriction yang berlaku pada {branch}", 200, summary))
        for flag, label in (("direct_push_restricted", "Direct push"), ("history_rewrite_prevented", "Force-push"),
                            ("deletion_prevented", "Penghapusan branch")):
            if not summary[flag]:
                caps.residual_risks.append(f"{label} ke {branch} tidak dibatasi oleh branch restriction Bitbucket")
        n = int(summary["min_successful_builds"] or 0)
        caps.required_status_checks = [f"min_successful_builds={n}"] if n else []
        enforced = bool(summary["merge_checks_enforced"])
        checks = list(summary["merge_checks"])
        if enforced:
            caps.entitlement = "OBSERVED"
            caps.checks.append(CapabilityCheck("merge_check_enforcement", "CONFIGURED",
                                               "Aturan 'enforce merge checks' (Premium) teramati pada branch ini "
                                               "(penegakan belum diuji)", 200))
        else:
            caps.entitlement = "NOT_OBSERVED"
            caps.checks.append(CapabilityCheck("merge_check_enforcement", "NOT_ENFORCEABLE", NOT_ENFORCEABLE_MESSAGE,
                                               200, {"merge_checks": checks}))
        caps.checks.append(CapabilityCheck(
            "required_status_checks",
            "CONFIGURED" if n and enforced else ("NOT_ENFORCEABLE" if n else "NOT_CONFIGURED"),
            f"Minimal {n} build sukses untuk merge" if n else "Tidak ada merge check build sukses"))
        if n:
            identity = CapabilityCheck("status_check_identity", "NOT_CONFIGURED",
                                       "Bitbucket menghitung build status tanpa mengikat pelapornya: siapa pun dengan "
                                       "akses write repository dapat melaporkan SUCCESSFUL")
            caps.residual_risks.append("Build status Bitbucket tidak terikat ke identitas pelapor tertentu - status "
                                       "dapat dilaporkan oleh akun lain dengan akses write")
        else:
            identity = CapabilityCheck("status_check_identity", "NOT_APPLICABLE",
                                       "Tidak ada merge check build; identitas pelapor tidak relevan")
        caps.checks.append(identity)
        caps.status_check_identity = identity.status
        if enforced and checks:
            caps.merge_gate = "CONFIGURED"
        elif enforced:
            caps.merge_gate = "NOT_CONFIGURED"
            caps.residual_risks.append("Enforce merge checks aktif tetapi tidak ada merge check yang dikonfigurasi "
                                       "- merge tetap tindakan manusia")
        else:
            caps.merge_gate = "NOT_ENFORCEABLE"
            caps.residual_risks.append(NOT_ENFORCEABLE_MESSAGE)
        if not summary["merge_access_restricted"]:
            caps.residual_risks.append(f"Siapa pun dengan akses write dapat me-merge ke {branch} (restrict_merges "
                                       "tidak dikonfigurasi)")
        try:
            envs = bb.environments()
        except ProviderError as exc:
            env_check = CapabilityCheck("deploy_environments", "UNKNOWN",
                                        f"Environments tidak terbaca: {_error_text(exc)}",
                                        getattr(exc, "status_code", None))
        else:
            rows = [{"name": e.get("name"), "type": (e.get("environment_type") or {}).get("name")
                     if isinstance(e.get("environment_type"), dict) else None,
                     "admin_only": bool((e.get("restrictions") or {}).get("admin_only"))
                     if isinstance(e.get("restrictions"), dict) else False} for e in envs]
            open_envs = [str(r["name"]) for r in rows if not r["admin_only"]]
            if not rows:
                env_check = CapabilityCheck("deploy_environments", "NOT_CONFIGURED",
                                            "Tidak ada deployment environment di Bitbucket", 200, {"environments": []})
            elif open_envs:
                env_check = CapabilityCheck("deploy_environments", "NOT_CONFIGURED",
                                            "Environment tanpa pembatasan admin-only: " + ", ".join(open_envs[:10]),
                                            200, {"environments": rows})
            else:
                env_check = CapabilityCheck("deploy_environments", "CONFIGURED",
                                            f"{len(rows)} environment admin-only (penegakan belum diuji)", 200,
                                            {"environments": rows})
        caps.checks.append(env_check)
        caps.deploy_restrictions = env_check.status
        if env_check.status != "CONFIGURED":
            caps.residual_risks.append("Pembatasan deploy/environment provider: " + env_check.summary +
                                       " - AEOS tidak memiliki operasi deploy; produksi tetap tindakan manusia")
        return caps
    except httpx.HTTPError as exc:
        caps.checks.append(CapabilityCheck("network", "UNKNOWN", f"Bitbucket tidak terjangkau: {type(exc).__name__}"))
        caps.residual_risks.append("Kemampuan provider UNKNOWN - merge tetap tindakan manusia")
        return caps
    finally:
        bb.close()


def capabilities_for(session: Session, project: Project, repo: str, branch: str | None = None, *,
                     client: httpx.Client | None = None) -> ProviderCapabilities:
    """Capability probe of an OFFICE project's repository - only while the OFFICE adapter is AVAILABLE; otherwise
    no provider call is made and the readiness status is reported (DISABLED/NOT_SUPPORTED/BLOCKED)."""
    validate_repo(repo)
    ready = office_delivery_readiness(session, project)
    if ready.status != "AVAILABLE":
        caps = ProviderCapabilities(provider="BITBUCKET", repo=repo, branch=branch, checked_at=utcnow().isoformat(),
                                    credential=f"NOT_READ ({ready.status})",
                                    source="Bitbucket Cloud REST API 2.0 (GET only) - not called")
        caps.checks.append(CapabilityCheck("office_delivery", ready.status, "; ".join(ready.reasons)
                                           or "OFFICE delivery not available"))
        caps.residual_risks += [ENFORCEMENT_UNTESTED, "Kemampuan provider UNKNOWN - merge tetap tindakan manusia"]
        return caps
    auth, credential = read_auth(project.domain)
    return check_capabilities(repo, branch or ready.default_branch, auth_header=auth, credential=credential,
                              client=client)


# ---------------------------------------------------------------------------------------------- CI


# commit status state -> (check status, conclusion) in the provider-ci vocabulary (provider.summarize_runs)
STATUS_STATES: dict[str, tuple[str, str | None]] = {
    "SUCCESSFUL": ("completed", "success"),
    "FAILED": ("completed", "failure"),
    "INPROGRESS": ("in_progress", None),
    "STOPPED": ("completed", "cancelled"),
}
STEP_RESULTS: dict[str, str] = {
    "SUCCESSFUL": "success", "FAILED": "failure", "ERROR": "failure", "STOPPED": "cancelled", "EXPIRED": "stale",
    "NOT_RUN": "skipped",
}


def _same_commit(observed: Any, sha: str) -> bool:
    """Bitbucket sometimes reports abbreviated hashes; a missing hash is accepted only from an endpoint that is
    already scoped to *sha*. Another commit's CI is never attributed to this one."""
    h = str(observed or "").lower()
    return not h or h == sha or (len(h) >= 12 and sha.startswith(h))


def parse_ci(repo: str, sha: str, statuses: list[dict[str, Any]], pipelines: list[tuple[dict[str, Any],
             list[dict[str, Any]]]], *, gate_map: dict[str, list[str]] | None = None,
             truncated: bool = False) -> CIStatus:
    runs: list[CheckRunView] = []
    for st in statuses:
        if not _same_commit((st.get("commit") or {}).get("hash") if isinstance(st.get("commit"), dict) else None,
                            sha):
            continue
        status, conclusion = STATUS_STATES.get(str(st.get("state") or "").upper(), ("completed", "unknown"))
        name = str(st.get("name") or st.get("key") or "")
        runs.append(CheckRunView(name=name, status=status, conclusion=conclusion,
                                 app=f"commit-status:{str(st.get('key') or '')[:80]}", url=st.get("url"),
                                 started_at=st.get("created_on"),
                                 completed_at=st.get("updated_on") if status == "completed" else None,
                                 gates=classify_check(name, gate_map)))
    ws, slug = repo.split("/", 1)
    for pipe, steps in pipelines:
        if not _same_commit(((pipe.get("target") or {}).get("commit") or {}).get("hash"), sha):
            continue
        number = pipe.get("build_number")
        for step in steps:
            raw_state = step.get("state")
            state: dict[str, Any] = raw_state if isinstance(raw_state, dict) else {}
            raw_result = state.get("result")
            result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
            if str(state.get("name") or "").upper() == "COMPLETED":
                status, conclusion = "completed", STEP_RESULTS.get(str(result.get("name") or "").upper(), "unknown")
            else:
                status, conclusion = "in_progress", None
            name = str(step.get("name") or "")
            url = (f"{WEB}/{ws}/{slug}/pipelines/results/{number}/steps/{step.get('uuid')}"
                   if number and step.get("uuid") else None)
            runs.append(CheckRunView(name=name, status=status, conclusion=conclusion, app=CI_PROVIDER, url=url,
                                     started_at=step.get("started_on"), completed_at=step.get("completed_on"),
                                     gates=classify_check(name, gate_map)))
    return parse_check_runs_summary(repo, sha, runs, truncated=truncated, gate_map=gate_map, provider=PROVIDER,
                                    source="Bitbucket commit statuses + Pipelines steps (provider is the CI source "
                                           "of truth)")


def fetch_ci_status(repo: str, sha: str, *, auth_header: str | None, client: httpx.Client | None = None,
                    gate_map: dict[str, list[str]] | None = None, max_pages: int = 5) -> CIStatus:
    """CI of one exact commit: Bitbucket commit statuses (what Bitbucket's own merge checks count) plus the
    steps of the Pipelines run(s) whose target is that commit."""
    from aeos.publisher.facade import ProviderError

    validate_repo(repo)
    if not SHA_RE.match(sha or ""):
        raise ValidationFailed("full 40-hex commit sha required", code="SHA_INVALID")
    if not auth_header:
        return CIStatus(repo, sha, "UNKNOWN", 0, [], utcnow().isoformat(), error="Bitbucket credential unavailable",
                        provider=PROVIDER)
    bb = _reader(repo, auth_header, client)
    pairs: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    try:
        statuses, truncated = bb.commit_statuses(sha, max_pages=max_pages)
        pipelines, more = bb.pipelines_for_commit(sha)
        truncated = truncated or more or len(pipelines) > MAX_PIPELINES
        for pipe in pipelines[:MAX_PIPELINES]:
            steps, more_steps = bb.pipeline_steps(str(pipe.get("uuid") or ""))
            truncated = truncated or more_steps
            pairs.append((pipe, steps))
    except ProviderError as exc:
        return CIStatus(repo, sha, "UNKNOWN", 0, [], utcnow().isoformat(), error=_error_text(exc), provider=PROVIDER)
    except httpx.HTTPError as exc:
        return CIStatus(repo, sha, "UNKNOWN", 0, [], utcnow().isoformat(), error=f"unreachable: {type(exc).__name__}",
                        provider=PROVIDER)
    finally:
        bb.close()
    return parse_ci(repo, sha, statuses, pairs, gate_map=gate_map, truncated=truncated)


# ---------------------------------------------------------------------------------------------- PR observation


def _user_type(user: dict[str, Any]) -> str | None:
    if not user:
        return None
    return "User" if str(user.get("type") or "").lower() == "user" else "App"


def fetch_pull(repo: str, number: int, *, auth_header: str | None, client: httpx.Client | None = None) -> dict[str, Any]:
    """Normalised PR observation (the vocabulary of ``provider.fetch_pull``). Bitbucket reports abbreviated
    commit hashes on a PR; each one is resolved to the full hash (GET commit) or the read fails - an
    abbreviated hash is never compared with the accepted head."""
    from aeos.publisher.facade import ProviderError

    validate_repo(repo)
    if not auth_header:
        return {"error": "Bitbucket credential unavailable"}
    bb = _reader(repo, auth_header, client)
    try:
        d = bb.pull_request(int(number))
        raw_state = str(d.get("state") or "UNKNOWN").upper()
        merged = raw_state == "MERGED"

        def full(obj: Any) -> str | None:
            short = ((obj or {}).get("commit") or {}).get("hash") if isinstance(obj, dict) else None
            if not short:
                return None
            resolved = str(bb.commit(str(short)).get("hash") or "")
            if not SHA_RE.match(resolved) or not resolved.startswith(str(short)):
                raise ProviderError(f"commit {str(short)[:12]} did not resolve to a full hash", retryable=True)
            return resolved

        head = full(d.get("source"))
        base = full(d.get("destination"))
        merge_obj = d.get("merge_commit") if isinstance(d.get("merge_commit"), dict) else None
        merge_sha = full({"commit": merge_obj}) if merged and merge_obj else None
    except ProviderError as exc:
        return {"error": _error_text(exc)}
    except httpx.HTTPError as exc:
        return {"error": f"unreachable: {type(exc).__name__}"}
    finally:
        bb.close()
    raw_closer = d.get("closed_by")
    closer: dict[str, Any] = raw_closer if merged and isinstance(raw_closer, dict) else {}
    return {
        "number": d.get("id"), "state": "OPEN" if raw_state == "OPEN" else ("MERGED" if merged else "CLOSED"),
        "provider_state": raw_state, "merged": merged,
        # stable identity: the Atlassian account id (release approvers name account ids, never display names)
        "merged_by_login": (closer.get("account_id") or closer.get("uuid")) if closer else None,
        "merged_by_type": _user_type(closer), "merged_by_name": closer.get("nickname") if closer else None,
        "merged_at": d.get("closed_on") if merged else None, "merge_commit_sha": merge_sha,
        "head_sha": head, "head_ref": ((d.get("source") or {}).get("branch") or {}).get("name"),
        "base_sha": base, "base_ref": ((d.get("destination") or {}).get("branch") or {}).get("name"),
        "url": ((d.get("links") or {}).get("html") or {}).get("href"), "draft": bool(d.get("draft")),
    }


def fetch_commit_files(repo: str, sha: str, *, auth_header: str | None,
                       client: httpx.Client | None = None) -> dict[str, Any]:
    """Files a (merge) commit changed relative to its first parent - G5 on the merged state (GET only)."""
    from aeos.publisher.facade import ProviderError

    validate_repo(repo)
    if not SHA_RE.match(sha or ""):
        raise ValidationFailed("full 40-hex commit sha required", code="SHA_INVALID")
    if not auth_header:
        return {"error": "Bitbucket credential unavailable"}
    bb = _reader(repo, auth_header, client)
    try:
        commit = bb.commit(sha)
        rows, truncated = bb.diffstat(sha)
    except ProviderError as exc:
        return {"error": _error_text(exc)}
    except httpx.HTTPError as exc:
        return {"error": f"unreachable: {type(exc).__name__}"}
    finally:
        bb.close()
    files: set[str] = set()
    for r in rows:
        for side in ("new", "old"):
            p = (r.get(side) or {}).get("path") if isinstance(r.get(side), dict) else None
            if p:
                files.add(str(p))
    return {"sha": sha, "files": sorted(files), "parents": list(commit.get("parents") or []), "truncated": truncated,
            "checked_at": utcnow().isoformat()}
