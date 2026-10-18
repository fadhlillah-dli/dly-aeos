"""OFFICE default-deny gate input (§23). Reads the policy module's office policy, falling back to the
reviewed ``policy/office-defaults.yaml``; if neither is readable the gate fails CLOSED."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from aeos.core.paths import repo_root

DEFAULT_DENY: dict[str, Any] = {
    "external_worker_allowed": False,
    "approved_worker_routes": [],
    "claude_max_allowed_for_office_data": False,
    "integrations_allowed": False,
}


def read_office_policy(session: Session | None, root: Path | None = None) -> tuple[dict[str, Any], str]:
    """(policy, source). Unknown/unreadable -> default deny with source 'fail-closed'."""
    if session is not None:
        try:
            from aeos.policy import facade as policy

            data = policy.office_policy(session)
            if isinstance(data, dict):
                return {**DEFAULT_DENY, **data}, "policy-module"
        except (ImportError, ModuleNotFoundError):
            pass
        except Exception:
            return dict(DEFAULT_DENY), "fail-closed"
    path = (root or repo_root()) / "policy" / "office-defaults.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        office = raw.get("office") if isinstance(raw, dict) else None
        if isinstance(office, dict):
            return {**DEFAULT_DENY, **office}, "policy/office-defaults.yaml"
    except (OSError, yaml.YAMLError):
        pass
    return dict(DEFAULT_DENY), "fail-closed"


# capability -> policy flag that a recorded company authorisation must set (otherwise BLOCKED).
OFFICE_FLAGS = {
    "CAN_PLAN_OFFICE": "claude_max_allowed_for_office_data",
    "CAN_USE_OFFICE_INTEGRATIONS": "integrations_allowed",
    "CAN_RUN_WORKER": "external_worker_allowed",
}


def office_gate(capability: str, session: Session | None, root: Path | None = None) -> tuple[bool, str]:
    """(allowed, reason) for an OFFICE-scoped capability.

    Only the policy module (which binds OFFICE permissions to a recorded company-authorisation
    Decision) can open the gate. A flag set in the reviewed defaults file is NOT a recorded
    authorisation (AGENTS.md invariant 9), so a file-sourced ``true`` keeps the gate closed.
    """
    flag = OFFICE_FLAGS.get(capability)
    pol, source = read_office_policy(session, root)
    if flag is None:
        return False, f"OFFICE default deny (§23); sumber {source}"
    if pol.get(flag) is True and source == "policy-module":
        return True, f"{flag}=true (sumber {source})"
    if pol.get(flag) is True:
        return False, (f"OFFICE default deny (§23): {flag}=true hanya ada di {source}, bukan keputusan otorisasi "
                       "perusahaan yang tercatat; gate tetap tertutup")
    return False, (f"OFFICE default deny (§23): {flag}=false menurut {source}; butuh Decision otorisasi perusahaan "
                   "yang tercatat")
