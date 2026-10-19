"""§08 Project adapters: GitHub repo, Google Drive/Docs scopes, Bitbucket/Jira/Confluence (OFFICE).

Credential presence is a status lookup. The optional GitHub probe is read-only, explicit
(``--functional``) and never logs the token. OFFICE adapters stay NOT_APPLICABLE until configured
and remain blocked by the OFFICE default-deny policy even when configured.
"""

from __future__ import annotations

from typing import Any

from aeos.core.enums import Domain
from aeos.doctor.checks import check
from aeos.doctor.checks._common import secret_ref_status, set_secret_remedy
from aeos.doctor.model import CheckContext, Outcome, Remedy, aeos_cmd
from aeos.portfolio import providers as pv

GITHUB_REF = "personal/github"
# Same references the publisher's Google adapter writes (aeos.publisher.providers.google): the OAuth client
# config and the consented token are stored separately; CAN_PUBLISH_PERSONAL needs both.
GOOGLE_CLIENT_REF = "personal/google-oauth-client"
GOOGLE_TOKEN_REF = "personal/google-oauth-token"
GOOGLE_REF = GOOGLE_TOKEN_REF  # kept for callers of the earlier single-ref contract
ATLASSIAN_REF = "office/atlassian"
BITBUCKET_REF = "office/bitbucket"
GITHUB_API = "https://api.github.com"
# secrets facade contract: purpose "<namespace>:<use>" with the namespace of the reference (§26)
GITHUB_PURPOSE = "personal:doctor.github-readonly-probe"
# The PERSONAL publication providers of the one provider vocabulary (aeos.portfolio.providers): Google Docs
# are files in the project's Drive folder, so ``google-drive`` covers both; there is no separate Docs write.
PERSONAL_PUBLISH_PROVIDERS: tuple[str, ...] = tuple(dict.fromkeys(
    pv.PUBLICATION_PROVIDER[Domain.PERSONAL.value].values()))
PERSONAL_PUBLISH_LABELS = ", ".join(pv.LABELS.get(p, p) for p in PERSONAL_PUBLISH_PROVIDERS)


def github_token() -> str:
    """Stored PERSONAL GitHub token for an in-process read-only probe (registered for redaction, never logged)."""
    from aeos.secrets import facade as secrets

    return secrets.get_secret(GITHUB_REF, purpose=GITHUB_PURPOSE, actor="doctor")


# Classic OAuth/PAT scopes that reach far beyond publishing issues on one repository (§07 step 7 least privilege).
BROAD_GITHUB_SCOPES = frozenset({"repo", "public_repo", "workflow", "delete_repo", "write:packages", "delete:packages",
                                 "admin:org", "admin:repo_hook", "admin:org_hook", "admin:public_key",
                                 "admin:gpg_key", "admin:ssh_signing_key", "admin:enterprise", "site_admin", "user",
                                 "codespace", "project"})


def _token_kind() -> str:
    """Kind of the stored token from its documented prefix only (the value itself is never read further)."""
    try:
        token = github_token()
    except Exception:
        return "unknown"
    if token.startswith("github_pat_"):
        return "fine-grained"
    if token.startswith(("ghp_", "gho_", "ghu_")):
        return "classic"
    return "unknown"


def github_token_privilege(kind: str, scopes_header: str | None) -> dict[str, Any]:
    """Least-privilege verdict from the token kind and GitHub's ``X-OAuth-Scopes`` header (classic tokens only).

    Fine-grained PATs are limited to selected repositories/permissions by construction; a classic/OAuth token
    (e.g. ``gh auth token``) with ``repo`` reaches every repository of the account — a residual risk the owner sees."""
    scopes = sorted({s.strip() for s in (scopes_header or "").split(",") if s.strip()})
    if kind == "fine-grained":
        return {"kind": kind, "scopes": [], "least_privilege": True,
                "note": "fine-grained PAT (repo terpilih, izin per resource)"}
    if scopes_header is None:
        return {"kind": kind, "scopes": [], "least_privilege": None,
                "note": "jenis/scope token tidak terbaca — least privilege belum terbukti"}
    broad = sorted(s for s in scopes if s in BROAD_GITHUB_SCOPES or s.startswith("admin:"))
    if broad:
        return {"kind": kind, "scopes": scopes, "least_privilege": False, "broad_scopes": broad,
                "risk": "Token GitHub classic/OAuth dengan scope luas (" + ", ".join(broad) + ") menjangkau semua repo "
                        "akun; gunakan fine-grained PAT khusus repo platform (Issues RW, Pull requests RW, Contents RW "
                        "untuk push branch aeos/* oleh broker, Metadata R; tanpa Workflows/Administration) dan "
                        "lindungi main dengan branch protection."}
    return {"kind": kind, "scopes": scopes, "least_privilege": True, "note": "scope classic terbatas"}


def github_get(ctx: CheckContext, path: str) -> Any:
    """Read-only GitHub API GET with the stored token (never logged). Returns HttpResult or None."""
    token = github_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "dly-aeos-doctor"}
    return ctx.runner.http_get(f"{GITHUB_API}{path}", timeout=10.0, headers=headers, allow_remote=True)


@check("adapter.github_token", "PROJECT_ADAPTERS", "GitHub PERSONAL: token dan akses repo (read-only)",
       component_id="github", ttl_seconds=3600, invalidators=(f"secret:{GITHUB_REF}", "target"),
       ok={"CONFIGURED", "CONNECTED", "VERIFIED"}, severity_fail="HIGH", scope={"domain": "PERSONAL"},
       observation="connectivity")
def adapter_github(ctx: CheckContext) -> Outcome:
    state, evidence = secret_ref_status(GITHUB_REF)
    slug = ctx.settings.platform_repo_slug
    evidence["target"] = f"github:{slug}"
    if state == "NOT_IMPLEMENTED":
        return Outcome("UNKNOWN", "Modul secrets belum tersedia.", evidence=evidence, error_code="SECRETS_NOT_IMPLEMENTED")
    if state == "STORE_UNAVAILABLE":
        return Outcome("MISSING", "Secret store tidak tersedia; token GitHub tidak dapat dibaca (fail closed).",
                       evidence=evidence, error_code="SECRET_STORE_UNAVAILABLE")
    if state == "ABSENT":
        return Outcome("MISSING", f"Token {GITHUB_REF} belum disimpan.", evidence=evidence, error_code="GITHUB_TOKEN_MISSING",
                       remedy=set_secret_remedy(GITHUB_REF, "GitHub (fine-grained, repo terpilih saja)"))
    if not ctx.functional:
        return Outcome("CONFIGURED", f"Token {GITHUB_REF} tersimpan; akses repo belum diprobe (gunakan --functional).",
                       evidence=evidence, target_identity=f"github:{slug}")
    try:
        hr = github_get(ctx, f"/repos/{slug}")
    except Exception as exc:  # store/network failure: never green
        return Outcome("UNKNOWN", f"Probe GitHub gagal ({type(exc).__name__}).", evidence=evidence,
                       error_code="GITHUB_PROBE_ERROR")
    evidence["http_status"] = hr.status
    if hr.status == 200 and isinstance(hr.body, dict):
        perms = hr.body.get("permissions") or {}
        evidence.update({"private": hr.body.get("private"), "default_branch": hr.body.get("default_branch"),
                         "permissions": {k: perms.get(k) for k in ("admin", "maintain", "push", "pull") if k in perms},
                         "repo_id": hr.body.get("id")})
        privilege = github_token_privilege(_token_kind(), (getattr(hr, "headers", None) or {}).get("x-oauth-scopes"))
        evidence["token_privilege"] = privilege
        summary = f"Repo github:{slug} terbaca dengan token (read-only); izin tulis/enforcement belum diverifikasi."
        if privilege["least_privilege"] is False:
            evidence["residual_risks"] = [privilege["risk"]]
            summary += " " + privilege["risk"]
        return Outcome("CONNECTED", summary, evidence=evidence, target_identity=f"github:{slug}",
                       remedy=None if privilege["least_privilege"] is not False else set_secret_remedy(
                           GITHUB_REF, "GitHub fine-grained PAT, hanya repo platform: Issues RW, Pull requests RW, "
                                       "Contents RW (push aeos/*), Metadata R"))
    if hr.status == 401:
        return Outcome("EXPIRED", "Token GitHub ditolak (kedaluwarsa/dicabut).", evidence=evidence,
                       error_code="GITHUB_TOKEN_REJECTED", remedy=set_secret_remedy(GITHUB_REF, "GitHub"))
    if hr.status in (403, 404):
        return Outcome("FAILED", f"Repo github:{slug} tidak terlihat dengan token ini ({hr.status}).", evidence=evidence,
                       error_code="GITHUB_REPO_NOT_VISIBLE")
    return Outcome("UNKNOWN", f"Probe GitHub tidak konklusif ({hr.status or hr.error}).", evidence=evidence,
                   error_code="GITHUB_PROBE_INCONCLUSIVE")


def google_connect_remedy() -> Remedy:
    return Remedy(
        operation="Hubungkan Google (consent pemilik di browser, redirect ke 127.0.0.1, scope drive.file saja); client "
                  "OAuth jenis 'Desktop app' dibuat pemilik di Google Cloud Console",
        command=aeos_cmd("publish google-connect --client-secret <path ke client_secret.json>"),
        source="Google OAuth installed-app flow (S18: scope drive.file)",
        target=f"{GOOGLE_CLIENT_REF} + {GOOGLE_TOKEN_REF}", privilege="pemilik akun Google", cost="tidak ada",
        rollback=aeos_cmd(f"secrets delete {GOOGLE_TOKEN_REF}") + " dan cabut akses aplikasi di akun Google",
        evidence_to_collect="adapter.google_oauth CONFIGURED; publikasi pertama adalah bukti fungsi")


@check("adapter.google_oauth", "PROJECT_ADAPTERS", "Google Drive/Docs PERSONAL: OAuth client + token",
       component_id="google-drive", ttl_seconds=3600,
       invalidators=(f"secret:{GOOGLE_CLIENT_REF}", f"secret:{GOOGLE_TOKEN_REF}"),
       ok={"CONFIGURED", "CONNECTED", "VERIFIED"}, severity_fail="HIGH", scope={"domain": "PERSONAL"},
       observation="configuration")
def adapter_google(ctx: CheckContext) -> Outcome:
    client_state, client_ev = secret_ref_status(GOOGLE_CLIENT_REF)
    token_state, token_ev = secret_ref_status(GOOGLE_TOKEN_REF)
    evidence: dict[str, Any] = {"client": client_ev, "token": token_ev,
                                "scope_policy": "drive.file (least privilege, S18)", "token_scopes_read": False}
    states = {client_state, token_state}
    if "NOT_IMPLEMENTED" in states:
        return Outcome("UNKNOWN", "Modul secrets belum tersedia.", evidence=evidence, error_code="SECRETS_NOT_IMPLEMENTED")
    if "STORE_UNAVAILABLE" in states:
        return Outcome("MISSING", "Secret store tidak tersedia; credential Google tidak dapat diperiksa (fail closed).",
                       evidence=evidence, error_code="SECRET_STORE_UNAVAILABLE")
    if token_state != "PRESENT":
        what = "client OAuth dan token" if client_state != "PRESENT" else "token consent"
        return Outcome("MISSING", f"Google belum terhubung: {what} belum disimpan ({GOOGLE_TOKEN_REF}).",
                       evidence=evidence, error_code="GOOGLE_OAUTH_MISSING", remedy=google_connect_remedy())
    if client_state != "PRESENT":
        return Outcome("FAILED", f"Token Google ada tetapi client {GOOGLE_CLIENT_REF} hilang; refresh token tidak dapat "
                       "dipakai.", evidence=evidence, error_code="GOOGLE_CLIENT_MISSING", remedy=google_connect_remedy())
    return Outcome("CONFIGURED", "Client OAuth dan token Google tersimpan (nilai tidak dibaca); scope dan root folder "
                   "dibuktikan oleh publikasi pertama, bukan oleh Doctor.", evidence=evidence)


def _enablement_fp(ctx: CheckContext) -> dict[str, Any] | None:
    if ctx.session is None:
        return None
    from sqlalchemy import select

    from aeos.models.decisions import Decision
    from aeos.models.identity import SystemSetting

    flags = ctx.session.execute(select(SystemSetting.key, SystemSetting.version).where(
        SystemSetting.key.like("write_enabled.%"))).all()
    decisions = ctx.session.execute(select(Decision.id, Decision.status).where(
        Decision.kind == "WRITE_ENABLEMENT")).all()
    return {"flags": sorted((k, int(v or 0)) for k, v in flags), "decisions": sorted((d[0], d[1]) for d in decisions)}


@check("adapter.write_enablement", "PROJECT_ADAPTERS", "Write enablement provider PERSONAL (keputusan manusia)",
       component_id="policy-decision-registry", needs_db=True, ttl_seconds=3600, ok={"CONFIGURED", "VERIFIED"},
       invalidators=("config:write_enablement", "config:policy"), fingerprint=_enablement_fp, severity_fail="HIGH",
       scope={"domain": "PERSONAL"},
       description="Flag write_enabled.<provider> + keputusan WRITE_ENABLEMENT ACTIVE untuk setiap provider publikasi "
                   f"PERSONAL ({PERSONAL_PUBLISH_LABELS}; Google Docs = file di folder Drive) (syarat interlock §26).")
def adapter_write_enablement(ctx: CheckContext) -> Outcome:
    assert ctx.session is not None
    try:
        from aeos.policy import enablement
    except (ImportError, ModuleNotFoundError):
        return Outcome("UNKNOWN", "Modul policy tidak tersedia; write eksternal ditolak (fail closed).",
                       error_code="POLICY_NOT_AVAILABLE")
    rows = {r["provider"]: r for r in enablement.status(ctx.session, "PERSONAL")}
    effective = [p for p in PERSONAL_PUBLISH_PROVIDERS if (rows.get(p) or {}).get("effective")]
    missing = [p for p in PERSONAL_PUBLISH_PROVIDERS if p not in effective]
    evidence = {"providers": {p: {"flag": (rows.get(p) or {}).get("flag"),
                                  "decision": bool((rows.get(p) or {}).get("decision_id")),
                                  "effective": p in effective} for p in PERSONAL_PUBLISH_PROVIDERS}}
    if missing:
        return Outcome("MISSING", f"Write enablement belum efektif untuk: {', '.join(missing)} (butuh flag + keputusan "
                       "WRITE_ENABLEMENT ACTIVE); interlock menolak publikasi ke provider tersebut.", evidence=evidence,
                       error_code="WRITE_NOT_ENABLED", remedy=Remedy(
                           operation="Pemilik mencatat keputusan WRITE_ENABLEMENT per provider di halaman Keputusan "
                           "(Policy & Decisions); tidak ada perintah otomatis", target=", ".join(missing),
                           privilege="SUPER_ADMIN (keputusan manusia)", cost="tidak ada",
                           rollback="cabut keputusan (flag otomatis kembali false)",
                           evidence_to_collect="adapter.write_enablement CONFIGURED"))
    return Outcome("CONFIGURED", f"Write enablement efektif untuk {PERSONAL_PUBLISH_LABELS}; setiap write tetap butuh "
                   "approval versi/hash yang tepat (interlock).", evidence=evidence)


def _office(component: str, label: str, ref: str) -> Any:
    def fn(ctx: CheckContext) -> Outcome:
        state, evidence = secret_ref_status(ref)
        evidence["policy"] = "OFFICE default deny (§23)"
        if state == "PRESENT":
            return Outcome("CONFIGURED", f"Credential {ref} ada; {label} tetap diblokir sampai otorisasi "
                           "perusahaan tercatat dan tenant/target diverifikasi.", evidence=evidence)
        return Outcome("NOT_APPLICABLE", f"Adapter {label} belum dikonfigurasi (OFFICE belum diotorisasi).",
                       evidence=evidence)

    fn.__name__ = f"adapter_office_{component.replace('-', '_')}"
    return fn


# Same references as the publisher's Atlassian adapter (Bitbucket has its own app password reference).
for _cid, _label, _ref in (("bitbucket", "Bitbucket", BITBUCKET_REF), ("jira", "Jira", ATLASSIAN_REF),
                           ("confluence", "Confluence", ATLASSIAN_REF)):
    check(f"adapter.office_{_cid}", "PROJECT_ADAPTERS", f"{_label} OFFICE", component_id=_cid, ttl_seconds=3600,
          invalidators=(f"secret:{_ref}", "config:office_policy"), ok={"CONFIGURED", "VERIFIED"},
          scope={"domain": "OFFICE"})(_office(_cid, _label, _ref))
