"""§08 Claude native: official binary/version, native login mode, model resolution, extra billing, quota.

Never uses the Claude API, never ``--bare``, never reads or moves Claude OAuth/session tokens. The
child environment is scrubbed of ANTHROPIC_* and alternative-provider variables (§12). Only four
non-identifying fields of ``claude auth status`` are kept (no e-mail, no organisation).
"""

from __future__ import annotations

import json
import sys
from typing import Any

from aeos.doctor.checks import check
from aeos.doctor.checks._common import binary_fingerprint, binary_outcome, resolve_binary
from aeos.doctor.model import CheckContext, Outcome, Remedy
from aeos.inventory.probes import PROVIDER_ENV_NAMES, scrubbed_env

FORBIDDEN_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", *PROVIDER_ENV_NAMES)
AUTH_FIELDS = ("loggedIn", "authMethod", "apiProvider", "subscriptionType")
LOGIN_DOCS = "https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan"
_fp_claude = binary_fingerprint("claude", "claude_binary")

INSTALL_REMEDY = Remedy(
    operation="Pasang Claude Code resmi (distribusi Anthropic) lalu login lewat alur resmi Max",
    command=None, source=f"dokumentasi resmi Claude Code (S03: {LOGIN_DOCS})", target="PATH atau setelan claude_binary",
    privilege="pengguna lokal", cost="langganan Max milik pemilik",
    rollback="uninstall lewat distribusi resmi", evidence_to_collect="claude --version; claude auth status",
)


@check("claude.binary", "CLAUDE_NATIVE", "Binary Claude Code resmi dan versinya", component_id="claude-code",
       ttl_seconds=86400, invalidators=("binary:claude",), fingerprint=_fp_claude, severity_fail="HIGH",
       observation="installation")
def claude_binary(ctx: CheckContext) -> Outcome:
    return binary_outcome(ctx, name="claude", override_attr="claude_binary", args=["--version"],
                          regex=r"(\d+\.\d+\.\d+)", missing_remedy=INSTALL_REMEDY, label="Claude Code", timeout=20.0)


def _parse_status(text: str) -> dict[str, Any] | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


@check("claude.auth", "CLAUDE_NATIVE", "Login native Max (claude auth status)", component_id="claude-code",
       ttl_seconds=3600, invalidators=("auth:claude", "binary:claude"), ok={"CONNECTED", "VERIFIED"},
       fingerprint=_fp_claude, severity_fail="HIGH", observation="connectivity")
def claude_auth(ctx: CheckContext) -> Outcome:
    path, _lookup = resolve_binary(ctx, "claude", "claude_binary")
    if not path:
        return Outcome("MISSING", "Claude Code tidak ditemukan; status login tidak dapat diperiksa.",
                       error_code="BINARY_MISSING", remedy=INSTALL_REMEDY)
    rr = ctx.runner.run([path, "auth", "status", "--json"], timeout=30.0, env=scrubbed_env(ctx.env), cwd=ctx.probe_cwd)
    data = _parse_status(rr.stdout) if rr.stdout else None
    if data is None:
        return Outcome("UNKNOWN", f"Output 'claude auth status' tidak dapat dibaca ({rr.error or rr.returncode}).",
                       error_code="AUTH_STATUS_UNPARSEABLE", evidence={"exit_code": rr.returncode})
    fields = {k: data.get(k) for k in AUTH_FIELDS}  # e-mail/org/directories intentionally dropped
    evidence: dict[str, Any] = {**fields, "operation_ref": "claude auth status --json (env tanpa ANTHROPIC_*)"}
    login_remedy = Remedy(operation="Login Claude Code lewat alur resmi (jalankan claude lalu /login); AEOS tidak "
                          "menyediakan form login", command="claude", source=f"S03: {LOGIN_DOCS}",
                          privilege="pemilik akun Max", cost="langganan Max", rollback="claude auth logout",
                          evidence_to_collect="claude.auth CONNECTED")
    if not fields["loggedIn"]:
        return Outcome("MISSING", "Claude Code belum login.", evidence=evidence, error_code="NOT_LOGGED_IN",
                       remedy=login_remedy)
    if fields["authMethod"] != "claude.ai":
        return Outcome("FAILED", f"Mode auth '{fields['authMethod']}' bukan login Claude.ai; jalur API/token tidak "
                       "diizinkan sebagai pengawas (U-06, §12).", evidence=evidence, error_code="AUTH_METHOD_NOT_NATIVE",
                       severity="CRITICAL", remedy=login_remedy)
    if fields["apiProvider"] != "firstParty":
        return Outcome("FAILED", f"Provider '{fields['apiProvider']}' bukan firstParty.", evidence=evidence,
                       error_code="API_PROVIDER_NOT_FIRST_PARTY", severity="CRITICAL", remedy=login_remedy)
    if str(fields["subscriptionType"]).lower() != "max":
        return Outcome("FAILED", f"Langganan '{fields['subscriptionType']}' bukan Max.", evidence=evidence,
                       error_code="SUBSCRIPTION_NOT_MAX", severity="HIGH", remedy=login_remedy)
    return Outcome("CONNECTED", "Login native Claude.ai (firstParty, Max) terdeteksi; fungsi bridge belum dibuktikan "
                   "oleh pemeriksaan ini.", evidence=evidence, target_identity="akun Max pemilik (identitas tidak disimpan)")


def _env_fp(ctx: CheckContext) -> dict[str, Any]:
    return {"present": sorted(k for k in FORBIDDEN_ENV if ctx.env.get(k))}


@check("claude.env", "CLAUDE_NATIVE", "Tidak ada ANTHROPIC_API_KEY/token/base URL di environment",
       component_id="claude-code", ttl_seconds=3600, invalidators=("env:anthropic",), fingerprint=_env_fp,
       severity_fail="CRITICAL")
def claude_env(ctx: CheckContext) -> Outcome:
    present = sorted(k for k in FORBIDDEN_ENV if ctx.env.get(k))
    evidence = {"variables_present": present, "values_shown": False}
    if present:
        if sys.platform == "win32":
            cmds = "; ".join(f"[Environment]::SetEnvironmentVariable('{k}', $null, 'User')" for k in present)
            operation = "Hapus variabel provider dari environment pengguna (PowerShell), lalu buka terminal baru"
        else:
            cmds = "unset " + " ".join(present)
            operation = ("Hapus variabel provider: unset untuk shell ini, lalu hapus baris export-nya dari profil shell "
                         "(~/.zshrc, ~/.bash_profile atau ~/.profile) dan buka terminal baru")
        return Outcome("FAILED", f"Variabel provider ada di environment: {', '.join(present)} (nilai tidak ditampilkan). "
                       "Sesi Max bisa beralih ke API berbayar.", evidence=evidence, error_code="PROVIDER_ENV_PRESENT",
                       remedy=Remedy(operation=operation, command=cmds,
                                     target="environment pengguna", privilege="pengguna lokal",
                                     rollback="set ulang variabel (tidak disarankan)",
                                     evidence_to_collect="claude.env VERIFIED"))
    return Outcome("VERIFIED", "ANTHROPIC_API_KEY/AUTH_TOKEN/BASE_URL dan variabel provider alternatif tidak ada.",
                   evidence=evidence)


@check("claude.extra_billing", "CLAUDE_NATIVE", "Konfigurasi extra usage berbayar", component_id="claude-code",
       ttl_seconds=86400, ok=frozenset(), severity_fail="MEDIUM")
def claude_extra_billing(ctx: CheckContext) -> Outcome:
    return Outcome("UNKNOWN", "Pengaturan extra usage berbayar tidak dapat dibaca secara resmi oleh AEOS; status UNKNOWN.",
                   severity="MEDIUM", evidence={"source": "tidak ada API resmi untuk status ini"},
                   remedy=Remedy(operation="Periksa manual pengaturan usage di akun Claude dan pastikan extra usage "
                                 "berbayar nonaktif (§12)", source=f"S03: {LOGIN_DOCS}", privilege="pemilik akun",
                                 cost="mencegah tagihan tambahan", evidence_to_collect="catatan keputusan pemilik"))


@check("claude.quota", "CLAUDE_NATIVE", "Status kuota Max", component_id="claude-code", ttl_seconds=3600,
       ok=frozenset(), severity_fail="LOW")
def claude_quota(ctx: CheckContext) -> Outcome:
    return Outcome("UNKNOWN", "Kuota mingguan Max (termasuk Fable) tidak diekspos secara resmi; ditampilkan UNKNOWN. "
                   "Saat kuota habis, review menunggu (tidak ada fallback).", severity="LOW",
                   evidence={"source": "S02/S03 (batas mingguan); tidak ada probe resmi"})


@check("claude.capability_test", "CLAUDE_NATIVE", "Capability test ClaudeNativeBridge", component_id="claude-native-bridge",
       ttl_seconds=86400, invalidators=("binary:claude", "auth:claude", "model:route"), functional=True,
       fingerprint=_fp_claude, severity_fail="HIGH", observation="functional")
def claude_capability_test(ctx: CheckContext) -> Outcome:
    try:
        from aeos.claude_bridge import facade as bridge

        result = bridge.capability_test()
    except (ImportError, ModuleNotFoundError):
        return Outcome("UNKNOWN", "ClaudeNativeBridge belum diimplementasikan; otomatisasi NOT-PROVEN (jalur paket "
                       "manual §12 tetap tersedia).", error_code="BRIDGE_NOT_IMPLEMENTED")
    except Exception as exc:  # fail closed
        return Outcome("FAILED", f"Capability test gagal ({type(exc).__name__}).", error_code="CAPABILITY_TEST_ERROR")
    if not isinstance(result, dict):
        return Outcome("UNKNOWN", "Hasil capability test tidak dikenali.", error_code="CAPABILITY_TEST_UNRECOGNISED")
    keep = {k: result.get(k) for k in ("ok", "engine_version", "model_requested", "model_resolved", "auth_mode",
                                       "error_code", "quota_exhausted", "mode")}
    if result.get("quota_exhausted"):
        return Outcome("FAILED", "Kuota Max habis; review menunggu, tidak ada fallback.", evidence=keep,
                       error_code="QUOTA_EXHAUSTED")
    if result.get("ok") and (result.get("mode") in (None, "NATIVE")):
        return Outcome("VERIFIED", f"Bridge berhasil: model teresolusi {result.get('model_resolved') or 'UNKNOWN'}, "
                       f"CLI {result.get('engine_version') or 'UNKNOWN'} (skenario: panggilan terstruktur kecil).",
                       evidence=keep, component_version=result.get("engine_version"),
                       observations=[{"component_id": "claude-code", "dimension": "functional", "status": "VERIFIED",
                                      "observed_version": result.get("engine_version")}])
    return Outcome("FAILED", f"Capability test tidak lulus ({result.get('error_code') or 'tidak diketahui'}).",
                   evidence=keep, error_code=str(result.get("error_code") or "CAPABILITY_TEST_FAILED"))
