"""ADR register loader (§02, §41): ``docs/decisions/ADR-*.md`` -> ``adrs`` table.

ADR files are the source artifacts; the table is an index with a content hash so the dashboard can
link decisions to ADRs. Loading is an idempotent upsert by ADR key; history stays in git.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.errors import Conflict, NotFound, ValidationFailed
from aeos.core.hashing import file_sha256
from aeos.core.paths import UnsafePathError, ensure_within, repo_root
from aeos.models.decisions import ArchitectureDecisionRecord, Decision
from aeos.policy import catalog

_DASHES = chr(0x2014) + chr(0x2013)  # em/en dash used in ADR headings
_TITLE_RE = re.compile(r"^#\s+(ADR-\d{4})\s*[" + _DASHES + r":-]+\s*(.+?)\s*$")
_FILE_RE = re.compile(r"^(ADR-\d{4})-[A-Za-z0-9._-]+\.md$")
_META_RE = re.compile(r"^\*\s+([A-Za-z][A-Za-z ]{0,40}):\s*(.*)$")
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
VALID_STATUSES = ("PROPOSED", "ACCEPTED", "SUPERSEDED", "REJECTED", "DEPRECATED")


def decisions_dir() -> Path:
    return repo_root() / "docs" / "decisions"


def parse_adr(text: str, *, filename: str) -> dict[str, Any]:
    file_match = _FILE_RE.match(filename)
    key = file_match.group(1) if file_match else None
    title = ""
    warnings: list[str] = []
    meta: dict[str, str] = {}
    sections: dict[str, str] = {}
    current_section: str | None = None
    current_meta: str | None = None
    buf: list[str] = []

    def flush_section() -> None:
        if current_section is not None:
            sections[current_section] = "\n".join(buf).strip()

    for line in text.splitlines():
        if not title:
            m = _TITLE_RE.match(line)
            if m:
                if key and m.group(1) != key:
                    warnings.append(f"heading key {m.group(1)} differs from file name key {key}")
                key = key or m.group(1)
                title = m.group(2)
                continue
        sm = _SECTION_RE.match(line)
        if sm:
            flush_section()
            current_section = sm.group(1)
            current_meta = None
            buf = []
            continue
        if current_section is None:
            mm = _META_RE.match(line)
            if mm:
                current_meta = mm.group(1).strip().lower()
                meta[current_meta] = mm.group(2).strip()
            elif current_meta and line.startswith("  ") and line.strip():
                meta[current_meta] = (meta[current_meta] + " " + line.strip()).strip()
            continue
        buf.append(line)
    flush_section()

    status = (meta.get("status") or "PROPOSED").split()[0].upper().strip(".,")
    if status not in VALID_STATUSES:
        warnings.append(f"unknown status {status!r}; stored as PROPOSED")
        status = "PROPOSED"
    if not key:
        warnings.append("no ADR key found")
    if not title:
        warnings.append("no title heading found")
    lowered = {k.lower(): v for k, v in sections.items()}
    supersedes = None
    sup = meta.get("supersedes")
    if sup:
        m = re.search(r"ADR-\d{4}", sup)
        supersedes = m.group(0) if m else None
    return {
        "key": key, "title": title or (key or filename), "status": status, "meta": meta, "sections": sections,
        "context": lowered.get("context", ""), "decision": lowered.get("decision", ""),
        "consequences": lowered.get("consequences", ""), "supersedes": supersedes, "warnings": warnings,
    }


def load_adrs(session: Session, directory: Path | None = None) -> dict[str, Any]:
    root = directory or decisions_dir()
    result: dict[str, Any] = {"created": 0, "updated": 0, "unchanged": 0, "skipped": [], "source": str(root)}
    if not root.is_dir():
        result["skipped"].append("decisions directory not found")
        return result
    for path in sorted(root.glob("ADR-*.md")):
        # utf-8-sig: an ADR saved by a Windows editor with a BOM must still parse its "# ADR-NNNN" title.
        parsed = parse_adr(path.read_text(encoding="utf-8-sig"), filename=path.name)
        if not parsed["key"]:
            result["skipped"].append(path.name)
            continue
        digest = file_sha256(path)
        rel = f"docs/decisions/{path.name}"
        row = session.scalar(select(ArchitectureDecisionRecord).where(ArchitectureDecisionRecord.adr_key == parsed["key"]))
        if row is not None and row.content_hash == digest and row.source_path == rel:
            result["unchanged"] += 1
            continue
        fields = {
            "title": parsed["title"][:256], "status": parsed["status"], "context": parsed["context"],
            "decision": parsed["decision"], "consequences": parsed["consequences"], "sections": parsed["sections"],
            "meta": {**parsed["meta"], "parse_warnings": parsed["warnings"]}, "source_path": rel,
            "content_hash": digest, "supersedes": parsed["supersedes"],
        }
        if row is None:
            row = ArchitectureDecisionRecord(adr_key=parsed["key"], **fields)
            session.add(row)
            result["created"] += 1
            action = "adr.load.create"
        else:
            for k, v in fields.items():
                setattr(row, k, v)
            result["updated"] += 1
            action = "adr.load.update"
        session.flush()
        audit.record(session, actor_type="system", actor_id="policy.seed", action=action, target_type="adr",
                     target_id=parsed["key"], details={"content_hash": digest, "status": parsed["status"]})
    return result


# ---------------------------------------------------------------- owner decisions on ADRs (§02 change control)
#
# The ``Status:`` line of an ADR file is written by whoever wrote the file (during the seed build: the builder). The
# owner's own acceptance is a Decision of kind ADR on ``adr/<ADR-NNNN>`` bound to the file's content hash, recorded
# only by an active SUPER_ADMIN (live session + CSRF on the decisions page, or ``aeos policy adr-decide`` with the
# password). The register shows both; the owner's outcome wins while it binds the current content, and a later
# edit of the file makes it STALE (the builder-written status applies again until the owner decides anew).

OWNER_DECISION = "OWNER_DECISION"
FILE_STATUS = "FILE (builder-written)"


def owner_decisions(session: Session, adr_keys: list[str] | None = None) -> dict[str, list[Decision]]:
    """ADR decisions per ADR key, newest first."""
    q = select(Decision).where(Decision.kind == "ADR", Decision.target_type == catalog.TARGET_ADR)
    if adr_keys is not None:
        q = q.where(Decision.target_id.in_(adr_keys))
    out: dict[str, list[Decision]] = {}
    for d in session.scalars(q.order_by(Decision.created_at.desc())):
        out.setdefault(d.target_id, []).append(d)
    return out


def register_status(row: ArchitectureDecisionRecord, decisions: list[Decision]) -> dict[str, Any]:
    """Owner-recorded vs builder-written status of one ADR (pure; ``decisions`` newest first)."""
    from aeos.policy.decisions import effective_status

    active = [d for d in decisions if effective_status(d) == "ACTIVE" and d.actor_kind == "HUMAN"]
    current = next((d for d in active if d.target_hash and d.target_hash == row.content_hash), None)
    stale = None if current is not None else next(iter(active), None)
    chosen = current or stale
    outcome = str((chosen.details or {}).get("outcome") or "") if chosen is not None else None
    if current is not None:
        state, effective, source = "RECORDED", outcome or row.status, OWNER_DECISION
    elif stale is not None:
        state, effective, source = "STALE", row.status, FILE_STATUS
    else:
        state, effective, source = "NOT_RECORDED", row.status, FILE_STATUS
    return {
        "file_status": row.status,
        "owner_status": outcome or None,
        "owner_status_state": state,  # RECORDED | STALE (file changed after the decision) | NOT_RECORDED
        "owner_decision": None if chosen is None else {
            "id": chosen.id, "outcome": outcome, "target_hash": chosen.target_hash,
            "decided_at": chosen.created_at.isoformat() if chosen.created_at else None,
            "superseded_by": (chosen.details or {}).get("superseded_by")},
        "effective_status": effective,
        "status_source": source,
    }


def effective_register_status(session: Session, row: ArchitectureDecisionRecord) -> str:
    return str(register_status(row, owner_decisions(session, [row.adr_key]).get(row.adr_key, []))["effective_status"])


def _file_hash_now(row: ArchitectureDecisionRecord) -> str | None:
    """Hash of the ADR file on disk now (``docs/decisions/<file>`` under the register's source directory)."""
    if not row.source_path or not row.source_path.startswith("docs/decisions/"):
        return None
    root = decisions_dir()
    try:
        path = ensure_within(root, root / row.source_path[len("docs/decisions/"):])
    except UnsafePathError:
        return None
    return file_sha256(path) if path.is_file() else None


def record_adr_decision(session: Session, *, adr_key: str, outcome: str, actor_user_id: str, rationale: str,
                        content_hash: str | None, superseded_by: str | None = None) -> Decision:
    """Record the owner's ACCEPTED/REJECTED/SUPERSEDED on one ADR, bound to the content hash the owner reviewed.

    Refused unless that hash is the loaded content hash AND the file on disk still has it (no decision on content
    nobody reviewed). Only an active SUPER_ADMIN user can hold it (the registry re-checks; ``owner:ADR-*`` actors may
    record EXCEPTION only). An earlier ACTIVE ADR decision on the same ADR is superseded. Does not commit."""
    from aeos.policy import decisions as registry

    row = session.scalar(select(ArchitectureDecisionRecord).where(ArchitectureDecisionRecord.adr_key == adr_key))
    if row is None:
        raise NotFound("ADR not found")
    outcome = str(outcome or "").upper()
    if outcome not in catalog.ADR_OUTCOMES:
        raise ValidationFailed(f"outcome must be one of {list(catalog.ADR_OUTCOMES)}", code="ADR_OUTCOME_INVALID")
    if not rationale or len(rationale.strip()) < 10:
        raise ValidationFailed("an ADR decision needs a rationale (min 10 characters)")
    if not row.content_hash or (content_hash or row.content_hash) != row.content_hash:
        raise Conflict("the ADR changed since you reviewed it; reload the register and review again",
                       code="ADR_CONTENT_CHANGED", details={"loaded_hash": row.content_hash})
    on_disk = _file_hash_now(row)
    if on_disk != row.content_hash:
        raise Conflict("the ADR file on disk differs from the loaded register entry (or is missing); reload with "
                       "'aeos policy adrs --load' and review again", code="ADR_FILE_CHANGED",
                       details={"loaded_hash": row.content_hash, "file_hash": on_disk})
    successor = None
    if outcome == "SUPERSEDED":
        successor = str(superseded_by or "").strip().upper()
        if not successor or successor == row.adr_key or session.scalar(select(ArchitectureDecisionRecord).where(
                ArchitectureDecisionRecord.adr_key == successor)) is None:
            raise ValidationFailed("SUPERSEDED needs superseded_by = the key of another loaded ADR",
                                   code="ADR_SUCCESSOR_REQUIRED")
    elif superseded_by:
        raise ValidationFailed("superseded_by applies to SUPERSEDED only", code="ADR_SUCCESSOR_UNEXPECTED")
    previous = [d for d in owner_decisions(session, [row.adr_key]).get(row.adr_key, []) if d.status == "ACTIVE"]
    decision = registry.record_decision(
        session, kind="ADR", actor_user_id=actor_user_id, target_type=catalog.TARGET_ADR, target_id=row.adr_key,
        target_hash=row.content_hash, allowed_operations=[], domain=None, rationale=rationale.strip(),
        auth_scope={"adr": row.adr_key, "outcome": outcome, "source_path": row.source_path},
        details={"outcome": outcome, "file_status_at_decision": row.status, "source_path": row.source_path,
                 "superseded_by": successor},
    )
    for old in previous:
        registry.supersede_decision(session, old, new_decision_id=decision.id, reason="new owner ADR decision",
                                    actor_type="user", actor_id=actor_user_id)
    return decision


def adr_view(row: ArchitectureDecisionRecord, *, full: bool = False,
             decisions: list[Decision] | None = None) -> dict[str, Any]:
    view: dict[str, Any] = {
        "key": row.adr_key, "title": row.title, "status": row.status, "source_path": row.source_path,
        "content_hash": row.content_hash, "decision_id": row.decision_id, "supersedes": row.supersedes,
        "date": (row.meta or {}).get("date"), "deciders": (row.meta or {}).get("deciders"),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if decisions is not None:
        view.update(register_status(row, decisions))
    if full:
        view.update({"sections": row.sections or {}, "meta": row.meta or {}, "context": row.context,
                     "decision": row.decision, "consequences": row.consequences})
    return view
