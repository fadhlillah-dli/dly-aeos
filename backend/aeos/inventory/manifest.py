"""InstallManifest loading, validation and the DESIGN-lens upsert (§07, §13, §37, §45).

The manifest is the reviewed desired state. Loading it:

* validates against ``install-manifest/schema.json`` (jsonschema) plus semantic rules the schema
  cannot express (unique ids, relation targets exist, every ``source_url`` appears verbatim in the
  master document, every S-id exists in master §52, loopback-only health probes, no "latest" pins);
* stores an ``InstallManifestRecord`` per distinct content hash (history is kept);
* upserts ``ComponentReference`` rows (mode DESIGN) and DESIGN-lens ``ComponentRelation`` rows.

Seeding NEVER sets installation/usage/enablement/verification state: new rows start with
admission DISCOVERED, enablement DISABLED and lifecycle ACTIVE, and existing decision-side columns
(admission, enablement, lifecycle_final, decision_refs) are never overwritten by a reload.
Components or relations dropped from the manifest are tombstoned, not deleted (§38).
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.errors import ValidationFailed
from aeos.core.hashing import content_hash, file_sha256
from aeos.core.paths import repo_root
from aeos.core.timeutil import utcnow
from aeos.models.inventory import ComponentReference, ComponentRelation, InstallManifestRecord

MANIFEST_REL = "install-manifest/manifest.yaml"
SCHEMA_REL = "install-manifest/schema.json"
MASTER_REL = "docs/architecture/DLY_AEOS_Master_Architecture_Final.md"

CATEGORIES: tuple[str, ...] = (
    "experience/intake",
    "planning/governance",
    "workforce",
    "tools/context",
    "code/docs/CI",
    "data/secrets",
    "operations/evaluation",
)
CATEGORY_LABELS: dict[str, str] = {
    "experience/intake": "Pengalaman & intake",
    "planning/governance": "Perencanaan & tata kelola",
    "workforce": "Workforce",
    "tools/context": "Tools & konteks",
    "code/docs/CI": "Kode, dokumen & CI",
    "data/secrets": "Data & secret",
    "operations/evaluation": "Operasi & evaluasi",
}
RELATION_TYPES: tuple[str, ...] = (
    "depends_on", "calls", "reads", "writes", "publishes_to", "verifies", "stores_in",
    "runs_with", "implements", "governed_by", "replaces", "alternative_to",
)
DOMAINS: tuple[str, ...] = ("PLATFORM", "PERSONAL", "OFFICE")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "[::1]"})
_URL_IN_TEXT = re.compile(r"https://[^\s)\]>|;,]+")
_SID_IN_TEXT = re.compile(r"^\|\s*(S\d{2})\s*\|", re.MULTILINE)


@dataclass
class ManifestLoad:
    data: dict[str, Any]
    content_hash: str
    file_hash: str
    path: Path
    warnings: list[str] = field(default_factory=list)


def manifest_path(root: Path | None = None) -> Path:
    return (root or repo_root()) / MANIFEST_REL


def schema_path(root: Path | None = None) -> Path:
    return (root or repo_root()) / SCHEMA_REL


def master_path(root: Path | None = None) -> Path:
    return (root or repo_root()) / MASTER_REL


def master_references(master_text: str) -> tuple[set[str], set[str]]:
    """URLs and S-ids that appear in the master (S-ids from the §52 reference table)."""
    urls = {u.rstrip(".") for u in _URL_IN_TEXT.findall(master_text)}
    sids = set(_SID_IN_TEXT.findall(master_text))
    return urls, sids


def _schema_errors(data: Any, schema: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(schema)
    errors = []
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
        loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"schema: {loc}: {err.message[:300]}")
    return errors


def settings_keys() -> set[str]:
    from aeos.core.config import Settings

    return set(Settings.model_fields)


def _identity_field_errors(cid: Any, c: dict[str, Any], master_urls: set[str] | None) -> list[str]:
    """§07/§25/§37 structured identity fields: digest, distribution, signature, ports, configuration references,
    compatibility. Settings references must name a real setting; ports are loopback only; a distribution URL is
    never guessed (it must appear in the master like ``source_url``); a VERIFIED signature needs its time."""
    errors: list[str] = []
    keys = settings_keys()
    for ref in c.get("config_refs") or []:
        kind, _, value = str(ref).partition(":")
        if kind == "settings" and value not in keys:
            errors.append(f"{cid}: config_ref {ref!r} is not a Settings field")
        if kind == "file" and (".." in value or value.startswith("/")):
            errors.append(f"{cid}: config_ref {ref!r} must be a repo-relative path")
    for p in c.get("ports") or []:
        if p.get("setting") and p["setting"] not in keys:
            errors.append(f"{cid}: port setting {p['setting']!r} is not a Settings field")
    dist = c.get("distribution") or {}
    if dist.get("url") and master_urls is not None and dist["url"] not in master_urls:
        errors.append(f"{cid}: distribution.url {dist['url']!r} does not appear in the master (never guessed)")
    sig = c.get("signature") or {}
    if sig.get("result") == "VERIFIED" and not (sig.get("verified") and sig.get("verified_at")):
        errors.append(f"{cid}: signature VERIFIED needs verified=true and verified_at")
    if sig.get("verified") and sig.get("result") != "VERIFIED":
        errors.append(f"{cid}: signature.verified=true contradicts result {sig.get('result')}")
    if c.get("approved_digest") and not c.get("approved_pin"):
        errors.append(f"{cid}: approved_digest needs the approved_pin it belongs to")
    return errors


def _semantic_errors(data: dict[str, Any], master_text: str | None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    comps = data.get("components") or []
    ids = [c.get("component_id") for c in comps]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        errors.append(f"duplicate component_id: {dupes}")
    known = set(ids)
    urls: set[str] = set()
    sids: set[str] = set()
    if master_text is not None:
        urls, sids = master_references(master_text)
    else:
        warnings.append("master document not found; source_url/S-id provenance not cross-checked")
    for c in comps:
        cid = c.get("component_id")
        for rel in c.get("relations") or []:
            if rel.get("target") not in known:
                errors.append(f"{cid}: relation target {rel.get('target')!r} is not a registered component")
            if rel.get("target") == cid:
                errors.append(f"{cid}: self relation")
        url = c.get("source_url")
        if url and master_text is not None and url not in urls:
            errors.append(f"{cid}: source_url {url!r} does not appear in the master (no guessed URLs, §37)")
        for sid in c.get("source_refs") or []:
            if master_text is not None and sid not in sids:
                errors.append(f"{cid}: source ref {sid} is not in master §52")
        pin = c.get("approved_pin")
        if isinstance(pin, str) and "latest" in pin.lower():
            errors.append(f"{cid}: approved_pin must not be 'latest' (§37/§38)")
        if c.get("license") not in (None, "") and master_text is not None and str(c["license"]) not in master_text:
            errors.append(f"{cid}: license {c['license']!r} is not stated in the master (never guessed)")
        probe = c.get("probe") or {}
        health = probe.get("health") if isinstance(probe, dict) else None
        if health and health.get("settings_url") not in ("openbao_addr",):
            errors.append(f"{cid}: health probe settings_url must be a known loopback setting")
        if c.get("identity_verified") == "UNVERIFIED" and (c.get("operations") or {}).get("install"):
            errors.append(f"{cid}: UNVERIFIED component must not carry an install operation")
        errors += _identity_field_errors(cid, c, urls if master_text is not None else None)
    cats = data.get("categories") or []
    if list(cats) != list(CATEGORIES):
        errors.append("categories must list the seven §36 fold domains in order")
    return errors, warnings


def validate_manifest(
    data: Any, *, schema: dict[str, Any] | None = None, master_text: str | None = None, root: Path | None = None
) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). An empty error list means the manifest is admissible."""
    if schema is None:
        schema = json.loads(schema_path(root).read_text(encoding="utf-8"))
    errors = _schema_errors(data, schema)
    if not isinstance(data, dict):
        return errors or ["manifest must be a mapping"], []
    sem_errors, warnings = _semantic_errors(data, master_text)
    return errors + sem_errors, warnings


_CACHE: dict[tuple[Any, ...], ManifestLoad] = {}


def _stamp(p: Path) -> tuple[str, int, int]:
    try:
        st = p.stat()
        return (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(p), 0, 0)


def load_manifest(path: Path | None = None, *, root: Path | None = None) -> ManifestLoad:
    """Read, parse and validate the manifest. Raises ``ValidationFailed`` with every error.

    Validated results are cached per (manifest, schema, master) file identity; callers get a copy.
    """
    root = root or repo_root()
    path = path or manifest_path(root)
    if not path.is_file():
        raise ValidationFailed(f"install manifest not found at {MANIFEST_REL}", code="MANIFEST_MISSING")
    key = (_stamp(path), _stamp(schema_path(root)), _stamp(master_path(root)))
    cached = _CACHE.get(key)
    if cached is not None:
        return ManifestLoad(data=copy.deepcopy(cached.data), content_hash=cached.content_hash,
                            file_hash=cached.file_hash, path=cached.path, warnings=list(cached.warnings))
    load = _load_uncached(path, root)
    _CACHE.clear()
    _CACHE[key] = load
    return ManifestLoad(data=copy.deepcopy(load.data), content_hash=load.content_hash, file_hash=load.file_hash,
                        path=load.path, warnings=list(load.warnings))


def _load_uncached(path: Path, root: Path) -> ManifestLoad:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValidationFailed(f"install manifest is not valid YAML: {exc}", code="MANIFEST_INVALID") from exc
    mp = master_path(root)
    master_text = mp.read_text(encoding="utf-8") if mp.is_file() else None
    errors, warnings = validate_manifest(data, master_text=master_text, root=root)
    if errors:
        raise ValidationFailed(
            f"install manifest failed validation ({len(errors)} errors)",
            code="MANIFEST_INVALID",
            details={"errors": errors[:200]},
        )
    return ManifestLoad(data=data, content_hash=content_hash(data), file_hash=file_sha256(path), path=path,
                        warnings=warnings)


def current_manifest(session: Session) -> InstallManifestRecord | None:
    return session.scalar(
        select(InstallManifestRecord).order_by(InstallManifestRecord.created_at.desc(),
                                               InstallManifestRecord.id.desc()).limit(1)
    )


def store_manifest(session: Session, load: ManifestLoad, *, loaded_by: str) -> tuple[InstallManifestRecord, bool]:
    """Store a new record when the content hash changed; return (record, stored_new)."""
    latest = current_manifest(session)
    if latest is not None and latest.content_hash == load.content_hash:
        return latest, False
    rel = MANIFEST_REL
    rec = InstallManifestRecord(
        manifest_version=str(load.data.get("manifest_version", "1")),
        source_path=rel,
        content_hash=load.content_hash,
        content=load.data,
        loaded_by=loaded_by,
    )
    session.add(rec)
    session.flush()
    return rec, True


# Columns the manifest owns (DESIGN side). Decision-side columns are intentionally absent.
_DESIGN_FIELDS = (
    "kind", "name", "friendly_name", "function", "why_selected", "category", "source_kind", "source_url",
    "source_refs", "upstream_identity", "identity_verified", "license", "usage_role", "requirement",
    "design_label", "approved_pin", "bootstrap_order", "probe", "capability_tests", "access", "cost",
    "operations", "placement",
)


def _component_values(c: dict[str, Any], manifest_hash: str, sources_checked: str | None) -> dict[str, Any]:
    values = {f: c.get(f) for f in _DESIGN_FIELDS}
    values["probe"] = c.get("probe") or {"type": "none", "reason": "tidak ada probe"}
    values["source_refs"] = list(c.get("source_refs") or [])
    values["capability_tests"] = list(c.get("capability_tests") or [])
    values["access"] = dict(c.get("access") or {})
    values["cost"] = dict(c.get("cost") or {})
    values["operations"] = dict(c.get("operations") or {})
    values["scope"] = {
        "domains": list(c.get("domains") or ["PLATFORM"]),
        "level": "platform",
        "environment": "local",
    }
    values["details"] = {
        "discovery": dict(c.get("discovery") or {}),
        "invalidators": list(c.get("invalidators") or []),
        "notes": c.get("notes") or "",
        "source_checked_on": sources_checked,
        "maintainer": None,
        # §07/§25/§37 structured identity of the reviewed artifact (DESIGN side; observations are compared to it)
        "approved_digest": c.get("approved_digest"),
        "distribution": dict(c["distribution"]) if c.get("distribution") else None,
        "signature": dict(c["signature"]) if c.get("signature") else None,
        "ports": list(c.get("ports") or []),
        "config_refs": list(c.get("config_refs") or []),
        "compatibility": dict(c["compatibility"]) if c.get("compatibility") else None,
        "tool_schema_hash": c.get("tool_schema_hash"),
    }
    values["manifest_hash"] = manifest_hash
    values["mode"] = "DESIGN"
    return values


def upsert_components(session: Session, data: dict[str, Any], *, manifest_hash: str) -> dict[str, int]:
    """Apply the manifest's DESIGN lens. Never touches admission/enablement/lifecycle/usage state."""
    now = utcnow()
    counts = {"created": 0, "updated": 0, "unchanged": 0, "tombstoned": 0, "restored": 0,
              "relations_created": 0, "relations_updated": 0, "relations_tombstoned": 0}
    sources_checked = data.get("sources_checked")
    desired = {c["component_id"]: c for c in data.get("components") or []}
    existing = {r.component_id: r for r in session.scalars(select(ComponentReference))}

    for cid, c in desired.items():
        values = _component_values(c, manifest_hash, sources_checked)
        row = existing.get(cid)
        if row is None:
            row = ComponentReference(component_id=cid, admission_status="DISCOVERED", enablement="DISABLED",
                                     lifecycle_final="ACTIVE", decision_refs=[], **values)
            session.add(row)
            counts["created"] += 1
            continue
        changed = False
        for k, v in values.items():
            if k == "manifest_hash":
                continue
            if getattr(row, k) != v:
                setattr(row, k, v)
                changed = True
        if row.tombstoned_at is not None:
            row.tombstoned_at = None
            counts["restored"] += 1
            changed = True
        if row.manifest_hash != manifest_hash:
            row.manifest_hash = manifest_hash
        counts["updated" if changed else "unchanged"] += 1

    for cid, row in existing.items():
        if cid not in desired and row.mode == "DESIGN" and row.tombstoned_at is None:
            row.tombstoned_at = now  # removal keeps history (§38 tombstones)
            counts["tombstoned"] += 1
    session.flush()

    desired_edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    for cid, c in desired.items():
        for rel in c.get("relations") or []:
            desired_edges[(cid, rel["target"], rel["relation"])] = rel
    rows = {
        (r.source_id, r.target_id, r.relation): r
        for r in session.scalars(select(ComponentRelation).where(ComponentRelation.lens == "DESIGN"))
    }
    for key, rel in desired_edges.items():
        src, tgt, kind = key
        domains_src = desired[src].get("domains") or ["PLATFORM"]
        domains_tgt = desired[tgt].get("domains") or ["PLATFORM"]
        vals = {
            "provenance": f"install-manifest:{manifest_hash}",
            "data_description": rel.get("data_description", ""),
            "boundary": rel.get("boundary"),
            "operation": rel.get("operation") or "",
            "identity_ref": rel.get("identity_ref"),
            "scope": {"domains": sorted(set(domains_src) | set(domains_tgt)), "source": "manifest"},
        }
        rel_row = rows.get(key)
        if rel_row is None:
            session.add(ComponentRelation(source_id=src, target_id=tgt, relation=kind, lens="DESIGN", **vals))
            counts["relations_created"] += 1
            continue
        changed = False
        for k, v in vals.items():
            if k == "provenance":
                continue
            if getattr(rel_row, k) != v:
                setattr(rel_row, k, v)
                changed = True
        if rel_row.tombstoned_at is not None:
            rel_row.tombstoned_at = None
            changed = True
        if changed or not rel_row.provenance:
            rel_row.provenance = vals["provenance"]
            counts["relations_updated"] += 1
    for key, stale_row in rows.items():
        if key not in desired_edges and stale_row.tombstoned_at is None:
            stale_row.tombstoned_at = now
            counts["relations_tombstoned"] += 1
    session.flush()
    return counts


def apply_manifest(session: Session, *, actor: str = "cli", path: Path | None = None,
                   root: Path | None = None) -> dict[str, Any]:
    """Load + validate + store + upsert, with audit and an event when the content changed."""
    from aeos.audit import service as audit
    from aeos.events import service as events

    load = load_manifest(path, root=root)
    previous = current_manifest(session)
    record, stored = store_manifest(session, load, loaded_by=actor)
    counts = upsert_components(session, load.data, manifest_hash=load.content_hash)
    invalidated = 0
    if stored and previous is not None:
        # The desired state changed: readiness results computed against the old manifest are STALE
        # until rechecked (§08 "Perubahan ... konfigurasi yang relevan membuat pemeriksaan terdampak STALE").
        try:
            from aeos.doctor import facade as doctor

            invalidated = doctor.invalidate(
                session, invalidator="manifest",
                reason=f"InstallManifest berubah {previous.content_hash[:19]} -> {load.content_hash[:19]}")
        except (ImportError, ModuleNotFoundError):
            invalidated = 0
    counts["checks_marked_stale"] = invalidated
    if stored:
        audit.record(
            session, actor_type="cli" if actor in ("cli", "seed") else "user", actor_id=actor,
            action="inventory.manifest.load", target_type="install_manifest", target_id=record.id,
            details={"content_hash": load.content_hash, "file_hash": load.file_hash, **counts},
        )
    if stored or any(v for k, v in counts.items() if k != "unchanged"):
        events.emit(
            session, source="inventory", kind="inventory.manifest.loaded", entity_type="install_manifest",
            entity_id=record.id, entity_version=load.content_hash, mode="DESIGN",
            payload={"stored_new_record": stored, "components": len(load.data.get("components") or []), **counts},
        )
    return {
        "manifest_record_id": record.id,
        "content_hash": load.content_hash,
        "stored_new_record": stored,
        "components": len(load.data.get("components") or []),
        "warnings": load.warnings,
        **counts,
    }
