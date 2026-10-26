"""§08 Project adapters: GitHub repo, Google Drive/Docs scopes, Bitbucket/Jira/Confluence (OFFICE).

Credential presence is a status lookup. The optional GitHub probe is read-only, explicit
(``--functional``) and never logs the token. OFFICE adapters stay NOT_APPLICABLE until configured
and remain blocked by the OFFICE default-deny policy even when configured.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from aeos.core.enums import Domain
from aeos.core.timeutil import ensure_aware
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
GITHUB_EXPIRY_WARN_DAYS = 14  # warn this many days before the token expires
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


FINE_GRAINED_REMEDY = ("gunakan fine-grained PAT dengan 'Only select repositories' (repo platform + repo proyek "
                       "terdaftar saja): Issues RW, Pull requests RW, Contents RW, Metadata R; tanpa Administration/"
                       "Workflows")


ADMINISTRATION_GRANTED, ADMINISTRATION_NOT_GRANTED = "GRANTED", "NOT_GRANTED"
ADMINISTRATION_UNKNOWN, ADMINISTRATION_NOT_POSSIBLE = "UNKNOWN", "NOT_POSSIBLE"


def _fine_grained_privilege(permissions: dict[str, Any] | None, reachable: list[str] | None,
                            allowed_repos: list[str] | None, truncated: bool,
                            administration: str | None = None, public: list[str] | None = None) -> dict[str, Any]:
    """A fine-grained PAT is only least privilege when what it can actually reach is observed: no repository
    beyond the ones AEOS registered (a token created for "All repositories" lists them all) and no Administration
    permission on the token itself.

    ``permissions.admin`` of ``GET /repos/{repo}`` is what GitHub reports for the token's *account* on the
    repository: a token never exceeds it, but the owner of a personal repository always shows ``admin`` while their
    token may carry no Administration at all. So it is never read as the token's grant: with an admin account role
    the token's Administration permission must be observed (``administration``: GRANTED / NOT_GRANTED, see
    ``_administration_grant``) or least privilege stays NOT proven (None, never True and never a false "has
    Administration" claim). The Workflows permission is not observable through the API.

    GitHub gives every fine-grained PAT read-only access to all public repositories ("Also includes public
    repositories (read-only)"), so ``GET /user/repos`` lists them even for "Only select repositories". An unregistered
    *public* repository is therefore no breach, but it also does not prove the selection: least privilege stays NOT
    proven (None). Only an unregistered private repository shows a broader selection."""
    allowed = {a.lower() for a in allowed_repos or []}
    admin_role = bool((permissions or {}).get("admin"))
    grant = (administration or ADMINISTRATION_UNKNOWN) if admin_role else ADMINISTRATION_NOT_POSSIBLE
    problems: list[str] = []
    if grant == ADMINISTRATION_GRANTED:
        problems.append("token membawa izin Administration pada repo platform (GET branch protection diizinkan)")
    public_names = {p.lower() for p in public or []}
    extra = sorted(r for r in reachable or [] if r.lower() not in allowed)
    extra_private = [r for r in extra if r.lower() not in public_names]
    extra_public = [r for r in extra if r.lower() in public_names]
    if extra_private:
        problems.append(f"token menjangkau {len(extra_private)} repo private di luar repo terdaftar AEOS (pemilihan "
                        "'All repositories'?): " + ", ".join(extra_private[:5]) + (" ..." if len(extra_private) > 5 else ""))
    out: dict[str, Any] = {"kind": "fine-grained", "scopes": [], "reachable_repositories": len(reachable or []),
                           "beyond_registered": extra_private[:20], "beyond_registered_public": extra_public[:20],
                           "admin": admin_role, "administration_permission": grant,
                           "workflows_permission": "NOT_OBSERVABLE"}
    if problems:
        return {**out, "least_privilege": False,
                "risk": "Fine-grained PAT melebihi least privilege: " + "; ".join(problems) + " - " + FINE_GRAINED_REMEDY}
    if reachable is None or truncated:
        return {**out, "least_privilege": None,
                "note": "pemilihan repo token belum terbaca (GET /user/repos gagal atau terpotong) - least privilege "
                        "belum terbukti"}
    if extra_public:
        return {**out, "least_privilege": None,
                "note": f"{len(extra_public)} repo public di luar repo terdaftar terbaca (akses baca public yang GitHub "
                        "berikan ke setiap fine-grained PAT); tidak ada repo private di luar daftar, tetapi pemilihan "
                        "'Only select repositories' tidak dapat dibuktikan dari daftar ini"}
    if grant == ADMINISTRATION_UNKNOWN:
        return {**out, "least_privilege": None,
                "note": "akun pemilik token berperan admin pada repo; izin Administration token itu sendiri tidak "
                        "teramati (probe branch protection tidak konklusif) - least privilege belum terbukti"}
    return {**out, "least_privilege": True,
            "note": "fine-grained PAT: hanya repo terdaftar, tanpa izin Administration (izin Workflows tidak terlihat "
                    "lewat API)"}


def github_token_privilege(kind: str, scopes_header: str | None, *, permissions: dict[str, Any] | None = None,
                           reachable: list[str] | None = None, allowed_repos: list[str] | None = None,
                           truncated: bool = False, administration: str | None = None,
                           public: list[str] | None = None) -> dict[str, Any]:
    """Least-privilege verdict from the token kind and GitHub's ``X-OAuth-Scopes`` header (classic tokens) or,
    for a fine-grained PAT, from what it observably reaches (``reachable`` repositories vs the registered ones, the
    observed ``administration`` grant). A classic/OAuth token (e.g. ``gh auth token``) with ``repo`` reaches every
    repository of the account — a residual risk the owner sees."""
    scopes = sorted({s.strip() for s in (scopes_header or "").split(",") if s.strip()})
    if kind == "fine-grained":
        return _fine_grained_privilege(permissions, reachable, allowed_repos, truncated, administration, public)
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


def _record_probe(ctx: CheckContext, providers: tuple[str, ...], root_id: str, connectivity: str) -> None:
    """§10 Integration Registry health: the probed exact target's integrations learn the result (best effort;
    a read-only probe never claims functional VERIFIED)."""
    if ctx.session is None:
        return
    try:
        from aeos.portfolio import facade as portfolio

        with ctx.session.begin_nested():
            portfolio.record_integration_probe(ctx.session, domain="PERSONAL", providers=providers, root_id=root_id,
                                               connectivity=connectivity, observer="doctor:adapter.github_token")
    except Exception as exc:  # the check result itself must never depend on the registry write
        import logging

        logging.getLogger("aeos.doctor").warning("integration probe not recorded: %s", type(exc).__name__)


REACHABLE_PAGE = 100


def _reachable_repos(ctx: CheckContext) -> tuple[list[str] | None, bool, list[str]]:
    """Repositories the stored token can reach (read-only ``GET /user/repos``: a fine-grained PAT lists its selected
    repositories plus every public one), whether a full page came back (truncated), and which of them are public.
    (None, False, []) when unreadable."""
    try:
        hr = github_get(ctx, f"/user/repos?per_page={REACHABLE_PAGE}")
    except Exception:
        return None, False, []
    if getattr(hr, "status", None) != 200 or not isinstance(getattr(hr, "body", None), list):
        return None, False, []
    repos = [r for r in hr.body if isinstance(r, dict) and r.get("full_name")]
    public = [str(r["full_name"]) for r in repos if r.get("private") is False]
    return [str(r["full_name"]) for r in repos], len(hr.body) >= REACHABLE_PAGE, public


def _administration_grant(ctx: CheckContext, slug: str, branch: str) -> str:
    """Whether the fine-grained token itself carries the repository Administration permission (read-only probe).

    ``GET /repos/{repo}/branches/{branch}/protection`` needs Administration (read). A fine-grained token without it
    is refused with 403 "Resource not accessible by personal access token"; a token with it gets 200 (protected) or
    404 "Branch not protected". Anything else (plan limits, rate limit, network) is UNKNOWN - never a pass."""
    from urllib.parse import quote

    try:
        hr = github_get(ctx, f"/repos/{slug}/branches/{quote(branch or 'main', safe='')}/protection")
    except Exception:
        return ADMINISTRATION_UNKNOWN
    status = getattr(hr, "status", None)
    body = getattr(hr, "body", None)
    message = str(body.get("message") or "").lower() if isinstance(body, dict) else ""
    if status == 403 and "not accessible by personal access token" in message:
        return ADMINISTRATION_NOT_GRANTED
    if status == 200 or (status == 404 and "branch not protected" in message):
        return ADMINISTRATION_GRANTED
    return ADMINISTRATION_UNKNOWN


def _registered_github_repos(ctx: CheckContext) -> list[str]:
    """GitHub repositories registered for PERSONAL projects (the token may legitimately reach those)."""
    if ctx.session is None:
        return []
    try:
        from sqlalchemy import select

        from aeos.models.portfolio import RepositoryReference

        return [r.full_name for r in ctx.session.scalars(select(RepositoryReference))
                if (r.provider or "").upper() == "GITHUB"]
    except Exception:
        return []


def _github_token_expiry(headers: dict[str, str] | None) -> datetime | None:
    """Expiry GitHub reports for the token (``GitHub-Authentication-Token-Expiration``, ``2027-01-24 10:57:27 UTC``
    or ``... +0700``); None when the header is absent (no expiry) or unreadable."""
    raw = (headers or {}).get("github-authentication-token-expiration")
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip().replace(" UTC", " +0000"), "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None


def _github_expiry_warning(expires: datetime | None, now: datetime) -> str | None:
    """Text for a token that expires within GITHUB_EXPIRY_WARN_DAYS; None otherwise."""
    if expires is None:
        return None
    days = (expires - ensure_aware(now)).total_seconds() / 86400
    if days > GITHUB_EXPIRY_WARN_DAYS:
        return None
    return (f"Token GitHub kedaluwarsa {expires.date().isoformat()} ({max(0, int(days))} hari lagi): buat token baru "
            f"dengan izin yang sama, lalu {aeos_cmd(f'secrets set {GITHUB_REF} --expires-at <YYYY-MM-DD>')}.")


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
    recorded = evidence.get("expires_at")
    recorded_expiry = ensure_aware(datetime.fromisoformat(recorded)) if isinstance(recorded, str) else None
    if not ctx.functional:
        summary = f"Token {GITHUB_REF} tersimpan; akses repo belum diprobe (gunakan --functional)."
        warning = _github_expiry_warning(recorded_expiry, ctx.now)
        return Outcome("CONFIGURED", summary + (" " + warning if warning else ""), evidence=evidence,
                       target_identity=f"github:{slug}",
                       remedy=set_secret_remedy(GITHUB_REF, "GitHub (token baru sebelum kedaluwarsa)") if warning else None)
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
                         "repo_id": hr.body.get("id"),
                         "token_scopes": _scopes(getattr(hr, "headers", None))})
        reported_expiry = _github_token_expiry(getattr(hr, "headers", None))
        evidence["token_expires_at"] = reported_expiry.isoformat() if reported_expiry else None
        expiry = min((e for e in (reported_expiry, recorded_expiry) if e is not None), default=None)
        kind = _token_kind()
        reachable, truncated, public = _reachable_repos(ctx) if kind == "fine-grained" else (None, False, [])
        # the account's admin role does not show the token's own grant: observe it (fine-grained only)
        administration = (_administration_grant(ctx, slug, str(hr.body.get("default_branch") or "main"))
                          if kind == "fine-grained" and isinstance(perms, dict) and perms.get("admin") else None)
        privilege = github_token_privilege(kind, (getattr(hr, "headers", None) or {}).get("x-oauth-scopes"),
                                           permissions=perms if isinstance(perms, dict) else {}, reachable=reachable,
                                           allowed_repos=[slug, *_registered_github_repos(ctx)], truncated=truncated,
                                           administration=administration, public=public)
        evidence["token_privilege"] = privilege
        summary = f"Repo github:{slug} terbaca dengan token (read-only); izin tulis/enforcement belum diverifikasi."
        warning = _github_expiry_warning(expiry, ctx.now)
        if warning:
            evidence["residual_risks"] = [warning]
            summary = warning + " " + summary
        if privilege["least_privilege"] is False:
            evidence.setdefault("residual_risks", []).append(privilege["risk"])
            summary += " " + privilege["risk"]
        elif privilege["least_privilege"] is None:
            summary += " Least privilege token belum terbukti: " + str(privilege.get("note") or "")
        _record_probe(ctx, ("github-repo", "github-issues"), slug, "CONNECTED")
        remedy = None
        if privilege["least_privilege"] is False or warning:
            remedy = set_secret_remedy(GITHUB_REF, "GitHub fine-grained PAT, hanya repo platform: Issues RW, Pull "
                                                   "requests RW, Contents RW (push aeos/*), Metadata R")
        return Outcome("CONNECTED", summary, evidence=evidence, target_identity=f"github:{slug}", remedy=remedy)
    if hr.status == 401:
        _record_probe(ctx, ("github-repo", "github-issues"), slug, "EXPIRED")
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
        # config holds no secret (Integration Registry rule); it carries deployment/space_id/enabled
        t["targets"].append({"provider": integ.provider, "purpose": integ.purpose, "root_id": integ.root_id,
                             "tenant_id": integ.tenant_id, "config": dict(integ.config or {})})
        t["invalidators"].append(f"integration:{integ.id}")
    return list(out.values())


OFFICE_PURPOSE = "office:doctor.atlassian-readonly-probe"


def _office_auth(ref: str) -> str:
    """``Authorization`` value from the stored OFFICE credential, registered for redaction; read only after the
    company authorisation gate opened. ``office/bitbucket`` is built exactly like the broker and the publisher do
    (``bitbucket_auth_header``: a repository/workspace access token -> Bearer, email + API token or the legacy
    username + app password -> Basic); ``office/atlassian`` is Basic email/username:API token."""
    import base64
    import json

    from aeos.core.redaction import register_secret
    from aeos.secrets import facade as secrets

    raw = secrets.get_secret(ref, purpose=OFFICE_PURPOSE, actor="doctor")
    if ref == BITBUCKET_REF:
        from aeos.publisher.providers.atlassian import bitbucket_auth_header

        return bitbucket_auth_header(raw)
    try:
        data = json.loads(raw)
        user = str(data.get("email") or data.get("username") or "")
        token = str(data.get("api_token") or data.get("app_password") or data.get("token") or "")
    except (ValueError, AttributeError):
        user, _, token = raw.partition(":")
    if not user or not token:
        raise ValueError(f"credential {ref} is incomplete")
    register_secret(token)
    encoded = base64.b64encode(f"{user}:{token}".encode()).decode()
    register_secret(encoded)
    return "Basic " + encoded


def _http_verdict(status: int | None, error: Any, what: str) -> tuple[str, str] | None:
    """Common HTTP outcome of a read-only identity probe; None = 200 (the caller checks the identity)."""
    if status == 200:
        return None
    if status == 401:
        return "EXPIRED", "OFFICE_CREDENTIAL_REJECTED"
    if status in (403, 404):
        return "FAILED", f"{what}_NOT_VISIBLE"
    return "UNKNOWN", str(error or status or "NO_RESPONSE")


def _target_authorisation(ctx: CheckContext, t: dict[str, Any], pol: dict[str, Any] | None) -> tuple[bool, str]:
    """§01 U-03 / §07 step 10: is the configured target one the company approved (``approved_targets``)?"""
    from aeos.policy import facade as policy

    if ctx.session is None:
        return False, "no database session: the company authorisation cannot be read (fail closed)"
    cfg = t.get("config") or {}
    return policy.office_target_authorised(ctx.session, str(t.get("provider") or ""), root_id=t.get("root_id"),
                                           tenant=t.get("tenant_id") or cfg.get("base_url"),
                                           space_id=cfg.get("space_id"), policy=pol)


def _confluence_probe(ctx: CheckContext, item: dict[str, Any], base_url: str, space_id: str, parent_id: str,
                      deployment: str, headers: dict[str, str]) -> dict[str, Any]:
    """The publisher's own capability rule (``confluence_capabilities``) over the Doctor's read-only runner: the
    space (REST v2), then the parent folder (page fallback). READY -> CONNECTED; a page parent, a tenant without
    folder support or a parent in another space -> FAILED (needs action) - never green for a target the
    publisher refuses (§50 T1 #1.19)."""
    from aeos.publisher.providers.atlassian import confluence_capabilities

    calls: list[tuple[int | None, Any]] = []

    def get(path: str, _what: str) -> tuple[int | None, Any]:
        hr = ctx.runner.http_get(f"{base_url}/wiki/api/v2{path}", timeout=10.0, headers=headers, allow_remote=True)
        calls.append((hr.status, hr.error))
        return hr.status, hr.body

    caps = confluence_capabilities(get, space_id=space_id, parent_id=parent_id, deployment=deployment)
    item.update(folders=caps["folders"], api_v2=caps["api_v2"], parent_kind=caps.get("parent_kind"),
                observed_space=caps.get("parent_space"), capability_reasons=list(caps["reasons"]),
                http_status=calls[-1][0] if calls else None)
    if caps["status"] == "READY":
        return {**item, "status": "CONNECTED", "needs_action": False}
    if 401 in [s for s, _ in calls]:
        return {**item, "status": "EXPIRED", "error": "OFFICE_CREDENTIAL_REJECTED"}
    http = caps.get("http") or {}
    space_status = http.get("space")
    if space_status is None:
        return {**item, "status": "UNKNOWN", "error": str(calls[0][1] if calls else "NO_RESPONSE")}
    if space_status != 200:
        if space_status in (403, 404):
            return {**item, "status": "FAILED", "error": "CONFLUENCE_SPACE_NOT_VISIBLE"}
        return {**item, "status": "UNKNOWN", "error": f"HTTP_{space_status}"}
    if caps.get("space_mismatch"):
        return {**item, "status": "FAILED", "error": "CONFLUENCE_PARENT_OTHER_SPACE", "needs_action": True}
    if caps["parent_kind"] == "page" or caps["folders"] == "NOT_SUPPORTED":
        # §50 T1 #1.19: the tenant/parent cannot hold a project folder -> needs action; the publisher refuses it
        return {**item, "status": "FAILED", "error": "CONFLUENCE_FOLDERS_UNSUPPORTED", "needs_action": True}
    folder_status, page_status = http.get("folder"), http.get("page", "not-read")
    if folder_status is None or page_status is None:
        return {**item, "status": "UNKNOWN", "error": "NO_RESPONSE"}
    if folder_status == 403 or page_status in (403, 404):
        return {**item, "status": "FAILED", "error": "CONFLUENCE_PARENT_NOT_VISIBLE"}
    return {**item, "status": "UNKNOWN", "error": f"HTTP_{folder_status}"}


def _probe_office_target(ctx: CheckContext, t: dict[str, Any]) -> dict[str, Any]:
    """Read-only GETs that prove the exact OFFICE target exists and is usable with the stored credential: Jira
    project key, Confluence space + parent folder (the publisher's capability rule), Bitbucket workspace/repo.
    Never a write."""
    from urllib.parse import quote

    from aeos.publisher.providers.atlassian import BITBUCKET_API, AtlassianConfig

    provider, root = str(t.get("provider") or ""), str(t.get("root_id") or "")
    item: dict[str, Any] = {"provider": provider, "root_id": root}
    cfg: AtlassianConfig | None = None
    if provider == pv.BITBUCKET:
        if "/" not in root:
            return {**item, "status": "FAILED", "error": "BITBUCKET_REPO_INVALID"}
        ws, slug = root.split("/", 1)
        url, ref = f"{BITBUCKET_API}/repositories/{quote(ws, safe='')}/{quote(slug, safe='')}", BITBUCKET_REF
    else:
        product = "jira" if provider == pv.JIRA else "confluence"
        cfg = AtlassianConfig.from_integration(t.get("tenant_id"), root, t.get("config"), product=product)
        if cfg.deployment == "DATA_CENTER":
            return {**item, "status": "FAILED", "error": "ATLASSIAN_DC_NOT_SUPPORTED"}
        if not cfg.base_url.startswith("https://"):
            return {**item, "status": "FAILED", "error": "ATLASSIAN_TENANT_MISSING"}
        item["tenant"] = cfg.base_url
        if product == "jira":
            url = f"{cfg.base_url}/rest/api/3/project/{quote(cfg.project_key, safe='')}"
        else:
            item["space_id"] = cfg.space_id or None
            if not cfg.space_id:
                # the publisher reads GET /wiki/api/v2/spaces/<numeric id> first: without it nothing can be proven
                return {**item, "status": "FAILED", "error": "CONFLUENCE_SPACE_MISSING"}
            url = f"{cfg.base_url}/wiki/api/v2/spaces/{quote(cfg.space_id, safe='')}"
        ref = ATLASSIAN_REF
    try:
        auth = _office_auth(ref)
    except Exception as exc:  # missing/incomplete credential: never green
        return {**item, "status": "MISSING", "error": "OFFICE_CREDENTIAL_MISSING", "detail": type(exc).__name__}
    headers = {"Authorization": auth, "Accept": "application/json", "User-Agent": "dly-aeos-doctor"}
    if provider == pv.CONFLUENCE and cfg is not None:
        try:
            return _confluence_probe(ctx, item, cfg.base_url, cfg.space_id, cfg.parent_id, cfg.deployment, headers)
        except Exception as exc:  # never green
            return {**item, "status": "UNKNOWN", "error": type(exc).__name__}
    try:
        hr = ctx.runner.http_get(url, timeout=10.0, headers=headers, allow_remote=True)
    except Exception as exc:  # never green
        return {**item, "status": "UNKNOWN", "error": type(exc).__name__}
    item["http_status"] = hr.status
    verdict = _http_verdict(hr.status, hr.error, provider.upper())
    if verdict is not None:
        return {**item, "status": verdict[0], "error": verdict[1]}
    body = hr.body if isinstance(hr.body, dict) else {}
    if provider == pv.JIRA:
        observed = str(body.get("key") or "")
        if observed.upper() != root.upper():
            return {**item, "status": "FAILED", "error": "JIRA_PROJECT_MISMATCH", "observed": observed}
        return {**item, "status": "CONNECTED", "project_id": str(body.get("id") or ""), "name": body.get("name")}
    observed = str(body.get("full_name") or "")
    if observed.lower() != root.lower():
        return {**item, "status": "FAILED", "error": "BITBUCKET_REPO_MISMATCH", "observed": observed}
    return {**item, "status": "CONNECTED", "uuid": body.get("uuid"), "is_private": body.get("is_private")}


@check("adapter.project_office_targets", "PROJECT_ADAPTERS",
       "Target OFFICE proyek: repo Bitbucket / project Jira / parent Confluence (per proyek)",
       component_id="integration-registry", ttl_seconds=3600, needs_db=True,
       invalidators=("config:office_policy", "integration:*", f"secret:{ATLASSIAN_REF}", f"secret:{BITBUCKET_REF}"),
       ok={"CONFIGURED", "CONNECTED", "VERIFIED"}, targets=project_office_targets,
       description="Per proyek OFFICE: identitas target (repo/project key/parent id + tenant). Tetap diblokir sampai "
                   "otorisasi perusahaan tercatat (§23); target di luar approved_targets otorisasi perusahaan GAGAL "
                   "tanpa probe. Dengan --functional GET read-only per target (Jira project; space + folder parent "
                   "Confluence dengan aturan kapabilitas publisher yang sama; repo Bitbucket) membuktikan identitasnya.")
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
    # §01 U-03 / §07 step 10: the configured tenant/target must be one the company approved - checked before any
    # credential is read or request sent; a target outside approved_targets FAILS (never probed, never green)
    pol: dict[str, Any] | None = None
    try:
        from aeos.policy import facade as policy

        if ctx.session is not None:
            pol = policy.office_policy(ctx.session)
    except ImportError:
        pol = None
    if pol is None:
        # the company-approved targets cannot be read: never green, and not claimed as "outside" either
        return Outcome("UNKNOWN", "Target yang disetujui perusahaan tidak dapat dibaca (modul policy atau database "
                       "tidak tersedia); tidak ada yang diprobe (fail closed).", evidence=evidence,
                       error_code="POLICY_UNAVAILABLE")
    scoped = [t for t in items if t.get("provider") in (pv.CONFLUENCE, pv.JIRA, pv.BITBUCKET)]
    outside = []
    for t in scoped:
        ok, why = _target_authorisation(ctx, t, pol)
        if not ok:
            outside.append({"provider": t.get("provider"), "root_id": t.get("root_id"), "reason": why})
    evidence["company_approved_targets"] = {"approved": len(pol.get("approved_targets") or []),
                                            "outside": outside}
    if outside:
        return Outcome("FAILED", "Target OFFICE di luar target yang disetujui perusahaan (approved_targets): "
                       + "; ".join(f"{o['provider']}:{o['root_id']}" for o in outside) + ". Tidak ada yang diprobe.",
                       evidence=evidence, error_code="OFFICE_TARGET_NOT_AUTHORISED",
                       remedy=Remedy(operation="Catat target yang disetujui perusahaan pada keputusan otorisasi "
                                     "perusahaan (Keputusan -> POLICY -> otorisasi perusahaan OFFICE), atau perbaiki "
                                     "target integrasi proyek", command=aeos_cmd(
                                         "policy decide --kind POLICY --domain OFFICE --office-authorization "
                                         "--approved-target <provider>:<key>=<value> ..."),
                                     privilege="SUPER_ADMIN (keputusan manusia)",
                                     evidence_to_collect="adapter.project_office_targets bukan FAILED"))
    if not ctx.functional:
        return Outcome("CONFIGURED", "Target OFFICE terkonfigurasi; identitas project/parent/repo belum diprobe "
                       "(gunakan --functional: GET read-only per target).", evidence=evidence)
    results = [_probe_office_target(ctx, t) for t in items]
    evidence["results"] = results
    statuses = {x["status"] for x in results}
    line = ", ".join(f"{x['provider']}:{x['root_id']} {x['status']}" for x in results)
    for bad, code in (("EXPIRED", "OFFICE_CREDENTIAL_REJECTED"), ("MISSING", "OFFICE_CREDENTIAL_MISSING"),
                      ("FAILED", "OFFICE_TARGET_NOT_VERIFIED"), ("UNKNOWN", "OFFICE_PROBE_INCONCLUSIVE")):
        if bad in statuses:
            return Outcome(bad, f"Target OFFICE: {line}.", evidence=evidence, error_code=code)
    return Outcome("CONNECTED", f"{len(results)} target OFFICE terbaca read-only dan identitasnya cocok ({line}); "
                   "izin tulis tidak diuji.", evidence=evidence,
                   target_identity=", ".join(f"{x['provider']}:{x['root_id']}" for x in results)[:250])


# ---------------------------------------------------------------- OFFICE credential identity (§04, master table §03)
#
# "Credential OFFICE: namespace office dan identity yang disetujui kantor": the company authorisation may record the
# identity each delegated office/ credential must act as (approved_identities). With --functional the Doctor asks the
# provider who the stored credential is (one read-only GET) and compares; a different account FAILS.

BITBUCKET_USER_URL = "https://api.bitbucket.org/2.0/user"
# Only identifiers that name exactly one account. A Bitbucket nickname is not unique (a personal account often
# carries the same nickname as the company-issued one, which is precisely the mix-up this check must catch), and the
# username field is no longer returned by the Bitbucket Cloud API: neither is compared.
_IDENTITY_FIELDS = {ATLASSIAN_REF: ("accountId", "emailAddress"),
                    BITBUCKET_REF: ("uuid", "account_id")}


def _mask(value: str) -> str:
    v = str(value or "")
    if "@" in v:
        user, _, host = v.partition("@")
        return (user[:2] + "***@" + host) if user else "***@" + host
    return v if len(v) <= 8 else v[:4] + "..." + v[-4:]


def _identity_matches(ref: str, approved: str, body: dict[str, Any]) -> tuple[bool, list[str]]:
    want = approved.strip().lower().strip("{}")
    seen: list[str] = []
    for f in _IDENTITY_FIELDS[ref]:
        v = body.get(f)
        if isinstance(v, str) and v.strip():
            seen.append(f)
            if v.strip().lower().strip("{}") == want:
                return True, [f]
    return False, seen


def _atlassian_tenant(pol: dict[str, Any]) -> str | None:
    for t in pol.get("approved_targets") or []:
        if isinstance(t, dict) and t.get("provider") in (pv.CONFLUENCE, pv.JIRA) and t.get("tenant"):
            return f"https://{t['tenant']}"
    return None


def _probe_identity(ctx: CheckContext, ref: str, approved: str, pol: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {"credential_ref": ref, "approved": _mask(approved)}
    if ref == ATLASSIAN_REF:
        tenant = _atlassian_tenant(pol)
        if tenant is None:
            return {**item, "status": "UNKNOWN", "error": "NO_APPROVED_ATLASSIAN_TENANT"}
        url = f"{tenant}/rest/api/3/myself"
    else:
        url = BITBUCKET_USER_URL
    item["url"] = url
    try:
        auth = _office_auth(ref)
    except Exception as exc:  # missing/incomplete credential: never green
        return {**item, "status": "MISSING", "error": "OFFICE_CREDENTIAL_MISSING", "detail": type(exc).__name__}
    try:
        hr = ctx.runner.http_get(url, timeout=10.0, allow_remote=True,
                                 headers={"Authorization": auth, "Accept": "application/json",
                                          "User-Agent": "dly-aeos-doctor"})
    except Exception as exc:  # never green
        return {**item, "status": "UNKNOWN", "error": type(exc).__name__}
    item["http_status"] = hr.status
    if hr.status == 401:
        return {**item, "status": "EXPIRED", "error": "OFFICE_CREDENTIAL_REJECTED"}
    if hr.status != 200 or not isinstance(hr.body, dict):
        # e.g. a repository access token has no user: the identity is not observable, which is never a pass
        return {**item, "status": "UNKNOWN", "error": "IDENTITY_NOT_OBSERVABLE" if hr.status in (403, 404)
                else str(hr.error or hr.status or "NO_RESPONSE")}
    ok, fields = _identity_matches(ref, approved, hr.body)
    if ok:
        return {**item, "status": "VERIFIED", "matched_field": fields[0]}
    observed = next((hr.body.get(f) for f in _IDENTITY_FIELDS[ref] if isinstance(hr.body.get(f), str)), "")
    return {**item, "status": "FAILED", "error": "OFFICE_IDENTITY_MISMATCH", "observed": _mask(str(observed)),
            "compared_fields": fields}


@check("secret.office_identity", "SECRET", "Identitas credential OFFICE = identitas yang disetujui perusahaan",
       component_id="secret-provider", ttl_seconds=3600, needs_db=True, level="E1", functional_level="E3",
       invalidators=("config:office_policy", f"secret:{ATLASSIAN_REF}", f"secret:{BITBUCKET_REF}"),
       ok={"VERIFIED", "CONFIGURED", "NOT_APPLICABLE"}, scope={"domain": "OFFICE"},
       description="Otorisasi perusahaan dapat mencatat identitas yang disetujui per credential office/ "
                   "(approved_identities). Dengan --functional satu GET read-only (Atlassian /rest/api/3/myself pada "
                   "tenant yang disetujui, Bitbucket /2.0/user) membandingkan akun credential tersimpan; akun lain "
                   "GAGAL. Tanpa otorisasi: NOT_APPLICABLE; identitas belum diputuskan: UNKNOWN (tidak pernah hijau).")
def secret_office_identity(ctx: CheckContext) -> Outcome:
    from aeos.doctor.office import office_gate

    allowed, reason = office_gate("CAN_USE_OFFICE_INTEGRATIONS", ctx.session)
    evidence: dict[str, Any] = {"office_gate": reason, "policy": "OFFICE default deny (§23)"}
    if not allowed:
        return Outcome("NOT_APPLICABLE", f"Tidak ada otorisasi perusahaan OFFICE yang tercatat; identitas credential "
                       f"tidak diperiksa ({reason}).", evidence=evidence)
    try:
        from aeos.policy import facade as policy

        if ctx.session is None:
            raise ImportError("no database session")
        pol = policy.office_policy(ctx.session)
    except ImportError:
        return Outcome("UNKNOWN", "Modul policy atau database tidak tersedia (fail closed).", evidence=evidence,
                       error_code="POLICY_UNAVAILABLE")
    approved: dict[str, str] = dict(pol.get("approved_identities") or {})
    evidence["approved"] = {ref: _mask(v) for ref, v in approved.items()}
    if not approved:
        return Outcome("UNKNOWN", "Identitas yang disetujui perusahaan untuk credential office/ belum dicatat "
                       "(approved_identities UNDECIDED); akun credential tidak dapat dibuktikan.", evidence=evidence,
                       error_code="OFFICE_IDENTITY_UNDECIDED", remedy=Remedy(
                           operation="Catat identitas yang diterbitkan perusahaan pada keputusan otorisasi perusahaan",
                           command=aeos_cmd("policy decide --kind POLICY --domain OFFICE --office-authorization "
                                            "--approved-identity office/atlassian=<account id/email> ..."),
                           privilege="SUPER_ADMIN (keputusan manusia)",
                           evidence_to_collect="secret.office_identity VERIFIED"))
    if not ctx.functional:
        missing = [ref for ref in approved if secret_ref_status(ref)[0] != "PRESENT"]
        if missing:
            return Outcome("MISSING", f"Credential belum disimpan: {', '.join(missing)}.", evidence=evidence,
                           error_code="OFFICE_CREDENTIAL_MISSING",
                           remedy=set_secret_remedy(missing[0], "OFFICE yang diterbitkan perusahaan"))
        return Outcome("CONFIGURED", "Identitas yang disetujui tercatat dan credential tersimpan; akun belum "
                       "dibandingkan (gunakan --functional: satu GET read-only per credential).", evidence=evidence)
    results = [_probe_identity(ctx, ref, value, pol) for ref, value in approved.items()]
    evidence["results"] = results
    statuses = {r["status"] for r in results}
    line = ", ".join(f"{r['credential_ref']} {r['status']}" for r in results)
    for bad, code in (("FAILED", "OFFICE_IDENTITY_MISMATCH"), ("EXPIRED", "OFFICE_CREDENTIAL_REJECTED"),
                      ("MISSING", "OFFICE_CREDENTIAL_MISSING"), ("UNKNOWN", "OFFICE_IDENTITY_INCONCLUSIVE")):
        if bad in statuses:
            return Outcome(bad, f"Identitas credential OFFICE: {line}.", evidence=evidence, error_code=code)
    return Outcome("VERIFIED", f"Credential OFFICE bertindak sebagai identitas yang disetujui perusahaan ({line}).",
                   evidence=evidence, target_identity=", ".join(approved)[:250])
