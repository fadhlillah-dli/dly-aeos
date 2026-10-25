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
from aeos.doctor.checks._common import (
    binary_fingerprint,
    binary_outcome,
    logical,
    resolve_binary,
    run_version,
)
from aeos.doctor.model import CheckContext, Outcome, Remedy, os_install_command
from aeos.inventory.probes import PROVIDER_ENV_NAMES, scrubbed_env

FORBIDDEN_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", *PROVIDER_ENV_NAMES)
AUTH_FIELDS = ("loggedIn", "authMethod", "apiProvider", "subscriptionType")
LOGIN_DOCS = "https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan"
SETUP_DOCS = "https://docs.claude.com/en/docs/claude-code/setup"
SHIM_SUFFIXES = (".cmd", ".bat", ".ps1")
_fp_claude = binary_fingerprint("claude", "claude_binary")
CREDENTIAL_FILES = (".credentials.json",)  # official client's credential file (Windows/Linux; macOS: Keychain)


def _fp_claude_auth(ctx: CheckContext) -> dict[str, Any]:
    """Binary identity plus METADATA (presence and size) of the official client's credential file: a logout
    (file removed/emptied) or a new login changes it, so a stored auth result goes STALE at once instead of at TTL
    expiry. The modification time is deliberately NOT part of it: the client rewrites the file on every routine
    token refresh (same size), which would make every gate that needs claude.auth (e.g. CAN_REVIEW_WITH_MAX)
    UNKNOWN after each Claude call. The file content is never read (§12: no token extraction)."""
    from pathlib import Path

    base = Path(ctx.env.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    files = {name: ctx.runner.stat(str(base / name)) for name in CREDENTIAL_FILES}
    return {**_fp_claude(ctx), "credential_metadata": {
        k: ({"present": True, "size": v.get("size")} if v is not None else {"present": False})
        for k, v in files.items()}}


def install_remedy() -> Remedy:
    # Package-manager builds of the official distribution (no piped install script, §07); Linux distributions
    # differ, so the remedy points to the official setup page instead of guessing a command.
    return Remedy(
        operation="Pasang Claude Code resmi (distribusi Anthropic, build native) lalu login lewat alur resmi Max",
        command=os_install_command(windows="winget install --id Anthropic.ClaudeCode -e",
                                   macos="brew install --cask claude-code"),
        source=f"dokumentasi resmi Claude Code ({SETUP_DOCS}; S03: {LOGIN_DOCS})",
        target="PATH atau setelan claude_binary", privilege="pengguna lokal", cost="langganan Max milik pemilik",
        rollback="uninstall lewat package manager yang sama", evidence_to_collect="claude --version; claude auth status",
    )


INSTALL_REMEDY = install_remedy()  # import-time copy (current OS); checks call install_remedy()


def _pinned_signer() -> str | None:
    from aeos.claude_bridge import binary as binmod

    return binmod.approved_publisher()


def _fp_claude_binary(ctx: CheckContext) -> dict[str, Any]:
    """Binary identity plus the publisher pin that applies on this OS: pinning (or changing) the signer in the
    reviewed manifest makes a stored authenticity result STALE."""
    return {**_fp_claude(ctx), "approved_signer": _pinned_signer()}


def signature_remedy() -> Remedy:
    from aeos.claude_bridge.binary import SIGNATURE_REMEDY

    return Remedy(
        operation=SIGNATURE_REMEDY["operation"] + ". " + SIGNATURE_REMEDY["pin_note"],
        command=os_install_command(windows=" ; ".join(SIGNATURE_REMEDY["windows"]),
                                   macos=" ; ".join(SIGNATURE_REMEDY["darwin"])),
        source=f"dokumentasi resmi Claude Code ({SETUP_DOCS}); install-manifest claude-code signature.approved_signer",
        target="binary claude di PATH atau setelan claude_binary", privilege="pengguna lokal (pin baru: reviewer manusia)",
        cost="tidak ada", rollback="uninstall lewat package manager yang sama",
        evidence_to_collect="claude.binary VERIFIED dengan keaslian OK (tanda tangan VALID, penerbit cocok dengan pin)",
    )


@check("claude.binary", "CLAUDE_NATIVE", "Binary Claude Code resmi dan versinya", component_id="claude-code",
       ttl_seconds=86400, invalidators=("binary:claude",), fingerprint=_fp_claude_binary, severity_fail="HIGH",
       observation="installation")
def claude_binary(ctx: CheckContext) -> Outcome:
    path, _lookup = resolve_binary(ctx, "claude", "claude_binary")
    if path and sys.platform == "win32" and path.lower().endswith(SHIM_SUFFIXES):
        # Same rule as the bridge: a .cmd/.ps1 shim goes through cmd.exe/PowerShell re-parsing, which cannot carry
        # the JSON schema argument safely, so the bridge refuses it (ADR-0040).
        return Outcome("FAILED", "Hanya shim skrip Claude Code (.cmd/.bat/.ps1) yang ditemukan; bridge hanya menerima "
                       "claude.exe native.", evidence={"location": logical(ctx, path)}, error_code="BINARY_IS_SHIM",
                       remedy=install_remedy(), target_identity=logical(ctx, path))
    out = binary_outcome(ctx, name="claude", override_attr="claude_binary", args=["--version"],
                         regex=r"(\d+\.\d+\.\d+)", missing_remedy=install_remedy(), label="Claude Code", timeout=20.0)
    if out.status != "VERIFIED" or not path:
        return out
    return _with_authenticity(out, path)


def _with_authenticity(out: Outcome, path: str) -> Outcome:
    """§12 "unmodified official binary": the same OS code-signature gate the bridge applies before every native
    call (checked once per binary fingerprint). BLOCKED fails the check with a signature remedy (never /login);
    DEGRADED/UNVERIFIED/NOT_CHECKED stay visible in the evidence and summary, never shown as authentic."""
    from pathlib import Path

    from aeos.claude_bridge import binary as binmod

    p = Path(path)
    sig = binmod.invocation_signature(p, binmod.fingerprint(p))
    pinned = _pinned_signer()
    auth = binmod.authenticity_view(sig, pinned)
    gate = auth["gate"]
    evidence = {**out.evidence, "authenticity": {k: auth[k] for k in ("gate", "reason", "approved_signer", "os",
                                                                      "signature_status", "observed_signer")},
                "distribution_verified": gate == "OK"}
    if gate == "BLOCKED":
        return Outcome("FAILED", f"{out.summary.split(' (skenario')[0]}, tetapi keaslian binary DIBLOKIR: "
                       f"{auth['reason']}. Bridge menolak panggilan native; gunakan paket manual.",
                       evidence=evidence, component_version=out.component_version, target_identity=out.target_identity,
                       error_code="BINARY_SIGNATURE_BLOCKED", severity="HIGH", remedy=signature_remedy(),
                       # the binary is installed (its authenticity failed): the inventory still records INSTALLED
                       observations=[{"component_id": "claude-code", "dimension": "installation",
                                      "status": "INSTALLED", "observed_version": out.component_version}])
    label = {"OK": "tanda tangan VALID dan penerbit cocok dengan pin pemilik",
             "DEGRADED": "binary TIDAK ditandatangani (DEGRADED): tidak dapat dibuktikan sebagai build resmi",
             "UNVERIFIED": "keaslian UNVERIFIED (" + ("tidak ada pin penerbit untuk OS ini" if not pinned
                                                     else "tanda tangan tidak dapat diperiksa") + ")",
             "NOT_CHECKED": "tanda tangan OS belum diperiksa"}.get(gate, gate)
    return Outcome(out.status, f"{out.summary.split(' (skenario')[0]} (skenario: perintah versi berhasil; {label}).",
                   evidence=evidence, component_version=out.component_version, target_identity=out.target_identity,
                   remedy=signature_remedy() if gate == "DEGRADED" else out.remedy, observations=out.observations)


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
       fingerprint=_fp_claude_auth, severity_fail="HIGH", observation="connectivity")
def claude_auth(ctx: CheckContext) -> Outcome:
    path, _lookup = resolve_binary(ctx, "claude", "claude_binary")
    if not path:
        return Outcome("MISSING", "Claude Code tidak ditemukan; status login tidak dapat diperiksa.",
                       error_code="BINARY_MISSING", remedy=install_remedy())
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
                   severity="MEDIUM", evidence={
                       "source": "tidak ada API resmi untuk status ini",
                       # advisory for the Max gates, but true even when they are READY (no silent paid usage, §12)
                       "residual_risks": ["Extra usage berbayar akun Claude UNKNOWN: bila aktif, pemakaian di atas kuota "
                                          "Max dapat ditagih; pastikan nonaktif di pengaturan akun (§12)"]},
                   remedy=Remedy(operation="Periksa manual pengaturan usage di akun Claude dan pastikan extra usage "
                                 "berbayar nonaktif (§12)", source=f"S03: {LOGIN_DOCS}", privilege="pemilik akun",
                                 cost="mencegah tagihan tambahan", evidence_to_collect="catatan keputusan pemilik"))


@check("claude.quota", "CLAUDE_NATIVE", "Status kuota Max", component_id="claude-code", ttl_seconds=3600,
       ok=frozenset(), severity_fail="LOW")
def claude_quota(ctx: CheckContext) -> Outcome:
    return Outcome("UNKNOWN", "Kuota mingguan Max (termasuk Fable) tidak diekspos secara resmi; ditampilkan UNKNOWN. "
                   "Saat kuota habis, review menunggu (tidak ada fallback).", severity="LOW",
                   evidence={"source": "S02/S03 (batas mingguan); tidak ada probe resmi"})


MANUAL_FALLBACK = ("jalur paket manual §12 tetap tersedia (dijalankan pemilik di Claude Code resmi); otomatisasi "
                   "NOT-PROVEN, tidak pernah diganti Claude API")
CAPABILITY_TEST_MAX_AGE_DAYS = 7  # same window as aeos.claude_bridge (a week-old proof is STALE)
_KEEP = ("status", "engine_version", "model_requested", "model_resolved", "error_code", "quota_exhausted", "mode",
         "checked_at", "evidence_id", "duration_ms")


def _bridge_result_outcome(result: dict[str, Any], *, fresh: bool, current_version: str | None = None) -> Outcome:
    """Map the ClaudeNativeBridge capability-test contract (``status`` VERIFIED|DEGRADED|FAILED|BLOCKED,
    ``mode`` LIVE) onto a CheckResult. Never READY-by-narration: only the bridge's own recorded
    structured call counts; DEGRADED/BLOCKED/never-run leave the manual fallback (DEGRADED gate)."""
    keep = {k: result.get(k) for k in _KEEP if k in result}
    keep["source"] = "uji baru (--functional)" if fresh else "uji terakhir yang direkam ClaudeNativeBridge"
    status = str(result.get("status") or "").upper()
    version = result.get("engine_version")
    # fail closed: a result that does not say it is LIVE is not proof (the bridge always records mode LIVE)
    mode = str(result.get("mode") or "").upper()
    when = f" pada {result.get('checked_at')}" if result.get("checked_at") and not fresh else ""
    if mode not in ("LIVE", "NATIVE"):
        return Outcome("UNKNOWN", f"Hasil uji bermode {mode or 'tanpa mode'} bukan bukti (hanya LIVE dihitung; "
                       f"MOCK/DEMO tidak); {MANUAL_FALLBACK}.", evidence=keep, error_code="CAPABILITY_TEST_NOT_LIVE")
    if result.get("quota_exhausted") or any(isinstance(s, dict) and s.get("status") == "EXHAUSTED"
                                            for s in result.get("steps") or []):
        return Outcome("FAILED", f"Kuota Max habis{when}; review menunggu, tidak ada fallback ke API/route lain.",
                       evidence=keep, error_code="QUOTA_EXHAUSTED", component_version=version)
    if current_version and version and current_version != version:
        return Outcome("UNKNOWN", f"Uji terakhir dibuat dengan Claude Code {version}, sekarang {current_version}: "
                       f"bukti STALE; jalankan ulang dengan --functional ({MANUAL_FALLBACK}).", evidence=keep,
                       error_code="CAPABILITY_TEST_STALE", component_version=version)
    if status == "VERIFIED":
        return Outcome("VERIFIED", f"Bridge berhasil{when}: model teresolusi {result.get('model_resolved') or 'UNKNOWN'}, "
                       f"CLI {version or 'UNKNOWN'} (skenario: satu panggilan terstruktur kecil lewat Claude Code resmi, "
                       "login native Max).", evidence=keep, component_version=version,
                       observations=[{"component_id": "claude-code", "dimension": "functional", "status": "VERIFIED",
                                      "observed_version": version}] if fresh else [])
    if status == "DEGRADED":
        return Outcome("CONFIGURED", f"Bridge menjawab{when} tetapi langganan bukan Max (DEGRADED): ketersediaan/limit "
                       "model pengawas berbeda; gate tidak READY.", evidence=keep, error_code="BRIDGE_DEGRADED",
                       component_version=version, severity="MEDIUM")
    if status == "BLOCKED":
        return Outcome("UNKNOWN", f"Bridge BLOCKED{when} (binary/login native tidak dapat dipakai: "
                       f"{result.get('error_code') or 'BRIDGE_BLOCKED'}); {MANUAL_FALLBACK}.", evidence=keep,
                       error_code="BRIDGE_BLOCKED", component_version=version)
    return Outcome("FAILED", f"Capability test tidak lulus{when} ({result.get('error_code') or 'tidak diketahui'}); "
                   f"{MANUAL_FALLBACK}.", evidence=keep, component_version=version,
                   error_code=str(result.get("error_code") or "CAPABILITY_TEST_FAILED"))


def _last_recorded_test(ctx: CheckContext) -> dict[str, Any] | None:
    """Latest capability test persisted by the bridge (``aeos bridge test``, ``doctor --functional`` or the
    dashboard), with the bridge's own STALE marks and the 7-day window. Read-only."""
    from datetime import timedelta

    from sqlalchemy import select

    from aeos.core.timeutil import ensure_aware
    from aeos.models.claude_bridge_ext import ClaudeCapabilityTest

    assert ctx.session is not None
    row = ctx.session.scalars(select(ClaudeCapabilityTest).order_by(ClaudeCapabilityTest.created_at.desc())).first()
    if row is None:
        return None
    created = ensure_aware(row.created_at)
    stale = row.stale_reason
    if not stale and created is not None and ctx.now - created > timedelta(days=CAPABILITY_TEST_MAX_AGE_DAYS):
        stale = f"lebih tua dari {CAPABILITY_TEST_MAX_AGE_DAYS} hari"
    return {"status": row.status, "engine_version": row.engine_version, "model_requested": row.model_requested,
            "model_resolved": row.model_resolved, "error_code": row.error_code, "mode": row.mode,
            "checked_at": created.isoformat() if created else None, "evidence_id": row.evidence_id,
            "duration_ms": row.duration_ms, "steps": row.steps or [], "stale_reason": stale, "id": row.id}


# ``model:route:SUPERVISOR``: only a supervisor route change (claude-native alias re-resolved, status/config of a
# supervisor route) invalidates the bridge proof; a WORKER route activation/disable must not push every review
# to the manual fallback (verification.review requires CAN_REVIEW_WITH_MAX READY).
@check("claude.capability_test", "CLAUDE_NATIVE", "Capability test ClaudeNativeBridge", component_id="claude-native-bridge",
       ttl_seconds=86400, invalidators=("binary:claude", "auth:claude", "model:route:SUPERVISOR"), needs_db=True,
       fingerprint=_fp_claude, severity_fail="HIGH", observation="functional",
       description="Tanpa --functional: membaca uji terakhir yang direkam bridge. Dengan --functional: satu panggilan "
                   "`claude -p` kecil lewat bridge (memakai kuota Max).")
def claude_capability_test(ctx: CheckContext) -> Outcome:
    if ctx.functional:
        try:
            from aeos.claude_bridge import facade as bridge

            result = bridge.capability_test()
        except (ImportError, ModuleNotFoundError):
            return Outcome("UNKNOWN", f"ClaudeNativeBridge belum tersedia; {MANUAL_FALLBACK}.",
                           error_code="BRIDGE_NOT_IMPLEMENTED")
        except Exception as exc:  # fail closed
            return Outcome("FAILED", f"Capability test gagal dijalankan ({type(exc).__name__}); {MANUAL_FALLBACK}.",
                           error_code="CAPABILITY_TEST_ERROR")
        if not isinstance(result, dict):
            return Outcome("UNKNOWN", "Hasil capability test tidak dikenali.", error_code="CAPABILITY_TEST_UNRECOGNISED")
        return _bridge_result_outcome(result, fresh=True)
    try:
        last = _last_recorded_test(ctx)
    except (ImportError, ModuleNotFoundError):
        return Outcome("UNKNOWN", f"ClaudeNativeBridge belum tersedia; {MANUAL_FALLBACK}.",
                       error_code="BRIDGE_NOT_IMPLEMENTED")
    if last is None:
        return Outcome("UNKNOWN", f"Bridge belum pernah diuji pada mesin ini (NOT-PROVEN); {MANUAL_FALLBACK}. Uji: "
                       "jalankan Doctor dengan --functional (satu panggilan kecil, memakai kuota Max).",
                       error_code="CAPABILITY_TEST_NOT_RUN")
    if last.get("stale_reason"):
        return Outcome("UNKNOWN", f"Uji terakhir ({last.get('checked_at')}) STALE: {last['stale_reason']}; "
                       f"{MANUAL_FALLBACK}.", evidence={k: last.get(k) for k in _KEEP if k in last},
                       error_code="CAPABILITY_TEST_STALE", component_version=last.get("engine_version"))
    path, _lookup = resolve_binary(ctx, "claude", "claude_binary")
    current = run_version(ctx, path, ["--version"], regex=r"(\d+\.\d+\.\d+)", timeout=20.0)[0] if path else None
    return _bridge_result_outcome(last, fresh=False, current_version=current)
