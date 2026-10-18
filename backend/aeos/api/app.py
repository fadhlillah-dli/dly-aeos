"""FastAPI application factory — one origin for UI + API on 127.0.0.1:8787 (§06)."""

from __future__ import annotations

import importlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from aeos import __version__
from aeos.api.routers import MODULE_ROUTERS
from aeos.api.security import SecurityMiddleware
from aeos.core.config import Settings, get_settings
from aeos.core.db import init_engine
from aeos.core.errors import AeosError
from aeos.core.paths import repo_root
from aeos.core.redaction import redact_text

log = logging.getLogger("aeos.api")


def _frontend_dist() -> Path | None:
    for candidate in (repo_root() / "frontend" / "dist", Path(__file__).resolve().parent.parent / "static"):
        if (candidate / "index.html").is_file():
            return candidate
    return None


def create_app(
    settings: Settings | None = None,
    *,
    include: list[str] | None = None,
    testing: bool = False,
    init_db: bool = True,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if init_db:
            init_engine(settings)
        yield

    app = FastAPI(
        title="DLY AEOS Control Plane",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs" if settings.dev_mode or testing else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.dev_mode or testing else None,
    )
    app.state.settings = settings
    app.add_middleware(SecurityMiddleware, settings=settings, allow_test_host=testing)

    @app.exception_handler(AeosError)
    async def _aeos_error(_req: Request, exc: AeosError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": redact_text(exc.message), "details": exc.details}},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_req: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg", "")} for e in exc.errors()]
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "VALIDATION_FAILED", "message": "invalid request", "details": {"errors": errors}}},
        )

    modules = include if include is not None else MODULE_ROUTERS
    for mod_name in modules:
        mod = importlib.import_module(mod_name)
        app.include_router(mod.router)

    dist = _frontend_dist()

    @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
    async def spa(full_path: str) -> FileResponse | JSONResponse:
        if full_path.startswith("api/"):
            return JSONResponse(status_code=404, content={"error": {"code": "NOT_FOUND", "message": "unknown API route", "details": {}}})
        if dist is None:
            return JSONResponse(
                status_code=503,
                content={"error": {"code": "FRONTEND_NOT_BUILT",
                                   "message": "frontend not built; run the bootstrap script or `npm run build` in frontend/",
                                   "details": {}}},
            )
        target = (dist / full_path).resolve()
        if full_path and target.is_file() and dist.resolve() in target.parents:
            return FileResponse(target)
        return FileResponse(dist / "index.html")

    return app
