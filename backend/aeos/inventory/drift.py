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
not a finding. Drift is not automatically a security incident: severity follows policy/evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.timeutil import utcnow
from aeos.inventory.configlens import config_basis, is_explicit
from aeos.inventory.observations import UNREGISTERED_PREFIX, is_stale, latest_observations
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
MISSING_STATES = frozenset({"NOT_INSTALLED", "REMOVED", "INSTALL_FAILED"})


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
                     now: datetime, basis: list[str] | None = None) -> list[DriftItem]:
    cid = comp.component_id
    scope = domain_scope(comp)
    inst = _fresh(latest.get((cid, "installation")), now)
    conf = _fresh(latest.get((cid, "configuration")), now)
    func = _fresh(latest.get((cid, "functional")), now)
    in_config = bool(basis)
    items: list[DriftItem] = []
    if comp.requirement in ("REQUIRED", "OPTIONAL") and inst is not None and inst.status in MISSING_STATES:
        sev = "MEDIUM" if comp.requirement == "REQUIRED" else "LOW"
        if comp.requirement == "REQUIRED" and comp.bootstrap_order is not None and comp.bootstrap_order <= 3:
            sev = "HIGH"
        items.append(DriftItem(
            cid, "planned-but-missing", sev,
            f"{comp.requirement}: dirancang tetapi tidak ditemukan saat probe {inst.observed_at.isoformat()}",
            detail={"observation_id": inst.id, "observed_status": inst.status, "observer": inst.observer,
                    "reason": (inst.detail or {}).get("reason")},
            domain_scope=scope,
        ))
    if inst is not None and inst.status == "INSTALLED" and not in_config:
        items.append(DriftItem(
            cid, "installed-but-disabled", "INFO",
            "Terpasang tetapi tidak aktif: belum di-enable melalui keputusan/konfigurasi yang sah",
            detail={"observation_id": inst.id, "enablement": comp.enablement}, lens_from="CONFIG",
            domain_scope=scope,
        ))
    if in_config and comp.capability_tests and (func is None or func.status != "VERIFIED"):
        items.append(DriftItem(
            cid, "enabled-but-unverified", "MEDIUM" if is_explicit(basis) else "LOW",
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
                cid, "resolved-version-mismatch", "HIGH",
                f"Versi teramati {observed} berbeda dari pin {comp.approved_pin}",
                key="version", detail={"approved_pin": comp.approved_pin, "observed_version": observed,
                                       "observation_id": inst.id},
                domain_scope=scope,
            ))
    if conf is not None and conf.status == "DRIFTED":
        aspect = (conf.detail or {}).get("aspect") or "configuration"
        items.append(DriftItem(
            cid, "resolved-version-mismatch", "MEDIUM",
            f"{aspect}: teramati berbeda dari yang disetujui ({(conf.detail or {}).get('reason') or 'DRIFTED'})",
            key=str(aspect), detail={**(conf.detail or {}), "observation_id": conf.id},
            lens_from="DESIGN", domain_scope=scope,
        ))
    return items


def compute_drift(session: Session, *, evaluated: set[str] | None = None,
                  now: datetime | None = None) -> list[DriftItem]:
    now = now or utcnow()
    comps = {c.component_id: c for c in session.scalars(select(ComponentReference))}
    latest = latest_observations(session)
    basis = config_basis(session, comps, latest, now)
    items: list[DriftItem] = []
    for cid, comp in comps.items():
        if comp.tombstoned_at is not None or (evaluated is not None and cid not in evaluated):
            continue
        items.extend(_component_items(comp, latest, now, basis.get(cid)))
    if evaluated is not None:
        return items
    for (cid, dim), obs in latest.items():
        if not cid.startswith(UNREGISTERED_PREFIX) or dim != "discovery" or is_stale(obs, now):
            continue
        d = obs.detail or {}
        items.append(DriftItem(
            cid, "observed-but-unregistered", "LOW",
            f"{d.get('kind', 'komponen')} '{d.get('name', cid)}' ditemukan di {d.get('source', 'managed scope')} "
            "tanpa registrasi; perlu review (tidak diadopsi, tidak diberi akses)",
            detail={"observation_id": obs.id, **d}, lens_from="OBSERVED", lens_to="DESIGN",
            domain_scope=domain_scope(None, obs),
        ))
    for rel in session.scalars(select(ComponentRelation).where(ComponentRelation.tombstoned_at.is_(None))):
        for end in (rel.source_id, rel.target_id):
            comp = comps.get(end)
            if comp is None or comp.tombstoned_at is not None:
                items.append(DriftItem(
                    rel.source_id, "obsolete-reference", "LOW",
                    f"Relasi {rel.relation} ({rel.lens}) masih merujuk komponen '{end}' yang sudah tidak terdaftar",
                    key=f"relation:{rel.id}:{end}",
                    detail={"relation_id": rel.id, "relation": rel.relation, "lens": rel.lens, "missing": end},
                    lens_from=rel.lens, lens_to="DESIGN", domain_scope=domain_scope(comps.get(rel.source_id)),
                ))
    for (cid, _dim), obs in latest.items():
        comp = comps.get(cid)
        if comp is None or comp.tombstoned_at is None or is_stale(obs, now):
            continue
        if obs.observed_at >= comp.tombstoned_at:
            items.append(DriftItem(
                cid, "obsolete-reference", "LOW",
                "Komponen sudah dihapus dari manifest (tombstone) tetapi masih teramati",
                key=f"observation:{obs.dimension}", detail={"observation_id": obs.id, "tombstoned_at":
                                                            comp.tombstoned_at.isoformat()},
                lens_from="OBSERVED", lens_to="DESIGN", domain_scope=domain_scope(comp),
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
    for key, row in index.items():
        if key in seen:
            continue
        if evaluated is not None and (row.component_id not in evaluated or row.drift_class not in COMPONENT_CLASSES):
            continue
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
