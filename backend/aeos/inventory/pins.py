"""Owner-approved pins of the reviewed InstallManifest, resolved for THIS host (§07, §12, §25, §37).

The owner runs DLY AEOS on Windows and macOS. A version pin is the same on every platform, but the
executable is not: ``approved_digest`` may therefore be one ``sha256:<hex>`` for every platform (the
scalar form) or a mapping keyed ``<os>_<arch>`` (``windows_amd64``, ``darwin_arm64``, ...), and
``signature.approved_signer`` may be one publisher identity or a mapping keyed by OS (``windows`` =
the Authenticode subject / CN / O, ``darwin`` = the TeamIdentifier).

A mapping without an entry for this host means *not recorded here* - never a mismatch and never
another platform's value. Nothing in this module invents a pin: every value comes from the reviewed
manifest (or its seeded copy), and absence is reported as such.
"""

from __future__ import annotations

import platform
import re
import sys
from typing import Any

PLATFORM_OSES = ("windows", "darwin", "linux")
PLATFORM_ARCHES = ("amd64", "arm64")
SIGNER_OSES = ("windows", "darwin")  # the OSes with an OS code-signature check (Authenticode / codesign)
PLATFORM_KEY = re.compile(r"^(windows|darwin|linux)_(amd64|arm64)$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARCH = {"amd64": "amd64", "x86_64": "amd64", "x64": "amd64", "arm64": "arm64", "aarch64": "arm64"}


def host_os() -> str:
    """``windows`` | ``darwin`` | ``linux`` (the OpenBao release naming, also used for the pin keys)."""
    return {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")


def host_arch() -> str | None:
    """``amd64`` | ``arm64``, or None for an architecture no reviewed artifact exists for."""
    return _ARCH.get(platform.machine().lower())


def platform_key(os_name: str | None = None, arch: str | None = None) -> str | None:
    """``<os>_<arch>`` of this host (or of the given parts); None when the architecture is unsupported."""
    os_name = os_name or host_os()
    arch = arch or host_arch()
    if not arch:
        return None
    key = f"{os_name}_{arch}"
    return key if PLATFORM_KEY.match(key) else None


def select_digest(raw: Any, key: str | None = None) -> dict[str, Any]:
    """The approved digest that applies to platform *key* (default: this host).

    Returns ``{"digest", "status", "platform", "form", "recorded_platforms", "reason"}`` where status is
    ``RECORDED`` (a valid digest applies), ``NOT_RECORDED`` (nothing recorded for this platform - never a
    mismatch) or ``INVALID`` (a malformed value; callers fail closed)."""
    key = key if key is not None else platform_key()
    out: dict[str, Any] = {"digest": None, "status": "NOT_RECORDED", "platform": key, "form": None,
                           "recorded_platforms": [], "reason": None}
    if raw in (None, "", {}):
        out["reason"] = "no approved_digest recorded in the install-manifest"
        return out
    if isinstance(raw, dict):
        out["form"] = "per_platform"
        out["recorded_platforms"] = sorted(str(k) for k in raw)
        bad_keys = [str(k) for k in raw if not PLATFORM_KEY.match(str(k))]
        if bad_keys:
            out.update(status="INVALID", reason=f"approved_digest keys must be <os>_<arch>: {bad_keys}")
            return out
        value = raw.get(key) if key else None
        if not value:
            out["reason"] = (f"approved_digest has no entry for this platform ({key or 'unsupported architecture'}); "
                             f"recorded for {out['recorded_platforms']}")
            return out
    elif isinstance(raw, str):
        out["form"] = "all_platforms"
        value = raw
    else:
        out.update(status="INVALID", reason=f"approved_digest must be a string or a mapping, not {type(raw).__name__}")
        return out
    digest = str(value).strip().lower()
    if not DIGEST.match(digest):
        out.update(status="INVALID", reason=f"approved_digest for {key} is not 'sha256:<64 hex>': {value!r}")
        return out
    out.update(digest=digest, status="RECORDED", reason=None)
    return out


def select_signer(raw: Any, os_name: str | None = None) -> str | None:
    """The owner-pinned publisher identity that applies on *os_name* (default: this host), or None.

    A plain string applies on every OS; a mapping ``{windows: ..., darwin: ...}`` applies per OS and has no
    value for an OS it does not name (not pinned there - authenticity stays UNVERIFIED, never borrowed)."""
    os_name = os_name or host_os()
    if isinstance(raw, dict):
        raw = raw.get(os_name)
    if raw is None or isinstance(raw, (dict, list)):
        return None
    value = str(raw).strip()
    return value or None


def normalise_pin(raw: Any) -> str | None:
    """An approved version pin as the runtime compares it (``v1.20.0`` -> ``1.20.0``); None when unset."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        raw = raw.get("version")
    value = str(raw or "").strip()
    if value[:1] in ("v", "V"):
        value = value[1:]
    return value or None
