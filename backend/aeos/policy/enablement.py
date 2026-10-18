"""Per-provider external write enablement (§26 "write enablement").

The flag lives in ``SystemSetting`` ``write_enabled.<provider>`` (value ``true``/``false``) and is
changed only together with a recorded human ``WRITE_ENABLEMENT`` decision. The interlock requires
both the flag and an ACTIVE enabling decision, so revoking/expiring the decision fails closed.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from aeos.models.decisions import Decision
from aeos.models.identity import SystemSetting
from aeos.policy import catalog


def setting_key(provider: str) -> str:
    return f"write_enabled.{provider}"


def flag(session: Session, provider: str) -> bool:
    row = session.get(SystemSetting, setting_key(provider))
    return bool(row is not None and row.value is True)


def set_flag(session: Session, provider: str, enabled: bool) -> None:
    key = setting_key(provider)
    row = session.get(SystemSetting, key)
    if row is None:
        session.add(SystemSetting(key=key, value=bool(enabled)))
    elif row.value is not bool(enabled):
        row.value = bool(enabled)
        row.version = (row.version or 1) + 1
    session.flush()


def active_enablement_decision(session: Session, provider: str) -> Decision | None:
    from aeos.policy.decisions import find_active_decision

    d = find_active_decision(session, kind="WRITE_ENABLEMENT", target_type=catalog.TARGET_PROVIDER_WRITE,
                             target_id=provider)
    if d is None or (d.details or {}).get("enabled") is not True:
        return None
    return d


def status(session: Session, domain: str) -> list[dict[str, Any]]:
    out = []
    for provider in catalog.providers_for(domain):
        d = active_enablement_decision(session, provider)
        enabled_flag = flag(session, provider)
        out.append({
            "provider": provider,
            "flag": enabled_flag,
            "decision_id": d.id if d else None,
            "decision_expires_at": d.expires_at.isoformat() if d and d.expires_at else None,
            # Effective only when both the flag and an ACTIVE human decision exist (fail closed).
            "effective": bool(enabled_flag and d is not None),
        })
    return out
