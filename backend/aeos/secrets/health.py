"""Secret-store health and status projections (§26, §35): metadata only, never values."""

from __future__ import annotations

from typing import Any

from aeos.core.config import Settings
from aeos.core.errors import AeosError
from aeos.core.timeutil import utcnow
from aeos.secrets import server
from aeos.secrets.facade import SecretStatus
from aeos.secrets.provider import (
    INTERIM_RESIDUAL_RISK,
    bootstrap_store,
    keyring_backend,
    platform_label,
    select_backend,
)
from aeos.secrets.refs import KNOWN_REFS, known


def _remedies(bao: dict[str, Any], binary_installed: bool, approle: bool | None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not binary_installed:
        out.append({"command": "aeos vault install", "why": "Pasang biner OpenBao resmi (level pengguna, checksum diverifikasi)"})
    if not bao.get("reachable"):
        out.append({"command": "aeos vault start", "why": "Jalankan server OpenBao lokal (127.0.0.1)"})
    elif not bao.get("initialized"):
        out.append({"command": "aeos vault init", "why": "Inisialisasi vault, policy per namespace dan AppRole controller"})
    elif bao.get("sealed"):
        out.append({"command": "aeos vault unseal", "why": "Buka segel vault setelah restart"})
    elif approle is False:
        out.append({"command": "aeos vault init", "why": "Kredensial AppRole tidak ada di OS keyring"})
    return out


def store_health(settings: Settings) -> dict[str, Any]:
    sel = select_backend(settings)
    bao = dict(sel.openbao)
    binary = server.bin_path(settings)
    srv = server.running(settings)
    kr_ok, kr_name = keyring_backend()
    approle: bool | None = None
    if kr_ok:
        try:
            approle = bootstrap_store(settings).approle() is not None
        except AeosError:
            approle = None
    if sel.backend == "openbao":
        status = "CONNECTED"  # health + AppRole login; functional read/write is proven by `aeos vault status --verify`
    elif sel.backend == "os-keyring-interim":
        status = "DEGRADED"
    elif sel.provider is not None:
        status = "CONNECTED"  # test override
    else:
        status = "BLOCKED"
    return {
        "status": status,
        "backend": sel.backend,
        "openbao": {
            "addr": bao.get("addr") or settings.openbao_addr, "reachable": bool(bao.get("reachable")),
            "initialized": bao.get("initialized"), "sealed": bao.get("sealed"), "version": bao.get("version"),
            "login": bao.get("login", "NOT_ATTEMPTED"), "approle_in_keyring": approle,
            "binary_installed": binary is not None, "binary_logical": f"{binary.name} (data dir)" if binary else None,
            "server_pid_alive": bool(srv and srv.get("alive")),
        },
        "keyring": {"available": kr_ok, "backend": kr_name, "platform_store": platform_label()},
        "interim": {
            "active": sel.backend == "os-keyring-interim", "decision": sel.interim,
            "residual_risk": INTERIM_RESIDUAL_RISK if sel.backend == "os-keyring-interim" else None,
        },
        "reasons": sel.reasons,
        "remedies": [] if sel.backend == "openbao" else _remedies(bao, binary is not None, approle),
        "checked_at": utcnow().isoformat(),
        "source": "OpenBao /v1/sys/health + AppRole login + OS keyring",
        "mode": "LIVE",
        "evidence_limit": "CONNECTED bukan VERIFIED: hanya health + login AppRole yang diperiksa",
    }


def _view(s: SecretStatus, state: str | None = None) -> dict[str, Any]:
    k = known(s.ref)
    expired = bool(s.expires_at and s.expires_at <= utcnow())
    return {
        "ref": s.ref, "namespace": s.ref.split("/", 1)[0], "exists": s.exists if state is None else None,
        "state": state or ("EXPIRED" if expired else ("PRESENT" if s.exists else "MISSING")),
        "backend": s.backend, "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        "expires_at": s.expires_at.isoformat() if s.expires_at else None, "scope": s.scope, "note": s.note,
        "purpose": k.purpose if k else None, "used_by": k.used_by if k else None,
        "integration": k.integration if k else None, "remedy": f"aeos secrets set {s.ref}",
    }


def secrets_status(settings: Settings, *, include_office: bool) -> dict[str, Any]:
    """Known + stored refs. When the store is unavailable every ref is UNKNOWN (never 'missing')."""
    sel = select_backend(settings)
    rows: dict[str, dict[str, Any]] = {}
    error = None
    if sel.provider is not None:
        try:
            stored = {s.ref: s for s in sel.provider.list_status()}
            for k in KNOWN_REFS:
                if k.ref not in stored:
                    stored[k.ref] = sel.provider.status(k.ref)
            rows = {ref: _view(s) for ref, s in stored.items()}
        except AeosError as exc:
            error = f"{exc.code}: {exc.message}"
    if sel.provider is None or error:
        for k in KNOWN_REFS:
            rows[k.ref] = _view(SecretStatus(ref=k.ref, exists=False, backend="none",
                                             note="secret store unavailable"), state="UNKNOWN")
    items = [v for ref, v in sorted(rows.items()) if include_office or not ref.startswith("office/")]
    return {
        "items": items, "backend": sel.backend, "store_error": error, "reasons": sel.reasons,
        "office_hidden": not include_office, "source": f"{sel.backend} metadata (tanpa nilai)",
        "checked_at": utcnow().isoformat(), "mode": "LIVE",
    }
