"""Secret-store health and status projections (§26, §35): metadata only, never values."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aeos.core.config import Settings
from aeos.core.errors import AeosError, PolicyDenied
from aeos.core.timeutil import utcnow
from aeos.secrets import server
from aeos.secrets.facade import SecretStatus
from aeos.secrets.provider import (
    INTERIM_RESIDUAL_RISK,
    bootstrap_store,
    keyring_backend,
    last_verification,
    platform_label,
    project_domain,
    select_backend,
)
from aeos.secrets.refs import KNOWN_REFS, known

VERIFY_MAX_AGE = timedelta(days=7)


def _remedies(bao: dict[str, Any], binary_installed: bool, approle: bool | None,
              rejected: str | None = None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not binary_installed and rejected:
        # the openbao_binary setting wins over <data>/bin, so installing would not help
        out.append({"command": "aeos vault status",
                    "why": f"Setting openbao_binary={rejected} bukan file eksekutabel; perbaiki atau hapus setting itu "
                           "di aeos.yaml (aeos vault install tidak membantu selama setting ini ada)"})
    elif not binary_installed:
        from aeos.secrets.installer import install_command

        cmd = install_command()
        out.append({"command": f"aeos {cmd}",
                    "why": "Pasang biner OpenBao resmi (level pengguna, checksum diverifikasi)"
                           + ("" if cmd == "vault install" else "; belum ada approved_pin di install-manifest, "
                              "tentukan versi persis yang sudah ditinjau")})
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
    lookup = server.bin_lookup(settings)
    binary = Path(lookup.path) if lookup.path else None
    srv = server.running(settings)
    kr_ok, kr_name = keyring_backend()
    approle: bool | None = None
    if kr_ok:
        try:
            approle = bootstrap_store(settings).approle() is not None
        except AeosError:
            approle = None
    if sel.backend == "openbao":
        # health + AppRole login only; the functional write/read/delete roundtrip is
        # `aeos vault status --verify` (its last result is shown under functional_check)
        status = "CONNECTED"
    elif sel.backend == "os-keyring-interim":
        status = "DEGRADED"
    elif sel.provider is not None:
        status = "CONNECTED"  # test override
    else:
        status = "BLOCKED"
    verify = last_verification(settings)
    if verify is not None:
        # a past roundtrip is history: STALE when the store is not usable now or the proof is old
        try:
            at = datetime.fromisoformat(str(verify.get("checked_at")))
            old = utcnow() - (at if at.tzinfo else at.replace(tzinfo=UTC)) > VERIFY_MAX_AGE
        except ValueError:
            old = True
        verify["stale"] = bool(sel.backend != "openbao" or old)
    remedies = [] if sel.backend == "openbao" else _remedies(bao, binary is not None, approle, lookup.rejected)
    if sel.backend == "openbao" and not (verify and verify.get("status") == "VERIFIED" and not verify["stale"]):
        remedies.append({"command": "aeos vault status --verify",
                         "why": "Buktikan tulis/baca/hapus dengan token AppRole (CONNECTED belum VERIFIED)"})
    return {
        "status": status,
        "backend": sel.backend,
        "openbao": {
            "addr": bao.get("addr") or settings.openbao_addr, "reachable": bool(bao.get("reachable")),
            "initialized": bao.get("initialized"), "sealed": bao.get("sealed"), "version": bao.get("version"),
            "login": bao.get("login", "NOT_ATTEMPTED"), "approle_in_keyring": approle,
            "token": bao.get("token") or {"present": False},
            "binary_installed": binary is not None, "binary_logical": f"{binary.name} (data dir)" if binary else None,
            "server_pid_alive": bool(srv and srv.get("alive")),
        },
        "functional_check": verify or {"status": "NOT_TESTED", "checked_at": None,
                                       "note": "jalankan `aeos vault status --verify`"},
        "keyring": {"available": kr_ok, "backend": kr_name, "platform_store": platform_label()},
        "interim": {
            "active": sel.backend == "os-keyring-interim", "decision": sel.interim,
            "residual_risk": INTERIM_RESIDUAL_RISK if sel.backend == "os-keyring-interim" else None,
        },
        "reasons": sel.reasons,
        "remedies": remedies,
        "checked_at": utcnow().isoformat(),
        "source": "OpenBao /v1/sys/health + AppRole login + OS keyring",
        "mode": "LIVE",
        "evidence_limit": "CONNECTED bukan VERIFIED: hanya health + login AppRole yang diperiksa; uji fungsi "
                          "tulis/baca ada di functional_check",
    }


NO_ACCESS_REMEDY = ("OFFICE: kebijakan office tidak terikat ke controller sampai otorisasi perusahaan tercatat "
                    "(tidak ada perintah yang perlu dijalankan sekarang)")


def _view(s: SecretStatus, state: str | None = None) -> dict[str, Any]:
    k = known(s.ref)
    expired = bool(s.expires_at and s.expires_at <= utcnow())
    no_access = state == "NO_ACCESS"
    return {
        "ref": s.ref, "namespace": s.ref.split("/", 1)[0], "exists": s.exists if state is None else None,
        "state": state or ("EXPIRED" if expired else ("PRESENT" if s.exists else "MISSING")),
        "backend": s.backend, "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        "expires_at": s.expires_at.isoformat() if s.expires_at else None, "scope": s.scope, "note": s.note,
        "purpose": k.purpose if k else None, "used_by": k.used_by if k else None,
        "integration": k.integration if k else None,
        # `aeos secrets set office/...` would be refused (policy not bound): no command to suggest
        "remedy": None if no_access else f"aeos secrets set {s.ref}",
        "remedy_note": NO_ACCESS_REMEDY if no_access else None,
    }


def _visible(ref: str, *, include_office: bool, domain: str | None) -> bool:
    """Office metadata never appears in a Personal view (§35/§50), project refs included.

    ``projects/<id>/...`` reveals a project id and that it has a credential, so it is shown only
    in a session whose active domain equals the project's immutable domain. ``domain=None`` is the
    local CLI (the owner): PERSONAL projects always, OFFICE projects only with ``include_office``."""
    ns, _, name = ref.partition("/")
    if ns == "office":
        return include_office
    if ns != "projects":
        return True
    pd = project_domain(name.split("/", 1)[0])
    if domain is None:
        return pd != "OFFICE" or include_office
    return pd == domain and (pd != "OFFICE" or include_office)


def secrets_status(settings: Settings, *, include_office: bool, domain: str | None = None) -> dict[str, Any]:
    """Known + stored refs. When the store is unavailable every ref is UNKNOWN (never 'missing').

    A ref the controller policy cannot read (office/* under OpenBao: the office policy is not bound)
    is NO_ACCESS — neither present nor missing."""
    sel = select_backend(settings)
    rows: dict[str, dict[str, Any]] = {}
    error = None
    if sel.provider is not None:
        try:
            stored = {s.ref: s for s in sel.provider.list_status()}
            denied = set(getattr(sel.provider, "denied_namespaces", []) or [])
            for k in KNOWN_REFS:
                if k.ref in stored:
                    continue
                if k.namespace in denied:
                    rows[k.ref] = _view(SecretStatus(ref=k.ref, exists=False, backend=sel.backend,
                                                     note="controller policy not bound to this namespace"),
                                        state="NO_ACCESS")
                    continue
                try:
                    stored[k.ref] = sel.provider.status(k.ref)
                except PolicyDenied:
                    rows[k.ref] = _view(SecretStatus(ref=k.ref, exists=False, backend=sel.backend,
                                                     note="controller policy denies this path"), state="NO_ACCESS")
            rows.update({ref: _view(s) for ref, s in stored.items()})
        except AeosError as exc:
            error = f"{exc.code}: {exc.message}"
    if sel.provider is None or error:
        rows = {}
        for k in KNOWN_REFS:
            rows[k.ref] = _view(SecretStatus(ref=k.ref, exists=False, backend="none",
                                             note="secret store unavailable"), state="UNKNOWN")
    items = [v for ref, v in sorted(rows.items()) if _visible(ref, include_office=include_office, domain=domain)]
    return {
        "items": items, "backend": sel.backend, "store_error": error, "reasons": sel.reasons,
        "office_hidden": not include_office, "source": f"{sel.backend} metadata (tanpa nilai)",
        "checked_at": utcnow().isoformat(), "mode": "LIVE",
    }
