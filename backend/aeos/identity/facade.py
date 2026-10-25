"""Cross-module contract of ``aeos.identity`` (§26 local accounts and sessions, U-14). Keep signatures stable.

Other modules read who is acting (``Principal``, resolved by ``aeos.api.deps``) and whether the first
SUPER_ADMIN exists; the Readiness Doctor verifies stored password hashes against known defaults and exercises
the login flow in its own throw-away probe database. Sessions, CSRF, lockout, recovery and the password policy
stay internal to identity and are never reached through another module.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from aeos.core.enums import Role
from aeos.identity.service import Principal
from aeos.models.identity import User

__all__ = ["Principal", "bootstrap_admin", "bootstrap_complete", "create_user", "verify_password"]


def bootstrap_complete(session: Session) -> bool:
    """True once the first SUPER_ADMIN was created through the local bootstrap and still exists."""
    from aeos.identity.service import bootstrap_complete as impl

    return impl(session)


def verify_password(password_hash: str, password: str) -> bool:
    """Constant-time check of *password* against a stored hash (never returns or logs the password)."""
    from aeos.identity.passwords import verify_password as impl

    return impl(password_hash, password)


def bootstrap_admin(session: Session, *, username: str, password: str, display_name: str = "") -> User:
    """Create the first SUPER_ADMIN (refused once one exists). Local CLI / Doctor probe database only."""
    from aeos.identity.service import bootstrap_admin as impl

    return impl(session, username=username, password=password, display_name=display_name)


def create_user(session: Session, *, username: str, password: str, role: Role, display_name: str = "",
                actor: str = "cli", must_change_password: bool = False) -> User:
    """Create a local account (password policy enforced, audited)."""
    from aeos.identity.service import create_user as impl

    return impl(session, username=username, password=password, role=role, display_name=display_name, actor=actor,
                must_change_password=must_change_password)
