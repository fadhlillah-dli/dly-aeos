"""HTTP hardening for the localhost control plane (§06, §26).

* Host header allow-list (DNS-rebinding protection) — loopback names on the configured port only.
* Origin allow-list for unsafe methods (explicit dev origin only in dev mode; no wildcard CORS).
* Strict security headers / CSP.
* Claim context (master Header/Istilah "every claim is read with ... version/config"): every /api response
  names the controller version, schema version and the hash of the non-secret runtime settings it was
  produced under (``X-AEOS-Version`` / ``X-AEOS-Schema-Version`` / ``X-AEOS-Settings-Hash``; the hash is the
  ``settings_hash`` that ``aeos.operations.release.runtime_config`` records at promotion).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from aeos import SCHEMA_VERSION, __version__
from aeos.core.config import Settings
from aeos.core.hashing import content_hash

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; "
    "object-src 'none'"
)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, "details": {}}})


class SecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: object, settings: Settings, *, allow_test_host: bool = False) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.settings = settings
        ports = {settings.port}
        if settings.dev_mode:
            ports.add(settings.dev_frontend_port)
        self.allowed_hosts = {f"{h}:{p}" for h in ("127.0.0.1", "localhost", "[::1]") for p in ports}
        if allow_test_host:
            self.allowed_hosts |= {"testserver", "testserver:80"}
        self.allowed_origins = set(settings.allowed_origins)
        if allow_test_host:
            self.allowed_origins.add("http://testserver")
        # Settings never hold secrets (core/config.py rejects them), so the dump is safe to hash; the
        # settings of a running process do not change, so this is computed once.
        self.claim_headers = {"X-AEOS-Version": __version__, "X-AEOS-Schema-Version": SCHEMA_VERSION,
                              "X-AEOS-Settings-Hash": content_hash(settings.model_dump(mode="json"))}

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        host = (request.headers.get("host") or "").lower()
        if host not in self.allowed_hosts:
            return _error(421, "HOST_NOT_ALLOWED", "request host is not an allowed loopback origin")
        if request.method.upper() not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and origin not in self.allowed_origins:
                return _error(403, "ORIGIN_NOT_ALLOWED", "cross-origin write rejected")
            if origin is None:
                referer = request.headers.get("referer")
                if referer and not any(referer.startswith(o + "/") or referer == o for o in self.allowed_origins):
                    return _error(403, "ORIGIN_NOT_ALLOWED", "cross-origin write rejected")
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers.setdefault("Content-Security-Policy", CSP)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
            response.headers.update(self.claim_headers)
        return response
