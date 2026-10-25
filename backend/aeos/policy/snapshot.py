"""Active PolicySnapshot, MCI reference and risk classes (§02, §08 "active policy hash", §20, §34).

* The snapshot hash covers the canonical content of ``policy/*.yaml`` plus the binding fields of
  ACTIVE POLICY/EXCEPTION decisions. A new snapshot is activated only when that hash changes.
* MCI_V2 has not been supplied: unless the referenced file exists and its sha256 matches the recorded
  hash, the reference is UNVERIFIED. Compliance is never claimed (``compliance`` is NOT-PROVEN).
* The repository is public: it holds the MCI *reference* only (status, version, content hash, logical
  ``local:<file>`` location). The document itself is read from this machine only - ``<data dir>/policy/canonical/``
  or the aeos.yaml ``mci_source_path`` - and a source inside the repository is never read.
* §34 packs: each committed pack slot gets its MCI controls, declared mapping status and evidence contract from the
  owner's local ``<data dir>/policy/canonical/mci-pack-mapping.yaml`` (derived from MCI_V2, so never committed);
  all empty/UNVERIFIED until the owner writes it. A pack mapping is APPROVED only while the MCI source is VERIFIED
  and an ACTIVE human POLICY decision binds that exact mapping + MCI hash.
* Risk classes R0-R4 are labels only; thresholds stay UNVERIFIED until canonical policy exists.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.config import Settings, get_settings
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


MCI_FILE = "mci-reference.yaml"
DECLARED_STATUSES = ("UNAVAILABLE", "REFERENCED", "VERIFIED")
# The platform repository is PUBLIC (owner, 2026-10-26; master §06 "MCI tidak dimasukkan tanpa hak"): the canonical
# MCI_V2 document and everything derived from it (the per-pack MCI controls and evidence contracts) live only on the
# owner's machine - <data dir>/policy/canonical/ or the aeos.yaml ``mci_source_path`` - and the repository keeps the
# reference only (status, version, content hash, logical ``local:<file>`` location). A source that resolves inside
# the repository is never read.
LOCAL_PREFIX = "local:"
LOCAL_DIR_LABEL = "<data dir>/policy/canonical"
PACK_MAPPING_FILE = "mci-pack-mapping.yaml"
# §34 packs that must be mapped to the correct MCI controls before a readiness that needs them. The keys are the
# master's list; the mapping itself (controls, evidence contract) comes only from the owner's local
# PACK_MAPPING_FILE, written from the supplied MCI_V2 - never from this code, a model or the public repository.
MASTER_PACKS: tuple[str, ...] = (
    "ARCH", "PRODUCT", "UX/FE", "BE/API", "DB/DATA", "PLATFORM/SRE", "QA", "SEC/GRC", "AGENT/HARNESS/ROUTING",
    "TOOL/MCP", "RESEARCH", "DOCS/DECISIONS", "CLIENT/AUDIT", "SUPPLY-CHAIN", "INCIDENT/REL", "AI-GOV", "COST/QUOTA",
)
# A pack mapping counts as APPROVED only while the MCI source is VERIFIED and an ACTIVE human POLICY decision on
# exactly this target binds the hash of the pack entry + the MCI content hash (aeos.policy.recording validates it).
TARGET_MCI_PACK_MAPPING = catalog.TARGET_MCI_PACK_MAPPING
EVIDENCE_LEVELS = ("E0", "E1", "E2", "E3", "E4")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_REFUSALS = {
    "LOCATION_NOT_LOCAL": ("The MCI source is read only from this machine: declare location: local:<file> (= "
                           f"{LOCAL_DIR_LABEL}/<file>) or set mci_source_path in aeos.yaml; the public repository never "
                           "holds MCI content -> UNVERIFIED."),
    "LOCATION_OUTSIDE_LOCAL_DIR": f"Declared location escapes {LOCAL_DIR_LABEL} -> UNVERIFIED.",
    "SOURCE_IN_REPOSITORY": ("The MCI source resolves inside the repository, which is public: it is never read there "
                             f"(keep it in {LOCAL_DIR_LABEL}) -> UNVERIFIED."),
}


def _mci_file(root: Path | None = None) -> dict[str, Any]:
    pdir = root / "policy" if root is not None else policy_dir()
    try:
        data = yaml.safe_load((pdir / MCI_FILE).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def normalize_hash(value: Any) -> str | None:
    """``sha256:<lowercase hex>`` of a recorded hash (a bare or upper-case hex digest is the same digest);
    anything else is returned as given and simply never matches a file hash (fail closed)."""
    if value is None:
        return None
    text = str(value).strip()
    digest = text[7:] if text.lower().startswith("sha256:") else text
    return "sha256:" + digest.lower() if _HEX64.match(digest.lower()) else text


def mci_source_settings(settings: Settings | None = None) -> tuple[Path, Path | None]:
    """(local canonical directory, aeos.yaml ``mci_source_path`` override) of this controller."""
    s = settings if settings is not None else get_settings()
    return s.layout.canonical_policy, s.mci_source_path


def _resolve_source(location: Any, root: Path | None,
                    settings: Settings | None) -> tuple[Path | None, str | None, str | None]:
    """(local source path, refusal code, logical location) of the declared MCI source. The path is None when nothing
    is declared or the location is refused; it is never inside the repository."""
    local_dir, override = mci_source_settings(settings)
    repo = (root if root is not None else repo_root()).resolve()
    if override is not None:
        logical = "aeos.yaml mci_source_path"
        candidate = Path(override).expanduser()
        if not candidate.is_absolute():
            return None, "LOCATION_NOT_LOCAL", logical
        path = candidate.resolve()
    elif location:
        logical = str(location).strip()
        if not logical.lower().startswith(LOCAL_PREFIX):
            return None, "LOCATION_NOT_LOCAL", logical
        rel = logical[len(LOCAL_PREFIX):].strip().replace("\\", "/")
        if not rel.strip("/"):
            return None, "LOCATION_NOT_LOCAL", logical
        try:
            path = ensure_within(local_dir, local_dir / rel)
        except UnsafePathError:
            return None, "LOCATION_OUTSIDE_LOCAL_DIR", logical
    else:
        return None, None, None
    if path == repo or repo in path.parents:
        return None, "SOURCE_IN_REPOSITORY", logical
    return path, None, logical


def mci_source_path(root: Path | None = None, settings: Settings | None = None) -> Path | None:
    """The declared MCI source file on this machine, or None (nothing declared, or the location is refused)."""
    raw = _mci_file(root).get("mci")
    location = raw.get("location") if isinstance(raw, dict) else None
    return _resolve_source(location, root, settings)[0]


def _verify(mci: dict[str, Any], root: Path | None, settings: Settings | None) -> tuple[str, str, str]:
    """(status, verification_code, verification_note) of the declared MCI reference."""
    declared = str(mci.get("status") or "UNAVAILABLE").upper()
    recorded_hash = normalize_hash(mci.get("content_hash"))
    if declared != "VERIFIED":
        if declared == "REFERENCED":
            return ("UNVERIFIED", "REFERENCED_NOT_VERIFIED",
                    "MCI_V2 declared REFERENCED: a reference is not a verified source; alignment UNVERIFIED.")
        if declared not in DECLARED_STATUSES:
            return ("UNVERIFIED", "UNKNOWN_DECLARED_STATUS",
                    f"Unknown declared MCI status {declared!r}; alignment UNVERIFIED (fail closed).")
        return "UNVERIFIED", "NOT_SUPPLIED", "MCI_V2 source of truth not supplied; alignment UNVERIFIED (§02, §51 P1)."
    path, refused, _logical = _resolve_source(mci.get("location"), root, settings)
    if refused:
        return "UNVERIFIED", refused, _REFUSALS[refused]
    if path is None or not recorded_hash:
        return ("UNVERIFIED", "INCOMPLETE_REFERENCE",
                "Declared VERIFIED without a location (or aeos.yaml mci_source_path) and a content_hash -> UNVERIFIED.")
    if not path.is_file():
        return ("UNVERIFIED", "SOURCE_MISSING",
                f"Declared VERIFIED but the local MCI source is missing on this machine ({LOCAL_DIR_LABEL}) "
                "-> UNVERIFIED.")
    if file_sha256(path) != recorded_hash:
        return ("UNVERIFIED", "HASH_MISMATCH",
                "Declared VERIFIED but hash mismatch: the local MCI source does not match the recorded content hash "
                "-> UNVERIFIED.")
    return "VERIFIED", "HASH_MATCH", "Local MCI source present and its hash matches the recorded content hash."


def _str_list(value: Any) -> list[str]:
    return [str(v).strip() for v in value if str(v).strip()] if isinstance(value, list) else []


def local_pack_mapping(settings: Settings | None = None) -> tuple[dict[str, dict[str, Any]], str]:
    """The owner's per-pack MCI mapping on this machine (``<data dir>/policy/canonical/mci-pack-mapping.yaml``,
    ``packs: [{key, mci_controls, mapping_status, evidence_contract}]``) by pack key, and its state
    PRESENT | ABSENT | INVALID. It is derived from MCI_V2, so it never lives in the public repository."""
    local_dir, _override = mci_source_settings(settings)
    path = local_dir / PACK_MAPPING_FILE
    if not path.is_file():
        return {}, "ABSENT"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}, "INVALID"
    raw = data.get("packs") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return {}, "INVALID"
    return {str(p["key"]).strip(): p for p in raw
            if isinstance(p, dict) and isinstance(p.get("key"), str) and p["key"].strip()}, "PRESENT"


def _has_mapping_content(raw: dict[str, Any]) -> bool:
    contract = raw.get("evidence_contract")
    return bool(_str_list(raw.get("mci_controls"))) or (
        isinstance(contract, dict) and any(v not in (None, "", [], {}) for v in contract.values()))


def _pack_entry(raw: Any, local: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Canonical form of one committed ``packs`` slot (a bare pack name is a slot with nothing mapped), filled from
    the owner's local mapping. Mapping content written into the committed file is ignored (public repository)."""
    if isinstance(raw, str):
        raw = {"key": raw}
    if not isinstance(raw, dict) or not isinstance(raw.get("key"), str) or not raw["key"].strip():
        return None
    key = raw["key"].strip()
    src: dict[str, Any] = local.get(key) or {}
    raw_contract = src.get("evidence_contract")
    contract: dict[str, Any] = raw_contract if isinstance(raw_contract, dict) else {}
    min_level = contract.get("min_level")
    freshness = contract.get("freshness")
    return {
        "key": key,
        "mci_controls": _str_list(src.get("mci_controls")),
        "declared_mapping_status": str(src.get("mapping_status") or raw.get("mapping_status") or "UNVERIFIED").upper(),
        "evidence_contract": {
            "claim_types": _str_list(contract.get("claim_types")),
            "required_evidence_kinds": _str_list(contract.get("required_evidence_kinds")),
            "min_level": str(min_level).upper() if min_level not in (None, "") else None,
            "trusted_origins": _str_list(contract.get("trusted_origins")),
            "freshness": str(freshness) if freshness not in (None, "") else None,
        },
        "mapping_source": "LOCAL" if key in local else "NONE",
        "committed_mapping_ignored": _has_mapping_content(raw),
    }


def pack_binding_hash(entry: dict[str, Any], mci_content_hash: Any) -> str:
    """Hash a human mapping decision binds: the pack's controls + evidence contract and the MCI content hash."""
    return content_hash({"pack": {k: entry[k] for k in ("key", "mci_controls", "evidence_contract")},
                         "mci_content_hash": normalize_hash(mci_content_hash)})


def _contract_complete(contract: dict[str, Any]) -> bool:
    return bool(contract["claim_types"]) and bool(contract["required_evidence_kinds"]) \
        and contract["min_level"] in EVIDENCE_LEVELS


def _mapping_decision(session: Session, key: str, binding: str) -> tuple[Any, bool]:
    """(ACTIVE human POLICY decision on this pack bound to *binding*, an older-bound one exists)."""
    from aeos.models.decisions import Decision
    from aeos.policy.decisions import effective_status

    rows = list(session.scalars(select(Decision).where(
        Decision.kind == "POLICY", Decision.target_type == TARGET_MCI_PACK_MAPPING, Decision.target_id == key,
        Decision.status == "ACTIVE").order_by(Decision.created_at.desc())))
    live = [d for d in rows if effective_status(d) == "ACTIVE" and d.actor_kind == "HUMAN"
            and d.domain in (None, "PERSONAL")]
    bound = next((d for d in live if d.target_hash == binding), None)
    return bound, bound is None and bool(live)


def _pack_views(entries: list[dict[str, Any]], *, mci_status: str, mci_hash: Any,
                session: Session | None, show_decision_ids: bool) -> list[dict[str, Any]]:
    out = []
    for e in entries:
        binding = pack_binding_hash(e, mci_hash)
        status, note, decision_id = "UNVERIFIED", "", None
        complete = bool(e["mci_controls"]) and _contract_complete(e["evidence_contract"])
        if mci_status != "VERIFIED":
            note = "MCI_V2 source is not VERIFIED: no pack can be mapped to its controls yet."
        elif not complete:
            note = ("No MCI controls and complete evidence contract (claim_types, required_evidence_kinds, min_level) "
                    f"recorded for this pack in {LOCAL_DIR_LABEL}/{PACK_MAPPING_FILE}.")
        elif session is None:
            note = "Mapping decision not evaluated without the database (UNVERIFIED)."
        else:
            decision, stale = _mapping_decision(session, e["key"], binding)
            if decision is not None:
                status, decision_id = "APPROVED", decision.id
                note = "Owner POLICY decision binds this mapping and the MCI content hash."
            elif stale:
                note = "An owner decision binds an earlier mapping or MCI version (STALE): record a new decision."
            else:
                note = "No ACTIVE owner POLICY decision binds this mapping."
        if e["declared_mapping_status"] != "UNVERIFIED" and status != "APPROVED":
            note = f"Mapping declares {e['declared_mapping_status']} without a binding owner decision. {note}".strip()
        if e["committed_mapping_ignored"]:
            note = (f"{note} Mapping content in the committed policy/{MCI_FILE} is ignored (public repository): keep "
                    f"it in {LOCAL_DIR_LABEL}/{PACK_MAPPING_FILE}.").strip()
        out.append({
            **e, "mapping_status": status, "note": note,
            "evidence_contract_status": "DECLARED" if _contract_complete(e["evidence_contract"]) else "UNDECIDED",
            "decision_id": decision_id if show_decision_ids else None,
            "binding": {"kind": "POLICY", "target_type": TARGET_MCI_PACK_MAPPING, "target_id": e["key"],
                        "target_hash": binding},
        })
    return out


def mci_reference(*, repo: Path | None = None, session: Session | None = None, show_decision_ids: bool = True,
                  settings: Settings | None = None) -> dict[str, Any]:
    """MCI_V2 reference as declared in ``policy/mci-reference.yaml`` and verified against the owner's LOCAL source.

    ``repo`` reads ``<repo>/policy`` (Doctor contexts, tests); by default the controller's own ``policy/``. The
    source and the per-pack mapping are read from this machine only (``settings``: data dir and aeos.yaml
    ``mci_source_path``; default the controller's settings). ``session`` evaluates the owner's per-pack mapping
    decisions; without it every pack stays UNVERIFIED. Compliance is never claimed (NOT-PROVEN)."""
    data = _mci_file(repo)
    raw = data.get("mci")
    mci: dict[str, Any] = raw if isinstance(raw, dict) else {}
    declared = str(mci.get("status") or "UNAVAILABLE").upper()
    status, code, verification_note = _verify(mci, repo, settings)
    _path, _refused, logical = _resolve_source(mci.get("location"), repo, settings)
    local, mapping_state = local_pack_mapping(settings)
    entries = [e for e in (_pack_entry(p, local) for p in (data.get("packs") or [])) if e is not None]
    packs = _pack_views(entries, mci_status=status, mci_hash=mci.get("content_hash"), session=session,
                        show_decision_ids=show_decision_ids)
    approved = {p["key"] for p in packs if p["mapping_status"] == "APPROVED"}
    keys = [p["key"] for p in packs]
    if approved and all(k in approved for k in MASTER_PACKS):
        packs_status = "APPROVED"
    elif approved:
        packs_status = "PARTIAL"
    else:
        packs_status = "UNVERIFIED"
    return {
        "name": mci.get("name") or "MCI_V2_SOURCE_OF_TRUTH",
        "declared_status": declared,
        "status": status,  # VERIFIED only with a readable, hash-matching local source; never READY
        "verification_code": code,
        "compliance": "NOT-PROVEN",  # §34: no compliance claim from references/templates
        "version": mci.get("version"),
        "content_hash": mci.get("content_hash"),
        "location": mci.get("location"),
        "source_location": logical,  # logical only (local:<file> / aeos.yaml mci_source_path), never a host path
        "source_placement": "LOCAL_ONLY",
        "local_directory": LOCAL_DIR_LABEL,
        "verified_at": mci.get("verified_at"),
        "note": mci.get("note") or "",
        "verification_note": verification_note,
        "packs": keys,
        "pack_mappings": packs,
        "pack_mapping_file": f"{LOCAL_DIR_LABEL}/{PACK_MAPPING_FILE}",
        "pack_mapping_state": mapping_state,
        "packs_missing": [k for k in MASTER_PACKS if k not in keys],
        "packs_mapping_status": packs_status,
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
