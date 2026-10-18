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
from aeos.core.hashing import file_sha256
from aeos.core.paths import repo_root
from aeos.models.decisions import ArchitectureDecisionRecord

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


def adr_view(row: ArchitectureDecisionRecord, *, full: bool = False) -> dict[str, Any]:
    view: dict[str, Any] = {
        "key": row.adr_key, "title": row.title, "status": row.status, "source_path": row.source_path,
        "content_hash": row.content_hash, "decision_id": row.decision_id, "supersedes": row.supersedes,
        "date": (row.meta or {}).get("date"), "deciders": (row.meta or {}).get("deciders"),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if full:
        view.update({"sections": row.sections or {}, "meta": row.meta or {}, "context": row.context,
                     "decision": row.decision, "consequences": row.consequences})
    return view
