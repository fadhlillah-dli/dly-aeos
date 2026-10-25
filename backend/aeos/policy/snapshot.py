"""Active PolicySnapshot, MCI reference and risk classes (§02, §08 "active policy hash", §20, §34).

* The snapshot hash covers the canonical content of ``policy/*.yaml`` plus the binding fields of
  ACTIVE POLICY/EXCEPTION decisions. A new snapshot is activated only when that hash changes.
* MCI_V2 has not been supplied: unless the referenced file exists and its sha256 matches the recorded
  hash, the reference is UNVERIFIED. Compliance is never claimed (``compliance`` is NOT-PROVEN).
* Risk classes R0-R4 are labels only; thresholds stay UNVERIFIED until canonical policy exists.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.hashing import content_hash, file_sha256
from aeos.core.paths import UnsafePathError, ensure_within, repo_root
from aeos.events import service as events
from aeos.models.decisions import PolicySnapshot
from aeos.policy import catalog

RISK_CLASS_DEFAULTS = {
    "R0": "read",
    "R1": "local reversible change",
    "R2": "external/shared change",
    "R3": "production/destructive/privileged",
    "R4": "catastrophic",
}


def policy_dir() -> Path:
    return repo_root() / "policy"


def load_policy_files() -> dict[str, dict[str, Any]]:
    """Every ``policy/*.yaml`` with its file hash and parsed content (parse errors are recorded)."""
    out: dict[str, dict[str, Any]] = {}
    root = policy_dir()
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*.yaml")):
        entry: dict[str, Any] = {"path": f"policy/{path.name}", "sha256": file_sha256(path)}
        try:
            entry["content"] = yaml.safe_load(path.read_text(encoding="utf-8"))
            entry["status"] = "LOADED"
        except (OSError, yaml.YAMLError) as exc:
            entry["content"] = None
            entry["status"] = "INVALID"
            entry["error"] = type(exc).__name__
        out[path.name] = entry
    return out


def _mci_file() -> dict[str, Any]:
    try:
        data = yaml.safe_load((policy_dir() / "mci-reference.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def mci_reference() -> dict[str, Any]:
    data = _mci_file()
    raw = data.get("mci")
    mci: dict[str, Any] = raw if isinstance(raw, dict) else {}
    declared = str(mci.get("status") or "UNAVAILABLE").upper()
    location = mci.get("location")
    recorded_hash = mci.get("content_hash")
    status = "UNVERIFIED"
    verification_note = "MCI_V2 source of truth not supplied; alignment UNVERIFIED (§02, §51 P1)."
    if declared == "VERIFIED" and location and recorded_hash:
        try:
            path = ensure_within(repo_root(), repo_root() / str(location))
            if path.is_file() and file_sha256(path) == str(recorded_hash):
                status = "VERIFIED"
                verification_note = "Referenced file present and hash matches the recorded content hash."
            else:
                verification_note = "Declared VERIFIED but file missing or hash mismatch -> UNVERIFIED."
        except UnsafePathError:
            verification_note = "Declared location escapes the repository -> UNVERIFIED."
    return {
        "name": mci.get("name") or "MCI_V2_SOURCE_OF_TRUTH",
        "declared_status": declared,
        "status": status,  # VERIFIED only with a readable, hash-matching source; never READY
        "compliance": "NOT-PROVEN",  # §34: no compliance claim from references/templates
        "version": mci.get("version"),
        "content_hash": recorded_hash,
        "location": location,
        "verified_at": mci.get("verified_at"),
        "note": mci.get("note") or "",
        "verification_note": verification_note,
        "packs": [p for p in (data.get("packs") or []) if isinstance(p, str)],
        "packs_mapping_status": "UNVERIFIED",
        "source": "policy/mci-reference.yaml",
    }


def risk_classes() -> list[dict[str, Any]]:
    declared = _mci_file().get("risk_classes")
    labels = declared if isinstance(declared, dict) else RISK_CLASS_DEFAULTS
    return [
        {"key": k, "label": str(labels.get(k, RISK_CLASS_DEFAULTS[k])), "thresholds": None,
         "threshold_status": "UNVERIFIED", "note": "Thresholds come from canonical policy (MCI) when available."}
        for k in RISK_CLASS_DEFAULTS
    ]


def _decision_refs(session: Session) -> list[dict[str, Any]]:
    from aeos.policy.decisions import active_decisions

    return [
        {
            "id": d.id, "kind": d.kind, "domain": d.domain, "project_id": d.project_id,
            "target_type": d.target_type, "target_id": d.target_id, "target_hash": d.target_hash,
            "allowed_operations": d.allowed_operations or [], "actor_kind": d.actor_kind,
            "expires_at": d.expires_at.isoformat() if d.expires_at else None,
            "details_hash": content_hash(d.details or {}),
        }
        for d in active_decisions(session, kinds=catalog.SNAPSHOT_DECISION_KINDS)
    ]


def compute_content(session: Session) -> dict[str, Any]:
    files = load_policy_files()
    return {
        "schema": "aeos.policy-snapshot/1",
        "files": {name: {"path": e["path"], "sha256": e["sha256"], "status": e["status"], "content": e["content"]}
                  for name, e in files.items()},
        "decisions": _decision_refs(session),
        "mci": {k: v for k, v in mci_reference().items() if k != "verification_note"},
        "risk_classes": risk_classes(),
    }


def active_snapshot(session: Session) -> PolicySnapshot | None:
    return session.scalar(
        select(PolicySnapshot).where(PolicySnapshot.active.is_(True)).order_by(PolicySnapshot.created_at.desc())
    )


def ensure_active_snapshot(session: Session, *, source: str = "policy") -> PolicySnapshot:
    """Activate a snapshot for the current policy files + decisions (idempotent by content hash)."""
    content = compute_content(session)
    digest = content_hash(content)
    current = active_snapshot(session)
    if current is not None and current.content_hash == digest:
        return current
    count = session.scalar(select(func.count()).select_from(PolicySnapshot)) or 0
    for old in session.scalars(select(PolicySnapshot).where(PolicySnapshot.active.is_(True))):
        old.active = False
    snap = PolicySnapshot(
        version=str(int(count) + 1), content=content, content_hash=digest, source=source[:64],
        mci_reference=content["mci"], active=True,
    )
    session.add(snap)
    session.flush()
    audit.record(session, actor_type="system", actor_id="policy", action="policy.snapshot.activate",
                 target_type="policy_snapshot", target_id=snap.id, reason=source,
                 details={"content_hash": digest, "previous": current.content_hash if current else None,
                          "decisions": len(content["decisions"])})
    events.emit(session, source="policy", kind="policy.snapshot.activated", entity_type="policy_snapshot",
                entity_id=snap.id, entity_version=digest,
                payload={"version": snap.version, "previous_hash": current.content_hash if current else None})
    return snap


def snapshot_view(
    snap: PolicySnapshot | None, *, include_content: bool = False,
    decision_filter: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    """Snapshot projection. ``decision_filter`` restricts the bound-decision references to what the
    viewer may see (§35/§50: OFFICE decision metadata — ids, targets, even counts — never reaches a
    PERSONAL view). The hash still covers every decision; it reveals no content."""
    if snap is None:
        return None
    content = snap.content or {}
    decisions = list(content.get("decisions") or [])
    if decision_filter is not None:
        decisions = decision_filter(decisions)
    view = {
        "id": snap.id,
        "version": snap.version,
        "content_hash": snap.content_hash,
        "created_at": snap.created_at.isoformat() if snap.created_at else None,
        "source": snap.source,
        "active": snap.active,
        "files": {name: {"path": f.get("path"), "sha256": f.get("sha256"), "status": f.get("status")}
                  for name, f in (content.get("files") or {}).items()},
        "decision_count": len(decisions),
        "decisions": decisions,
        "decisions_scope": "all" if decision_filter is None else "active domain + global",
        "mci_status": (snap.mci_reference or {}).get("status", "UNVERIFIED"),
    }
    if include_content and decision_filter is None:
        view["content"] = content
    return view
