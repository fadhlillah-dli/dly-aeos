"""Seed the role catalog and agent definitions (§15-§17). Idempotent.

Seeded rows are DESIGN / NOT_EVALUATED: they describe competence and configuration, they never
create runs, leases, reservations or usage (§50 "Role catalog berisi banyak jabatan, belum ada
task -> Tidak ada LLM run/cost"). Changed definitions get a new version + revision hash; history
of runs that used an older revision is not rewritten.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.enums import DataMode
from aeos.core.hashing import content_hash
from aeos.models.workforce import AgentDefinition, RoleSkillPack
from aeos.workforce.catalog import AGENT_DEFINITIONS, ALL_ROLES

_ROLE_FIELDS = (
    "name", "group", "kind", "description", "outputs", "boundaries", "forbidden_actions", "verification",
    "failure_modes", "practice_sources", "deterministic_service", "engine_hint", "source_section",
    "inputs", "allowed_scope", "example_outputs", "evaluation_cases",
)
_AGENT_FIELDS = (
    "role_key", "harness", "description", "route_policy", "tools", "limits", "allowed_scope", "deterministic",
)


def _bump(version: str) -> str:
    try:
        return str(int(version) + 1)
    except (TypeError, ValueError):
        return "2"


def evaluation_cases_for(role_key: str) -> list[dict[str, Any]]:
    """References to the protected evaluation suites written for *role_key* (§43, §46). The suites live in
    ``aeos.evaluation.definitions``; a role without a suite has no cases (it stays NOT_EVALUATED)."""
    try:
        from aeos.evaluation import definitions
    except ImportError:
        return []
    out = []
    for suite in definitions.SUITES:
        if suite.get("role_key") == role_key:
            out.append({"suite_key": suite["suite_key"], "task_class": suite.get("task_class", ""),
                        "case_ids": [c["case_id"] for c in suite.get("cases") or []]})
    return out


def role_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Catalog entry with its evaluation case references (the revision covers them too)."""
    return {**spec, "evaluation_cases": evaluation_cases_for(spec["role_key"])}


def role_revision_hash(spec: dict[str, Any]) -> str:
    full = role_spec(spec)
    return content_hash({k: full[k] for k in ("role_key", *_ROLE_FIELDS)})


def seed_roles(session: Session) -> dict[str, int]:
    created = updated = unchanged = 0
    for raw in ALL_ROLES:
        spec = role_spec(raw)
        rev = role_revision_hash(raw)
        row = session.scalar(select(RoleSkillPack).where(RoleSkillPack.role_key == spec["role_key"]))
        if row is None:
            row = RoleSkillPack(role_key=spec["role_key"], version="1", revision_hash=rev,
                                eval_status="NOT_EVALUATED", mode=DataMode.DESIGN.value)
            for f in _ROLE_FIELDS:
                setattr(row, f, spec[f])
            session.add(row)
            created += 1
        elif row.revision_hash != rev:
            for f in _ROLE_FIELDS:
                setattr(row, f, spec[f])
            row.version = _bump(row.version)
            row.revision_hash = rev
            row.eval_status = "NOT_EVALUATED"  # a changed definition never inherits an old evaluation
            updated += 1
        else:
            unchanged += 1
    session.flush()
    return {"roles_created": created, "roles_updated": updated, "roles_unchanged": unchanged}


def seed_agents(session: Session) -> dict[str, int]:
    created = updated = unchanged = 0
    for spec in AGENT_DEFINITIONS:
        rev = content_hash({k: spec[k] for k in ("agent_key", *_AGENT_FIELDS)})
        row = session.scalar(select(AgentDefinition).where(AgentDefinition.agent_key == spec["agent_key"]))
        if row is None:
            row = AgentDefinition(agent_key=spec["agent_key"], revision="1", revision_hash=rev,
                                  enabled=bool(spec["enabled"]), enabled_reason=spec["enabled_reason"],
                                  mode=DataMode.DESIGN.value, owner="owner")
            for f in _AGENT_FIELDS:
                setattr(row, f, spec[f])
            session.add(row)
            created += 1
        elif row.revision_hash != rev:
            for f in _AGENT_FIELDS:
                setattr(row, f, spec[f])
            row.revision = _bump(row.revision)
            row.revision_hash = rev
            # Re-seeding never enables an agent; enablement is an explicit, audited owner action.
            updated += 1
        else:
            unchanged += 1
    session.flush()
    return {"agents_created": created, "agents_updated": updated, "agents_unchanged": unchanged}


def run(session: Session) -> dict[str, Any]:
    result: dict[str, Any] = {}
    result.update(seed_roles(session))
    result.update(seed_agents(session))
    result["mode"] = DataMode.DESIGN.value
    result["runs_created"] = 0
    return result
