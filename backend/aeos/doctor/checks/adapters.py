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


def github_get(ctx: CheckContext, path: str) -> Any:
    """Read-only GitHub API GET with the stored token (never logged). Returns HttpResult or None."""
    token = github_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "dly-aeos-doctor"}
    return ctx.runner.http_get(f"{GITHUB_API}{path}", timeout=10.0, headers=headers, allow_remote=True)


def _scopes(headers: dict[str, str] | None) -> list[str] | str:
    """Token scopes GitHub reports for classic tokens (``X-OAuth-Scopes``); fine-grained tokens expose none."""
    raw = (headers or {}).get("x-oauth-scopes")
    if raw is None:
        return "NOT_EXPOSED (fine-grained token: izin per repo dibaca dari 'permissions')"
    return sorted(s.strip() for s in raw.split(",") if s.strip())


# "integration:*": a project integration target change (aeos.portfolio emits ``integration:<id>``) or an observed
# permission/scope change makes these results STALE (§08).
@check("adapter.github_token", "PROJECT_ADAPTERS", "GitHub PERSONAL: token dan akses repo (read-only)",
       component_id="github", ttl_seconds=3600, invalidators=(f"secret:{GITHUB_REF}", "integration:*"),
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
                         "repo_id": hr.body.get("id"), "token_scopes": _scopes(hr.headers)})
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


def google_connect_remedy() -> Remedy:
    return Remedy(
        operation="Hubungkan Google (consent pemilik di browser, redirect ke 127.0.0.1, scope drive.file saja); client "
                  "OAuth jenis 'Desktop app' dibuat pemilik di Google Cloud Console",
        command=aeos_cmd("publish google-connect --client-secret <path ke client_secret.json>"),
        source="Google OAuth installed-app flow (S18: scope drive.file)",
        target=f"{GOOGLE_CLIENT_REF} + {GOOGLE_TOKEN_REF}", privilege="pemilik akun Google", cost="tidak ada",
        rollback=aeos_cmd(f"secrets delete {GOOGLE_TOKEN_REF}") + " dan cabut akses aplikasi di akun Google",
        evidence_to_collect="adapter.google_oauth CONFIGURED; publikasi pertama adalah bukti fungsi")


DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
GOOGLE_SCOPE_PURPOSE = "personal:doctor.google-scope-read"


def stored_google_token() -> dict[str, Any] | None:
    """The consented Google token as stored by ``publish google-connect`` (registered for redaction; only its
    non-secret fields are ever evidence)."""
    import json

    from aeos.core.redaction import register_secret
    from aeos.secrets import facade as secrets

    raw = secrets.get_secret(GOOGLE_TOKEN_REF, purpose=GOOGLE_SCOPE_PURPOSE, actor="doctor")
    try:
        tok = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(tok, dict):
        return None
    for k in ("token", "refresh_token", "client_secret"):
        register_secret(tok.get(k))
    return tok


def google_scope_state() -> tuple[str, list[str]]:
    """('LEAST_PRIVILEGE'|'EXCESSIVE'|'MISSING_DRIVE_FILE'|'UNREADABLE', granted scopes) from the stored token.
    Scopes are what Google returned at consent/refresh time (``scopes`` in the token JSON)."""
    try:
        tok = stored_google_token()
    except Exception:
        return "UNREADABLE", []
    if tok is None:
        return "UNREADABLE", []
    granted = sorted(str(s) for s in tok.get("scopes") or [])
    if not granted:
        return "UNREADABLE", []
    if DRIVE_FILE_SCOPE not in granted:
        return "MISSING_DRIVE_FILE", granted
    return ("LEAST_PRIVILEGE" if granted == [DRIVE_FILE_SCOPE] else "EXCESSIVE"), granted


@check("adapter.google_oauth", "PROJECT_ADAPTERS", "Google Drive/Docs PERSONAL: OAuth client + token + scope",
       component_id="google-drive", ttl_seconds=3600,
       invalidators=(f"secret:{GOOGLE_CLIENT_REF}", f"secret:{GOOGLE_TOKEN_REF}", "integration:*"),
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
    scope_state, granted = google_scope_state()
    evidence.update({"token_scopes_read": scope_state != "UNREADABLE", "granted_scopes": granted,
                     "scope_state": scope_state, "scope_source": "respons token Google saat consent/refresh"})
    if scope_state in ("EXCESSIVE", "MISSING_DRIVE_FILE"):
        return Outcome("FAILED", f"Scope token Google tidak sesuai least privilege drive.file ({scope_state}); "
                       "hubungkan ulang dengan scope drive.file saja.", evidence=evidence,
                       error_code="GOOGLE_SCOPE_NOT_LEAST_PRIVILEGE", remedy=google_connect_remedy())
    note = ("scope drive.file terbaca dari token tersimpan" if scope_state == "LEAST_PRIVILEGE"
            else "scope token tidak dapat dibaca (UNKNOWN)")
    return Outcome("CONFIGURED", f"Client OAuth dan token Google tersimpan; {note}. Akses root folder tiap proyek "
                   "diperiksa oleh adapter.project_drive_root.", evidence=evidence)


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


# ---------------------------------------------------------------- project-scoped targets (§08 project scope)
#
# One CheckResult per (check, project): readiness of one project's repository / Drive root is never inferred
# from the platform repo or another project's target (§08 "Readiness dievaluasi menurut capability dan target").

DRIVE_API = "https://www.googleapis.com/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
OFFICE_TARGET_PROVIDERS = (pv.BITBUCKET, pv.JIRA, pv.CONFLUENCE)


def _projects(ctx: CheckContext) -> dict[str, Any]:
    from sqlalchemy import select

    from aeos.models.portfolio import Project

    assert ctx.session is not None
    return {p.id: p for p in ctx.session.scalars(select(Project).where(Project.lifecycle != "RETIRED"))}


def project_repo_targets(ctx: CheckContext) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from aeos.models.portfolio import RepositoryReference

    projects = _projects(ctx)
    by_project: dict[str, list[Any]] = {}
    assert ctx.session is not None
    for r in ctx.session.scalars(select(RepositoryReference).order_by(RepositoryReference.full_name)):
        if r.project_id in projects and (r.provider or "").upper() == "GITHUB":
            by_project.setdefault(r.project_id, []).append(r)
    return [{"project_id": pid, "domain": projects[pid].domain, "project_key": projects[pid].project_key,
             "repos": [{"id": r.id, "full_name": r.full_name, "provider_repo_id": r.provider_repo_id} for r in repos],
             "invalidators": [f"repository:{r.id}" for r in repos]}
            for pid, repos in by_project.items()]


@check("adapter.project_repo", "PROJECT_ADAPTERS", "Repo GitHub proyek: ID repo persis dan izin (per proyek)",
       component_id="github", ttl_seconds=3600, needs_db=True, invalidators=(f"secret:{GITHUB_REF}", "integration:*"),
       ok={"CONFIGURED", "CONNECTED", "VERIFIED"}, severity_fail="HIGH", targets=project_repo_targets,
       description="Per proyek: repo terdaftar (provider repo ID persis), token tersimpan; dengan --functional GET "
                   "read-only /repos/<owner>/<name> mencocokkan ID repo (repo bernama sama bukan target yang sama, "
                   "§37) dan membaca izin/scope token.")
def adapter_project_repo(ctx: CheckContext) -> Outcome:
    target = ctx.project or {}
    repos = target.get("repos") or []
    evidence: dict[str, Any] = {"project_key": target.get("project_key"),
                                "repos": [r["full_name"] for r in repos]}
    if target.get("domain") != Domain.PERSONAL.value:
        return Outcome("UNKNOWN", "Repo GitHub pada proyek non-PERSONAL tidak diprobe (OFFICE default deny, §23).",
                       evidence=evidence, error_code="OFFICE_DEFAULT_DENY")
    state, token_ev = secret_ref_status(GITHUB_REF)
    evidence["token"] = {k: token_ev.get(k) for k in ("ref", "backend", "updated_at")}
    if state in ("NOT_IMPLEMENTED", "STORE_UNAVAILABLE"):
        return Outcome("MISSING" if state == "STORE_UNAVAILABLE" else "UNKNOWN",
                       "Secret store tidak tersedia; token GitHub tidak dapat dibaca (fail closed).", evidence=evidence,
                       error_code="SECRET_STORE_UNAVAILABLE")
    if state == "ABSENT":
        return Outcome("MISSING", f"Token {GITHUB_REF} belum disimpan.", evidence=evidence,
                       error_code="GITHUB_TOKEN_MISSING", remedy=set_secret_remedy(GITHUB_REF, "GitHub"))
    unpinned = [r["full_name"] for r in repos if not r.get("provider_repo_id")]
    if not ctx.functional:
        return Outcome("CONFIGURED", f"{len(repos)} repo terdaftar, token tersimpan; akses dan ID repo belum diprobe "
                       "(gunakan --functional)." + (f" ID repo belum tercatat: {', '.join(unpinned)}." if unpinned
                                                   else ""), evidence=evidence,
                       target_identity=", ".join(f"github:{r['full_name']}" for r in repos)[:250])
    results: list[dict[str, Any]] = []
    permissions: dict[str, Any] = {}
    scopes: Any = None
    for r in repos:
        try:
            hr = github_get(ctx, f"/repos/{r['full_name']}")
        except Exception as exc:  # never green
            results.append({"repo": r["full_name"], "status": "UNKNOWN", "error": type(exc).__name__})
            continue
        item: dict[str, Any] = {"repo": r["full_name"], "http_status": hr.status}
        if hr.status == 200 and isinstance(hr.body, dict):
            observed_id = str(hr.body.get("id") or "")
            expected_id = str(r.get("provider_repo_id") or "")
            perms = hr.body.get("permissions") or {}
            permissions[r["full_name"]] = {k: perms.get(k) for k in ("admin", "maintain", "push", "pull") if k in perms}
            scopes = _scopes(hr.headers)
            if expected_id and observed_id != expected_id:
                item.update(status="FAILED", error="REPO_ID_MISMATCH", observed_id=observed_id, expected_id=expected_id)
            else:
                item.update(status="CONNECTED", repo_id=observed_id, id_pinned=bool(expected_id))
        elif hr.status == 401:
            item.update(status="EXPIRED", error="GITHUB_TOKEN_REJECTED")
        elif hr.status in (403, 404):
            item.update(status="FAILED", error="GITHUB_REPO_NOT_VISIBLE")
        else:
            item.update(status="UNKNOWN", error=str(hr.error or hr.status))
        results.append(item)
    evidence.update({"results": results, "permissions": permissions, "token_scopes": scopes})
    statuses = {x["status"] for x in results}
    for bad, code in (("EXPIRED", "GITHUB_TOKEN_REJECTED"), ("FAILED", "PROJECT_REPO_NOT_VERIFIED"),
                      ("UNKNOWN", "GITHUB_PROBE_INCONCLUSIVE")):
        if bad in statuses:
            return Outcome(bad, f"Repo proyek: {', '.join(x['repo'] + ' ' + x['status'] for x in results)}.",
                           evidence=evidence, error_code=code,
                           remedy=set_secret_remedy(GITHUB_REF, "GitHub") if bad == "EXPIRED" else None)
    return Outcome("CONNECTED", f"{len(results)} repo proyek terbaca dengan token (read-only), ID repo cocok"
                   + ("" if not unpinned else f" (ID belum dipin: {', '.join(unpinned)})")
                   + "; izin tulis/enforcement belum diverifikasi.", evidence=evidence,
                   target_identity=", ".join(f"github:{x['repo']}" for x in results)[:250])


def project_drive_targets(ctx: CheckContext) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from aeos.models.portfolio import ProjectIntegration

    projects = _projects(ctx)
    out: dict[str, dict[str, Any]] = {}
    assert ctx.session is not None
    for integ in ctx.session.scalars(select(ProjectIntegration).where(ProjectIntegration.provider == pv.GOOGLE_DRIVE)):
        p = projects.get(integ.project_id)
        if p is None:
            continue
        t = out.setdefault(p.id, {"project_id": p.id, "domain": p.domain, "project_key": p.project_key,
                                  "roots": [], "invalidators": []})
        t["roots"].append({"integration_id": integ.id, "root_id": integ.root_id, "purpose": integ.purpose})
        t["invalidators"].append(f"integration:{integ.id}")
    return list(out.values())


def drive_get(ctx: CheckContext, root_id: str, token: dict[str, Any]) -> Any:
    """Read-only Drive metadata GET of one folder with the stored token (refreshed through the publisher's
    GoogleAuth when expired). Never logged; the token is registered for redaction."""
    import json

    import httpx

    from aeos.publisher.providers.google import GoogleAuth

    with httpx.Client(timeout=10.0) as client:
        header = GoogleAuth(client, token_json=json.dumps(token)).header()
    url = (f"{DRIVE_API}/{root_id}?fields=id,name,mimeType,trashed,capabilities(canAddChildren)"
           "&supportsAllDrives=true")
    return ctx.runner.http_get(url, timeout=10.0, headers=header, allow_remote=True)


@check("adapter.project_drive_root", "PROJECT_ADAPTERS", "Root Google Drive proyek dan scope token (per proyek)",
       component_id="google-drive", ttl_seconds=3600, needs_db=True,
       invalidators=(f"secret:{GOOGLE_TOKEN_REF}", f"secret:{GOOGLE_CLIENT_REF}", "integration:*"),
       ok={"CONFIGURED", "CONNECTED", "VERIFIED"}, severity_fail="HIGH", targets=project_drive_targets,
       description="Per proyek: root folder Drive terkonfigurasi dan token dengan scope drive.file; dengan --functional "
                   "GET metadata folder root (read-only): ada, folder, tidak di trash, dapat menambah file.")
def adapter_project_drive_root(ctx: CheckContext) -> Outcome:
    target = ctx.project or {}
    roots = target.get("roots") or []
    evidence: dict[str, Any] = {"project_key": target.get("project_key"),
                                "roots": [{"root_id": r.get("root_id"), "purpose": r.get("purpose")} for r in roots]}
    if target.get("domain") != Domain.PERSONAL.value:
        return Outcome("UNKNOWN", "Root Drive pada proyek non-PERSONAL tidak diprobe (OFFICE default deny, §23).",
                       evidence=evidence, error_code="OFFICE_DEFAULT_DENY")
    if any(not r.get("root_id") for r in roots):
        return Outcome("MISSING", "Integrasi Google Drive proyek belum punya root folder (root_id).", evidence=evidence,
                       error_code="DRIVE_ROOT_MISSING", remedy=Remedy(
                           operation="Atur root folder Drive proyek (folder yang dibuat/dibuka aplikasi, drive.file)",
                           command="PUT /api/projects/<id>/integrations (root_id = folder id)",
                           target=str(target.get("project_key")), privilege="SUPER_ADMIN",
                           evidence_to_collect="adapter.project_drive_root CONFIGURED"))
    token_state, _ev = secret_ref_status(GOOGLE_TOKEN_REF)
    if token_state != "PRESENT":
        return Outcome("MISSING", "Token Google belum tersimpan; root Drive proyek tidak dapat diakses.",
                       evidence=evidence, error_code="GOOGLE_OAUTH_MISSING", remedy=google_connect_remedy())
    scope_state, granted = google_scope_state()
    evidence.update({"granted_scopes": granted, "scope_state": scope_state})
    if scope_state in ("EXCESSIVE", "MISSING_DRIVE_FILE"):
        return Outcome("FAILED", f"Scope token Google {scope_state}; hanya drive.file yang diizinkan (S18).",
                       evidence=evidence, error_code="GOOGLE_SCOPE_NOT_LEAST_PRIVILEGE", remedy=google_connect_remedy())
    if not ctx.functional:
        return Outcome("CONFIGURED", f"{len(roots)} root Drive terkonfigurasi; akses root belum diprobe (gunakan "
                       "--functional).", evidence=evidence,
                       target_identity=", ".join(f"google-drive:{r['root_id']}" for r in roots)[:250])
    try:
        token = stored_google_token()
    except Exception as exc:
        return Outcome("UNKNOWN", f"Token Google tidak dapat dibaca ({type(exc).__name__}).", evidence=evidence,
                       error_code="GOOGLE_TOKEN_UNREADABLE")
    results: list[dict[str, Any]] = []
    for r in roots:
        try:
            hr = drive_get(ctx, str(r["root_id"]), token or {})
        except Exception as exc:  # refresh/network failure: never green
            results.append({"root_id": r["root_id"], "status": "UNKNOWN", "error": type(exc).__name__})
            continue
        item: dict[str, Any] = {"root_id": r["root_id"], "http_status": hr.status}
        body = hr.body if isinstance(hr.body, dict) else {}
        if hr.status == 200:
            ok_folder = body.get("mimeType") == FOLDER_MIME and not body.get("trashed")
            can_add = ((body.get("capabilities") or {}).get("canAddChildren")) is True
            item.update(status="CONNECTED" if ok_folder and can_add else "FAILED",
                        is_folder=body.get("mimeType") == FOLDER_MIME, trashed=bool(body.get("trashed")),
                        can_add_children=can_add, error=None if ok_folder and can_add else "DRIVE_ROOT_NOT_WRITABLE_FOLDER")
        elif hr.status == 401:
            item.update(status="EXPIRED", error="GOOGLE_TOKEN_REJECTED")
        elif hr.status in (403, 404):
            # drive.file only sees files the app created or the user opened with it (S18)
            item.update(status="FAILED", error="DRIVE_ROOT_NOT_VISIBLE")
        else:
            item.update(status="UNKNOWN", error=str(hr.error or hr.status))
        results.append(item)
    evidence["results"] = results
    statuses = {x["status"] for x in results}
    for bad, code in (("EXPIRED", "GOOGLE_TOKEN_REJECTED"), ("FAILED", "DRIVE_ROOT_NOT_VERIFIED"),
                      ("UNKNOWN", "DRIVE_PROBE_INCONCLUSIVE")):
        if bad in statuses:
            return Outcome(bad, "Root Drive proyek: " + ", ".join(f"{x['root_id']} {x['status']}" for x in results)
                           + " (drive.file hanya melihat folder yang dibuat/dibuka aplikasi).", evidence=evidence,
                           error_code=code, remedy=google_connect_remedy() if bad == "EXPIRED" else None)
    return Outcome("CONNECTED", f"{len(results)} root Drive proyek terbaca (folder, dapat menambah file; probe "
                   "read-only, bukan uji tulis).", evidence=evidence,
                   target_identity=", ".join(f"google-drive:{x['root_id']}" for x in results)[:250])


def project_office_targets(ctx: CheckContext) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from aeos.models.portfolio import ProjectIntegration

    projects = _projects(ctx)
    out: dict[str, dict[str, Any]] = {}
    assert ctx.session is not None
    for integ in ctx.session.scalars(select(ProjectIntegration).where(
            ProjectIntegration.provider.in_(OFFICE_TARGET_PROVIDERS))):
        p = projects.get(integ.project_id)
        if p is None or p.domain != Domain.OFFICE.value:
            continue
        t = out.setdefault(p.id, {"project_id": p.id, "domain": p.domain, "project_key": p.project_key,
                                  "targets": [], "invalidators": []})
        t["targets"].append({"provider": integ.provider, "purpose": integ.purpose, "root_id": integ.root_id,
                             "tenant_id": integ.tenant_id})
        t["invalidators"].append(f"integration:{integ.id}")
    return list(out.values())


@check("adapter.project_office_targets", "PROJECT_ADAPTERS",
       "Target OFFICE proyek: repo Bitbucket / project Jira / parent Confluence (per proyek)",
       component_id="integration-registry", ttl_seconds=3600, needs_db=True,
       invalidators=("config:office_policy", "integration:*", f"secret:{ATLASSIAN_REF}", f"secret:{BITBUCKET_REF}"),
       ok={"CONFIGURED", "CONNECTED", "VERIFIED"}, targets=project_office_targets,
       description="Per proyek OFFICE: identitas target (repo/project key/parent id + tenant). Tetap diblokir sampai "
                   "otorisasi perusahaan tercatat (§23); probe identitas Atlassian belum diimplementasikan (NOT-PROVEN).")
def adapter_project_office_targets(ctx: CheckContext) -> Outcome:
    from aeos.doctor.office import office_gate

    target = ctx.project or {}
    items = target.get("targets") or []
    evidence: dict[str, Any] = {"project_key": target.get("project_key"),
                                "targets": [{k: t.get(k) for k in ("provider", "purpose", "root_id", "tenant_id")}
                                            for t in items], "policy": "OFFICE default deny (§23)"}
    allowed, reason = office_gate("CAN_USE_OFFICE_INTEGRATIONS", ctx.session)
    evidence["office_gate"] = reason
    if not allowed:
        return Outcome("UNKNOWN", f"Target OFFICE tidak diprobe: {reason}.", evidence=evidence,
                       error_code="OFFICE_DEFAULT_DENY")
    missing = [t["provider"] for t in items if not t.get("root_id")]
    if missing:
        return Outcome("MISSING", f"Target OFFICE belum lengkap (root_id): {', '.join(missing)}.", evidence=evidence,
                       error_code="OFFICE_TARGET_MISSING")
    return Outcome("CONFIGURED", "Target OFFICE terkonfigurasi; identitas project/parent/repo belum diprobe oleh Doctor "
                   "(NOT-PROVEN, probe Atlassian belum diimplementasikan).", evidence=evidence)
