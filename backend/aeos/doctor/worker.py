"""Startup Doctor pass (§38 "Saat startup, Doctor memeriksa prasyarat yang diperlukan sebelum worker admission").

Started by the app lifespan when registered in ``aeos.api.routers.LIFECYCLE_HOOKS``. Runs the
non-functional, loopback-only checks once in a background thread shortly after startup so the
dashboard shows fresh readiness; never calls Claude, never touches the network, never installs.
"""

from __future__ import annotations

import logging
import threading

from aeos.core.config import Settings

log = logging.getLogger("aeos.doctor")
_stop = threading.Event()
_thread: threading.Thread | None = None
STARTUP_DELAY_SECONDS = 3.0


def _run(settings: Settings) -> None:
    if _stop.wait(STARTUP_DELAY_SECONDS):
        return
    try:
        from aeos.core.db import session_scope
        from aeos.doctor.runner import run_doctor

        with session_scope() as s:
            report = run_doctor(settings, s, functional=False, actor="system", trigger="startup")
        log.info("startup doctor run %s: %s", report.run_id, report.counts())
    except Exception:  # visible through the Readiness page (no fresh results), never fatal
        log.exception("startup doctor run failed")


def start(settings: Settings) -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, args=(settings,), name="aeos-doctor-startup", daemon=True)
    _thread.start()


def stop() -> None:
    global _thread
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=30)
    _thread = None
