"""Feature flags (§35 Control Center "feature flags").

Flags are non-secret ``SystemSetting`` rows under ``flags.<key>``. Only registered keys can be set:
a flag must name its consumer, so a toggle never pretends to control something that does not read
it. Flags can only narrow behaviour inside what policy already allows — they never grant AUTH,
enable a paid route, bypass review or open OFFICE access (§20, §23). Every change is audited, emits
``ops.flag.changed`` and invalidates dependent readiness checks (``flag:<key>``, §08).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.errors import AeosError, NotFound, ValidationFailed
from aeos.core.redaction import contains_secret
from aeos.events import service as events
from aeos.models.identity import SystemSetting
from aeos.operations._util import iso

PREFIX = "flags."
_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,95}$")


@dataclass(frozen=True)
class FlagSpec:
    key: str
    kind: str  # bool|int|str
    default: Any
    description: str  # Indonesian UI copy
    consumer: str
    narrows_only: bool = True


REGISTRY: dict[str, FlagSpec] = {}


def register_flag(spec: FlagSpec) -> None:
    if not _KEY_RE.match(spec.key):
        raise ValueError(f"invalid flag key {spec.key!r}")
    REGISTRY[spec.key] = spec


register_flag(FlagSpec(
    key="ops.sli_loop_enabled", kind="bool", default=True,
    description="Evaluasi SLI dan alert berkala oleh controller (aeos.operations.loop).",
    consumer="aeos.operations.loop",
))
register_flag(FlagSpec(
    key="ops.sli_loop_interval_seconds", kind="int", default=300,
    description="Interval evaluasi SLI berkala dalam detik (minimum 30).",
    consumer="aeos.operations.loop",
))
register_flag(FlagSpec(
    key="ui.advanced_view_default", kind="bool", default=False,
    description="Tampilkan mode Advanced (dependency/SBOM/provenance) secara default di dashboard.",
    consumer="frontend",
))


def _coerce(spec: FlagSpec, value: Any) -> Any:
    if spec.kind == "bool":
        if not isinstance(value, bool):
            raise ValidationFailed(f"flag {spec.key} expects true/false")
        return value
    if spec.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationFailed(f"flag {spec.key} expects an integer")
        if spec.key.endswith("interval_seconds") and value < 30:
            raise ValidationFailed("interval must be >= 30 seconds")
        return value
    if not isinstance(value, str) or len(value) > 200:
        raise ValidationFailed(f"flag {spec.key} expects a short string")
    if contains_secret(value):
        raise ValidationFailed("flag values must never contain secrets")
    return value


def get_flag(session: Session, key: str) -> Any:
    spec = REGISTRY.get(key)
    row = session.get(SystemSetting, PREFIX + key)
    if row is not None:
        return row.value
    return spec.default if spec else None


def list_flags(session: Session) -> list[dict[str, Any]]:
    rows = {r.key[len(PREFIX):]: r for r in session.scalars(select(SystemSetting).where(SystemSetting.key.startswith(PREFIX)))}
    out: list[dict[str, Any]] = []
    for key, spec in sorted(REGISTRY.items()):
        row = rows.get(key)
        out.append({"key": key, "kind": spec.kind, "default": spec.default,
                    "value": row.value if row is not None else spec.default, "is_default": row is None,
                    "description": spec.description, "consumer": spec.consumer, "narrows_only": spec.narrows_only,
                    "updated_at": iso(row.updated_at) if row is not None else None,
                    "version": row.version if row is not None else 0})
    for key, row in sorted(rows.items()):
        if key not in REGISTRY:
            out.append({"key": key, "kind": "unknown", "default": None, "value": row.value, "is_default": False,
                        "description": "Flag tidak terdaftar - tidak ada consumer yang diketahui membacanya.",
                        "consumer": "UNREGISTERED", "narrows_only": True, "updated_at": iso(row.updated_at),
                        "version": row.version})
    return out


def set_flag(session: Session, key: str, value: Any, *, actor_type: str, actor_id: str, reason: str = "") -> dict[str, Any]:
    spec = REGISTRY.get(key)
    if spec is None:
        raise NotFound(f"unknown feature flag {key!r}", details={"known": sorted(REGISTRY)})
    value = _coerce(spec, value)
    row = session.get(SystemSetting, PREFIX + key)
    previous = row.value if row is not None else spec.default
    if row is None:
        row = SystemSetting(key=PREFIX + key, value=value, version=1)
        session.add(row)
    else:
        row.value = value
        row.version = (row.version or 1) + 1
    session.flush()
    invalidation: dict[str, Any]
    try:
        from aeos.doctor import facade as doctor

        invalidation = {"status": "DONE", "checks": doctor.invalidate(session, invalidator=f"flag:{key}",
                                                                      reason=f"feature flag {key} changed")}
    except ImportError:
        invalidation = {"status": "UNAVAILABLE", "reason": "doctor module unavailable; dependent checks not invalidated"}
    except AeosError as exc:
        invalidation = {"status": "FAILED", "reason": exc.message}
    audit.record(session, actor_type=actor_type, actor_id=actor_id, action="ops.flag.set", target_type="feature_flag",
                 target_id=key, reason=reason, details={"previous": previous, "new": value,
                                                         "readiness_invalidation": invalidation["status"]})
    events.emit(session, source="operations", kind="ops.flag.changed", entity_type="feature_flag", entity_id=key,
                entity_version=str(row.version), payload={"previous": previous, "new": value})
    return {"key": key, "value": value, "previous": previous, "version": row.version,
            "readiness_invalidation": invalidation}
