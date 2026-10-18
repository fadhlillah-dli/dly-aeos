"""Secret status/health API (§26, §35 Integrations & Access). Read-only: there is deliberately NO
secret write endpoint in the web API (V1 values are entered via the local CLI only)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.api.deps import DB, CurrentPrincipal, SettingsDep
from aeos.core.config import Settings
from aeos.core.enums import Domain
from aeos.core.timeutil import utcnow
from aeos.identity.service import Principal
from aeos.models.cost import QuotaState
from aeos.secrets.health import secrets_status, store_health

router = APIRouter(tags=["secrets"])


def _active(principal: Principal) -> str:
    """Active domain for project-ref visibility; no selection -> only non-project refs are shown."""
    d = principal.active_domain
    return d.value if d is not None and principal.can_access_domain(d) else "NONE"


def _office_visible(principal: Principal) -> bool:
    """OFFICE metadata (even existence of an office credential) only inside an OFFICE session."""
    return principal.active_domain == Domain.OFFICE and principal.can_access_domain(Domain.OFFICE)


@router.get("/api/secrets/status")
def get_secrets_status(settings: SettingsDep, principal: CurrentPrincipal) -> dict[str, Any]:
    return secrets_status(settings, include_office=_office_visible(principal), domain=_active(principal))


@router.get("/api/secrets/health")
def get_secrets_health(settings: SettingsDep, _p: CurrentPrincipal) -> dict[str, Any]:
    return store_health(settings)


def _ref_rows(status: dict[str, Any], refs: list[str]) -> list[dict[str, Any]]:
    by_ref = {i["ref"]: i for i in status["items"]}
    return [
        {k: by_ref[r].get(k) for k in ("ref", "state", "backend", "updated_at", "expires_at", "scope")}
        if r in by_ref else {"ref": r, "state": "HIDDEN"}
        for r in refs
    ]


def _cred_status(rows: list[dict[str, Any]]) -> str:
    states = {r["state"] for r in rows}
    if "PRESENT" in states:
        return "CONFIGURED"  # a stored credential is configuration, not a functional proof
    if "EXPIRED" in states:
        return "EXPIRED"
    if states <= {"UNKNOWN", "HIDDEN", "NO_ACCESS"}:
        return "UNKNOWN"
    return "MISSING"


def _openrouter_quota(db: Session) -> dict[str, Any] | None:
    rows = {q.scope: q for q in db.scalars(select(QuotaState).where(QuotaState.provider == "openrouter"))}
    credits = rows.get("credits")
    if credits is None:
        return None
    return {"state": credits.state, "source": credits.source,
            "observed_at": credits.observed_at.isoformat() if credits.observed_at else None,
            "details": {k: v for k, v in (credits.details or {}).items()
                        if k in {"limit", "limit_remaining", "usage", "is_free_tier", "free_model_daily_limit_documented",
                                 "reason", "secret_ref"}},
            "free_daily": rows["free-daily"].state if "free-daily" in rows else "UNKNOWN"}


def integrations_overview(db: Session, settings: Settings, principal: Principal) -> dict[str, Any]:
    from aeos.claude_bridge import bridge
    from aeos.claude_bridge.router import mask_account

    office = _office_visible(principal)
    st = secrets_status(settings, include_office=office, domain=_active(principal))
    health = store_health(settings)
    claude = bridge.status(db, settings)
    test = claude.get("last_capability_test") or {}
    claude_status = ("BLOCKED" if claude["bridge_status"] == "BLOCKED"
                     else (test.get("status") or "NOT_TESTED"))
    cards: list[dict[str, Any]] = [
        {
            "id": "claude-native", "name": "Claude Code resmi (pengawas Max)", "domain": "PERSONAL",
            "status": claude_status,
            "capability": "Perencanaan/review terstruktur lewat binary resmi, login claude.ai (tanpa API)",
            "scope": "Workspace supervisor terpisah; alat baca saja (Read/Grep/Glob); OFFICE ditolak tanpa otorisasi",
            "account": {k: mask_account(claude["auth"], principal).get(k)
                        for k in ("authMethod", "subscriptionType", "apiProvider", "email")},
            "credential_refs": [], "credential_note": "Kredensial native dikelola binary resmi; tidak disimpan di vault.",
            "expiry": None, "proof_time": test.get("checked_at"), "proof_source": "uji kemampuan bridge (E2)",
            "remedies": [{"command": "claude", "why": "Login resmi: jalankan lalu ketik /login (akun Max)"},
                         {"command": "aeos bridge test", "why": "Uji kemampuan (1 panggilan kecil)"}],
        },
        {
            "id": "secret-store", "name": "OpenBao (vault runtime)", "domain": None, "status": health["status"],
            "capability": "Sumber kebenaran kredensial runtime; AppRole + TTL; nilai tidak pernah tampil",
            "scope": "Namespace personal/workers/platform/projects; office tidak terikat ke controller",
            "credential_refs": [], "expiry": None, "proof_time": health["checked_at"],
            "proof_source": health["source"], "remedies": health["remedies"],
            "warning": health["interim"]["residual_risk"],
        },
    ]

    def card(cid: str, name: str, domain: str | None, refs: list[str], capability: str, scope: str,
             remedies: list[dict[str, str]], **extra: Any) -> dict[str, Any]:
        rows = _ref_rows(st, refs)
        expiry = next((r.get("expires_at") for r in rows if r.get("expires_at")), None)
        return {"id": cid, "name": name, "domain": domain, "status": _cred_status(rows), "capability": capability,
                "scope": scope, "credential_refs": rows, "expiry": expiry, "proof_time": None,
                "proof_source": "Belum ada bukti fungsi dari modul ini (UNKNOWN)", "remedies": remedies, **extra}

    cards.append(card(
        "github", "GitHub (PERSONAL)", "PERSONAL", ["personal/github"],
        "Broker mendorong cabang aeos/* dan membuka PR; merge tetap manusia",
        f"Repo platform {settings.platform_repo_slug}; repo proyek per catatan integrasi proyek",
        [{"command": "gh auth token | aeos secrets set personal/github --stdin",
          "why": "Simpan token GitHub ke vault (tanpa ditampilkan)"}],
    ))
    cards.append(card(
        "google", "Google Drive / Docs (PERSONAL)", "PERSONAL", ["personal/google-oauth"],
        "Publikasi dokumen Product 1 ke folder Drive yang disetujui (scope drive.file)",
        "Folder root yang dipilih pemilik; revisi dilindungi requiredRevisionId",
        [{"command": "aeos publish google-connect", "why": "Hubungkan akun Google melalui OAuth resmi"}],
    ))
    cards.append(card(
        "openrouter", "OpenRouter (rute pekerja)", None, ["workers/openrouter", "personal/openrouter"],
        "Akses model pekerja lewat broker; info kunci/kuota resmi dari /api/v1/key",
        "Hanya rute yang disetujui; tanpa fallback berbayar otomatis",
        [{"command": "aeos secrets set workers/openrouter", "why": "Simpan kunci OpenRouter (input tersembunyi)"},
         {"command": "aeos routes refresh", "why": "Perbarui katalog + info kunci/kuota"}],
        quota=_openrouter_quota(db),
    ))
    cards.append(card(
        "opencode", "OpenCode Zen (mesin pekerja)", None, ["workers/opencode-zen"],
        "Rute gratis OpenCode Zen untuk pekerja pertama", "Rute disetujui saja; data terms per rute",
        [{"command": "aeos secrets set workers/opencode-zen", "why": "Simpan kunci OpenCode Zen"}],
    ))
    atl = card(
        "atlassian", "Atlassian Jira/Confluence/Bitbucket (OFFICE)", "OFFICE",
        ["office/atlassian", "office/bitbucket"],
        "Target OFFICE pada tenant yang diotorisasi", "OFFICE default deny sampai otorisasi perusahaan tercatat",
        [{"command": "aeos policy", "why": "Rekam otorisasi perusahaan (Decision POLICY, domain OFFICE) lebih dulu"}],
    )
    atl["status"] = "BLOCKED"
    atl["blocked_reason"] = "OFFICE: external_worker_allowed=false, approved_worker_routes=[] (§23)"
    cards.append(atl)
    return {"items": cards, "office_hidden": not office, "checked_at": utcnow().isoformat(),
            "source": "status vault/keyring, uji bridge, QuotaState; tanpa nilai secret", "mode": "LIVE"}


@router.get("/api/secrets/integrations")
def get_integrations(db: DB, settings: SettingsDep, principal: CurrentPrincipal) -> dict[str, Any]:
    out = integrations_overview(db, settings, principal)
    db.commit()  # bridge.status may persist a STALE mark
    return out
