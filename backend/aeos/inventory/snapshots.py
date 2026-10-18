"""ArchitectureSnapshot export and diff (§13, §36, §41, §50 "Snapshot export dibuka setelah runtime berubah").

A snapshot is an immutable artifact of references and hashes — manifest record + content hash,
master document hash, ADR file hashes, frozen contract hashes, active component decisions, and
the DESIGN/CONFIG nodes and edges visible in the snapshot's domain scope. It is for render, diff
and audit; it is not live inventory and not a new policy authority. Rows are append-only (DB
trigger installed by ``aeos.models.inventory_ext``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.errors import NotFound, ValidationFailed
from aeos.core.hashing import content_hash, file_sha256
from aeos.core.paths import repo_root
from aeos.core.timeutil import utcnow
from aeos.inventory.manifest import MASTER_REL, SCHEMA_REL, current_manifest
from aeos.inventory.views import _visible_components
from aeos.models.decisions import ArchitectureSnapshot, Decision
from aeos.models.inventory import ComponentRelation

SNAPSHOT_SCHEMA = "architecture-snapshot/1"
NODE_FIELDS = ("kind", "category", "requirement", "design_label", "approved_pin", "admission_status", "enablement",
               "lifecycle_final", "identity_verified", "source_url")
DECISION_TARGETS = ("component", "install_manifest", "adr", "architecture", "change_proposal")


def _file_refs(root: Path, paths: list[Path]) -> list[dict[str, str]]:
    out = []
    for p in sorted(paths):
        if p.is_file():
            out.append({"path": p.relative_to(root).as_posix(), "sha256": file_sha256(p)})
    return out


def build_refs(session: Session, *, domain: str | None, root: Path | None = None) -> dict[str, Any]:
    root = root or repo_root()
    comps = _visible_components(session, domain)
    rec = current_manifest(session)
    nodes = []
    for cid, c in sorted(comps.items()):
        node = {"id": cid}
        for f in NODE_FIELDS:
            node[f] = getattr(c, f)
        nodes.append(node)
    edges = []
    for r in session.scalars(select(ComponentRelation).where(ComponentRelation.tombstoned_at.is_(None),
                                                             ComponentRelation.lens.in_(("DESIGN", "CONFIG")))):
        if r.source_id in comps and r.target_id in comps:
            edges.append({"source": r.source_id, "target": r.target_id, "relation": r.relation, "lens": r.lens,
                          "operation": r.operation or "", "boundary": r.boundary})
    edges.sort(key=lambda e: (e["source"], e["target"], e["relation"], e["lens"]))
    decisions = []
    for d in session.scalars(select(Decision).where(Decision.target_type.in_(DECISION_TARGETS),
                                                    Decision.status == "ACTIVE")):
        if d.domain not in (None, domain):
            continue
        decisions.append({"id": d.id, "kind": d.kind, "target_type": d.target_type, "target_id": d.target_id,
                          "target_hash": d.target_hash})
    decisions.sort(key=lambda d: d["id"])
    master = root / MASTER_REL
    contracts = _file_refs(root, [root / SCHEMA_REL, *(root / "backend" / "aeos").glob("*/facade.py")])
    adrs = _file_refs(root, list((root / "docs" / "decisions").glob("ADR-*.md")))
    return {
        "schema": SNAPSHOT_SCHEMA,
        "scope": {"domain": domain, "lenses": ["DESIGN", "CONFIG"]},
        "manifest": {"record_id": rec.id, "content_hash": rec.content_hash,
                     "manifest_version": rec.manifest_version} if rec else None,
        "master": {"path": MASTER_REL, "sha256": file_sha256(master)} if master.is_file() else None,
        "adrs": adrs,
        "contracts": contracts,
        "decisions": decisions,
        "nodes": nodes,
        "edges": edges,
        "note": "Snapshot berisi referensi rancangan/konfigurasi bertanggal; bukan live inventory.",
    }


def create_snapshot(session: Session, *, label: str, domain: str | None, actor: str) -> ArchitectureSnapshot:
    from aeos.audit import service as audit
    from aeos.events import service as events

    label = (label or "").strip()[:128] or f"snapshot {utcnow().strftime('%Y-%m-%d %H:%M')}"
    refs = build_refs(session, domain=domain)
    refs["taken_at"] = utcnow().isoformat()
    body = {k: v for k, v in refs.items() if k != "taken_at"}
    snap = ArchitectureSnapshot(label=label, refs=refs, snapshot_hash=content_hash(body), created_by=actor)
    session.add(snap)
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor, action="inventory.snapshot.create",
                 target_type="architecture_snapshot", target_id=snap.id, domain=domain,
                 details={"snapshot_hash": snap.snapshot_hash, "nodes": len(refs["nodes"]), "edges": len(refs["edges"])})
    events.emit(session, source="inventory", kind="inventory.snapshot.created", entity_type="architecture_snapshot",
                entity_id=snap.id, entity_version=snap.snapshot_hash, domain=domain, mode="DESIGN",
                payload={"label": label, "nodes": len(refs["nodes"]), "edges": len(refs["edges"])})
    return snap


def _in_scope(snap: ArchitectureSnapshot, domain: str | None) -> bool:
    return ((snap.refs or {}).get("scope") or {}).get("domain") == domain


def snapshot_meta(snap: ArchitectureSnapshot) -> dict[str, Any]:
    refs = snap.refs or {}
    return {"id": snap.id, "label": snap.label, "snapshot_hash": snap.snapshot_hash,
            "created_at": snap.created_at.isoformat(), "created_by": snap.created_by,
            "scope": refs.get("scope"), "taken_at": refs.get("taken_at"),
            "counts": {"nodes": len(refs.get("nodes") or []), "edges": len(refs.get("edges") or []),
                       "decisions": len(refs.get("decisions") or [])},
            "manifest_hash": (refs.get("manifest") or {}).get("content_hash")}


def list_snapshots(session: Session, *, domain: str | None) -> list[dict[str, Any]]:
    rows = session.scalars(select(ArchitectureSnapshot).order_by(ArchitectureSnapshot.created_at.desc()).limit(200))
    return [snapshot_meta(s) for s in rows if _in_scope(s, domain)]


def get_snapshot(session: Session, snapshot_id: str, *, domain: str | None) -> ArchitectureSnapshot:
    snap = session.get(ArchitectureSnapshot, snapshot_id)
    if snap is None or not _in_scope(snap, domain):
        raise NotFound("snapshot not found")
    return snap


def _diff_keyed(a: list[dict[str, Any]], b: list[dict[str, Any]], key: Any) -> dict[str, Any]:
    ia = {key(x): x for x in a}
    ib = {key(x): x for x in b}
    added = [ib[k] for k in sorted(set(ib) - set(ia), key=str)]
    removed = [ia[k] for k in sorted(set(ia) - set(ib), key=str)]
    changed = []
    for k in sorted(set(ia) & set(ib), key=str):
        fields = {f: [ia[k].get(f), ib[k].get(f)] for f in set(ia[k]) | set(ib[k]) if ia[k].get(f) != ib[k].get(f)}
        if fields:
            changed.append({"key": k if isinstance(k, str) else list(k), "fields": fields})
    return {"added": added, "removed": removed, "changed": changed}


def diff_snapshots(session: Session, a_id: str, b_id: str, *, domain: str | None) -> dict[str, Any]:
    if not a_id or not b_id:
        raise ValidationFailed("both snapshot ids (a, b) are required")
    a, b = get_snapshot(session, a_id, domain=domain), get_snapshot(session, b_id, domain=domain)
    ra, rb = a.refs or {}, b.refs or {}
    nodes = _diff_keyed(ra.get("nodes") or [], rb.get("nodes") or [], lambda n: n["id"])
    edges = _diff_keyed(ra.get("edges") or [], rb.get("edges") or [],
                        lambda e: (e["source"], e["target"], e["relation"], e["lens"]))
    refs_changed = {}
    for key in ("manifest", "master", "adrs", "contracts", "decisions"):
        if ra.get(key) != rb.get(key):
            refs_changed[key] = {"a": ra.get(key), "b": rb.get(key)}
    return {"a": snapshot_meta(a), "b": snapshot_meta(b), "same_hash": a.snapshot_hash == b.snapshot_hash,
            "nodes": nodes, "edges": edges, "refs_changed": refs_changed,
            "note": "Diff antara dua artefak snapshot; bukan perbandingan dengan live inventory."}
