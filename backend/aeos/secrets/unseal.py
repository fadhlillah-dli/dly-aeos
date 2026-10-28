"""OpenBao unseal key in the OS keyring (ADR-0123): unseal from the keyring, import, offline recovery copy.

* ``unseal_from_keyring`` unseals a sealed OpenBao with the share(s) stored by ``vault init`` (or imported once
  with ``vault import-unseal-key``). It reads the keyring only after OpenBao itself reported "sealed", so an
  unreachable, uninitialised or unsealed vault never causes a keyring read. Every attempt is audited without a value.
* ``write_recovery_file`` writes the stored material to a NEW owner-only file (0600) outside every git work tree
  and outside the AEOS data dir (backups must not carry it, §33): the unseal key share(s), the AppRole
  role_id/secret_id (without them a lost keyring locks the controller out of an unsealed vault) and a root token
  kept by an incomplete init. Windows is refused: an owner-only ACL is not guaranteed by this code (NOT-PROVEN);
  there ``vault init`` shows the same material once instead (``recovery_export_supported``).

No function here prints, logs or returns a key or token; callers print paths, counts and outcomes only.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aeos.core.config import Settings
from aeos.core.errors import AeosError
from aeos.core.redaction import register_secret
from aeos.core.timeutil import utcnow
from aeos.secrets.openbao import OpenBaoClient, OpenBaoError, OpenBaoUnavailable, logical_addr
from aeos.secrets.provider import KeyringBootstrapStore, bootstrap_store, platform_label

# outcomes of unseal_from_keyring
UNREACHABLE = "UNREACHABLE"
NOT_INITIALIZED = "NOT_INITIALIZED"
ALREADY_UNSEALED = "ALREADY_UNSEALED"
NO_KEY_IN_KEYRING = "NO_KEY_IN_KEYRING"
KEYRING_UNAVAILABLE = "KEYRING_UNAVAILABLE"
UNSEALED = "UNSEALED"
STILL_SEALED = "STILL_SEALED"
FAILED = "FAILED"

RECOVERY_HEADER = "DLY AEOS OpenBao recovery copy"

log = logging.getLogger("aeos.secrets")


@dataclass
class UnsealOutcome:
    status: str
    detail: str = ""
    keys_applied: int = 0
    seal: dict[str, Any] | None = None


def audit_db_not_ready() -> str | None:
    """Why the metadata DB cannot take an audit row without preparing it first, or None when it is ready.

    Read-only: never creates the database file and never runs a migration (``aeos serve`` must not migrate as a side
    effect of auto-unseal; the controller's own startup does that)."""
    from aeos.core import db
    from aeos.core.config import get_settings
    from aeos.core.migrate import migration_status

    if db._engine is None and not get_settings().layout.db_path.is_file():
        return "database not created yet"
    status = migration_status()
    if status["head"] is not None and status["current"] != status["head"]:
        return "database not at the migration head"
    return None


def audit(action: str, result: str, details: dict[str, Any], *, actor_id: str = "local-owner",
          prepare_db: bool = False) -> None:
    """Best-effort audit row (never a value); an audit failure never blocks the vault operation.

    ``prepare_db`` (explicit ``aeos vault`` commands only) creates/upgrades the database first, as every other CLI
    audit does. Without it the row goes only into a database that is already at the migration head; otherwise it is
    skipped with a logged reason (the action and the reason, never a value)."""
    try:
        from aeos.audit import service as audit_service
        from aeos.core.db import session_scope

        if prepare_db:
            from aeos.cli import ensure_database

            ensure_database()
        else:
            reason = audit_db_not_ready()
            if reason:
                log.warning("openbao audit row %s skipped: %s", action, reason)
                return
        with session_scope() as s:
            audit_service.record(s, actor_type="cli", actor_id=actor_id, action=action, result=result,
                                 target_type="secret_store", target_id="openbao", details=details)
    except Exception as exc:  # audit is best effort here; the outcome is still printed, never a value
        log.warning("openbao audit record failed (%s)", type(exc).__name__)


def keyring_location(settings: Settings) -> str:
    """Where the unseal key lives, for messages: store name + service, never an entry value."""
    return f"OS keyring ({platform_label()}), service {bootstrap_store(settings).service}"


def unseal_from_keyring(settings: Settings, *, trigger: str, client: OpenBaoClient | None = None,
                        store: KeyringBootstrapStore | None = None) -> UnsealOutcome:
    """Unseal OpenBao with the share(s) in the OS keyring, only when OpenBao reports sealed."""
    try:
        c = client or OpenBaoClient(settings.openbao_addr)
    except OpenBaoUnavailable as exc:
        return UnsealOutcome(UNREACHABLE, str(exc))
    try:
        try:
            st = c.seal_status()
        except OpenBaoError as exc:
            return UnsealOutcome(UNREACHABLE, str(exc))
        if not st.get("initialized"):
            return UnsealOutcome(NOT_INITIALIZED, seal=st)
        if not st.get("sealed"):
            return UnsealOutcome(ALREADY_UNSEALED, seal=st)
        bs = store or bootstrap_store(settings)
        try:
            keys = bs.unseal_keys()
        except AeosError as exc:
            return UnsealOutcome(KEYRING_UNAVAILABLE, exc.message, seal=st)
        if not keys:
            return UnsealOutcome(NO_KEY_IN_KEYRING, seal=st)
        need = max(int(st.get("t") or 1) - int(st.get("progress") or 0), 1)
        applied = 0
        try:
            for key in keys[:need]:
                register_secret(key)
                st = c.unseal(key)
                applied += 1
                if not st.get("sealed"):
                    break
        except OpenBaoError as exc:
            out = UnsealOutcome(FAILED, str(exc), applied, st)
        else:
            out = UnsealOutcome(STILL_SEALED if st.get("sealed") else UNSEALED, "", applied, st)
    finally:
        if client is None:
            c.close()
    audit("openbao.unseal", "SUCCESS" if out.status == UNSEALED else "FAILED",
          {"source": "os-keyring", "trigger": trigger, "outcome": out.status, "keys_applied": out.keys_applied,
           "addr": logical_addr(settings.openbao_addr)}, prepare_db=trigger != "serve")
    if out.status == UNSEALED:
        signal_store_changed("OpenBao unsealed from the OS keyring", trigger=trigger)
    return out


def signal_store_changed(reason: str, *, trigger: str = "cli") -> None:
    """Tell the Doctor the store changed state (``secret:store``), so checks that saw it down re-run. Under ``aeos
    serve`` (auto-unseal) only into a database already at the migration head, as the audit row above."""
    if trigger == "serve" and audit_db_not_ready():
        return
    from aeos.secrets.readiness import store_state_changed

    store_state_changed(reason)


def unseal_at_start(settings: Settings) -> str | None:
    """`aeos serve`: unseal from the keyring when OpenBao is sealed. Returns a line to print, or None.

    Never raises: the controller starts either way and secret-dependent work fails closed while sealed."""
    try:
        out = unseal_from_keyring(settings, trigger="serve")
    except Exception as exc:  # startup must not fail on the vault; only the exception name is shown
        return f"OpenBao auto-unseal skipped ({type(exc).__name__}); run: aeos vault unseal"
    if out.status == UNSEALED:
        return f"OpenBao was sealed: unsealed with the key from the {keyring_location(settings)}."
    if out.status in (NO_KEY_IN_KEYRING, KEYRING_UNAVAILABLE):
        return "OpenBao is sealed. Next: aeos vault unseal"
    if out.status == STILL_SEALED:
        return "OpenBao is still sealed after the key(s) from the OS keyring: run aeos vault unseal for the rest."
    if out.status == FAILED:
        return f"OpenBao auto-unseal failed ({out.detail}); run: aeos vault unseal"
    return None


# ------------------------------------------------------------------------------ recovery copy


class RecoveryRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _inside(path: Path, root: Path) -> bool:
    # one comparison for both refusals: path text plus file identity (case-insensitive macOS volume)
    from aeos.core.paths import is_within

    return is_within(path, root.resolve())


def _git_work_tree(path: Path) -> Path | None:
    for p in (path, *path.parents):
        if (p / ".git").exists():
            return p
    return None


def recovery_export_supported() -> bool:
    """False on Windows: an owner-only ACL for the recovery file is NOT-PROVEN, so the export refuses there and
    ``vault init`` shows the recovery material once instead (ADR-0123)."""
    return sys.platform != "win32"


def recovery_target(raw: str, settings: Settings) -> Path:
    """The resolved target, or RecoveryRefused. The file itself must not exist yet."""
    if not recovery_export_supported():
        raise RecoveryRefused("WINDOWS_ACL_NOT_PROVEN", "Refused on Windows: this command cannot guarantee an "
                              "owner-only ACL for the file (NOT-PROVEN). Nothing was written.")
    given = Path(raw).expanduser()
    if not given.name or given.name in (".", ".."):
        raise RecoveryRefused("NOT_A_FILE", "Name a file, not a directory. Nothing was written.")
    parent = given.parent if given.is_absolute() else (Path.cwd() / given).parent
    if not parent.is_dir():
        raise RecoveryRefused("NO_PARENT_DIR", f"Directory {parent} does not exist. Nothing was written.")
    target = parent.resolve() / given.name
    from aeos.core.paths import repo_root

    if _inside(target, repo_root()) or _git_work_tree(target.parent) is not None:
        raise RecoveryRefused("INSIDE_REPOSITORY", f"{target} is inside a git repository; a recovery copy never "
                              "goes there. Choose a path outside every repository. Nothing was written.")
    if _inside(target, settings.layout.root):
        raise RecoveryRefused("INSIDE_DATA_DIR", f"{target} is inside the AEOS data directory, which backups copy; "
                              "choose a path outside it. Nothing was written.")
    if target.exists() or target.is_symlink():
        raise RecoveryRefused("EXISTS", f"{target} already exists; refusing to overwrite. Nothing was written.")
    return target


def lost_keyring_line(shares: int, *, approle_included: bool) -> str:
    """What a lost keyring needs, for the recovery file and the Windows init screen: ``--with-approle`` only when the
    copy really holds the AppRole credentials; otherwise the command without it and what is missing."""
    if approle_included:
        return (f"Lost keyring (profile reset, new machine with the same OpenBao data): aeos vault import-unseal-key "
                f"--shares {shares} --with-approle (hidden prompts: each unseal key share, then the AppRole role_id "
                "and secret_id), then aeos vault unseal and aeos vault status --verify.")
    return (f"Lost keyring (profile reset, new machine with the same OpenBao data): aeos vault import-unseal-key "
            f"--shares {shares} (hidden prompt: each unseal key share), then aeos vault unseal. The AppRole "
            "credentials are NOT included in this copy, so the controller cannot log in after a lost keyring: "
            "re-initialise and re-enter every credential (runbook openbao-unseal-recovery, step 5).")


def write_recovery_file(raw: str, settings: Settings, store: KeyringBootstrapStore | None = None) -> Path:
    """Write the stored unseal key share(s), the AppRole role_id/secret_id and a root token kept by an incomplete
    init to a new 0600 file: everything a lost keyring needs (``vault import-unseal-key --with-approle``)."""
    target = recovery_target(raw, settings)
    bs = store or bootstrap_store(settings)
    keys = bs.unseal_keys()
    if not keys:
        raise RecoveryRefused("NO_KEY_IN_KEYRING", "No unseal key is stored in the OS keyring (run aeos vault "
                              "import-unseal-key first). Nothing was written.")
    approle = bs.approle()
    root = bs.root_token()
    lines = [RECOVERY_HEADER, f"created_at: {utcnow().isoformat()}",
             f"openbao_addr: {logical_addr(settings.openbao_addr)}",
             f"keyring_service: {bs.service}",
             "Keep this file offline (encrypted USB drive, password manager or paper). Never put it in a repository, "
             "a cloud-synced folder, a chat or an AI assistant.",
             "Use: aeos vault unseal (prompts for the key) or aeos vault import-unseal-key (puts it back into the "
             "OS keyring).",
             lost_keyring_line(len(keys), approle_included=bool(approle)),
             f"unseal_key_shares: {len(keys)}"]
    lines += [f"unseal_key_{i}: {k}" for i, k in enumerate(keys, 1)]
    if approle:
        lines += [f"approle_role_id: {approle[0]}", f"approle_secret_id: {approle[1]}"]
    else:
        lines.append("approle: NOT in the OS keyring when this copy was made (a lost keyring then needs a new "
                     "vault init)")
    if root:
        lines.append(f"root_token: {root}  (vault init was incomplete; revoke it once the setup is finished)")
    data = ("\n".join(lines) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags, 0o600)
    except FileExistsError as exc:
        raise RecoveryRefused("EXISTS", f"{target} already exists; refusing to overwrite. Nothing was written.") from exc
    try:
        if sys.platform != "win32":  # recovery_target refused Windows already; this keeps mypy win32 exact
            os.fchmod(fd, 0o600)  # umask can only remove bits; set it explicitly anyway
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        target.unlink(missing_ok=True)
        raise
    os.close(fd)
    if target.stat().st_mode & 0o077:
        target.unlink(missing_ok=True)
        raise RecoveryRefused("MODE_NOT_PRIVATE", f"{target} was not owner-only after writing; it was removed.")
    audit("openbao.recovery_export", "SUCCESS", {"path": str(target), "unseal_key_shares": len(keys),
                                                 "approle_included": bool(approle), "root_token_included": bool(root)},
          prepare_db=True)
    return target
