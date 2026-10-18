"""Argon2id password hashing (§26)."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from aeos.core.errors import ValidationFailed

# argon2-cffi defaults are Argon2id (RFC 9106 low-memory profile).
_hasher = PasswordHasher()

MIN_LENGTH = 12
_COMMON = {"password1234", "123456789012", "qwertyuiopas", "adminadmin12", "changeme1234"}


def validate_password_policy(password: str, *, username: str = "") -> None:
    if len(password) < MIN_LENGTH:
        raise ValidationFailed(f"password must be at least {MIN_LENGTH} characters", code="WEAK_PASSWORD")
    if password.lower() in _COMMON or (username and username.lower() in password.lower()):
        raise ValidationFailed("password is too easy to guess", code="WEAK_PASSWORD")
    if len(set(password)) < 6:
        raise ValidationFailed("password needs more distinct characters", code="WEAK_PASSWORD")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


# Used to equalise timing when the username does not exist.
DUMMY_HASH = hash_password("dummy-password-for-timing-equalisation")
