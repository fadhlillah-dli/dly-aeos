"""§08 Secret: OpenBao health/auth/policy/TTL, OS store, no raw secret exposure; fail closed."""

from __future__ import annotations

import json
import secrets as pysecrets
from typing import Any

from sqlalchemy import func, select

from aeos.core.redaction import contains_secret, redact_text, register_secret
from aeos.doctor.checks import check
from aeos.doctor.checks._common import binary_fingerprint, binary_outcome, resolve_openbao
from aeos.doctor.model import CheckContext, Outcome, Remedy, aeos_cmd
from aeos.inventory.probes import ProbeRefused, is_loopback_url

OPENBAO_DOCS = "https://openbao.org/docs/auth/approle/"
OPENBAO_RELEASES = "https://github.com/openbao/openbao/releases"
KEYRING_PROBE_SERVICE = "dly-aeos-doctor-probe"
DOMAIN_PRIVATE_NAMESPACES = frozenset({"office", "projects"})


@check("secret.keyring", "SECRET", "Backend credential store OS", component_id="os-credential-store",
       ttl_seconds=86400, ok={"CONFIGURED", "VERIFIED"}, severity_fail="HIGH", observation="installation")
def secret_keyring(ctx: CheckContext) -> Outcome:
    try:
        import keyring

        kr = keyring.get_keyring()
    except Exception as exc:
        return Outcome("UNKNOWN", f"Backend keyring tidak dapat dimuat ({type(exc).__name__}).",
                       error_code="KEYRING_UNAVAILABLE")
    name = f"{type(kr).__module__}.{type(kr).__name__}"
    evidence: dict[str, Any] = {"backend": name, "values_read": False}
    if any(x in name.lower() for x in ("fail", "null")):
        return Outcome("MISSING", "Tidak ada backend credential store OS yang dapat dipakai.", evidence=evidence,
                       error_code="NO_KEYRING_BACKEND", remedy=Remedy(
                           operation="Aktifkan credential store OS (Windows Credential Manager / macOS Keychain / "
                           "Secret Service)", privilege="pengguna lokal", evidence_to_collect="secret.keyring CONFIGURED"))
    if not ctx.functional:
        return Outcome("CONFIGURED", f"Backend {type(kr).__name__} tersedia (tes tulis hanya dengan --functional).",
                       evidence=evidence)
    probe_value = pysecrets.token_urlsafe(24)
    register_secret(probe_value)
    try:
        keyring.set_password(KEYRING_PROBE_SERVICE, "roundtrip", probe_value)
        ok = keyring.get_password(KEYRING_PROBE_SERVICE, "roundtrip") == probe_value
        keyring.delete_password(KEYRING_PROBE_SERVICE, "roundtrip")
    except Exception as exc:
        return Outcome("FAILED", f"Tes tulis/baca/hapus keyring gagal ({type(exc).__name__}).", evidence=evidence,
                       error_code="KEYRING_ROUNDTRIP_FAILED")
    evidence["scenario"] = f"set/get/delete entri uji {KEYRING_PROBE_SERVICE}"
    if not ok:
        return Outcome("FAILED", "Nilai uji keyring tidak terbaca kembali.", evidence=evidence,
                       error_code="KEYRING_ROUNDTRIP_MISMATCH")
    return Outcome("VERIFIED", "Tes tulis/baca/hapus entri uji keyring berhasil.", evidence=evidence,
                   observations=[{"component_id": "os-credential-store", "dimension": "functional",
                                  "status": "VERIFIED"}])


_bao_binary_fp = binary_fingerprint("bao", "openbao_binary", resolver=resolve_openbao)


def vault_remedy(step: str) -> Remedy:
    """Remedy through the reviewed ``aeos vault`` commands (official release, checksum-verified, user-level,
    loopback only; master §07 step 3). The owner runs them; the Doctor never does."""
    steps = {
        "install": ("Pasang biner OpenBao resmi ke <data>/bin (rencana ditampilkan dan dikonfirmasi dulu; SHA256 "
                    "dicocokkan dengan checksums.txt, tanda tangan GPG belum diverifikasi)", "vault install",
                    "hapus <data>/bin/bao", "secret.openbao_binary VERIFIED"),
        "start": ("Jalankan server OpenBao lokal (hanya 127.0.0.1)", "vault start", aeos_cmd("vault stop"),
                  "secret.openbao_health CONNECTED"),
        "init": ("Inisialisasi OpenBao, policy per namespace dan AppRole controller (tindakan pemilik; recovery "
                 "material disimpan terpisah)", "vault init", "tidak dapat dibatalkan; rencanakan sebelum menjalankan",
                 "secret.openbao_health CONNECTED, secret.status CONFIGURED"),
        "unseal": ("Buka segel OpenBao oleh pemilik", "vault unseal", "tidak diperlukan", "sealed=false"),
    }
    op, cmd, rollback, evidence = steps[step]
    return Remedy(operation=op, command=aeos_cmd(cmd), source=f"S24: {OPENBAO_RELEASES} / {OPENBAO_DOCS}",
                  target="<data>/bin, 127.0.0.1:8200", privilege="pemilik (pengguna lokal, tanpa elevasi)",
                  cost="tidak ada", rollback=rollback, evidence_to_collect=evidence)


@check("secret.openbao_binary", "SECRET", "Binary OpenBao", component_id="openbao", ttl_seconds=86400,
       invalidators=("binary:bao",), fingerprint=_bao_binary_fp, severity_fail="MEDIUM",
       observation="installation")
def secret_openbao_binary(ctx: CheckContext) -> Outcome:
    return binary_outcome(ctx, name="bao", override_attr="openbao_binary", args=["version"],
                          regex=r"v?(\d+\.\d+\.\d+)", label="OpenBao", resolver=resolve_openbao,
                          missing_remedy=vault_remedy("install"))


def _bao_fp(ctx: CheckContext) -> dict[str, Any]:
    return {"addr": ctx.settings.openbao_addr, **_bao_binary_fp(ctx)}


@check("secret.openbao_health", "SECRET", "Health OpenBao (initialized/sealed)", component_id="openbao",
       ttl_seconds=900, invalidators=("config:openbao_addr", "binary:bao"), ok={"CONNECTED", "VERIFIED"},
       fingerprint=_bao_fp, severity_fail="HIGH", observation="connectivity")
def secret_openbao_health(ctx: CheckContext) -> Outcome:
    addr = (ctx.settings.openbao_addr or "").rstrip("/")
    url = f"{addr}/v1/sys/health"
    evidence: dict[str, Any] = {"endpoint": "loopback" if is_loopback_url(url) else "non-loopback"}
    if not is_loopback_url(url):
        return Outcome("FAILED", "openbao_addr bukan loopback; V1 tidak mem-probe endpoint lain (fail closed).",
                       evidence=evidence, error_code="OPENBAO_NOT_LOOPBACK")
    try:
        hr = ctx.runner.http_get(url, timeout=5.0)
    except ProbeRefused:
        return Outcome("FAILED", "Probe ditolak (non-loopback).", evidence=evidence, error_code="OPENBAO_NOT_LOOPBACK")
    if hr.status is None:
        has_binary = resolve_openbao(ctx)[0] is not None
        status = "FAILED" if has_binary else "MISSING"
        hint = "server belum berjalan" if has_binary else "biner OpenBao belum terpasang"
        return Outcome(status, f"OpenBao tidak terjangkau di {addr} ({hr.error}; {hint}); operasi yang butuh secret "
                       "ditolak.", evidence={**evidence, "error": hr.error, "binary_installed": has_binary},
                       error_code="OPENBAO_UNREACHABLE", remedy=vault_remedy("start" if has_binary else "install"))
    body = hr.body if isinstance(hr.body, dict) else {}
    evidence.update({"http_status": hr.status, "initialized": body.get("initialized"), "sealed": body.get("sealed"),
                     "standby": body.get("standby"), "server_version": body.get("version")})
    if "initialized" not in body:
        return Outcome("FAILED", f"Endpoint {addr} menjawab tetapi bukan respons health OpenBao.", evidence=evidence,
                       error_code="OPENBAO_IDENTITY_UNCONFIRMED")
    if not body.get("initialized"):
        return Outcome("FAILED", "OpenBao belum diinisialisasi.", evidence=evidence, error_code="OPENBAO_NOT_INITIALIZED",
                       remedy=vault_remedy("init"), requires_destructive=True)
    if body.get("sealed"):
        return Outcome("FAILED", "OpenBao sealed; secret tidak tersedia (fail closed).", evidence=evidence,
                       error_code="OPENBAO_SEALED", remedy=vault_remedy("unseal"))
    return Outcome("CONNECTED", "OpenBao terjangkau, initialized dan unsealed; auth/policy/TTL belum diverifikasi oleh "
                   "pemeriksaan ini.", evidence=evidence, component_version=body.get("version"),
                   target_identity="openbao@loopback")


# Depends on the store itself (backend selection, OpenBao address/binary), not on individual references: a
# credential write or a Google token refresh does not change whether the store is reachable, so it must not
# make CAN_RUN_WORKER / CAN_PUBLISH_PERSONAL STALE. A secret_store decision (interim keyring exception)
# emits ``secret:*``, which still matches ``secret:store``.
@check("secret.status", "SECRET", "Status secret store (tanpa nilai)", component_id="secret-provider",
       ttl_seconds=900, ok={"CONFIGURED", "VERIFIED"}, severity_fail="HIGH",
       invalidators=("secret:store", "config:openbao_addr", "binary:bao"))
def secret_status(ctx: CheckContext) -> Outcome:
    from aeos.core.errors import Blocked

    try:
        from aeos.secrets import facade as secrets

        items = secrets.list_status()
    except Blocked as exc:
        reasons = [redact_text(str(r))[:200] for r in (exc.details or {}).get("reasons") or []][:5]
        has_binary = resolve_openbao(ctx)[0] is not None
        remedy = vault_remedy("start" if has_binary else "install")
        remedy = Remedy(**{**remedy.as_dict(), "operation": remedy.operation + "; lalu " + aeos_cmd("vault init")
                           + ". Alternatif sementara (PERSONAL saja, residual risk): keputusan EXCEPTION pemilik untuk "
                           "OS keyring - lihat " + aeos_cmd("secrets interim-keyring")})
        return Outcome("MISSING", "Secret store tidak tersedia (OpenBao belum siap dan tidak ada pengecualian interim "
                       "OS keyring); operasi yang butuh credential ditolak (fail closed).",
                       evidence={"error_code": exc.code, "reasons": reasons, "binary_installed": has_binary},
                       error_code="SECRET_STORE_UNAVAILABLE", remedy=remedy)
    except (ImportError, ModuleNotFoundError):
        return Outcome("UNKNOWN", "Modul secrets belum tersedia.", error_code="SECRETS_NOT_IMPLEMENTED")
    except Exception as exc:
        return Outcome("MISSING", f"Secret store gagal diperiksa ({type(exc).__name__}); fail closed.",
                       error_code="SECRET_STORE_ERROR")
    # This is a platform-level row shown to every domain: OFFICE and per-project refs (which may belong
    # to either domain) are not listed or counted here, so their existence never leaks into a PERSONAL
    # view (§35, §50). OFFICE credential status is reported by the OFFICE-scoped adapter checks.
    shown = [st for st in items if st.ref.partition("/")[0] not in DOMAIN_PRIVATE_NAMESPACES]
    by_ns: dict[str, int] = {}
    for st in shown:
        ns = st.ref.partition("/")[0]
        by_ns[ns] = by_ns.get(ns, 0) + (1 if st.exists else 0)
    backends = sorted({st.backend for st in items})
    evidence: dict[str, Any] = {"refs_by_namespace": by_ns, "backends": backends,
                                "refs": [st.ref for st in shown if st.exists],
                                "not_listed": "namespace office/projects dilaporkan oleh pemeriksaan ber-scope domain"}
    interim = "os-keyring-interim" in backends
    if interim:
        from aeos.secrets.provider import INTERIM_RESIDUAL_RISK

        evidence["residual_risks"] = [INTERIM_RESIDUAL_RISK]
    store = "OS keyring sementara (pengecualian pemilik, residual risk)" if interim else "terjangkau"
    return Outcome("CONFIGURED", f"Secret store {store}; {sum(by_ns.values())} ref platform/personal/worker "
                   "tersimpan (nilai tidak dibaca).", evidence=evidence, severity="MEDIUM" if interim else None)


def _bao_auth_fp(ctx: CheckContext) -> dict[str, Any]:
    return {"addr": ctx.settings.openbao_addr, "service": ctx.settings.keyring_service, **_bao_binary_fp(ctx)}


def _duration_seconds(text: str) -> int | None:
    import re

    m = re.fullmatch(r"(\d+)([smhd])", str(text or "").strip())
    if not m:
        return None
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


@check("secret.openbao_auth", "SECRET", "OpenBao AppRole: login, policy namespace, TTL token", component_id="openbao",
       ttl_seconds=900, invalidators=("config:openbao_addr", "binary:bao", "secret:store"),
       ok={"CONNECTED", "VERIFIED", "NOT_APPLICABLE"}, fingerprint=_bao_auth_fp, severity_fail="HIGH",
       description="Login AppRole controller, policy token = aeos-personal/aeos-workers/aeos-platform/aeos-projects "
                   "(aeos-office TIDAK terikat, §23), TTL <= token_ttl yang dikonfigurasi, plus roundtrip "
                   "`aeos vault status --verify` terakhir (<= 7 hari) sebagai bukti fungsi.")
def secret_openbao_auth(ctx: CheckContext) -> Outcome:
    try:
        from aeos.secrets.health import store_health
        from aeos.secrets.provider import select_backend
        from aeos.secrets.setup import CONTROLLER_POLICIES, TOKEN_TTL
    except (ImportError, ModuleNotFoundError):
        return Outcome("UNKNOWN", "Modul secrets belum tersedia.", error_code="SECRETS_NOT_IMPLEMENTED")
    try:
        sel = select_backend(ctx.settings)
    except Exception as exc:  # fail closed, never green
        return Outcome("UNKNOWN", f"Status OpenBao tidak dapat dibaca ({type(exc).__name__}).",
                       error_code="OPENBAO_STATUS_ERROR")
    backend = sel.backend
    if backend == "os-keyring-interim":
        return Outcome("NOT_APPLICABLE", "OpenBao tidak dipakai: pengecualian interim OS keyring aktif (residual "
                       "risk dilaporkan oleh secret.status).", evidence={"backend": backend})
    if backend != "openbao":
        if sel.provider is not None:
            return Outcome("NOT_APPLICABLE", f"Secret provider {backend} (bukan OpenBao) dipakai; auth/policy OpenBao "
                           "tidak berlaku.", evidence={"backend": backend})
        has_binary = resolve_openbao(ctx)[0] is not None
        return Outcome("MISSING", "Login AppRole OpenBao tidak berhasil; operasi yang butuh credential ditolak "
                       "(fail closed).", evidence={"backend": backend, "login": (sel.openbao or {}).get("login"),
                                                   "reasons": [redact_text(str(r))[:200] for r in sel.reasons][:5]},
                       error_code="OPENBAO_LOGIN_FAILED",
                       remedy=vault_remedy("start" if has_binary else "install"))
    try:
        h = store_health(ctx.settings)
    except Exception as exc:
        return Outcome("UNKNOWN", f"Status OpenBao tidak dapat dibaca ({type(exc).__name__}).",
                       error_code="OPENBAO_STATUS_ERROR")
    bao = h.get("openbao") or {}
    token = bao.get("token") or {}
    evidence: dict[str, Any] = {"backend": backend, "login": bao.get("login"), "approle_in_keyring":
                                bao.get("approle_in_keyring"), "token": {k: token.get(k) for k in (
                                    "present", "ttl_seconds", "renewable", "policies")},
                                "functional_check": {k: (h.get("functional_check") or {}).get(k)
                                                     for k in ("status", "checked_at", "stale")}}
    policies = sorted(set(token.get("policies") or []) - {"default"})
    expected = sorted(CONTROLLER_POLICIES)
    ttl = token.get("ttl_seconds")
    max_ttl = _duration_seconds(TOKEN_TTL)
    problems: list[str] = []
    if not token.get("present"):
        problems.append("token controller tidak ada setelah login")
    if "root" in policies:
        problems.append("token controller memakai policy root")
    if "aeos-office" in policies:
        problems.append("policy aeos-office terikat ke controller (OFFICE default deny, §23)")
    if policies != expected:
        problems.append(f"policy token {policies} != yang diharapkan {expected}")
    if not isinstance(ttl, int) or ttl <= 0 or (max_ttl is not None and ttl > max_ttl):
        problems.append(f"TTL token {ttl}s di luar batas token_ttl {TOKEN_TTL}")
    evidence.update({"expected_policies": expected, "max_ttl_seconds": max_ttl})
    if problems:
        return Outcome("FAILED", "AppRole OpenBao tidak sesuai kontrak: " + "; ".join(problems) + ".",
                       evidence=evidence, error_code="OPENBAO_AUTH_SCOPE_INVALID", remedy=vault_remedy("init"))
    verify = h.get("functional_check") or {}
    if verify.get("status") == "VERIFIED" and not verify.get("stale"):
        return Outcome("VERIFIED", f"Login AppRole OK, policy {', '.join(policies)}, TTL {ttl}s; roundtrip tulis/baca/"
                       f"hapus terakhir {verify.get('checked_at')} (skenario: platform/aeos-healthcheck).",
                       evidence=evidence, target_identity="openbao@loopback")
    return Outcome("CONNECTED", f"Login AppRole OK, policy {', '.join(policies)}, TTL {ttl}s; roundtrip fungsi belum "
                   "dibuktikan dalam 7 hari (jalankan " + aeos_cmd("vault status --verify") + ").", evidence=evidence,
                   target_identity="openbao@loopback")


# ---------------------------------------------------------------- persisted raw-secret scan (incremental, all rows)

SCAN_STATE_KEY = "doctor.secret_exposure_scan"
SCAN_BATCH = 50_000  # rows per table per run beyond the newest window; the sweep covers every row over runs
RECENT_WINDOW = 500
EXPOSURE_TARGET = "secret_exposure"


def _scan_sources() -> dict[str, tuple[Any, Any, Any, Any]]:
    """table -> (model, order column, unique tie-break column, text). Timestamps are not unique (every result
    of one Doctor run shares ``last_checked_at``), so the watermark is the pair (order column, primary key)."""
    from aeos.models.audit import AuditEvent, Event
    from aeos.models.inventory import CheckResult, ComponentObservation

    return {
        "audit_events": (AuditEvent, AuditEvent.seq, AuditEvent.seq,
                         lambda r: json.dumps(r.details, default=str) + (r.reason or "")),
        "events": (Event, Event.seq, Event.seq, lambda r: json.dumps(r.payload, default=str)),
        "observations": (ComponentObservation, ComponentObservation.observed_at, ComponentObservation.id,
                         lambda r: json.dumps(r.detail, default=str)),
        "check_results": (CheckResult, CheckResult.last_checked_at, CheckResult.id,
                          lambda r: json.dumps(r.observed_evidence, default=str) + (r.summary or "")),
    }


def _after(order_col: Any, tie_col: Any, mark: Any) -> Any:
    """SQL condition: rows strictly after the stored watermark (legacy scalar marks are read as (value, None))."""
    from datetime import datetime

    from sqlalchemy import and_, or_

    if isinstance(mark, dict):
        value, tie = mark.get("v"), mark.get("id")
    else:
        value, tie = mark, None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if tie is None or order_col is tie_col:
        return order_col > value
    return or_(order_col > value, and_(order_col == value, tie_col > tie))


def _row_id(r: Any) -> str:
    return str(getattr(r, "event_id", None) or getattr(r, "id", None) or getattr(r, "seq", ""))


def _mark(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def accepted_exposures(session: Any, findings: dict[str, dict[str, Any]] | None = None) -> set[str]:
    """Finding ids an ACTIVE human RISK_ACCEPTANCE decision covers (credential rotated / accepted). With *findings*
    the decision must be bound to that finding's exact hash (§19: risk acceptance bound to target + hash)."""
    from aeos.core.hashing import content_hash
    from aeos.models.decisions import Decision

    out: set[str] = set()
    for d in session.scalars(select(Decision).where(
            Decision.kind == "RISK_ACCEPTANCE", Decision.target_type == EXPOSURE_TARGET, Decision.status == "ACTIVE")):
        if d.actor_kind not in ("HUMAN", "HUMAN_RECORDED"):
            continue
        if findings is not None:
            f = findings.get(d.target_id)
            if f is None or d.target_hash != content_hash(f):
                continue
        out.add(d.target_id)
    return out


@check("secret.no_raw_exposure", "SECRET", "Tidak ada secret mentah di audit/event/observasi", needs_db=True,
       ttl_seconds=3600, severity_fail="CRITICAL",
       description="Pemindaian bertahap SELURUH record audit/event/observasi/hasil Doctor (watermark per tabel) "
                   "plus 500 record terbaru tiap run; temuan diingat sampai pemilik merotasi credential dan "
                   "mencatat keputusan RISK_ACCEPTANCE untuk temuan itu. Nilai tidak pernah ditampilkan.")
def secret_no_raw_exposure(ctx: CheckContext) -> Outcome:
    from aeos.models.identity import SystemSetting

    assert ctx.session is not None
    s = ctx.session
    row = s.get(SystemSetting, SCAN_STATE_KEY)
    state: dict[str, Any] = dict(row.value or {}) if row is not None and isinstance(row.value, dict) else {}
    marks: dict[str, Any] = dict(state.get("marks") or {})
    findings: dict[str, dict[str, Any]] = {f["id"]: f for f in state.get("findings") or [] if isinstance(f, dict)}
    scanned: dict[str, int] = dict(state.get("scanned") or {})
    totals: dict[str, int] = {}
    remaining: dict[str, int] = {}
    recent_hits: dict[str, int] = {}

    def note(table: str, r: Any) -> None:
        fid = f"{table}:{_row_id(r)}"
        findings.setdefault(fid, {"id": fid, "table": table, "row": _row_id(r), "found_at": ctx.now.isoformat()})

    for table, (model, order_col, tie_col, text_of) in _scan_sources().items():
        totals[table] = int(s.scalar(select(func.count()).select_from(model)) or 0)
        hits = 0
        for r in s.scalars(select(model).order_by(order_col.desc()).limit(RECENT_WINDOW)):
            if contains_secret(text_of(r)):
                hits += 1
                note(table, r)
        recent_hits[table] = hits
        q = select(model).order_by(order_col.asc(), tie_col.asc())
        mark = marks.get(table)
        if mark is not None:
            q = q.where(_after(order_col, tie_col, mark))
        last = None
        n = 0
        for r in s.scalars(q.limit(SCAN_BATCH)):
            n += 1
            last = {"v": _mark(getattr(r, order_col.key)), "id": getattr(r, tie_col.key)}
            if contains_secret(text_of(r)):
                note(table, r)
        if last is not None:
            marks[table] = last
        scanned[table] = int(scanned.get(table, 0)) + n
        rest_q = select(func.count()).select_from(model)
        if marks.get(table) is not None:
            rest_q = rest_q.where(_after(order_col, tie_col, marks[table]))
        remaining[table] = int(s.scalar(rest_q) or 0)
    accepted = accepted_exposures(s, findings)
    open_findings = sorted(fid for fid in findings if fid not in accepted)
    # every OPEN finding is kept (forgetting one would turn the check green); only accepted ones are capped
    kept = [f for fid, f in findings.items() if fid not in accepted]
    kept += [f for fid, f in findings.items() if fid in accepted][-500:]
    state = {"marks": marks, "findings": kept, "scanned": scanned, "updated_at": ctx.now.isoformat()}
    if row is None:
        s.add(SystemSetting(key=SCAN_STATE_KEY, value=state))
    else:
        row.value = state
    complete = not any(remaining.values())
    per_table = {t: sum(1 for fid in open_findings if fid.startswith(f"{t}:")) for t in totals}
    evidence = {"findings": per_table, "scanned": scanned, "recent_window": RECENT_WINDOW,
                "recent_findings": recent_hits, "rows_total": totals,
                "rows_remaining_unscanned": remaining, "sweep_complete": complete,
                "open_findings": len(open_findings), "finding_ids": open_findings[:20],
                "accepted_findings": len(set(findings) & accepted), "values_shown": False}
    if open_findings:
        return Outcome("FAILED", f"{len(open_findings)} record tersimpan berisi pola credential (nilai tidak "
                       "ditampilkan).", evidence=evidence, error_code="RAW_SECRET_PERSISTED", remedy=Remedy(
                           operation="Rotasi credential terdampak, perbaiki jalur yang tidak meredaksi, lalu catat "
                           "keputusan RISK_ACCEPTANCE per temuan (target secret_exposure/<id>) di halaman Keputusan",
                           target=", ".join(open_findings[:5]), privilege="pemilik (SUPER_ADMIN)",
                           rollback="tidak berlaku (record append-only)",
                           evidence_to_collect="secret.no_raw_exposure VERIFIED"))
    if not complete:
        return Outcome("UNKNOWN", f"Pemindaian bertahap belum selesai: {sum(remaining.values())} record lama belum "
                       f"dipindai (record terbaru bersih); jalankan ulang Doctor.", evidence=evidence,
                       error_code="SECRET_SCAN_INCOMPLETE", severity="MEDIUM")
    return Outcome("VERIFIED", f"Seluruh {sum(totals.values())} record dipindai (bertahap, watermark per tabel); tidak "
                   "ada pola credential yang terbuka.", evidence=evidence)
