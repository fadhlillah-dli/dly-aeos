"""OFFICE default-deny policy (§23, §51 P1, AGENTS.md invariant 9).

Defaults come from ``policy/office-defaults.yaml``. They can only be relaxed by ACTIVE human
``POLICY`` decisions for the OFFICE domain *and* only while an ACTIVE company-authorisation
decision (``details.office_authorization == true``) exists. Configuration files or model output
never relax them; ``Settings.office_external_worker_allowed`` is ignored when true.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from aeos.core.enums import Domain
from aeos.core.hashing import content_hash
from aeos.core.paths import repo_root
from aeos.policy import catalog

# Hard fallback when the defaults file is missing or unreadable: everything denied.
_DENY_ALL: dict[str, Any] = {
    "external_worker_allowed": False,
    "approved_worker_routes": [],
    "claude_max_allowed_for_office_data": False,
    "trace_destinations_allowed": [],
    "requires_isolated_runtime": True,
    "atlassian_deployment": "UNKNOWN",
}


def policy_dir() -> Path:
    return repo_root() / "policy"


def load_defaults() -> tuple[dict[str, Any], str]:
    """Return (office defaults, source note). Missing/invalid file -> deny-all defaults."""
    path = policy_dir() / "office-defaults.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        office = data.get("office") if isinstance(data, dict) else None
        if not isinstance(office, dict):
            return dict(_DENY_ALL), "policy/office-defaults.yaml invalid -> deny-all"
    except (OSError, yaml.YAMLError):
        return dict(_DENY_ALL), "policy/office-defaults.yaml unavailable -> deny-all"
    merged = dict(_DENY_ALL)
    for k in catalog.OFFICE_POLICY_KEYS:
        if k in office:
            merged[k] = office[k]
    # Extra keys from the file are informational but cannot loosen the defaults above.
    for k, v in office.items():
        merged.setdefault(k, v)
    return merged, "policy/office-defaults.yaml"


def _office_policy_decisions(session: Session) -> list[Any]:
    """ACTIVE human POLICY decisions recorded for the OFFICE policy target (the only target the OFFICE
    authorisation/override endpoint writes). POLICY decisions about other targets never relax OFFICE."""
    from aeos.policy.decisions import active_decisions

    return [d for d in active_decisions(session, kinds=["POLICY"], domain=Domain.OFFICE.value)
            if d.target_type == catalog.TARGET_OFFICE_POLICY and d.target_id == Domain.OFFICE.value
            and d.actor_kind == "HUMAN"]


def _authorisation_decisions(session: Session) -> list[Any]:
    return [d for d in _office_policy_decisions(session)
            if (d.details or {}).get("office_authorization") is True and (d.details or {}).get("company_reference")]


def company_authorisation(session: Session) -> Any | None:
    """Newest ACTIVE company-authorisation decision for OFFICE, or None."""
    rows = _authorisation_decisions(session)
    return rows[-1] if rows else None


def office_policy(session: Session) -> dict[str, Any]:
    policy, source = load_defaults()
    defaults_hash = content_hash(policy)
    auth = company_authorisation(session)
    applied: list[str] = []
    ignored: list[dict[str, str]] = []
    for d in _office_policy_decisions(session):
        overrides = (d.details or {}).get("office_policy") or {}
        if not isinstance(overrides, dict) or not overrides:
            continue
        if auth is None:
            ignored.append({"decision_id": d.id, "reason": "no ACTIVE company authorisation; overrides ignored"})
            continue
        for k, v in overrides.items():
            if k in catalog.OFFICE_POLICY_KEYS:
                policy[k] = v
        applied.append(d.id)
    authorized_providers: list[str] = []
    if auth is not None:
        raw = (auth.details or {}).get("allowed_providers") or (auth.auth_scope or {}).get("providers") or []
        authorized_providers = [p for p in raw if p in catalog.PROVIDERS_BY_DOMAIN[Domain.OFFICE.value]]
    # Type hygiene: never let a malformed override read as "allowed".
    policy["external_worker_allowed"] = policy.get("external_worker_allowed") is True and auth is not None
    policy["claude_max_allowed_for_office_data"] = (
        policy.get("claude_max_allowed_for_office_data") is True and auth is not None
    )
    routes = policy.get("approved_worker_routes")
    policy["approved_worker_routes"] = [r for r in routes if isinstance(r, str)] if isinstance(routes, list) and auth else []
    traces = policy.get("trace_destinations_allowed")
    policy["trace_destinations_allowed"] = [t for t in traces if isinstance(t, str)] if isinstance(traces, list) and auth else []
    policy["requires_isolated_runtime"] = policy.get("requires_isolated_runtime") is not False
    policy["atlassian_deployment"] = str(policy.get("atlassian_deployment") or "UNKNOWN").upper()
    return {
        **policy,
        "office_authorization": auth is not None,
        "authorization_decision_id": auth.id if auth else None,
        "authorized_providers": authorized_providers,
        "applied_decision_ids": applied,
        "ignored_decisions": ignored,
        "source": source,
        "defaults_hash": defaults_hash,
        "config_override_ignored": _config_override_requested(),
    }


def _config_override_requested() -> bool:
    try:
        from aeos.core.config import get_settings

        return bool(get_settings().office_external_worker_allowed)
    except Exception:  # pragma: no cover - settings always load in practice
        return False
