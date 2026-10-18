"""§08 Secret: OpenBao health/auth/policy/TTL, OS store, no raw secret exposure; fail closed."""

from __future__ import annotations

import json
import secrets as pysecrets
from typing import Any

from sqlalchemy import select

from aeos.core.redaction import contains_secret, register_secret
from aeos.doctor.checks import check
from aeos.doctor.checks._common import binary_fingerprint, binary_outcome
from aeos.doctor.model import CheckContext, Outcome, Remedy
from aeos.inventory.probes import ProbeRefused, is_loopback_url

OPENBAO_DOCS = "https://openbao.org/docs/auth/approle/"
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


@check("secret.openbao_binary", "SECRET", "Binary OpenBao", component_id="openbao", ttl_seconds=86400,
       invalidators=("binary:bao",), fingerprint=binary_fingerprint("bao", "openbao_binary"), severity_fail="MEDIUM",
       observation="installation")
def secret_openbao_binary(ctx: CheckContext) -> Outcome:
    return binary_outcome(ctx, name="bao", override_attr="openbao_binary", args=["version"],
                          regex=r"v?(\d+\.\d+\.\d+)", label="OpenBao", missing_remedy=Remedy(
                              operation="Pasang OpenBao dari distribusi resmi; inisialisasi/unseal oleh pemilik",
                              source=f"S24: {OPENBAO_DOCS}", target="PATH atau setelan openbao_binary",
                              privilege="pengguna lokal", rollback="uninstall OpenBao",
                              evidence_to_collect="bao version"))


def _bao_fp(ctx: CheckContext) -> dict[str, Any]:
    return {"addr": ctx.settings.openbao_addr, **binary_fingerprint("bao", "openbao_binary")(ctx)}


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
    start_remedy = Remedy(operation="Jalankan server OpenBao lokal, lalu init/unseal oleh pemilik (recovery material "
                          "disimpan terpisah)", command="bao server -config=<file konfigurasi anda>",
                          source=f"S24: {OPENBAO_DOCS}", target=addr, privilege="pemilik",
                          rollback="hentikan server", evidence_to_collect="secret.openbao_health CONNECTED")
    if hr.status is None:
        has_binary = bool(ctx.runner.which("bao") or ctx.settings.openbao_binary)
        status = "FAILED" if has_binary else "MISSING"
        return Outcome(status, f"OpenBao tidak terjangkau di {addr} ({hr.error}); operasi yang butuh secret ditolak.",
                       evidence={**evidence, "error": hr.error}, error_code="OPENBAO_UNREACHABLE", remedy=start_remedy)
    body = hr.body if isinstance(hr.body, dict) else {}
    evidence.update({"http_status": hr.status, "initialized": body.get("initialized"), "sealed": body.get("sealed"),
                     "standby": body.get("standby"), "server_version": body.get("version")})
    if "initialized" not in body:
        return Outcome("FAILED", f"Endpoint {addr} menjawab tetapi bukan respons health OpenBao.", evidence=evidence,
                       error_code="OPENBAO_IDENTITY_UNCONFIRMED")
    if not body.get("initialized"):
        return Outcome("FAILED", "OpenBao belum diinisialisasi.", evidence=evidence, error_code="OPENBAO_NOT_INITIALIZED",
                       remedy=Remedy(operation="Inisialisasi OpenBao (tindakan pemilik; simpan recovery material "
                                     "terpisah)", command="bao operator init", source=f"S24: {OPENBAO_DOCS}",
                                     target=addr, privilege="pemilik",
                                     rollback="tidak dapat dibatalkan; rencanakan sebelum menjalankan",
                                     evidence_to_collect="initialized=true"),
                       requires_destructive=True)
    if body.get("sealed"):
        return Outcome("FAILED", "OpenBao sealed; secret tidak tersedia (fail closed).", evidence=evidence,
                       error_code="OPENBAO_SEALED", remedy=Remedy(
                           operation="Unseal OpenBao oleh pemilik", command="bao operator unseal",
                           source=f"S24: {OPENBAO_DOCS}", target=addr, privilege="pemilik",
                           evidence_to_collect="sealed=false"))
    return Outcome("CONNECTED", "OpenBao terjangkau, initialized dan unsealed; auth/policy/TTL belum diverifikasi oleh "
                   "pemeriksaan ini.", evidence=evidence, component_version=body.get("version"),
                   target_identity="openbao@loopback")


@check("secret.status", "SECRET", "Status secret store (tanpa nilai)", component_id="secret-provider",
       ttl_seconds=900, ok={"CONFIGURED", "VERIFIED"}, severity_fail="HIGH", invalidators=("secret:*",))
def secret_status(ctx: CheckContext) -> Outcome:
    from aeos.core.errors import Blocked

    try:
        from aeos.secrets import facade as secrets

        items = secrets.list_status()
    except Blocked as exc:
        return Outcome("MISSING", "Secret store tidak tersedia; operasi yang butuh credential ditolak (fail closed).",
                       evidence={"error_code": exc.code}, error_code="SECRET_STORE_UNAVAILABLE", remedy=Remedy(
                           operation="Siapkan OpenBao (atau credential store OS) untuk SecretProvider",
                           source=f"S24: {OPENBAO_DOCS}", privilege="pemilik",
                           evidence_to_collect="secret.status CONFIGURED"))
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
    return Outcome("CONFIGURED", f"Secret store terjangkau; {sum(by_ns.values())} ref platform/personal/worker "
                   "tersimpan (nilai tidak dibaca).",
                   evidence={"refs_by_namespace": by_ns, "backends": backends,
                             "refs": [st.ref for st in shown if st.exists],
                             "not_listed": "namespace office/projects dilaporkan oleh pemeriksaan ber-scope domain"})


@check("secret.no_raw_exposure", "SECRET", "Tidak ada secret mentah di audit/event/observasi", needs_db=True,
       ttl_seconds=3600, severity_fail="CRITICAL")
def secret_no_raw_exposure(ctx: CheckContext) -> Outcome:
    from aeos.models.audit import AuditEvent, Event
    from aeos.models.inventory import CheckResult, ComponentObservation

    assert ctx.session is not None
    s = ctx.session
    sources = {
        "audit_events": [json.dumps(r.details, default=str) + (r.reason or "")
                         for r in s.scalars(select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(500))],
        "events": [json.dumps(r.payload, default=str) for r in s.scalars(select(Event).order_by(Event.seq.desc()).limit(500))],
        "observations": [json.dumps(r.detail, default=str) for r in
                         s.scalars(select(ComponentObservation).order_by(ComponentObservation.observed_at.desc()).limit(500))],
        "check_results": [json.dumps(r.observed_evidence, default=str) + r.summary for r in
                          s.scalars(select(CheckResult).order_by(CheckResult.last_checked_at.desc()).limit(500))],
    }
    hits = {k: sum(1 for text in v if contains_secret(text)) for k, v in sources.items()}
    evidence = {"scanned": {k: len(v) for k, v in sources.items()}, "findings": hits, "values_shown": False}
    if any(hits.values()):
        return Outcome("FAILED", "Pola credential ditemukan di record tersimpan (nilai tidak ditampilkan).",
                       evidence=evidence, error_code="RAW_SECRET_PERSISTED", remedy=Remedy(
                           operation="Rotasi credential terdampak dan perbaiki jalur yang tidak meredaksi",
                           privilege="pemilik", evidence_to_collect="secret.no_raw_exposure VERIFIED"))
    return Outcome("VERIFIED", f"{sum(len(v) for v in sources.values())} record terbaru dipindai; tidak ada pola credential.",
                   evidence=evidence)
