"""§08 Project adapters: GitHub repo, Google Drive/Docs scopes, Bitbucket/Jira/Confluence (OFFICE).

Credential presence is a status lookup. The optional GitHub probe is read-only, explicit
(``--functional``) and never logs the token. OFFICE adapters stay NOT_APPLICABLE until configured
and remain blocked by the OFFICE default-deny policy even when configured.
"""

from __future__ import annotations

from typing import Any

from aeos.doctor.checks import check
from aeos.doctor.checks._common import secret_ref_status, set_secret_remedy
from aeos.doctor.model import CheckContext, Outcome, Remedy, aeos_cmd

GITHUB_REF = "personal/github"
# The OAuth token written by `aeos publish google-connect` and read by the publisher (provider
# vocabulary: aeos.portfolio.providers); the earlier "personal/google-oauth" was never written.
GOOGLE_REF = "personal/google-oauth-token"
ATLASSIAN_REF = "office/atlassian"
GITHUB_API = "https://api.github.com"


def github_get(ctx: CheckContext, path: str) -> Any:
    """Read-only GitHub API GET with the stored token (never logged). Returns HttpResult or None."""
    from aeos.secrets import facade as secrets

    token = secrets.get_secret(GITHUB_REF, purpose="doctor.github_readonly_probe", actor="doctor")
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
        return Outcome("CONNECTED", f"Repo github:{slug} terbaca dengan token (read-only); izin tulis/enforcement belum "
                       "diverifikasi.", evidence=evidence, target_identity=f"github:{slug}")
    if hr.status == 401:
        return Outcome("EXPIRED", "Token GitHub ditolak (kedaluwarsa/dicabut).", evidence=evidence,
                       error_code="GITHUB_TOKEN_REJECTED", remedy=set_secret_remedy(GITHUB_REF, "GitHub"))
    if hr.status in (403, 404):
        return Outcome("FAILED", f"Repo github:{slug} tidak terlihat dengan token ini ({hr.status}).", evidence=evidence,
                       error_code="GITHUB_REPO_NOT_VISIBLE")
    return Outcome("UNKNOWN", f"Probe GitHub tidak konklusif ({hr.status or hr.error}).", evidence=evidence,
                   error_code="GITHUB_PROBE_INCONCLUSIVE")


@check("adapter.google_oauth", "PROJECT_ADAPTERS", "Google Drive/Docs PERSONAL: OAuth client", component_id="google-drive",
       ttl_seconds=3600, invalidators=(f"secret:{GOOGLE_REF}",), ok={"CONFIGURED", "CONNECTED", "VERIFIED"},
       severity_fail="HIGH", scope={"domain": "PERSONAL"}, observation="configuration")
def adapter_google(ctx: CheckContext) -> Outcome:
    state, evidence = secret_ref_status(GOOGLE_REF)
    evidence["scope_policy"] = "drive.file (least privilege, S18)"
    if state == "NOT_IMPLEMENTED":
        return Outcome("UNKNOWN", "Modul secrets belum tersedia.", evidence=evidence, error_code="SECRETS_NOT_IMPLEMENTED")
    if state == "STORE_UNAVAILABLE":
        return Outcome("MISSING", "Secret store tidak tersedia (fail closed).", evidence=evidence,
                       error_code="SECRET_STORE_UNAVAILABLE")
    if state == "ABSENT":
        # The token (and the client config next to it) is written only by the official consent flow of
        # `aeos publish google-connect`, never typed in with `aeos secrets set`.
        return Outcome("MISSING", f"Token OAuth Google {GOOGLE_REF} belum ada; consent dilakukan pemilik lewat alur "
                       "Google (aeos publish google-connect).", evidence=evidence, error_code="GOOGLE_OAUTH_MISSING",
                       remedy=Remedy(
                           operation="Hubungkan Google Drive melalui consent OAuth resmi (scope drive.file saja)",
                           command=aeos_cmd("publish google-connect --client-secret <client_secret.json>"),
                           source="Google OAuth 2.0 untuk aplikasi Desktop (redirect 127.0.0.1)",
                           target=GOOGLE_REF, privilege="pemilik akun Google", cost="tidak ada",
                           rollback="cabut akses aplikasi di myaccount.google.com/permissions",
                           evidence_to_collect="status ref tanpa nilai; jalankan ulang Doctor"))
    return Outcome("CONFIGURED", f"Token OAuth {GOOGLE_REF} tersimpan; scope dan root folder diverifikasi oleh "
                   "adapter publisher.", evidence=evidence)


def _office(component: str, label: str) -> Any:
    def fn(ctx: CheckContext) -> Outcome:
        state, evidence = secret_ref_status(ATLASSIAN_REF)
        evidence["policy"] = "OFFICE default deny (§23)"
        if state == "PRESENT":
            return Outcome("CONFIGURED", f"Credential {ATLASSIAN_REF} ada; {label} tetap diblokir sampai otorisasi "
                           "perusahaan tercatat dan tenant/target diverifikasi.", evidence=evidence)
        return Outcome("NOT_APPLICABLE", f"Adapter {label} belum dikonfigurasi (OFFICE belum diotorisasi).",
                       evidence=evidence)

    fn.__name__ = f"adapter_office_{component.replace('-', '_')}"
    return fn


for _cid, _label in (("bitbucket", "Bitbucket"), ("jira", "Jira"), ("confluence", "Confluence")):
    check(f"adapter.office_{_cid}", "PROJECT_ADAPTERS", f"{_label} OFFICE", component_id=_cid, ttl_seconds=3600,
          invalidators=(f"secret:{ATLASSIAN_REF}", "config:office_policy"), ok={"CONFIGURED", "VERIFIED"},
          scope={"domain": "OFFICE"})(_office(_cid, _label))
