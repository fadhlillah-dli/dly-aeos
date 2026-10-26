"""Drift between the DESIGN, CONFIG and OBSERVED lenses (§36, §38 "Drift dan kegagalan").

Drift classes (§36):

* ``planned-but-missing``      — designed (REQUIRED/OPTIONAL) but a fresh probe did not find it;
* ``installed-but-disabled``   — found installed, but not enabled in approved config ("Terpasang
  tetapi tidak aktif");
* ``enabled-but-unverified``   — enabled, but no fresh VERIFIED functional proof;
* ``resolved-version-mismatch``— observed version/identity/content differs from the approved pin;
* ``observed-but-unregistered``— found in managed scope without registration (never adopted);
* ``obsolete-reference``       — a live record still references a tombstoned/unknown component.

Only fresh (non-expired) observations create drift; stale or missing evidence is a coverage gap,
not a finding. Drift is not automatically a security incident: severity follows the reviewed policy
reference ``policy/inventory-drift.yaml`` (protected path) and the evidence; the constants below are
only the fail-safe defaults when that file is absent or invalid.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.paths import repo_root
from aeos.core.timeutil import utcnow
from aeos.inventory.configlens import config_basis, is_explicit
from aeos.inventory.observations import UNREGISTERED_PREFIX, is_stale, latest_observations
from aeos.inventory.pins import platform_key, select_digest
from aeos.inventory.probes import version_tuple
from aeos.models.inventory import ComponentObservation, ComponentReference, ComponentRelation
from aeos.models.inventory_ext import DriftFinding

DRIFT_CLASSES: tuple[str, ...] = (
    "planned-but-missing",
    "installed-but-disabled",
    "enabled-but-unverified",
    "resolved-version-mismatch",
    "observed-but-unregistered",
    "obsolete-reference",
)
DRIFT_LABELS = {
    "planned-but-missing": "Direncanakan tetapi tidak ditemukan",
    "installed-but-disabled": "Terpasang tetapi tidak aktif",
    "enabled-but-unverified": "Aktif tetapi belum terverifikasi",
    "resolved-version-mismatch": "Versi/identitas berbeda dari pin",
    "observed-but-unregistered": "Teramati tetapi tidak terdaftar",
    "obsolete-reference": "Referensi usang",
}
COMPONENT_CLASSES = frozenset(DRIFT_CLASSES[:4])
# findings derived from the installation observation (pin / reviewed digest): they are only evaluated while that
# observation is INSTALLED. An UNKNOWN installation (the binary did not run) is "not evaluated", never "resolved".
INSTALLATION_MISMATCH_KEYS = frozenset({"version", "digest"})
MISSING_STATES = frozenset({"NOT_INSTALLED", "REMOVED", "INSTALL_FAILED"})
SEVERITIES = ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")
POLICY_REL = "policy/inventory-drift.yaml"
DEFAULT_POLICY: dict[str, Any] = {
    "bootstrap_core_max_order": 3,
    "severity": {
        "planned-but-missing": {"required_bootstrap_core": "HIGH", "required": "MEDIUM", "optional": "LOW"},
        "installed-but-disabled": {"default": "INFO"},
        "enabled-but-unverified": {"explicit": "MEDIUM", "implicit": "LOW"},
        "resolved-version-mismatch": {"version": "HIGH", "digest": "HIGH", "tool_schema": "HIGH",
                                      "configuration": "MEDIUM"},
        "observed-but-unregistered": {"default": "LOW"},
        "obsolete-reference": {"default": "LOW"},
    },
}
_policy_cache: dict[str, Any] = {}


def policy_path(root: Path | None = None) -> Path:
    return (root or repo_root()) / POLICY_REL


def drift_policy(root: Path | None = None) -> dict[str, Any]:
    """The reviewed severity policy merged over the defaults (unknown keys/invalid values ignored).
    Cached per file identity, so an edited policy applies on the next reconciliation."""
    path = policy_path(root)
    try:
        st = path.stat()
        stamp: tuple[Any, ...] = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = (str(path), 0, 0)
    if _policy_cache.get("stamp") == stamp:
        return copy.deepcopy(_policy_cache["policy"])
    merged = copy.deepcopy(DEFAULT_POLICY)
    merged["source"] = "default (policy file missing)"
    merged["warnings"] = []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) if stamp[1] else None
    except (OSError, yaml.YAMLError) as exc:
        raw = None
        merged["warnings"].append(f"{POLICY_REL} unreadable ({type(exc).__name__}); defaults used")
    if isinstance(raw, dict):
        merged["source"] = POLICY_REL
        order = raw.get("bootstrap_core_max_order")
        if isinstance(order, int) and 0 <= order <= 11:
            merged["bootstrap_core_max_order"] = order
        for cls, levels in (raw.get("severity") or {}).items():
            if cls not in merged["severity"] or not isinstance(levels, dict):
                merged["warnings"].append(f"unknown drift class {cls!r} ignored")
                continue
            for key, value in levels.items():
                if key in merged["severity"][cls] and value in SEVERITIES:
                    merged["severity"][cls][key] = value
                else:
                    merged["warnings"].append(f"{cls}.{key}={value!r} ignored")
    _policy_cache.update(stamp=stamp, policy=merged)
    return copy.deepcopy(merged)


def severity(policy: dict[str, Any], drift_class: str, key: str) -> str:
    levels = policy["severity"].get(drift_class) or {}
    return levels.get(key) or levels.get("default") or DEFAULT_POLICY["severity"][drift_class].get(
        key, next(iter(DEFAULT_POLICY["severity"][drift_class].values())))


@dataclass
class DriftItem:
    component_id: str
    drift_class: str
    severity: str
    summary: str
    key: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    lens_from: str = "DESIGN"
    lens_to: str = "OBSERVED"
    domain_scope: str = "PLATFORM"


def domain_scope(comp: ComponentReference | None, obs: ComponentObservation | None = None) -> str:
    if obs is not None and (obs.scope or {}).get("domain") in ("PERSONAL", "OFFICE"):
        return str(obs.scope["domain"])
    domains = set(((comp.scope or {}).get("domains") or ["PLATFORM"]) if comp else ["PLATFORM"])
    if "PLATFORM" in domains or len(domains) != 1:
        return "PLATFORM"
    return next(iter(domains))


def _fresh(obs: ComponentObservation | None, now: datetime) -> ComponentObservation | None:
    return obs if obs is not None and not is_stale(obs, now) else None


def versions_match(pin: str, observed: str) -> bool:
    a, b = version_tuple(pin), version_tuple(observed)
    if a and b:
        return a == b[: len(a)]
    return pin.strip().lstrip("v") == observed.strip().lstrip("v")


def _component_items(comp: ComponentReference, latest: dict[tuple[str, str], ComponentObservation],
                     now: datetime, basis: list[str] | None = None, policy: dict[str, Any] | None = None
                     ) -> list[DriftItem]:
    policy = policy or drift_policy()
    cid = comp.component_id
    scope = domain_scope(comp)
    inst = _fresh(latest.get((cid, "installation")), now)
    conf = _fresh(latest.get((cid, "configuration")), now)
    func = _fresh(latest.get((cid, "functional")), now)
    in_config = bool(basis)
    items: list[DriftItem] = []
    if comp.requirement in ("REQUIRED", "OPTIONAL") and inst is not None and inst.status in MISSING_STATES:
        sev = severity(policy, "planned-but-missing", "required" if comp.requirement == "REQUIRED" else "optional")
        if comp.requirement == "REQUIRED" and comp.bootstrap_order is not None and \
                comp.bootstrap_order <= policy["bootstrap_core_max_order"]:
            sev = severity(policy, "planned-but-missing", "required_bootstrap_core")
        items.append(DriftItem(
            cid, "planned-but-missing", sev,
            f"{comp.requirement}: dirancang tetapi tidak ditemukan saat probe {inst.observed_at.isoformat()}",
            detail={"observation_id": inst.id, "observed_status": inst.status, "observer": inst.observer,
                    "reason": (inst.detail or {}).get("reason")},
            domain_scope=scope,
        ))
    if inst is not None and inst.status == "INSTALLED" and not in_config:
        items.append(DriftItem(
            cid, "installed-but-disabled", severity(policy, "installed-but-disabled", "default"),
            "Terpasang tetapi tidak aktif: belum di-enable melalui keputusan/konfigurasi yang sah",
            detail={"observation_id": inst.id, "enablement": comp.enablement}, lens_from="CONFIG",
            domain_scope=scope,
        ))
    if in_config and comp.capability_tests and (func is None or func.status != "VERIFIED"):
        items.append(DriftItem(
            cid, "enabled-but-unverified",
            severity(policy, "enabled-but-unverified", "explicit" if is_explicit(basis) else "implicit"),
            "Berlaku dalam konfigurasi tetapi belum ada bukti fungsi VERIFIED yang masih berlaku",
            detail={"functional": func.status if func else "NOT_TESTED",
                    "observation_id": func.id if func else None, "config_basis": basis,
                    "capability_tests": [t.get("id") for t in comp.capability_tests]},
            lens_from="CONFIG", domain_scope=scope,
        ))
    if comp.approved_pin and inst is not None and inst.status == "INSTALLED":
        observed = inst.observed_version
        if observed and not versions_match(comp.approved_pin, observed):
            items.append(DriftItem(
                cid, "resolved-version-mismatch", severity(policy, "resolved-version-mismatch", "version"),
                f"Versi teramati {observed} berbeda dari pin {comp.approved_pin}",
                key="version", detail={"approved_pin": comp.approved_pin, "observed_version": observed,
                                       "observation_id": inst.id},
                domain_scope=scope,
            ))
    # §07: the reviewed digest of THIS host's <os>_<arch> (a per-platform mapping without an entry for it is
    # NOT_RECORDED here - never compared with another platform's executable)
    digest_sel = select_digest((comp.details or {}).get("approved_digest"))
    approved_digest = digest_sel["digest"]
    if digest_sel["status"] == "INVALID" and inst is not None and inst.status == "INSTALLED":
        # a malformed reviewed digest cannot be compared: fail closed (drift), never "nothing to compare"
        items.append(DriftItem(
            cid, "resolved-version-mismatch", severity(policy, "resolved-version-mismatch", "digest"),
            f"Digest yang disetujui tidak valid ({digest_sel['reason']}); executable teramati tidak dapat dibandingkan",
            key="digest", detail={"approved_digest_status": "INVALID", "reason": digest_sel["reason"],
                                  "observed_digest": inst.observed_identity, "platform": digest_sel["platform"],
                                  "observation_id": inst.id},
            domain_scope=scope,
        ))
    if approved_digest and inst is not None and inst.status == "INSTALLED" and inst.observed_identity \
            and str(inst.observed_identity).startswith("sha256:") and inst.observed_identity != approved_digest:
        # same --version but different bytes: the executable is not the reviewed artifact (§37)
        items.append(DriftItem(
            cid, "resolved-version-mismatch", severity(policy, "resolved-version-mismatch", "digest"),
            f"Digest executable teramati {str(inst.observed_identity)[:23]}... berbeda dari digest yang disetujui",
            key="digest", detail={"approved_digest": approved_digest, "observed_digest": inst.observed_identity,
                                  "platform": platform_key(), "observation_id": inst.id},
            domain_scope=scope,
        ))
    approved_schema = (comp.details or {}).get("tool_schema_hash")
    schema_obs = next((o for o in (conf, _fresh(latest.get((cid, "functional")), now))
                       if o is not None and (o.detail or {}).get("tool_schema_hash")), None)
    if approved_schema and schema_obs is not None and schema_obs.detail["tool_schema_hash"] != approved_schema:
        items.append(DriftItem(
            cid, "resolved-version-mismatch", severity(policy, "resolved-version-mismatch", "tool_schema"),
            "Schema tool/MCP yang teramati berbeda dari yang disetujui saat admission; perubahan sistem perlu proposal",
            key="tool_schema", detail={"approved_tool_schema_hash": approved_schema,
                                       "observed_tool_schema_hash": schema_obs.detail["tool_schema_hash"],
                                       "observation_id": schema_obs.id},
            domain_scope=scope,
        ))
    if conf is not None and conf.status == "DRIFTED":
        aspect = (conf.detail or {}).get("aspect") or "configuration"
        items.append(DriftItem(
            cid, "resolved-version-mismatch", severity(policy, "resolved-version-mismatch", "configuration"),
            f"{aspect}: teramati berbeda dari yang disetujui ({(conf.detail or {}).get('reason') or 'DRIFTED'})",
            key=str(aspect), detail={**(conf.detail or {}), "observation_id": conf.id},
            lens_from="DESIGN", domain_scope=scope,
        ))
    return items


def compute_drift(session: Session, *, evaluated: set[str] | None = None,
                  now: datetime | None = None) -> list[DriftItem]:
    now = now or utcnow()
    policy = drift_policy()
    comps = {c.component_id: c for c in session.scalars(select(ComponentReference))}
    latest = latest_observations(session)
    basis = config_basis(session, comps, latest, now)
    items: list[DriftItem] = []
    for cid, comp in comps.items():
        if comp.tombstoned_at is not None or (evaluated is not None and cid not in evaluated):
            continue
        items.extend(_component_items(comp, latest, now, basis.get(cid), policy))
    if evaluated is not None:
        return items
    for (cid, dim), obs in latest.items():
        if not cid.startswith(UNREGISTERED_PREFIX) or dim != "discovery" or is_stale(obs, now):
            continue
        d = obs.detail or {}
        items.append(DriftItem(
            cid, "observed-but-unregistered", severity(policy, "observed-but-unregistered", "default"),
            f"{d.get('kind', 'komponen')} '{d.get('name', cid)}' ditemukan di {d.get('source', 'managed scope')} "
            "tanpa registrasi; perlu review (tidak diadopsi, tidak diberi akses)",
            detail={"observation_id": obs.id, **d}, lens_from="OBSERVED", lens_to="DESIGN",
            domain_scope=domain_scope(None, obs),
        ))
    for rel in session.scalars(select(ComponentRelation).where(ComponentRelation.tombstoned_at.is_(None))):
        for end in (rel.source_id, rel.target_id):
            end_comp = comps.get(end)
            if end_comp is None or end_comp.tombstoned_at is not None:
                items.append(DriftItem(
                    rel.source_id, "obsolete-reference", severity(policy, "obsolete-reference", "default"),
                    f"Relasi {rel.relation} ({rel.lens}) masih merujuk komponen '{end}' yang sudah tidak terdaftar",
                    key=f"relation:{rel.id}:{end}",
                    detail={"relation_id": rel.id, "relation": rel.relation, "lens": rel.lens, "missing": end},
                    lens_from=rel.lens, lens_to="DESIGN", domain_scope=domain_scope(comps.get(rel.source_id)),
                ))
    for (cid, _dim), obs in latest.items():
        tomb = comps.get(cid)
        if tomb is None or tomb.tombstoned_at is None or is_stale(obs, now):
            continue
        if obs.observed_at >= tomb.tombstoned_at:
            items.append(DriftItem(
                cid, "obsolete-reference", severity(policy, "obsolete-reference", "default"),
                "Komponen sudah dihapus dari manifest (tombstone) tetapi masih teramati",
                key=f"observation:{obs.dimension}", detail={"observation_id": obs.id, "tombstoned_at":
                                                            tomb.tombstoned_at.isoformat()},
                lens_from="OBSERVED", lens_to="DESIGN", domain_scope=domain_scope(tomb),
            ))
    return items


def persist_drift(session: Session, items: list[DriftItem], *, run_id: str | None,
                  evaluated: set[str] | None = None) -> dict[str, Any]:
    """Upsert OPEN findings, resolve those no longer seen (within the evaluated scope)."""
    from aeos.events import service as events

    now = utcnow()
    open_rows = list(session.scalars(select(DriftFinding).where(DriftFinding.status == "OPEN")))
    index = {(r.component_id, r.drift_class, r.finding_key): r for r in open_rows}
    seen: set[tuple[str, str, str]] = set()
    new = 0
    for it in items:
        key = (it.component_id, it.drift_class, it.key)
        if key in seen:
            continue
        seen.add(key)
        row = index.get(key)
        if row is None:
            row = DriftFinding(component_id=it.component_id, drift_class=it.drift_class, finding_key=it.key,
                               severity=it.severity, summary=it.summary, detail=it.detail, lens_from=it.lens_from,
                               lens_to=it.lens_to, domain_scope=it.domain_scope, status="OPEN", first_run_id=run_id,
                               last_run_id=run_id, first_detected_at=now, last_detected_at=now)
            session.add(row)
            session.flush()
            new += 1
            events.emit(session, source="inventory", kind="inventory.drift.detected", entity_type="component",
                        entity_id=it.component_id,
                        domain=it.domain_scope if it.domain_scope != "PLATFORM" else None,
                        payload={"finding_id": row.id, "drift_class": it.drift_class, "severity": it.severity,
                                 "summary": it.summary})
        else:
            row.severity, row.summary, row.detail = it.severity, it.summary, it.detail
            row.last_detected_at, row.last_run_id = now, run_id
    resolved = 0
    latest_inst = latest_observations(session, list({r.component_id for r in open_rows}), ("installation",))
    for key, row in index.items():
        if key in seen:
            continue
        if evaluated is not None and (row.component_id not in evaluated or row.drift_class not in COMPONENT_CLASSES):
            continue
        inst = latest_inst.get((row.component_id, "installation"))
        if row.drift_class == "resolved-version-mismatch" and row.finding_key in INSTALLATION_MISMATCH_KEYS \
                and inst is not None and inst.status == "UNKNOWN":
            continue  # not evaluated (evidence honesty): the mismatch is not proven gone, so it stays OPEN unchanged
        row.status, row.resolved_at = "RESOLVED", now
        resolved += 1
        events.emit(session, source="inventory", kind="inventory.drift.resolved", entity_type="component",
                    entity_id=row.component_id,
                    domain=row.domain_scope if row.domain_scope != "PLATFORM" else None,
                    payload={"finding_id": row.id, "drift_class": row.drift_class})
    session.flush()
    by_class: dict[str, int] = dict.fromkeys(DRIFT_CLASSES, 0)
    for it_key in seen:
        by_class[it_key[1]] = by_class.get(it_key[1], 0) + 1
    return {"open_detected": len(seen), "new": new, "resolved": resolved, "by_class": by_class}
