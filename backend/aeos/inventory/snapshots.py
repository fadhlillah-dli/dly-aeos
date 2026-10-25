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
    from aeos.inventory.configlens import config_basis
    from aeos.inventory.decisions import decision_states
    from aeos.inventory.observations import latest_observations
    from aeos.inventory.views import enablement_dim, write_enabled_components

    root = root or repo_root()
    comps = _visible_components(session, domain)
    rec = current_manifest(session)
    # decision-side dimensions come from the Decision registry / approved configuration, as the views show them;
    # the observation-based "released controller" basis is left out so the snapshot hash does not churn with TTLs
    states = decision_states(session, comps, domain=domain)
    basis = config_basis(session, comps, latest_observations(session, list(comps), visible_to=domain), utcnow(),
                         domain=domain)
    writable = write_enabled_components(session, domain)
    nodes = []
    for cid, c in sorted(comps.items()):
        node = {"id": cid}
        for f in NODE_FIELDS:
            node[f] = getattr(c, f)
        st = states.get(cid)
        if st is not None:
            node["admission_status"], node["lifecycle_final"] = st.admission, st.lifecycle
        decided = [r for r in basis.get(cid, []) if not r.startswith("bagian dari released controller")]
        node["enablement"] = enablement_dim(c, decided, write_enabled=cid in writable)["value"]
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
    decisions.sort(key=lambda d: d["id"] or "")
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


SNAPSHOT_TARGET = "architecture_snapshot"


def approval_of(session: Session, snap: ArchitectureSnapshot) -> dict[str, Any] | None:
    """The ACTIVE human APPROVAL bound to this snapshot's exact hash (§41 "dua approved snapshots")."""
    d = session.scalar(select(Decision).where(Decision.kind == "APPROVAL", Decision.target_type == SNAPSHOT_TARGET,
                                              Decision.target_id == snap.id, Decision.status == "ACTIVE",
                                              Decision.target_hash == snap.snapshot_hash)
                       .order_by(Decision.created_at.desc()).limit(1))
    if d is None or d.actor_kind not in ("HUMAN", "HUMAN_RECORDED"):
        return None
    return {"decision_id": d.id, "approved_at": d.created_at.isoformat() if d.created_at else None,
            "actor_user_id": d.actor_user_id}


def snapshot_meta(snap: ArchitectureSnapshot, session: Session | None = None) -> dict[str, Any]:
    refs = snap.refs or {}
    approval = approval_of(session, snap) if session is not None else None
    return {"id": snap.id, "label": snap.label, "snapshot_hash": snap.snapshot_hash,
            "created_at": snap.created_at.isoformat(), "created_by": snap.created_by,
            "scope": refs.get("scope"), "taken_at": refs.get("taken_at"),
            "counts": {"nodes": len(refs.get("nodes") or []), "edges": len(refs.get("edges") or []),
                       "decisions": len(refs.get("decisions") or [])},
            "manifest_hash": (refs.get("manifest") or {}).get("content_hash"),
            "approved": approval is not None, "approval": approval}


def list_snapshots(session: Session, *, domain: str | None, approved_only: bool = False) -> list[dict[str, Any]]:
    rows = [s for s in session.scalars(select(ArchitectureSnapshot).order_by(ArchitectureSnapshot.created_at.desc()))
            if _in_scope(s, domain)]
    metas = [snapshot_meta(s, session) for s in rows]
    return [m for m in metas if m["approved"]] if approved_only else metas


def list_snapshots_page(session: Session, *, domain: str | None, page: int = 1, page_size: int = 50,
                        approved_only: bool = False) -> dict[str, Any]:
    metas = list_snapshots(session, domain=domain, approved_only=approved_only)
    page, page_size = max(1, page), max(1, min(page_size, 200))
    return {"items": metas[(page - 1) * page_size:page * page_size], "total": len(metas), "page": page,
            "page_size": page_size}


def approve_snapshot(session: Session, snapshot_id: str, *, domain: str | None, actor_user_id: str,
                     rationale: str) -> dict[str, Any]:
    """Record a human APPROVAL bound to the snapshot's exact hash (the snapshot itself stays immutable)."""
    from aeos.policy import facade as policy

    if not (rationale or "").strip():
        raise ValidationFailed("rationale is required")
    snap = get_snapshot(session, snapshot_id, domain=domain)
    if approval_of(session, snap) is not None:
        return snapshot_meta(snap, session)
    policy.record_decision(session, kind="APPROVAL", actor_user_id=actor_user_id, target_type=SNAPSHOT_TARGET,
                           target_id=snap.id, target_hash=snap.snapshot_hash,
                           allowed_operations=["architecture.snapshot.approve"], domain=domain, rationale=rationale,
                           details={"label": snap.label,
                                    "manifest_hash": ((snap.refs or {}).get("manifest") or {}).get("content_hash")})
    return snapshot_meta(snap, session)


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
    ma, mb = snapshot_meta(a, session), snapshot_meta(b, session)
    return {"a": ma, "b": mb, "same_hash": a.snapshot_hash == b.snapshot_hash,
            "both_approved": ma["approved"] and mb["approved"],
            "approval_note": None if ma["approved"] and mb["approved"] else
            "Satu atau kedua snapshot belum disetujui manusia; diff ini bukan perbandingan dua approved snapshot (§41).",
            "nodes": nodes, "edges": edges, "refs_changed": refs_changed,
            "note": "Diff antara dua artefak snapshot; bukan perbandingan dengan live inventory."}
