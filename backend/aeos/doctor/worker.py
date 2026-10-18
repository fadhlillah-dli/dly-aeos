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

        # run_doctor emits doctor.check / doctor.check.stale / doctor.run.completed (trigger=startup), which
        # the Readiness page receives over the event stream.
        with session_scope() as s:
            report = run_doctor(settings, s, functional=False, actor="system", trigger="startup")
        log.info("startup doctor run %s: %s", report.run_id, report.counts())
    except Exception as exc:  # visible through the Readiness page (event + no fresh results), never fatal
        log.exception("startup doctor run failed")
        _emit_failure(exc)


def _emit_failure(exc: Exception) -> None:
    try:
        from aeos.core.db import session_scope
        from aeos.core.redaction import redact_text
        from aeos.events import service as events

        with session_scope() as s:
            events.emit(s, source="doctor", kind="doctor.run.failed", entity_type="doctor_run", entity_id="startup",
                        payload={"trigger": "startup", "error": type(exc).__name__,
                                 "message": redact_text(str(exc))[:200]})
    except Exception:  # pragma: no cover - the log line above is the last resort
        log.exception("could not record the startup doctor failure")


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
