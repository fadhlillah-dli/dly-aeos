"""Service inventory and runbook index (§10 Operations "service inventory ... runbook").

The inventory lists the local services DLY AEOS depends on and what was *observed* about each one
right now. A reachable port is CONNECTED, never VERIFIED (§08): functional verification belongs to
the Readiness Doctor.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from aeos import SCHEMA_VERSION, __version__
from aeos.core.config import Settings
from aeos.core.paths import repo_root
from aeos.core.timeutil import utcnow
from aeos.operations._util import port_in_use

RUNBOOK_DIR = Path("docs") / "runbooks"
_TITLE_RE = re.compile(r"^#\s+(.+)$", re.M)
_TRIGGER_RE = re.compile(r"^##\s+Trigger\s*\n+(.+?)(?:\n\n|\n##)", re.M | re.S)


def _module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _openbao(settings: Settings) -> dict[str, Any]:
    url = settings.openbao_addr.rstrip("/")
    parsed = urlparse(url)
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        return {"status": "UNKNOWN", "detail": "OpenBao address is not loopback; not probed from the dashboard"}
    try:
        r = httpx.get(f"{url}/v1/sys/health", timeout=1.5,
                      params={"standbyok": "true", "sealedcode": "200", "uninitcode": "200"})
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        state = "sealed" if body.get("sealed") else ("uninitialized" if body.get("initialized") is False else "unsealed")
        return {"status": "CONNECTED", "detail": f"health endpoint reachable ({state}); not a functional verification",
                "initialized": body.get("initialized"), "sealed": body.get("sealed"), "version": body.get("version")}
    except (httpx.HTTPError, ValueError):
        return {"status": "UNREACHABLE", "detail": f"no response from {parsed.hostname}:{parsed.port}"}


def service_inventory(settings: Settings, *, probe: bool = True) -> dict[str, Any]:
    from aeos.api.routers import LIFECYCLE_HOOKS
    from aeos.operations import loop

    db = settings.layout.db_path
    items: list[dict[str, Any]] = [
        {"service": "control-plane-api", "kind": "process", "status": "RUNNING",
         "detail": f"serving this request on {settings.host}:{settings.port} (loopback only)",
         "version": __version__},
        {"service": "metadata-database", "kind": "sqlite", "status": "CONFIGURED" if db.is_file() else "MISSING",
         "detail": f"{db.name} ({db.stat().st_size if db.is_file() else 0} bytes), schema {SCHEMA_VERSION}"},
    ]
    hooks = [*LIFECYCLE_HOOKS]
    if "aeos.operations.loop" not in hooks:
        hooks.append("aeos.operations.loop")
    for hook in hooks:
        present = _module_present(hook)
        registered = hook in LIFECYCLE_HOOKS
        status = "MISSING" if not present else ("CONFIGURED" if registered else "NOT_REGISTERED")
        detail = "background service started by the app lifespan" if registered else \
            "implemented but not registered in LIFECYCLE_HOOKS (integrator action)"
        if hook == "aeos.operations.loop" and present:
            state = loop.state()
            status = "RUNNING" if state["running"] else status
            detail += f"; last evaluation {state['last_run_at'] or 'never'}"
            if state.get("last_error"):
                status = "DEGRADED"
                detail += f"; last error {state['last_error']}"
        items.append({"service": hook, "kind": "background", "status": status, "detail": detail})
    if probe:
        items.append({"service": "openbao", "kind": "external", **_openbao(settings)})
        items.append({"service": "vite-dev-server", "kind": "dev-only",
                      "status": "CONNECTED" if settings.dev_mode and port_in_use("127.0.0.1", settings.dev_frontend_port)
                      else "NOT_APPLICABLE",
                      "detail": "only in `aeos serve --dev`"})
    return {"items": items, "checked_at": utcnow().isoformat(),
            "source": "local process + loopback probes (CONNECTED != VERIFIED)", "mode": "LIVE"}


def runbooks(root: Path | None = None) -> list[dict[str, Any]]:
    base = (root or repo_root()) / RUNBOOK_DIR
    out: list[dict[str, Any]] = []
    if not base.is_dir():
        return out
    for path in sorted(base.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        text = path.read_text(encoding="utf-8")
        title = _TITLE_RE.search(text)
        trigger = _TRIGGER_RE.search(text)
        out.append({"id": path.stem, "title": title.group(1).strip() if title else path.stem,
                    "trigger": " ".join(trigger.group(1).split())[:300] if trigger else "",
                    "path": f"docs/runbooks/{path.name}"})
    return out
