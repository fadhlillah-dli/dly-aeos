"""Projection item envelope shared by Control Center endpoints (§35, §41).

Every item carries ``status``, ``source``, ``checked_at``, ``reason`` and ``evidence_limit`` so the
UI can always show "Sumber data / Diperiksa pada / Batas bukti". A section whose owning module is
missing (or fails) becomes UNKNOWN with the reason - never READY, never green (§35, AGENTS #7).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from aeos.core.errors import AeosError
from aeos.core.redaction import redact_text


def item(
    status: str,
    *,
    source: str,
    checked_at: datetime | str | None,
    reason: str = "",
    evidence_limit: str = "",
    data: Any = None,
) -> dict[str, Any]:
    if isinstance(checked_at, datetime):
        checked_at = checked_at.isoformat()
    return {"status": status, "source": source, "checked_at": checked_at, "reason": reason,
            "evidence_limit": evidence_limit, "data": data}


def unknown(source: str, reason: str, *, data: Any = None) -> dict[str, Any]:
    return item("UNKNOWN", source=source, checked_at=None, reason=reason,
                evidence_limit="Tidak ada data yang dapat dipercaya; jangan dianggap lulus.", data=data)


def guarded(source: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run a section builder; a missing module or failure yields UNKNOWN with the reason."""
    try:
        return fn()
    except ImportError as exc:
        return unknown(source, f"modul belum tersedia: {exc.name or exc}")
    except AeosError as exc:
        return unknown(source, f"{exc.code}: {redact_text(exc.message)[:300]}")
    except Exception as exc:  # projection must never crash the dashboard
        return unknown(source, f"gagal membaca sumber: {type(exc).__name__}")
