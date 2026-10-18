"""Typed control-plane errors mapped to HTTP responses by the API layer."""

from __future__ import annotations

from typing import Any


class AeosError(Exception):
    status_code = 400
    code = "AEOS_ERROR"

    def __init__(self, message: str, *, code: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details or {}


class NotFound(AeosError):
    status_code = 404
    code = "NOT_FOUND"


class Unauthenticated(AeosError):
    status_code = 401
    code = "UNAUTHENTICATED"


class Forbidden(AeosError):
    status_code = 403
    code = "FORBIDDEN"


class Conflict(AeosError):
    status_code = 409
    code = "CONFLICT"


class PolicyDenied(AeosError):
    """A deterministic policy/interlock refused the operation (fail closed)."""

    status_code = 403
    code = "POLICY_DENIED"


class Blocked(AeosError):
    """Prerequisite capability missing; the operation must not run (§08, §25)."""

    status_code = 409
    code = "BLOCKED"


class ValidationFailed(AeosError):
    status_code = 422
    code = "VALIDATION_FAILED"


class TooManyRequests(AeosError):
    status_code = 429
    code = "TOO_MANY_REQUESTS"
