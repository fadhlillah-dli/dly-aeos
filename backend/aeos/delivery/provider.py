"""GitHub provider adapter — **read-only** capability, CI and PR observations (§08, §30, §31).

* Branch protection / rulesets / branch rules / plan visibility are read with GET only. A private
  repository on GitHub Free cannot enforce protection (``403 Upgrade to GitHub Pro…`` or ``404``):
  that is reported as ``NOT_ENFORCEABLE`` with a residual-risk message, never as "required checks
  block merge" (§31). Even a CONFIGURED protection is not *proven* enforced until bypass tests
  (direct push, merge API, force-push, deletion) ran against it.
* CI status comes from the provider's check-runs for an exact commit (provider is the source of
  truth for CI, §30) and is recorded as ``provider-ci`` evidence (E3) for that commit.
* This module performs no write calls of any kind: no merge, no deploy, no status/comment write.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy.orm import Session

from aeos.core.errors import AeosError, ValidationFailed
from aeos.core.redaction import redact_text, register_secret
from aeos.core.timeutil import utcnow
from aeos.models.evidence import EvidenceArtifact
from aeos.models.portfolio import Project

log = logging.getLogger("aeos.delivery.provider")

GITHUB_API = "https://api.github.com"
TIMEOUT = httpx.Timeout(15.0, connect=10.0)
SECRET_REF = "personal/github"
# Secrets contract: "<namespace>:<use>" with the reference namespace (aeos.secrets.facade).
SECRET_PURPOSE = "personal:delivery.provider-read"
REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
NOT_ENFORCEABLE_MESSAGE = "Branch protection tidak dapat ditegakkan pada paket GitHub ini — merge tetap tindakan manusia"
NOT_CONFIGURED_MESSAGE = ("Branch protection/ruleset tersedia tetapi belum dikonfigurasi untuk branch ini — "
                          "merge tetap tindakan manusia")
ENFORCEMENT_UNTESTED = ("Penegakan human-only merge belum diuji terhadap direct push, merge API, force-push dan "
                        "penghapusan branch (NOT-PROVEN)")
FAIL_CONCLUSIONS = frozenset({"failure", "timed_out", "action_required", "startup_failure"})
NOT_PASS_CONCLUSIONS = frozenset({"cancelled", "stale"})
PASS_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})

# §31 gate mapping for check-run names (CI jobs). The platform CI runs ruff/secret-scan/tests in the
# "backend" jobs and typecheck/vitest/build in the "frontend" job (.github/workflows/ci.yml).
GATE_HINTS: dict[str, tuple[str, ...]] = {
    "G1": ("lint", "ruff", "static", "type", "secret", "backend", "frontend"),
    "G2": ("backend", "pytest", "unit", "test"),
    "G3": ("frontend", "ui", "e2e", "browser", "playwright", "vitest", "a11y"),
}


def validate_repo(full_name: str) -> str:
    if not isinstance(full_name, str) or not REPO_RE.match(full_name) or ".." in full_name:
        raise ValidationFailed("repository must be 'owner/name'", code="REPO_INVALID")
    return full_name


def github_token(domain: str) -> tuple[str | None, str]:
    """Read token for a trusted in-process caller. Never returned to UI/logs; registered for redaction."""
    if domain != "PERSONAL":
        return None, ("NOT_APPLICABLE: OFFICE code lives in Bitbucket Cloud (aeos.delivery.bitbucket, disabled until "
                      "a recorded company authorisation)")
    try:
        from aeos.secrets import facade as secrets

        token = secrets.get_secret(SECRET_REF, purpose=SECRET_PURPOSE, actor="delivery")
    except ImportError:
        return None, "UNAVAILABLE: secrets module not available"
    except AeosError as exc:
        return None, f"MISSING: {exc.code}"
    register_secret(token)
    return token, "AVAILABLE"


def _observe(resp: httpx.Response) -> None:
    """§08 per-call permission observation: the response's method/URL/status/headers (never a body or the token)
    go to the same observer hook the publisher feeds, so a 401/403/scope change seen by a delivery read marks the
    checks of ``personal/github`` STALE (``aeos.doctor.provider_watch``). A plan-entitlement answer ("Upgrade to
    GitHub Pro") is not a permission change and is not reported."""
    if _upgrade_required(resp):
        return
    try:
        from aeos.publisher.facade import observe_httpx_response
    except ImportError:  # pragma: no cover - the publisher ships with the platform
        return
    observe_httpx_response(resp)


class GitHubReader:
    """Minimal GET-only GitHub REST client."""

    def __init__(self, token: str, *, client: httpx.Client | None = None):
        self._client = client or httpx.Client(base_url=GITHUB_API, timeout=TIMEOUT, follow_redirects=False)
        self._owns = client is None
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {token}",
            "User-Agent": "dly-aeos-delivery",
        }

    def get(self, path: str, params: dict[str, Any] | None = None, *, observe: bool = True) -> httpx.Response:
        """``observe=False`` for capability probes whose 403/404 is the provider's answer about a feature the
        least-privilege token deliberately lacks (no Administration permission, FINE_GRAINED_REMEDY) - reporting it
        would keep the token's checks STALE on every read."""
        resp = self._client.get(GITHUB_API + path, params=params, headers=self._headers)
        if observe:
            _observe(resp)
        return resp

    def close(self) -> None:
        if self._owns:
            self._client.close()


@dataclass
class CapabilityCheck:
    key: str
    status: str  # CONFIGURED | NOT_CONFIGURED | NOT_ENFORCEABLE | UNKNOWN | NOT_APPLICABLE | AVAILABLE
    summary: str
    http_status: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderCapabilities:
    provider: str
    repo: str
    branch: str | None
    checked_at: str
    credential: str
    visibility: str | None = None
    plan: str | None = None
    permissions: dict[str, Any] = field(default_factory=dict)
    checks: list[CapabilityCheck] = field(default_factory=list)
    required_status_checks: list[str] = field(default_factory=list)
    status_check_identity: str = "UNKNOWN"  # CONFIGURED | NOT_CONFIGURED | NOT_APPLICABLE | UNKNOWN
    deploy_restrictions: str = "UNKNOWN"  # CONFIGURED | NOT_CONFIGURED | NOT_ENFORCEABLE | UNKNOWN
    merge_gate: str = "UNKNOWN"
    enforcement_tested: str = "NOT-PROVEN"
    residual_risks: list[str] = field(default_factory=list)
    source: str = "GitHub REST API (GET only)"
    mode: str = "LIVE"
    # Provider plan entitlement for merge enforcement as far as it is observable: OBSERVED (an enforcing rule is
    # in place) | AVAILABLE (the provider answers the feature, nothing configured) | NOT_ENTITLED (the provider says
    # this plan/visibility cannot enforce it, e.g. GitHub "Upgrade to GitHub Pro") | NOT_OBSERVED (not visible
    # without an enforcing rule - never claimed; Bitbucket "enforce merge checks" is Premium) | UNKNOWN.
    entitlement: str = "UNKNOWN"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        msg = body.get("message", "") if isinstance(body, dict) else ""
    except ValueError:
        msg = resp.text[:200]
    return redact_text(str(msg))[:300]


def _rate_limited(resp: httpx.Response) -> bool:
    return resp.status_code == 429 or (resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0")


def _upgrade_required(resp: httpx.Response) -> bool:
    msg = _message(resp).lower()
    return resp.status_code == 403 and ("upgrade to github pro" in msg or "make this repository public" in msg)


def _protection_check(resp: httpx.Response, *, private: bool | None, plan: str | None) -> CapabilityCheck:
    code = resp.status_code
    if code == 200:
        data = resp.json()
        rsc = data.get("required_status_checks") or {}
        contexts: list[str] = [str(c) for c in (rsc.get("contexts") or []) if c] + [
            str(c["context"]) for c in (rsc.get("checks") or []) if isinstance(c, dict) and c.get("context")]
        # Status-check identity (§08): which GitHub App must report each required check. ``app_id`` null/-1
        # (or a legacy ``contexts`` entry) means any app/user with status-write access can satisfy it.
        apps = {str(c["context"]): c.get("app_id") for c in (rsc.get("checks") or [])
                if isinstance(c, dict) and c.get("context")}
        for ctx in rsc.get("contexts") or []:
            apps.setdefault(str(ctx), None)
        reviews = data.get("required_pull_request_reviews") or {}
        detail = {
            "required_status_check_apps": {k: (v if isinstance(v, int) and v > 0 else None)
                                           for k, v in sorted(apps.items())},
            "required_status_checks": sorted(set(contexts)),
            "strict": bool(rsc.get("strict")),
            "required_reviews": reviews.get("required_approving_review_count") if reviews else None,
            "requires_pull_request": bool(reviews),
            "enforce_admins": bool((data.get("enforce_admins") or {}).get("enabled")),
            "allow_force_pushes": bool((data.get("allow_force_pushes") or {}).get("enabled")),
            "allow_deletions": bool((data.get("allow_deletions") or {}).get("enabled")),
            "restrictions": data.get("restrictions") is not None,
        }
        return CapabilityCheck("branch_protection", "CONFIGURED",
                               "Branch protection dikonfigurasi (penegakan belum diuji)", code, detail)
    if _rate_limited(resp):
        return CapabilityCheck("branch_protection", "UNKNOWN", "GitHub rate limit; coba lagi nanti", code)
    if _upgrade_required(resp):
        return CapabilityCheck("branch_protection", "NOT_ENFORCEABLE", NOT_ENFORCEABLE_MESSAGE, code,
                               {"provider_message": _message(resp)})
    if code == 404:
        msg = _message(resp)
        if private and (plan or "").lower() == "free":
            return CapabilityCheck("branch_protection", "NOT_ENFORCEABLE", NOT_ENFORCEABLE_MESSAGE, code,
                                   {"provider_message": msg, "plan": plan})
        if "not protected" in msg.lower():
            return CapabilityCheck("branch_protection", "NOT_CONFIGURED", NOT_CONFIGURED_MESSAGE, code,
                                   {"provider_message": msg})
        return CapabilityCheck("branch_protection", "UNKNOWN",
                               "Tidak dapat membaca branch protection (404; izin admin atau paket tidak diketahui)",
                               code, {"provider_message": msg})
    return CapabilityCheck("branch_protection", "UNKNOWN",
                           f"Tidak dapat membaca branch protection (HTTP {code}): {_message(resp)}", code)


def _rulesets_check(resp: httpx.Response, *, private: bool | None, plan: str | None) -> CapabilityCheck:
    code = resp.status_code
    if code == 200:
        rows = resp.json() if isinstance(resp.json(), list) else []
        active = [r for r in rows if isinstance(r, dict) and r.get("enforcement") == "active"]
        evaluate = [r for r in rows if isinstance(r, dict) and r.get("enforcement") == "evaluate"]
        detail = {"total": len(rows), "active": [r.get("name") for r in active],
                  "evaluate_only": [r.get("name") for r in evaluate]}
        if active:
            return CapabilityCheck("rulesets", "CONFIGURED", f"{len(active)} ruleset aktif", code, detail)
        return CapabilityCheck("rulesets", "NOT_CONFIGURED",
                               "Tidak ada ruleset aktif" + (" (hanya mode evaluate)" if evaluate else ""), code, detail)
    if _rate_limited(resp):
        return CapabilityCheck("rulesets", "UNKNOWN", "GitHub rate limit; coba lagi nanti", code)
    if _upgrade_required(resp) or (code == 404 and private and (plan or "").lower() == "free"):
        return CapabilityCheck("rulesets", "NOT_ENFORCEABLE", NOT_ENFORCEABLE_MESSAGE, code,
                               {"provider_message": _message(resp)})
    return CapabilityCheck("rulesets", "UNKNOWN", f"Tidak dapat membaca rulesets (HTTP {code})", code,
                           {"provider_message": _message(resp)})


def github_entitlement(prot: CapabilityCheck, rules: CapabilityCheck, *, gate_configured: bool) -> str:
    """§08 "actual provider entitlements": what GitHub itself shows about merge enforcement on this repository,
    never inferred from a missing plan name. OBSERVED - an enforcing rule is in place; NOT_ENTITLED - GitHub says
    the plan/visibility cannot enforce it (upgrade required, or 404 on a private repository of a Free plan);
    AVAILABLE - GitHub answered "Branch not protected" (the feature exists, nothing configured); UNKNOWN otherwise
    (an empty rulesets list alone does not show that a private repository can enforce rulesets)."""
    if gate_configured:
        return "OBSERVED"
    if "NOT_ENFORCEABLE" in (prot.status, rules.status):
        return "NOT_ENTITLED"
    if prot.status == "NOT_CONFIGURED":
        return "AVAILABLE"
    return "UNKNOWN"


def _branch_rules_check(resp: httpx.Response) -> CapabilityCheck:
    code = resp.status_code
    if code == 200:
        rows = resp.json() if isinstance(resp.json(), list) else []
        types = sorted({str(r["type"]) for r in rows if isinstance(r, dict) and r.get("type")})
        contexts: list[str] = []
        apps: dict[str, int | None] = {}
        for r in rows:
            if isinstance(r, dict) and r.get("type") == "required_status_checks":
                for c in (r.get("parameters") or {}).get("required_status_checks") or []:
                    if isinstance(c, dict) and c.get("context"):
                        contexts.append(c["context"])
                        iid = c.get("integration_id")
                        apps[str(c["context"])] = iid if isinstance(iid, int) and iid > 0 else None
        detail = {"rule_types": types, "required_status_checks": sorted(set(contexts)),
                  "required_status_check_apps": dict(sorted(apps.items()))}
        status = "CONFIGURED" if types else "NOT_CONFIGURED"
        return CapabilityCheck("branch_rules", status, f"{len(types)} jenis aturan berlaku pada branch", code, detail)
    return CapabilityCheck("branch_rules", "UNKNOWN", f"Tidak dapat membaca aturan branch (HTTP {code})", code)


def _status_check_identity(prot: CapabilityCheck, branch_rules: CapabilityCheck,
                           required: list[str]) -> CapabilityCheck:
    """§08 "status-check identity": each required check bound to the app that must report it."""
    apps: dict[str, int | None] = {}
    for src in (prot.detail.get("required_status_check_apps") or {}, branch_rules.detail.get(
            "required_status_check_apps") or {}):
        for ctx, app in src.items():
            apps[ctx] = apps.get(ctx) or app
    if not required:
        return CapabilityCheck("status_check_identity", "NOT_APPLICABLE",
                               "Tidak ada required status check; identitas pelapor tidak relevan")
    unbound = sorted(c for c in required if not apps.get(c))
    if unbound:
        return CapabilityCheck("status_check_identity", "NOT_CONFIGURED",
                               "Check tanpa app_id (pelapor bebas): " + ", ".join(unbound[:10]),
                               detail={"apps": apps, "unbound": unbound})
    return CapabilityCheck("status_check_identity", "CONFIGURED",
                           "Setiap required status check terikat ke GitHub App tertentu", detail={"apps": apps})


def _environments_check(resp: httpx.Response, *, private: bool | None, plan: str | None) -> CapabilityCheck:
    """§08 deploy restrictions: deployment environments and their protection rules (GET only)."""
    code = resp.status_code
    if code == 200:
        body = resp.json() if isinstance(resp.json(), dict) else {}
        envs = [e for e in body.get("environments") or [] if isinstance(e, dict)]
        rows: list[dict[str, Any]] = []
        for e in envs:
            types = sorted({str(r.get("type")) for r in e.get("protection_rules") or [] if isinstance(r, dict)})
            rows.append({"name": e.get("name"), "protection_rules": types,
                         "branch_policy": e.get("deployment_branch_policy")})
        if not rows:
            return CapabilityCheck("deploy_environments", "NOT_CONFIGURED",
                                   "Tidak ada deployment environment di provider", code, {"environments": []})
        unprotected = [r["name"] for r in rows if "required_reviewers" not in r["protection_rules"]]
        if unprotected:
            return CapabilityCheck("deploy_environments", "NOT_CONFIGURED",
                                   "Environment tanpa required reviewers: " + ", ".join(map(str, unprotected[:10])),
                                   code, {"environments": rows})
        return CapabilityCheck("deploy_environments", "CONFIGURED",
                               f"{len(rows)} environment dengan required reviewers (penegakan belum diuji)", code,
                               {"environments": rows})
    if _rate_limited(resp):
        return CapabilityCheck("deploy_environments", "UNKNOWN", "GitHub rate limit; coba lagi nanti", code)
    if _upgrade_required(resp) or (code == 404 and private and (plan or "").lower() == "free"):
        return CapabilityCheck("deploy_environments", "NOT_ENFORCEABLE",
                               "Proteksi environment tidak tersedia pada paket GitHub ini", code,
                               {"provider_message": _message(resp)})
    return CapabilityCheck("deploy_environments", "UNKNOWN", f"Tidak dapat membaca environments (HTTP {code})", code,
                           {"provider_message": _message(resp)})


def check_capabilities(repo: str, branch: str | None = None, *, token: str | None, credential: str = "AVAILABLE",
                       client: httpx.Client | None = None) -> ProviderCapabilities:
    """Read-only capability probe. Without a token nothing is called and everything is UNKNOWN."""
    validate_repo(repo)
    caps = ProviderCapabilities(provider="GITHUB", repo=repo, branch=branch, checked_at=utcnow().isoformat(),
                                credential=credential)
    caps.residual_risks.append(ENFORCEMENT_UNTESTED)
    if not token:
        caps.checks.append(CapabilityCheck("credential", "UNKNOWN",
                                           f"Credential GitHub tidak tersedia ({credential}); tidak ada panggilan API"))
        caps.residual_risks.append("Kemampuan provider UNKNOWN — merge tetap tindakan manusia")
        return caps
    gh = GitHubReader(token, client=client)
    try:
        owner = repo.split("/", 1)[0]
        r = gh.get(f"/repos/{repo}")
        if r.status_code != 200:
            caps.checks.append(CapabilityCheck("repository", "UNKNOWN",
                                               f"Repository tidak terbaca (HTTP {r.status_code}): {_message(r)}",
                                               r.status_code))
            caps.residual_risks.append("Kemampuan provider UNKNOWN — merge tetap tindakan manusia")
            return caps
        info = r.json()
        private = bool(info.get("private"))
        caps.visibility = info.get("visibility") or ("private" if private else "public")
        caps.permissions = {k: bool(v) for k, v in (info.get("permissions") or {}).items()}
        branch = branch or info.get("default_branch") or "main"
        caps.branch = branch
        caps.checks.append(CapabilityCheck("repository", "AVAILABLE",
                                           f"{caps.visibility}; default branch {info.get('default_branch')}", 200,
                                           {"archived": bool(info.get("archived")),
                                            "owner_type": (info.get("owner") or {}).get("type")}))
        owner_info = info.get("owner") or {}
        plan: str | None = None
        if owner_info.get("type") == "User":
            u = gh.get("/user", observe=False)
            if u.status_code == 200 and (u.json() or {}).get("login") == owner_info.get("login"):
                plan = ((u.json() or {}).get("plan") or {}).get("name")
        elif owner_info.get("type") == "Organization":
            o = gh.get(f"/orgs/{owner}", observe=False)
            if o.status_code == 200:
                plan = ((o.json() or {}).get("plan") or {}).get("name")
        caps.plan = plan
        caps.checks.append(CapabilityCheck("plan", "AVAILABLE" if plan else "UNKNOWN",
                                           f"Paket: {plan}" if plan else "Paket akun tidak terlihat oleh credential ini"))
        b = quote(branch, safe="")
        prot = _protection_check(gh.get(f"/repos/{repo}/branches/{b}/protection", observe=False), private=private, plan=plan)
        rules = _rulesets_check(gh.get(f"/repos/{repo}/rulesets", params={"includes_parents": "true"}, observe=False),
                                private=private, plan=plan)
        branch_rules = _branch_rules_check(gh.get(f"/repos/{repo}/rules/branches/{b}", observe=False))
        caps.checks += [prot, rules, branch_rules]
        required = sorted(set(prot.detail.get("required_status_checks", []))
                          | set(branch_rules.detail.get("required_status_checks", [])))
        caps.required_status_checks = required
        if required:
            rsc_status = "CONFIGURED"
        elif prot.status != "CONFIGURED" and "NOT_ENFORCEABLE" in (prot.status, rules.status):
            rsc_status = "NOT_ENFORCEABLE"
        elif "CONFIGURED" in (prot.status, branch_rules.status) or "NOT_CONFIGURED" in (prot.status, branch_rules.status):
            rsc_status = "NOT_CONFIGURED"
        else:
            rsc_status = "UNKNOWN"
        caps.checks.append(CapabilityCheck(
            "required_status_checks", rsc_status,
            ", ".join(required) if required else "Tidak ada required status check yang terlihat",
        ))
        identity = _status_check_identity(prot, branch_rules, required)
        caps.checks.append(identity)
        caps.status_check_identity = identity.status
        if identity.status == "NOT_CONFIGURED":
            caps.residual_risks.append("Required status check tidak terikat ke GitHub App tertentu — status dapat "
                                       "dilaporkan oleh app/pengguna lain dengan izin statuses:write")
        env_check = _environments_check(gh.get(f"/repos/{repo}/environments", params={"per_page": 100}, observe=False),
                                        private=private, plan=plan)
        caps.checks.append(env_check)
        caps.deploy_restrictions = env_check.status
        if env_check.status != "CONFIGURED":
            caps.residual_risks.append("Pembatasan deploy/environment provider: " + env_check.summary +
                                       " — AEOS tidak memiliki operasi deploy; produksi tetap tindakan manusia")
        rule_types = set(branch_rules.detail.get("rule_types", []))
        gate_configured = (
            (prot.status == "CONFIGURED" and (prot.detail.get("requires_pull_request")
                                             or prot.detail.get("required_status_checks")))
            or bool(rule_types & {"pull_request", "required_status_checks"})
        )
        if gate_configured:
            caps.merge_gate = "CONFIGURED"
            if prot.status == "CONFIGURED" and not prot.detail.get("enforce_admins"):
                caps.residual_risks.append("Admin repository dapat melewati branch protection (enforce_admins=false)")
        elif "NOT_ENFORCEABLE" in (prot.status, rules.status):
            caps.merge_gate = "NOT_ENFORCEABLE"
            caps.residual_risks.append(NOT_ENFORCEABLE_MESSAGE)
        elif "NOT_CONFIGURED" in (prot.status, rules.status):
            caps.merge_gate = "NOT_CONFIGURED"
            caps.residual_risks.append(NOT_CONFIGURED_MESSAGE)
        else:
            caps.merge_gate = "UNKNOWN"
            caps.residual_risks.append("Kemampuan penegakan merge UNKNOWN — merge tetap tindakan manusia")
        caps.entitlement = github_entitlement(prot, rules, gate_configured=bool(gate_configured))
        return caps
    except httpx.HTTPError as exc:
        caps.checks.append(CapabilityCheck("network", "UNKNOWN", f"GitHub tidak terjangkau: {type(exc).__name__}"))
        caps.residual_risks.append("Kemampuan provider UNKNOWN — merge tetap tindakan manusia")
        return caps
    finally:
        gh.close()


# ---------------------------------------------------------------------------- CI status


@dataclass
class CheckRunView:
    name: str
    status: str
    conclusion: str | None
    app: str | None
    url: str | None
    started_at: str | None
    completed_at: str | None
    gates: list[str] = field(default_factory=list)


@dataclass
class CIStatus:
    repo: str
    sha: str
    overall: str  # PASS | FAIL | PENDING | NOT_PROVEN | NO_CHECKS | UNKNOWN
    total: int
    runs: list[CheckRunView]
    checked_at: str
    error: str | None = None
    source: str = "GitHub check-runs API (provider is the CI source of truth)"
    truncated: bool = False  # the provider reported more check-runs than were read
    # PIPELINE_REFERENCE: gates from the project's explicit job -> gate map; NAME_HEURISTIC: GATE_HINTS
    gate_mapping: str = "NAME_HEURISTIC"
    provider: str = "github"  # github | bitbucket (the CI source of truth of the project's domain)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def gate_status(self, gate: str) -> tuple[str, list[str]]:
        runs = [r for r in self.runs if gate in r.gates]
        status = summarize_runs(runs, error=self.error)
        if self.truncated and status in ("PASS", "NO_CHECKS"):
            status = "NOT_PROVEN"
        return status, [r.name for r in runs]


_EXPLICIT_GATES = re.compile(r"\[(G[0-8](?:\s*,\s*G[0-8])*|NO-GATE)\]", re.I)


def classify_check(name: str, gate_map: dict[str, list[str]] | None = None) -> list[str]:
    """Gates a CI job (check-run name) proves. With the project's pipeline reference (§31 "pipeline reference
    per project") the mapping is explicit: exact job names per gate. Otherwise an explicit tag in the job
    name - ``[G1,G2]`` or ``[NO-GATE]`` for report-only jobs - wins over the name heuristics (the CI evidence
    then still says NAME_HEURISTIC: no project pipeline reference was configured)."""
    low = (name or "").lower().strip()
    if gate_map:
        return sorted(g for g, jobs in gate_map.items() if low in {j.lower().strip() for j in jobs})
    m = _EXPLICIT_GATES.search(name or "")
    if m:
        tag = m.group(1).upper()
        return [] if tag == "NO-GATE" else sorted({g.strip() for g in tag.split(",")})
    return [g for g, needles in GATE_HINTS.items() if any(n in low for n in needles)]


PIPELINE_GATES = ("G1", "G2", "G3", "G5")


def pipeline_reference(session: Session, project: Project) -> dict[str, list[str]] | None:
    """The project's pipeline reference: the CI integration's ``config.gate_G1`` ... ``gate_G5`` entries,
    each a comma-separated list of exact CI job (check-run) names (blueprints/pipeline-reference.yaml)."""
    from sqlalchemy import select

    from aeos.models.portfolio import ProjectIntegration

    row = session.scalar(select(ProjectIntegration).where(ProjectIntegration.project_id == project.id,
                                                          ProjectIntegration.purpose == "ci")
                         .order_by(ProjectIntegration.created_at.desc()).limit(1))
    cfg = (row.config or {}) if row is not None else {}
    out = {g: [j.strip() for j in str(cfg[f"gate_{g}"]).split(",") if j.strip()]
           for g in PIPELINE_GATES if cfg.get(f"gate_{g}")}
    return out or None


def summarize_runs(runs: list[CheckRunView], *, error: str | None = None) -> str:
    if error:
        return "UNKNOWN"
    if not runs:
        return "NO_CHECKS"
    if any(r.status == "completed" and (r.conclusion or "") in FAIL_CONCLUSIONS for r in runs):
        return "FAIL"
    if any(r.status != "completed" for r in runs):
        return "PENDING"
    if any((r.conclusion or "") not in PASS_CONCLUSIONS for r in runs):  # cancelled/stale/unknown
        return "NOT_PROVEN"
    if all(r.conclusion == "skipped" for r in runs):
        return "NOT_PROVEN"
    return "PASS"


def parse_check_runs(repo: str, sha: str, payloads: list[dict[str, Any]],
                     gate_map: dict[str, list[str]] | None = None) -> CIStatus:
    runs: list[CheckRunView] = []
    total = 0
    read = 0
    for body in payloads:
        total = max(total, int(body.get("total_count") or 0))
        for cr in body.get("check_runs") or []:
            read += 1
            if not isinstance(cr, dict):
                continue
            if cr.get("head_sha") and cr.get("head_sha") != sha:
                continue  # never attribute another commit's CI to this one
            name = str(cr.get("name") or "")
            runs.append(CheckRunView(
                name=name, status=str(cr.get("status") or "unknown"), conclusion=cr.get("conclusion"),
                app=(cr.get("app") or {}).get("slug"), url=cr.get("html_url"), started_at=cr.get("started_at"),
                completed_at=cr.get("completed_at"), gates=classify_check(name, gate_map),
            ))
    return parse_check_runs_summary(repo, sha, runs, truncated=total > read, gate_map=gate_map)


def parse_check_runs_summary(repo: str, sha: str, runs: list[CheckRunView], *, truncated: bool,
                             gate_map: dict[str, list[str]] | None, provider: str = "github",
                             source: str | None = None) -> CIStatus:
    """One rule for every CI provider: a partial read is never a pass."""
    overall = summarize_runs(runs)
    if truncated and overall in ("PASS", "NO_CHECKS"):
        overall = "NOT_PROVEN"  # unseen check-runs could be failing: a partial read is never a pass
    ci = CIStatus(repo=repo, sha=sha, overall=overall, total=len(runs), runs=runs,
                  checked_at=utcnow().isoformat(), truncated=truncated,
                  gate_mapping="PIPELINE_REFERENCE" if gate_map else "NAME_HEURISTIC", provider=provider)
    if source:
        ci.source = source
    return ci


def fetch_check_runs(repo: str, sha: str, *, token: str | None, client: httpx.Client | None = None,
                     max_pages: int = 5, gate_map: dict[str, list[str]] | None = None) -> CIStatus:
    validate_repo(repo)
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha or ""):
        raise ValidationFailed("commit sha required", code="SHA_INVALID")
    if not token:
        return CIStatus(repo, sha, "UNKNOWN", 0, [], utcnow().isoformat(), error="GitHub credential unavailable")
    gh = GitHubReader(token, client=client)
    payloads: list[dict[str, Any]] = []
    try:
        for page in range(1, max_pages + 1):
            r = gh.get(f"/repos/{repo}/commits/{sha}/check-runs", params={"per_page": 100, "page": page})
            if r.status_code != 200:
                return CIStatus(repo, sha, "UNKNOWN", 0, [], utcnow().isoformat(),
                                error=f"HTTP {r.status_code}: {_message(r)}")
            body = r.json()
            payloads.append(body)
            if len(body.get("check_runs") or []) < 100:
                break
    except httpx.HTTPError as exc:
        return CIStatus(repo, sha, "UNKNOWN", 0, [], utcnow().isoformat(), error=f"unreachable: {type(exc).__name__}")
    finally:
        gh.close()
    return parse_check_runs(repo, sha, payloads, gate_map)


def fetch_pull(repo: str, number: int, *, token: str | None, client: httpx.Client | None = None) -> dict[str, Any]:
    validate_repo(repo)
    if not token:
        return {"error": "GitHub credential unavailable"}
    gh = GitHubReader(token, client=client)
    try:
        r = gh.get(f"/repos/{repo}/pulls/{int(number)}")
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}: {_message(r)}"}
        d = r.json()
    except httpx.HTTPError as exc:
        return {"error": f"unreachable: {type(exc).__name__}"}
    finally:
        gh.close()
    merged_by = d.get("merged_by") or {}
    return {
        "number": d.get("number"), "state": d.get("state"), "merged": bool(d.get("merged")),
        "merged_by_login": merged_by.get("login"), "merged_by_type": merged_by.get("type"),
        "merged_at": d.get("merged_at"), "merge_commit_sha": d.get("merge_commit_sha"),
        "head_sha": (d.get("head") or {}).get("sha"), "head_ref": (d.get("head") or {}).get("ref"),
        "base_sha": (d.get("base") or {}).get("sha"), "base_ref": (d.get("base") or {}).get("ref"),
        "url": d.get("html_url"),
    }


COMMIT_FILES_LIMIT = 300  # GitHub lists at most 300 files on the single-commit endpoint


def fetch_commit_files(repo: str, sha: str, *, token: str | None, client: httpx.Client | None = None) -> dict[str, Any]:
    """Files a commit changed relative to its first parent (GET only) — for a merge commit: what the merge
    actually brought into the integration branch (G5 "policy files protected" on the merged state)."""
    validate_repo(repo)
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha or ""):
        raise ValidationFailed("commit sha required", code="SHA_INVALID")
    if not token:
        return {"error": "GitHub credential unavailable"}
    gh = GitHubReader(token, client=client)
    try:
        r = gh.get(f"/repos/{repo}/commits/{sha}")
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}: {_message(r)}"}
        d = r.json()
    except httpx.HTTPError as exc:
        return {"error": f"unreachable: {type(exc).__name__}"}
    finally:
        gh.close()
    files = [str(f.get("filename")) for f in d.get("files") or [] if isinstance(f, dict) and f.get("filename")]
    files += [str(f.get("previous_filename")) for f in d.get("files") or []
              if isinstance(f, dict) and f.get("previous_filename")]
    return {"sha": sha, "files": sorted(set(files)), "parents": [p.get("sha") for p in d.get("parents") or []
                                                                  if isinstance(p, dict)],
            "truncated": len(d.get("files") or []) >= COMMIT_FILES_LIMIT, "checked_at": utcnow().isoformat()}


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------- evidence


def record_ci_evidence(session: Session, project: Project, ci: CIStatus, *, task_id: str | None = None,
                       run_id: str | None = None) -> EvidenceArtifact | None:
    """Provider CI observation for one commit → ``provider-ci`` evidence (E3). Errors are not recorded as E3."""
    if ci.error:
        return None
    from aeos.verification import facade as verification

    identity, label = (("bitbucket-commit-statuses", "Bitbucket commit statuses/Pipelines") if ci.provider == "bitbucket"
                       else ("github-check-runs", "GitHub check-runs"))
    return verification.collect_evidence(
        session, kind="CI_STATUS", origin="provider-ci", collector_identity=f"{identity}:{ci.repo}"[:128],
        claim=f"{label} for {ci.sha[:12]}: {ci.overall} ({ci.total} run(s))", content=ci.to_dict(),
        evidence_level="E3", subject_type="commit", subject_id=ci.sha, project_id=project.id, domain=project.domain,
        run_id=run_id, task_id=task_id,
        environment={"commit": ci.sha, "repo": ci.repo, "provider": ci.provider, "checked_at": ci.checked_at},
    )


def record_capability_evidence(session: Session, project: Project, caps: ProviderCapabilities) -> EvidenceArtifact:
    from aeos.verification import facade as verification

    provider = caps.provider.lower()
    label = "Bitbucket" if provider == "bitbucket" else "GitHub"
    return verification.collect_evidence(
        session, kind="PROVIDER_CAPABILITY", origin="collector", collector_identity=f"{provider}-capability-probe",
        claim=f"{label} merge-gate capability for {caps.repo}@{caps.branch}: {caps.merge_gate} "
              f"(enforcement {caps.enforcement_tested}, entitlement {caps.entitlement})",
        content=caps.to_dict(), evidence_level="E1", subject_type="repository", subject_id=caps.repo,
        project_id=project.id, domain=project.domain,
        environment={"repo": caps.repo, "branch": caps.branch, "provider": provider, "checked_at": caps.checked_at},
    )
