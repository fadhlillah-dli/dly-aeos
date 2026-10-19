"""Selective, reviewed import of external role/skill packs (§45 Agency Agents / gstack / Superpowers, §50 T2 2.13).

Nothing is installed or updated automatically. A pack is imported only by a human SUPER_ADMIN, with:

* the exact source (https URL), a 40-hex commit pin and an SPDX license (UNKNOWN is refused),
* a competence definition in RoleSkillPack shape (outputs, boundaries, forbidden actions incl. the common
  ones, verification, failure modes, inputs),
* no field that could change behaviour outside the pack: hooks, auto-update, policy, settings, permissions,
  install scripts, MCP servers, env (``SKILL_PACK_FORBIDDEN_FIELD``),
* a PROMOTION decision bound to the pack content hash.

The result is a versioned RoleSkillPack in mode CANDIDATE / NOT_EVALUATED. It is not an executor role: the
scheduler only runs roles of the reviewed catalog, so a candidate pack never runs before review + evaluation.
A changed pack is a new version with a new decision; an imported pack never updates itself.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.errors import Conflict, ValidationFailed
from aeos.core.hashing import content_hash
from aeos.models.decisions import Decision
from aeos.models.workforce import RoleSkillPack
from aeos.workforce import catalog
from aeos.workforce import integrations as ext

FORBIDDEN_FIELDS = frozenset({"hooks", "hook", "auto_update", "autoupdate", "auto-update", "update_url", "policy",
                              "policies", "settings", "permissions", "install", "postinstall", "preinstall",
                              "scripts", "mcp_servers", "mcpservers", "env", "environment", "approval",
                              "approvals"})
REQUIRED_LISTS = ("outputs", "boundaries", "forbidden_actions", "verification", "failure_modes", "inputs")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_KEY = re.compile(r"^ext-[a-z0-9][a-z0-9-]{2,50}$")
TARGET_TYPE = "role_skill_pack"


def _forbidden(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            here = f"{path}.{k}" if path else str(k)
            if str(k).strip().lower().replace(" ", "_") in FORBIDDEN_FIELDS:
                found.append(here)
            found += _forbidden(v, here)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            found += _forbidden(v, f"{path}[{i}]")
    return found


def validate_pack(pack: Any) -> dict[str, Any]:
    if not isinstance(pack, dict):
        raise ValidationFailed("a skill pack is a JSON object")
    bad = _forbidden(pack)
    if bad:
        raise ValidationFailed("skill packs may not carry hooks, auto-update, policy, settings, permissions, install "
                               "scripts, MCP servers or env", code="SKILL_PACK_FORBIDDEN_FIELD",
                               details={"fields": bad[:20]})
    key = str(pack.get("role_key") or "")
    if not _KEY.match(key):
        raise ValidationFailed("imported role keys are ext-<name> (they never replace a catalog role)",
                               code="SKILL_PACK_INVALID")
    if pack.get("group") not in catalog.GROUP_LABELS:
        raise ValidationFailed(f"group must be one of {sorted(catalog.GROUP_LABELS)}", code="SKILL_PACK_INVALID")
    missing = [f for f in REQUIRED_LISTS if not isinstance(pack.get(f), list) or not pack.get(f)]
    if missing or not str(pack.get("name") or "").strip():
        raise ValidationFailed(f"skill pack incomplete: {missing or ['name']}", code="SKILL_PACK_INVALID")
    forbidden = [str(x) for x in pack["forbidden_actions"]]
    lacking = [x for x in catalog.COMMON_FORBIDDEN if x not in forbidden]
    if lacking:
        raise ValidationFailed("a pack must keep the common forbidden actions (§21)", code="SKILL_PACK_INVALID",
                               details={"missing": lacking})
    return {"role_key": key, "name": str(pack["name"])[:128], "group": pack["group"], "kind": "EXECUTOR",
            "description": str(pack.get("description") or "")[:2000],
            **{f: [str(x)[:500] for x in pack[f]][:50] for f in REQUIRED_LISTS},
            "example_outputs": [str(x)[:500] for x in (pack.get("example_outputs") or [])][:20]}


def import_pack(session: Session, *, pack: Any, source_url: str, commit_pin: str, license_spdx: str,
                actor_user_id: str, rationale: str) -> RoleSkillPack:
    clean = validate_pack(pack)
    if not str(source_url or "").startswith("https://"):
        raise ValidationFailed("source_url must be an https URL", code="SKILL_PACK_SOURCE")
    if not _SHA.match(str(commit_pin or "")):
        raise ValidationFailed("commit_pin must be a full 40-hex commit sha (no branch, no 'latest')",
                               code="SKILL_PACK_PIN")
    spdx = str(license_spdx or "").strip()
    if not spdx or spdx.upper() in ("UNKNOWN", "NOASSERTION", "NONE"):
        raise ValidationFailed("a reviewed SPDX license is required", code="SKILL_PACK_LICENSE")
    if len((rationale or "").strip()) < 10:
        raise ValidationFailed("a rationale (min 10 characters) is required")
    provenance = {"source_url": source_url, "commit_pin": commit_pin, "license_spdx": spdx}
    pack_hash = content_hash({**clean, "provenance": provenance})
    row = session.scalar(select(RoleSkillPack).where(RoleSkillPack.role_key == clean["role_key"]))
    if row is not None and row.revision_hash == pack_hash:
        raise Conflict("this exact pack version is already imported", code="SKILL_PACK_UNCHANGED")
    d: Decision = ext.record_decision(
        session, kind="PROMOTION", actor_user_id=actor_user_id, target_type=TARGET_TYPE, target_id=clean["role_key"],
        target_hash=pack_hash, allowed_operations=["role_pack.candidate"], rationale=rationale,
        auth_scope={"role_key": clean["role_key"], "mode": "CANDIDATE", "executes": False},
        details={**provenance, "note": "candidate only; never auto-updated; not an executor until reviewed"})
    if row is None:
        row = RoleSkillPack(role_key=clean["role_key"], version="1")
        session.add(row)
    else:
        try:
            row.version = str(int(row.version or "1") + 1)
        except ValueError:
            row.version = "2"
    for f, v in clean.items():
        if f != "role_key":
            setattr(row, f, v)
    row.practice_sources = [f"{source_url}@{commit_pin} ({spdx})", f"decision {d.id}"]
    row.allowed_scope = dict(catalog._KIND_SCOPE["EXECUTOR"])
    row.evaluation_cases = []
    row.revision_hash = pack_hash
    row.eval_status = "NOT_EVALUATED"
    row.mode = "CANDIDATE"
    row.source_section = "§45"
    session.flush()
    return row
