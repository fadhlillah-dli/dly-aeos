"""Locate and probe the official Claude Code binary; parse the native auth mode (§12, §08).

Only the unmodified official binary with the owner's native claude.ai login (Max) is accepted.
``claude auth status`` runs with the same scrubbed environment the bridge uses, so its answer is
what a bridge child would actually see. Anything other than ``loggedIn=true``,
``authMethod=claude.ai`` and ``apiProvider=firstParty`` is BLOCKED — never an API fallback.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aeos.claude_bridge.env import child_env
from aeos.core.config import Settings
from aeos.core.hashing import file_sha256
from aeos.core.redaction import redact_text

REQUIRED_AUTH_METHOD = "claude.ai"
REQUIRED_API_PROVIDER = "firstParty"
EXPECTED_SUBSCRIPTION = "max"
_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")
_PROBE_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class BinaryInfo:
    found: bool
    path: Path | None = None
    source: str = ""  # settings.claude_binary | PATH
    version: str | None = None
    fingerprint: str | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def logical_path(self) -> str:
        """§37: local paths can be sensitive — the UI gets a logical name, not the full path."""
        if not self.path:
            return "UNKNOWN"
        return f"{self.path.name} ({self.source})"


@dataclass(frozen=True)
class AuthInfo:
    status: str  # VERIFIED | DEGRADED | BLOCKED | UNKNOWN
    logged_in: bool | None = None
    auth_method: str | None = None
    api_provider: str | None = None
    subscription_type: str | None = None
    email: str | None = None  # the owner's own account e-mail; never tokens
    reasons: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "loggedIn": self.logged_in,
            "authMethod": self.auth_method,
            "apiProvider": self.api_provider,
            "subscriptionType": self.subscription_type,
            "email": self.email,
            "reasons": list(self.reasons),
        }


def _is_batch_shim(path: Path) -> bool:
    return path.suffix.lower() in {".cmd", ".bat", ".ps1"}


def locate_binary(settings: Settings) -> BinaryInfo:
    """Find the official binary. On Windows only a real ``claude.exe`` is accepted: npm ``.cmd``
    shims go through cmd.exe, whose quoting cannot safely carry the JSON schema argument."""
    candidates: list[tuple[Path, str]] = []
    if settings.claude_binary:
        candidates.append((Path(settings.claude_binary).expanduser(), "settings.claude_binary"))
    else:
        names = ("claude.exe", "claude") if sys.platform == "win32" else ("claude",)
        for name in names:
            found = shutil.which(name)
            if found:
                candidates.append((Path(found), "PATH"))
                break
    if not candidates:
        return BinaryInfo(found=False, reasons=["official Claude Code binary not found on PATH"])
    path, source = candidates[0]
    if not path.is_file():
        return BinaryInfo(found=False, path=path, source=source, reasons=[f"configured binary does not exist ({source})"])
    if sys.platform == "win32" and _is_batch_shim(path):
        return BinaryInfo(
            found=False, path=path, source=source,
            reasons=["only a script shim (.cmd/.bat/.ps1) was found; install the native build "
                     "(`claude install` or `winget install Anthropic.ClaudeCode`) so arguments are not re-parsed by cmd.exe"],
        )
    return BinaryInfo(found=True, path=path, source=source)


def neutral_cwd() -> str:
    """Working directory for probes: never the controller's cwd (it may be a repository whose
    project settings/hooks the client would read, S05). The OS temp dir holds no project config."""
    return tempfile.gettempdir()


def _run(argv: list[str], *, timeout: float = 30.0) -> tuple[int, str, str]:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace", env=child_env(),
        stdin=subprocess.DEVNULL, timeout=timeout, check=False, cwd=neutral_cwd(), **kwargs,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


_SIG_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


def publisher_signature(path: Path | None, *, fingerprint_value: str | None = None,
                        run: Any = None) -> dict[str, Any]:
    """§12 "unmodified official binary": the OS code-signature report of the located binary.

    Windows: ``Get-AuthenticodeSignature``; macOS: ``codesign --verify`` + ``codesign -dv`` (TeamIdentifier).
    The result is reported (and cached per fingerprint), never assumed: VALID only when the OS verifies the
    signature; INVALID/UNSIGNED/UNKNOWN otherwise. Whether the signer is Anthropic is shown as the observed
    signer - AEOS has no pinned publisher identity, so authenticity stays UNVERIFIED without it."""
    if path is None:
        return {"status": "UNKNOWN", "method": None, "reason": "binary not available"}
    key = (str(path), fingerprint_value or "")
    if run is None and key in _SIG_CACHE:
        return _SIG_CACHE[key]
    runner = run or _run
    result: dict[str, Any]
    try:
        if sys.platform == "win32":
            quoted = str(path).replace("'", "''")  # PowerShell single-quoted literal: no expansion, quotes doubled
            script = (f"$s = Get-AuthenticodeSignature -LiteralPath '{quoted}'; "
                      "[pscustomobject]@{Status=[string]$s.Status; Signer=[string]$s.SignerCertificate.Subject} "
                      "| ConvertTo-Json -Compress")
            rc, out, _err = runner(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=30)
            data = json.loads(out.strip() or "{}") if rc == 0 else {}
            status = str(data.get("Status") or "")
            result = {"method": "Authenticode", "signer": data.get("Signer") or None,
                      "status": {"Valid": "VALID", "NotSigned": "UNSIGNED", "HashMismatch": "INVALID",
                                 "NotTrusted": "INVALID"}.get(status, "UNKNOWN"), "os_status": status or None}
        elif sys.platform == "darwin":
            rc, _out, err = runner(["codesign", "--verify", "--strict", str(path)], timeout=30)
            _rc2, out2, err2 = runner(["codesign", "-dv", "--verbose=2", str(path)], timeout=30)
            text = (out2 or "") + (err2 or "")
            team = re.search(r"TeamIdentifier=(\S+)", text)
            unsigned = "not signed" in (err or "").lower() or "not signed" in text.lower()
            result = {"method": "codesign", "signer": team.group(1) if team else None,
                      "status": "VALID" if rc == 0 else ("UNSIGNED" if unsigned else "INVALID")}
        else:
            result = {"method": None, "signer": None, "status": "UNKNOWN",
                      "reason": "no OS code-signature check on this platform"}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        result = {"method": None, "signer": None, "status": "UNKNOWN", "reason": type(exc).__name__}
    pin = approved_publisher()
    result["publisher_pin"] = (f"{pin} (owner-pinned in the install-manifest; see the authenticity gate)" if pin else
                               "NONE (install-manifest has no approved pin; authenticity UNVERIFIED)")
    if run is None:
        _SIG_CACHE[key] = result
    return result


def invocation_signature(path: Path | None, fingerprint_value: str | None) -> dict[str, Any]:
    """The OS signature report that gates a native invocation: checked once per binary fingerprint per process
    (``publisher_signature`` caches it), so a swapped binary is checked again before it is used."""
    return publisher_signature(path, fingerprint_value=fingerprint_value)


def approved_publisher() -> str | None:
    """Owner-pinned Claude Code publisher identity from the reviewed InstallManifest (None = not pinned)."""
    try:
        from aeos.inventory import facade as inventory

        return inventory.approved_signer("claude-code")
    except ImportError:
        return None


# One RDN of an X.500 subject as Windows prints it: ``KEY=value`` or ``KEY="value, with commas"``; an unquoted value
# runs up to the next ``, KEY=`` (so ``CN=Anthropic, PBC, O=...`` reads CN as ``Anthropic, PBC``).
_RDN = re.compile(r'([A-Za-z][A-Za-z0-9.]*)=("(?:[^"]|"")*"|.*?)(?=,\s*[A-Za-z][A-Za-z0-9.]*=|$)')


def _norm_identity(value: str) -> str:
    return " ".join(value.split()).casefold()


def signer_matches(observed: str | None, approved: str | None) -> bool:
    """Exact publisher-identity match, never a substring: the pin equals the whole observed signer (Authenticode
    subject / macOS TeamIdentifier), one whole ``KEY=value`` component of the subject, or the exact CN / O value.
    A certificate for "Anthropic, PBC Labs" therefore never satisfies the pin "Anthropic, PBC"."""
    obs, want = _norm_identity(observed or ""), _norm_identity(approved or "")
    if not obs or not want:
        return False
    if obs == want:
        return True
    for key, raw in _RDN.findall(observed or ""):
        value = raw[1:-1].replace('""', '"') if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"') else raw
        k = key.upper()
        if want == _norm_identity(f"{k}={value}") or (k in ("CN", "O") and want == _norm_identity(value)):
            return True
    return False


def signature_gate(sig: dict[str, Any] | None, approved_signer: str | None) -> tuple[str, str]:
    """What the observed OS code signature means for the bridge (§12 "unmodified official binary").

    * ``BLOCKED``: the OS reports the signature INVALID (e.g. Authenticode HashMismatch / NotTrusted, codesign
      verify failure); or the owner pinned a publisher identity for this OS and the binary is not proven to be
      it - the signature is not VALID (UNSIGNED, UNKNOWN, not checked) or the observed signer is not exactly
      the pin (``signer_matches``: whole subject, whole RDN or exact CN/O value - never a substring);
    * ``DEGRADED``: no pin, and the binary is not signed (reported, never treated as the official build);
    * ``OK``: VALID and the observed signer matches the owner-pinned identity;
    * ``UNVERIFIED``: no pin, and VALID, or no check ran / UNKNOWN (never shown as authentic)."""
    status = str((sig or {}).get("status") or "NOT_CHECKED")
    signer = str((sig or {}).get("signer") or "")
    pin_ref = "install-manifest claude-code signature.approved_signer"
    if status == "INVALID":
        return "BLOCKED", "OS reports the code signature of the claude binary INVALID (modified or untrusted build)"
    if approved_signer:
        if status != "VALID":
            # a pinned publisher must be PROVEN: an unsigned or unverifiable binary is not that publisher's build
            return "BLOCKED", (f"code signature {status}: the binary cannot be shown to be signed by the owner-pinned "
                               f"publisher {approved_signer!r} ({pin_ref})")
        if not signer_matches(signer, approved_signer):
            return "BLOCKED", (f"observed signer {signer or 'unknown'!r} is not the owner-pinned publisher "
                               f"{approved_signer!r} ({pin_ref})")
        return "OK", f"signature VALID and signer matches the pinned publisher {approved_signer!r}"
    if status == "UNSIGNED":
        return "DEGRADED", "the claude binary is not code-signed; it cannot be shown to be the official build"
    if status == "VALID":
        return "UNVERIFIED", "signature VALID; no publisher identity pinned in the install-manifest (owner step)"
    return "UNVERIFIED", f"code signature {status}: authenticity not established"


SIGNATURE_REMEDY: dict[str, Any] = {
    "operation": "Pasang ulang build resmi Claude Code (distribusi Anthropic), lalu periksa tanda tangan OS-nya",
    "windows": ["winget install --id Anthropic.ClaudeCode -e",
                "(Get-AuthenticodeSignature (Get-Command claude.exe).Source).SignerCertificate.Subject"],
    "darwin": ["brew reinstall --cask claude-code", 'codesign -dv --verbose=2 "$(which claude)" 2>&1 | grep TeamIdentifier'],
    "pin_note": "Bila penerbit resmi memang berubah, ubah signature.approved_signer (per OS: windows / darwin) di "
                "install-manifest/manifest.yaml lewat PR yang direview; jangan menonaktifkan pemeriksaan.",
}


def authenticity_view(sig: dict[str, Any] | None, approved_signer: str | None, *, found: bool = True
                      ) -> dict[str, Any]:
    """The §12 authenticity claim as status pages and the Doctor show it: the gate, its reason, the pin that applies
    on this OS and - when not authentic - the signature-specific remedy (never the login remedy)."""
    from aeos.inventory import facade as inventory

    status = str((sig or {}).get("status") or "NOT_CHECKED")
    if not found:
        gate, reason = "UNVERIFIED", "no binary"
    elif status == "NOT_CHECKED":
        gate, reason = "NOT_CHECKED", ("OS signature not checked yet in this process (refresh); every native call "
                                       "checks it first" + (" and is BLOCKED unless VALID and the pinned signer"
                                                            if approved_signer else ""))
    else:
        gate, reason = signature_gate(sig, approved_signer)
    os_name = inventory.host_platform()[0]
    return {"gate": gate, "reason": reason, "approved_signer": approved_signer, "os": os_name,
            "signature_status": status, "observed_signer": (sig or {}).get("signer"),
            "owner_step": None if approved_signer else
            f"pin the Anthropic signer for {os_name} (windows: Authenticode subject / CN / O; darwin: TeamIdentifier) "
            "in install-manifest claude-code signature.approved_signer",
            "remedy": None if gate in ("OK", "UNVERIFIED", "NOT_CHECKED") else
            {"operation": SIGNATURE_REMEDY["operation"], "commands": SIGNATURE_REMEDY.get(os_name, []),
             "note": SIGNATURE_REMEDY["pin_note"]}}


_fp_cache: dict[tuple[str, int, int], str] = {}
_fp_lock = threading.Lock()


def fingerprint(path: Path) -> str | None:
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_size, int(st.st_mtime))
    with _fp_lock:
        if key in _fp_cache:
            return _fp_cache[key]
    fp = file_sha256(path)
    with _fp_lock:
        _fp_cache[key] = fp
    return fp


def probe_version(info: BinaryInfo) -> BinaryInfo:
    if not info.found or info.path is None:
        return info
    try:
        rc, out, err = _run([str(info.path), "--version"], timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return BinaryInfo(found=False, path=info.path, source=info.source,
                          reasons=[f"binary could not be executed: {type(exc).__name__}"])
    m = _VERSION_RE.search(out)
    reasons = [] if rc == 0 and m else [f"`claude --version` failed (exit {rc}): {redact_text(err.strip())[:200]}"]
    return BinaryInfo(found=rc == 0 and bool(m), path=info.path, source=info.source,
                      version=m.group(1) if m else None, fingerprint=fingerprint(info.path), reasons=reasons)


def parse_auth_status(rc: int, stdout: str, stderr: str = "") -> AuthInfo:
    """Interpret ``claude auth status`` JSON. Pure function (unit-tested with fixtures)."""
    try:
        data = json.loads(stdout.strip() or "{}")
    except json.JSONDecodeError:
        return AuthInfo(status="UNKNOWN", reasons=[f"`claude auth status` output is not JSON (exit {rc})"])
    if not isinstance(data, dict):
        return AuthInfo(status="UNKNOWN", reasons=["`claude auth status` returned an unexpected shape"])
    logged_in = data.get("loggedIn")
    method = data.get("authMethod")
    provider = data.get("apiProvider")
    sub = data.get("subscriptionType")
    email = data.get("email") if isinstance(data.get("email"), str) else None
    reasons: list[str] = []
    if logged_in is not True:
        reasons.append("not logged in: run `claude` and complete the official claude.ai login (/login)")
    if method != REQUIRED_AUTH_METHOD:
        reasons.append(f"authMethod is {method!r}; only the native claude.ai (Max) login is allowed - "
                       "API keys/console/third-party auth are never used as a fallback")
    if provider != REQUIRED_API_PROVIDER:
        reasons.append(f"apiProvider is {provider!r}; Bedrock/Vertex/Foundry/other providers are not allowed")
    if reasons:
        return AuthInfo(status="BLOCKED", logged_in=logged_in if isinstance(logged_in, bool) else None,
                        auth_method=method, api_provider=provider, subscription_type=sub, email=email, reasons=reasons)
    if sub != EXPECTED_SUBSCRIPTION:
        return AuthInfo(status="DEGRADED", logged_in=True, auth_method=method, api_provider=provider,
                        subscription_type=sub, email=email,
                        reasons=[f"subscriptionType is {sub!r} (expected 'max'): supervisor model availability and "
                                 "limits differ; Fable may be unavailable"])
    return AuthInfo(status="VERIFIED", logged_in=True, auth_method=method, api_provider=provider,
                    subscription_type=sub, email=email)


def probe_auth(info: BinaryInfo) -> AuthInfo:
    if not info.found or info.path is None:
        return AuthInfo(status="UNKNOWN", reasons=["binary not available"])
    try:
        rc, out, err = _run([str(info.path), "auth", "status"], timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return AuthInfo(status="UNKNOWN", reasons=[f"`claude auth status` failed: {type(exc).__name__}"])
    return parse_auth_status(rc, out, err)


@dataclass(frozen=True)
class Probe:
    binary: BinaryInfo
    auth: AuthInfo
    checked_at: float

    @property
    def usable(self) -> bool:
        return self.binary.found and self.auth.status in {"VERIFIED", "DEGRADED"}

    @property
    def reasons(self) -> list[str]:
        return [*self.binary.reasons, *self.auth.reasons]


_probe_lock = threading.Lock()
_probe_cache: dict[str, Probe] = {}


def probe(settings: Settings, *, refresh: bool = False) -> Probe:
    """Binary + version + fingerprint + auth, cached briefly (status page polling)."""
    key = settings.claude_binary or "<PATH>"
    now = time.monotonic()
    with _probe_lock:
        cached = _probe_cache.get(key)
    if cached and not refresh and now - cached.checked_at < _PROBE_TTL_SECONDS:
        return cached
    info = probe_version(locate_binary(settings))
    auth = probe_auth(info) if info.found else AuthInfo(status="UNKNOWN", reasons=["binary not available"])
    result = Probe(binary=info, auth=auth, checked_at=now)
    with _probe_lock:
        _probe_cache[key] = result
    return result


def clear_probe_cache() -> None:
    with _probe_lock:
        _probe_cache.clear()
