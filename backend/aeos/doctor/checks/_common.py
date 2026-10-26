"""Shared helpers for doctor checks (binary probes, fingerprints, secret status, safe imports)."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aeos.core.binaries import is_executable_file, locate_binary
from aeos.doctor.model import CheckContext, Outcome, Remedy
from aeos.inventory.probes import (
    clean_output,
    file_is_placeholder,
    logical_location,
    parse_version,
    path_fingerprint,
    scrubbed_env,
)

Resolver = Callable[[CheckContext], tuple[str | None, str]]
# A FingerprintFn that always yields a fingerprint (narrower than model.FingerprintFn, still assignable to it):
# callers such as claude._fp_claude_auth extend the result and need the non-None guarantee.
Fingerprint = Callable[[CheckContext], dict[str, Any]]


def resolve_binary(ctx: CheckContext, name: str, override_attr: str | None = None) -> tuple[str | None, str]:
    """Return (path, lookup) by the one rule the reconciler also uses (``aeos.core.binaries``): an explicit settings
    override (``~`` expanded, authoritative), then PATH; a regular executable file only, never a directory."""
    return _locate(ctx, name, getattr(ctx.settings, override_attr, None) if override_attr else None, None)


def _locate(ctx: CheckContext, name: str, configured: Any, data_dir: Path | None) -> tuple[str | None, str]:
    found = locate_binary(name, configured=configured, data_dir=data_dir,
                          is_executable=getattr(ctx.runner, "is_executable", is_executable_file),
                          which=ctx.runner.which)
    return found.path, found.lookup


def binary_fingerprint(name: str, override_attr: str | None = None, resolver: Resolver | None = None) -> Fingerprint:
    def fp(ctx: CheckContext) -> dict[str, Any]:
        path, lookup = resolver(ctx) if resolver else resolve_binary(ctx, name, override_attr)
        return {"binary": name, "lookup": lookup, **path_fingerprint(ctx.runner, path)}

    return fp


def file_fingerprint(rel: str) -> Fingerprint:
    def fp(ctx: CheckContext) -> dict[str, Any]:
        st = ctx.runner.stat(str(ctx.repo / rel))
        return {"file": rel, **(st or {"present": False})}

    return fp


def logical(ctx: CheckContext, path: str | Path | None) -> str | None:
    return logical_location(path, repo=ctx.repo, data_dir=ctx.data_dir)


def run_version(ctx: CheckContext, path: str, args: list[str], *, regex: str | None = None, timeout: float = 15.0,
                scrub: bool = True) -> tuple[str | None, Any]:
    env = scrubbed_env(ctx.env) if scrub else dict(ctx.env)
    rr = ctx.runner.run([path, *args], timeout=timeout, env=env, cwd=ctx.probe_cwd)
    version = parse_version(f"{rr.stdout}\n{rr.stderr}", regex) if rr.ok else None
    return version, rr


def binary_outcome(ctx: CheckContext, *, name: str, override_attr: str | None, args: list[str],
                   missing_remedy: Remedy, regex: str | None = None, timeout: float = 15.0,
                   label: str | None = None, resolver: Resolver | None = None) -> Outcome:
    label = label or name
    path, lookup = resolver(ctx) if resolver else resolve_binary(ctx, name, override_attr)
    if not path:
        where = f"setelan {override_attr}" if lookup == "settings" else ("<data>/bin dan PATH" if resolver else "PATH")
        return Outcome("MISSING", f"{label} tidak ditemukan ({where}).", evidence={"lookup": lookup},
                       error_code="BINARY_MISSING", remedy=missing_remedy)
    version, rr = run_version(ctx, path, args, regex=regex, timeout=timeout)
    evidence = {"lookup": lookup, "location": logical(ctx, path), "operation_ref": " ".join([name, *args]),
                "exit_code": rr.returncode, "distribution_verified": False}
    if version is None:
        evidence["output"] = clean_output(rr.stdout or rr.stderr, 200)
        return Outcome("FAILED", f"{label} ditemukan tetapi perintah versi gagal ({rr.error or rr.returncode}).",
                       evidence=evidence, error_code="VERSION_PROBE_FAILED", target_identity=evidence["location"])
    return Outcome("VERIFIED", f"{label} {version} ditemukan (skenario: perintah versi berhasil; keaslian "
                   "distribusi tidak diverifikasi).", evidence=evidence, component_version=version,
                   target_identity=evidence["location"])


def module_present(name: str) -> tuple[bool, bool]:
    """(present, placeholder) without importing the module body.

    ``placeholder`` follows the shared inventory rule (explicit ``PLACEHOLDER —`` marker in the first
    docstring line), so a real module that only mentions the word is never reported as missing."""
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return False, False
    if spec is None:
        return False, False
    return True, file_is_placeholder(getattr(spec, "origin", None))


def resolve_openbao(ctx: CheckContext) -> tuple[str | None, str]:
    """(path, lookup) of the OpenBao binary by the one rule the reconciler and ``aeos vault`` also use
    (``aeos.core.binaries``): settings override, then ``<data>/bin``, then PATH; a regular executable file only."""
    return _locate(ctx, "bao", getattr(ctx.settings, "openbao_binary", None), ctx.data_dir)


def secret_ref_status(ref: str) -> tuple[str, dict[str, Any]]:
    """('PRESENT'|'ABSENT'|'STORE_UNAVAILABLE'|'NOT_IMPLEMENTED', evidence) - values never read."""
    from aeos.core.errors import Blocked

    try:
        from aeos.secrets import facade as secrets
    except ImportError:
        return "NOT_IMPLEMENTED", {"ref": ref}
    try:
        st = secrets.secret_status(ref)
    except Blocked as exc:
        return "STORE_UNAVAILABLE", {"ref": ref, "error_code": exc.code}
    except (ImportError, ModuleNotFoundError):
        return "NOT_IMPLEMENTED", {"ref": ref}
    except Exception as exc:  # fail closed, never green
        return "STORE_UNAVAILABLE", {"ref": ref, "error_code": type(exc).__name__}
    ev = {"ref": ref, "backend": st.backend,
          "updated_at": st.updated_at.isoformat() if st.updated_at else None,
          "expires_at": st.expires_at.isoformat() if st.expires_at else None}
    return ("PRESENT" if st.exists else "ABSENT"), ev


def set_secret_remedy(ref: str, purpose: str) -> Remedy:
    from aeos.doctor.model import aeos_cmd

    return Remedy(
        operation=f"Simpan credential {purpose} di secret store (nilai tidak pernah ditampilkan)",
        command=aeos_cmd(f"secrets set {ref}"),
        source="SecretProvider (OpenBao / OS credential store, §26)",
        target=ref, privilege="pemilik (SUPER_ADMIN, lokal)", cost="tidak ada",
        rollback=aeos_cmd(f"secrets delete {ref}") + " (atau cabut di provider)",
        evidence_to_collect="status ref tanpa nilai; jalankan ulang Doctor",
    )
