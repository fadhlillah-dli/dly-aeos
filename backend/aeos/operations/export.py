"""Scoped, redacted JSON export snapshots (§35 export, §41, §50).

§41: "Export inventory/architecture/evidence memakai snapshot ID/hash, timestamp, scope dan redaction
yang sama. Snapshot ekspor bukan status live terus-menerus." Every export:

* is scoped to the session's active domain (plus platform-level rows without a domain) and the
  user's project scope — Office rows/counts never appear in a Personal export (§50);
* is passed through ``aeos.core.redaction`` again (legacy/trace rows that slipped past redaction at
  write time are scrubbed; the number of redacted fields is reported — §50 "Trace berisi secret");
* carries ``snapshot_id``, ``content_hash``, ``generated_at``, scope and an explicit "snapshot, not
  live" notice, and is registered in ``ops_export_snapshots`` + audited.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.enums import Domain
from aeos.core.errors import ValidationFailed
from aeos.core.hashing import canonical_json, content_hash
from aeos.core.ids import new_id
from aeos.core.redaction import redact
from aeos.core.timeutil import utcnow
from aeos.models.audit import AuditEvent
from aeos.models.decisions import ArchitectureDecisionRecord, ArchitectureSnapshot, Decision
from aeos.models.evidence import EvidenceArtifact
from aeos.models.inventory import ComponentObservation, ComponentReference, ComponentRelation
from aeos.models.operations_ext import OpsExportSnapshot
from aeos.models.portfolio import Project
from aeos.operations._util import iso

KINDS = ("inventory", "architecture", "evidence", "audit", "decisions")
MAX_ITEMS = 5000
NOTICE = ("SNAPSHOT - bukan status live. Isi dibekukan pada generated_at untuk scope di atas; status runtime "
          "dapat sudah berubah (§41).")


def _scope_domains(scope: dict[str, Any] | None) -> set[str]:
    scope = scope or {}
    out: set[str] = set()
    raw = scope.get("domains")
    if isinstance(raw, list):
        out |= {str(d).upper() for d in raw}
    if isinstance(scope.get("domain"), str):
        out.add(scope["domain"].upper())
    return out


def _visible_scope(scope: dict[str, Any] | None, domain: str, project_ids: set[str]) -> bool:
    domains = _scope_domains(scope)
    if domains and domain not in domains:
        return False
    pid = (scope or {}).get("project_id")
    return not (pid and pid not in project_ids)


def _projects(session: Session, domain: str, allowed: list[str] | None) -> set[str]:
    q = select(Project.id).where(Project.domain == domain)
    ids = set(session.scalars(q))
    return ids if allowed is None else ids & set(allowed)


def _cols(row: Any, skip: tuple[str, ...] = ()) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in row.__table__.columns:
        if col.key in skip:
            continue
        v = getattr(row, col.key)
        out[col.key] = v.isoformat() if hasattr(v, "isoformat") else (float(v) if hasattr(v, "as_tuple") else v)
    return out


def _inventory(session: Session, domain: str, pids: set[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    comps = [c for c in session.scalars(select(ComponentReference).order_by(ComponentReference.component_id))
             if _visible_scope(c.scope, domain, pids)]
    latest: dict[tuple[str, str], ComponentObservation] = {}
    for obs in session.scalars(select(ComponentObservation).order_by(ComponentObservation.observed_at)):
        if _visible_scope(obs.scope, domain, pids):
            latest[(obs.component_id, obs.dimension)] = obs
    for c in comps:
        row = _cols(c)
        row["observations"] = [_cols(o) for (cid, _dim), o in sorted(latest.items()) if cid == c.component_id]
        row["record_type"] = "component"
        items.append(row)
    return items


def _architecture(session: Session, domain: str, pids: set[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for s in session.scalars(select(ArchitectureSnapshot).order_by(ArchitectureSnapshot.created_at)):
        items.append({**_cols(s), "record_type": "architecture_snapshot"})
    for a in session.scalars(select(ArchitectureDecisionRecord).order_by(ArchitectureDecisionRecord.adr_key)):
        items.append({**_cols(a), "record_type": "adr"})
    for r in session.scalars(select(ComponentRelation).order_by(ComponentRelation.source_id, ComponentRelation.target_id)):
        if _visible_scope(r.scope, domain, pids):
            items.append({**_cols(r), "record_type": "component_relation"})
    return items


def _evidence(session: Session, domain: str, pids: set[str]) -> list[dict[str, Any]]:
    q = (select(EvidenceArtifact)
         .where(or_(EvidenceArtifact.domain == domain, EvidenceArtifact.domain.is_(None)))
         .order_by(EvidenceArtifact.created_at).limit(MAX_ITEMS + 1))
    return [{**_cols(e), "record_type": "evidence"} for e in session.scalars(q)
            if e.project_id is None or e.project_id in pids]


def _audit(session: Session, domain: str, pids: set[str]) -> list[dict[str, Any]]:
    q = (select(AuditEvent)
         .where(or_(AuditEvent.domain == domain, AuditEvent.domain.is_(None)))
         .order_by(AuditEvent.seq.desc()).limit(MAX_ITEMS + 1))
    return [{**_cols(a), "record_type": "audit_event"} for a in session.scalars(q)
            if a.project_id is None or a.project_id in pids]


def _decisions(session: Session, domain: str, pids: set[str]) -> list[dict[str, Any]]:
    q = (select(Decision)
         .where(or_(Decision.domain == domain, Decision.domain.is_(None)))
         .order_by(Decision.created_at).limit(MAX_ITEMS + 1))
    return [{**_cols(d), "record_type": "decision"} for d in session.scalars(q)
            if d.project_id is None or d.project_id in pids]


_BUILDERS = {"inventory": _inventory, "architecture": _architecture, "evidence": _evidence, "audit": _audit,
             "decisions": _decisions}


def build_export(
    session: Session,
    *,
    kind: str,
    domain: Domain,
    actor_user_id: str,
    allowed_project_ids: list[str] | None,
    project_id: str | None = None,
) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValidationFailed(f"unknown export kind {kind!r}", details={"kinds": list(KINDS)})
    d = domain.value
    pids = _projects(session, d, allowed_project_ids)
    if project_id is not None:
        pids = pids & {project_id}
    raw_items = _BUILDERS[kind](session, d, pids)
    truncated = len(raw_items) > MAX_ITEMS
    raw_items = raw_items[:MAX_ITEMS]
    redacted_fields = 0
    items: list[dict[str, Any]] = []
    for item in raw_items:
        clean = redact(item, pii=True)
        if canonical_json(clean) != canonical_json(item):
            redacted_fields += _count_diffs(item, clean)
        items.append(clean)
    generated_at = utcnow()
    snapshot_id = new_id()
    scope = {"domain": d, "project_id": project_id, "project_count": len(pids),
             "includes_platform_rows": True, "user_project_scope": "restricted" if allowed_project_ids is not None
             else "all-in-domain"}
    digest = content_hash({"kind": kind, "scope": scope, "generated_at": generated_at.isoformat(), "items": items})
    session.add(OpsExportSnapshot(id=snapshot_id, kind=kind, domain=d, project_id=project_id,
                                  actor_user_id=actor_user_id, content_hash=digest, item_count=len(items),
                                  redactions_applied=redacted_fields, scope=scope, created_at=generated_at))
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="ops.export", target_type="export",
                 target_id=snapshot_id, domain=d, project_id=project_id,
                 details={"kind": kind, "content_hash": digest, "items": len(items), "redacted_fields": redacted_fields,
                          "truncated": truncated})
    return {
        "snapshot_id": snapshot_id,
        "kind": kind,
        "mode": "SNAPSHOT",
        "generated_at": generated_at.isoformat(),
        "scope": scope,
        "notice": NOTICE,
        "content_hash": digest,
        "hash_covers": "canonical JSON of {kind, scope, generated_at, items}",
        "item_count": len(items),
        "truncated": truncated,
        "redaction": {"applied": True, "engine": "aeos.core.redaction", "fields_redacted": redacted_fields},
        "items": items,
    }


def _count_diffs(a: Any, b: Any) -> int:
    if isinstance(a, dict) and isinstance(b, dict):
        return sum(_count_diffs(a.get(k), b.get(k)) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return sum(_count_diffs(x, y) for x, y in zip(a, b, strict=False))
    return 0 if a == b else 1


def list_snapshots(session: Session, *, domain: Domain, limit: int = 50) -> list[dict[str, Any]]:
    q = (select(OpsExportSnapshot).where(OpsExportSnapshot.domain == domain.value)
         .order_by(OpsExportSnapshot.created_at.desc()).limit(min(limit, 200)))
    return [{"snapshot_id": s.id, "kind": s.kind, "generated_at": iso(s.created_at), "content_hash": s.content_hash,
             "item_count": s.item_count, "redactions_applied": s.redactions_applied, "project_id": s.project_id,
             "actor_user_id": s.actor_user_id} for s in session.scalars(q)]
