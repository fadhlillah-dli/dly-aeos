"""TechnologyInventoryView and SystemMapView projections (§13, §35-§38).

Pure read models over InstallManifest/ComponentReference (DESIGN), approved configuration records
(CONFIG) and dated observations (OBSERVED). They own no scheduler, credential or task state.

Rules kept here:
* the §38 dimensions stay separate (never one "active" badge), each with value, time and source;
* absent data is UNKNOWN / NOT_APPLICABLE / NOT_CHECKED with a reason — never guessed;
* local paths are logical names; no credential values exist in any of the source records;
* PERSONAL and OFFICE components never mix: a session sees PLATFORM + its own domain only;
* nodes/edges that are not instrumented are labelled UNKNOWN, never invented.
"""

from __future__ import annotations

import itertools
from collections import deque
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aeos.core.errors import NotFound, ValidationFailed
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.inventory.configlens import PROVIDER_COMPONENTS, config_basis
from aeos.inventory.drift import DRIFT_CLASSES, DRIFT_LABELS
from aeos.inventory.manifest import CATEGORIES, CATEGORY_LABELS, current_manifest
from aeos.inventory.observations import (
    OBSERVED_DIMENSIONS,
    UNREGISTERED_PREFIX,
    history,
    is_stale,
    latest_observations,
)
from aeos.inventory.reconcile import is_probeable, latest_run, run_view
from aeos.models.inventory import CheckResult, ComponentObservation, ComponentReference, ComponentRelation
from aeos.models.inventory_ext import DriftFinding

LENSES = ("DESIGN", "CONFIG", "OBSERVED")
LENS_LABELS = {
    "DESIGN": "Rancangan disetujui",
    "CONFIG": "Konfigurasi berlaku",
    "OBSERVED": "Observasi terakhir",
}
LENS_SOURCES = {
    "DESIGN": "InstallManifest + ADR/master (rancangan; belum membuktikan instalasi)",
    "CONFIG": "Enablement dari keputusan + ModelRoute/ProjectIntegration/AgentDefinition + setelan non-secret",
    "OBSERVED": "Observasi probe/doctor/broker/runner yang bertanggal; dapat STALE/UNKNOWN",
}
DIMENSIONS = ("admission", "installation", "configuration", "connectivity", "functional", "enablement", "usage",
              "lifecycle_final")
DIMENSION_LABELS = {
    "admission": "Admission/keputusan",
    "installation": "Instalasi",
    "configuration": "Konfigurasi",
    "connectivity": "Konektivitas",
    "functional": "Verifikasi fungsi",
    "enablement": "Enablement",
    "usage": "Penggunaan",
    "lifecycle_final": "Lifecycle akhir",
}
IMPACT_RELATIONS = frozenset({"depends_on", "calls", "reads", "writes", "publishes_to", "stores_in", "runs_with",
                              "implements", "verifies", "governed_by"})
OBSERVED_EDGE_TTL = timedelta(hours=1)


# ---------------------------------------------------------------- domain scope


def component_domains(comp: ComponentReference) -> set[str]:
    return set((comp.scope or {}).get("domains") or ["PLATFORM"])


def visible(comp: ComponentReference, domain: str | None) -> bool:
    """PLATFORM components are shared; PERSONAL-only / OFFICE-only ones only in their own domain."""
    domains = component_domains(comp)
    if "PLATFORM" in domains:
        return True
    return domain is not None and domain in domains


def _visible_components(session: Session, domain: str | None, *, include_tombstoned: bool = False
                        ) -> dict[str, ComponentReference]:
    rows = session.scalars(select(ComponentReference).order_by(ComponentReference.component_id))
    return {c.component_id: c for c in rows
            if visible(c, domain) and (include_tombstoned or c.tombstoned_at is None)}


def _obs_visible(obs: ComponentObservation, domain: str | None) -> bool:
    d = (obs.scope or {}).get("domain")
    return d is None or d == domain


# ---------------------------------------------------------------- dimensions


def _iso(dt: datetime | None) -> str | None:
    dt = ensure_aware(dt)
    return dt.isoformat() if dt else None


def _dim_from_obs(obs: ComponentObservation, now: datetime) -> dict[str, Any]:
    stale = is_stale(obs, now)
    return {
        "value": obs.status,
        "effective": "STALE" if stale else obs.status,
        "at": _iso(obs.observed_at),
        "expires_at": _iso(obs.expires_at),
        "stale": stale,
        "source": f"observasi:{obs.observer}",
        "observation_id": obs.id,
        "version": obs.observed_version,
        "reason": (obs.detail or {}).get("reason"),
    }


def _dim(value: str, reason: str, source: str, at: datetime | None = None) -> dict[str, Any]:
    return {"value": value, "effective": value, "at": _iso(at), "expires_at": None, "stale": False, "source": source,
            "observation_id": None, "version": None, "reason": reason}


def dimension_values(comp: ComponentReference, latest: dict[tuple[str, str], ComponentObservation],
                     now: datetime) -> dict[str, dict[str, Any]]:
    cid = comp.component_id
    probe = comp.probe or {}
    out: dict[str, dict[str, Any]] = {}
    decided = bool(comp.decision_refs)
    out["admission"] = _dim(
        comp.admission_status,
        "terikat keputusan yang tercatat" if decided else "belum ada keputusan admission (katalog desain)",
        "decision registry" if decided else "seed manifest", comp.updated_at,
    )
    inst = latest.get((cid, "installation"))
    if inst is not None:
        out["installation"] = _dim_from_obs(inst, now)
    elif str(probe.get("type")) == "none" and probe.get("local_install") is False:
        out["installation"] = _dim("NOT_APPLICABLE", "layanan eksternal/rujukan; tidak dipasang lokal", "manifest")
    elif not is_probeable(comp):
        out["installation"] = _dim("UNKNOWN", str(probe.get("reason") or "tidak diprobe"), "manifest")
    else:
        out["installation"] = _dim("UNKNOWN", "belum diprobe; jalankan aeos inventory reconcile", "belum ada observasi")
    for dim, missing, reason in (
        ("configuration", "UNKNOWN", "konfigurasi belum diperiksa"),
        ("connectivity", "NOT_CHECKED", "konektivitas belum diperiksa"),
    ):
        o = latest.get((cid, dim))
        out[dim] = _dim_from_obs(o, now) if o is not None else _dim(missing, reason, "belum ada observasi")
    func_obs = latest.get((cid, "functional"))
    if func_obs is not None:
        out["functional"] = _dim_from_obs(func_obs, now)
    elif comp.capability_tests:
        out["functional"] = _dim("NOT_TESTED", "capability test belum dijalankan", "belum ada observasi")
    else:
        out["functional"] = _dim("NOT_APPLICABLE", "tidak ada capability test yang didefinisikan", "manifest")
    out["enablement"] = _dim(
        comp.enablement,
        "di-enable oleh keputusan/konfigurasi yang sah" if comp.enablement == "ENABLED"
        else "belum di-enable dalam konfigurasi yang disetujui",
        "approved config", comp.updated_at,
    )
    usage = latest.get((cid, "usage"))
    if usage is not None:
        d = _dim_from_obs(usage, now)
        if d["stale"] and usage.status == "IN_USE":
            d["effective"] = "STALE"
        out["usage"] = d
    else:
        out["usage"] = _dim("NEVER_OBSERVED", "belum ada run/proses/operasi teramati", "belum ada observasi")
    out["lifecycle_final"] = _dim(
        comp.lifecycle_final,
        "dihapus dari manifest (tombstone); histori dipertahankan" if comp.tombstoned_at else "status lifecycle",
        "decision registry", comp.updated_at,
    )
    return out


def version_status(comp: ComponentReference, latest: dict[tuple[str, str], ComponentObservation]) -> dict[str, Any]:
    from aeos.inventory.drift import versions_match

    inst = latest.get((comp.component_id, "installation"))
    observed = inst.observed_version if inst is not None else None
    if not comp.approved_pin:
        status = "NO_PIN"
    elif not observed:
        status = "UNKNOWN"
    else:
        status = "MATCH" if versions_match(comp.approved_pin, observed) else "MISMATCH"
    return {"approved_pin": comp.approved_pin, "observed_version": observed, "status": status,
            "pin_note": None if comp.approved_pin else "pin belum disetujui (UNKNOWN); 'latest' tidak dipakai"}


def _open_drift(session: Session, component_ids: Iterable[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    q = select(DriftFinding).where(DriftFinding.status == "OPEN")
    if component_ids is not None:
        q = q.where(DriftFinding.component_id.in_(list(component_ids)))
    out: dict[str, list[dict[str, Any]]] = {}
    for f in session.scalars(q.order_by(DriftFinding.first_detected_at)):
        out.setdefault(f.component_id, []).append(drift_view(f))
    return out


def drift_view(f: DriftFinding) -> dict[str, Any]:
    return {
        "id": f.id, "component_id": f.component_id, "drift_class": f.drift_class,
        "label": DRIFT_LABELS.get(f.drift_class, f.drift_class), "severity": f.severity, "summary": f.summary,
        "detail": f.detail, "lens_from": f.lens_from, "lens_to": f.lens_to, "status": f.status,
        "domain_scope": f.domain_scope, "first_detected_at": _iso(f.first_detected_at),
        "last_detected_at": _iso(f.last_detected_at), "resolved_at": _iso(f.resolved_at),
    }


# ---------------------------------------------------------------- list


def component_row(comp: ComponentReference, latest: dict[tuple[str, str], ComponentObservation],
                  drift: dict[str, list[dict[str, Any]]], now: datetime, *, advanced: bool = False,
                  relation_counts: dict[str, int] | None = None, basis: list[str] | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "config_lens": {"present": bool(basis), "basis": list(basis or [])},
        "component_id": comp.component_id, "name": comp.name, "friendly_name": comp.friendly_name,
        "kind": comp.kind, "category": comp.category, "category_label": CATEGORY_LABELS.get(comp.category, comp.category),
        "domains": sorted(component_domains(comp)), "requirement": comp.requirement,
        "design_label": comp.design_label, "identity_verified": comp.identity_verified,
        "source_url": comp.source_url, "source_kind": comp.source_kind, "usage_role": comp.usage_role,
        "bootstrap_order": comp.bootstrap_order, "version": version_status(comp, latest),
        "dimensions": dimension_values(comp, latest, now),
        "drift": [{"drift_class": d["drift_class"], "label": d["label"], "severity": d["severity"]}
                  for d in drift.get(comp.component_id, [])],
        "tombstoned_at": _iso(comp.tombstoned_at), "mode": comp.mode,
    }
    if advanced:
        row.update({
            "source_refs": comp.source_refs, "upstream_identity": comp.upstream_identity,
            "license": comp.license, "manifest_hash": comp.manifest_hash,
            "relations": (relation_counts or {}).get(comp.component_id, 0), "placement": comp.placement,
            "decision_refs": comp.decision_refs,
        })
    return row


def _relation_counts(session: Session, ids: set[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rel in session.scalars(select(ComponentRelation).where(ComponentRelation.tombstoned_at.is_(None))):
        for end in (rel.source_id, rel.target_id):
            if end in ids:
                counts[end] = counts.get(end, 0) + 1
    return counts


def list_components(
    session: Session, *, domain: str | None, kind: str | None = None, requirement: str | None = None,
    category: str | None = None, design_label: str | None = None, q: str | None = None,
    include_tombstoned: bool = False, page: int = 1, page_size: int = 50, view: str = "default",
) -> dict[str, Any]:
    now = utcnow()
    comps = list(_visible_components(session, domain, include_tombstoned=include_tombstoned).values())
    facets = {
        "kinds": sorted({c.kind for c in comps}),
        "requirements": sorted({c.requirement for c in comps}),
        "categories": [c for c in CATEGORIES if any(x.category == c for x in comps)],
        "design_labels": sorted({c.design_label for c in comps}),
    }
    needle = (q or "").strip().lower()

    def match(c: ComponentReference) -> bool:
        if kind and c.kind != kind:
            return False
        if requirement and c.requirement != requirement:
            return False
        if category and c.category != category:
            return False
        if design_label and c.design_label != design_label:
            return False
        if needle:
            hay = " ".join([c.component_id, c.name, c.friendly_name, c.function, c.kind, c.category,
                            c.upstream_identity or ""]).lower()
            return needle in hay
        return True

    filtered = [c for c in comps if match(c)]
    filtered.sort(key=lambda c: (CATEGORIES.index(c.category) if c.category in CATEGORIES else 99,
                                 c.bootstrap_order if c.bootstrap_order is not None else 99, c.component_id))
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    start = (page - 1) * page_size
    window = filtered[start:start + page_size]
    ids = [c.component_id for c in window]
    latest = latest_observations(session, ids) if ids else {}
    drift = _open_drift(session, ids)
    advanced = view == "advanced"
    rel_counts = _relation_counts(session, set(ids)) if advanced else None
    basis = config_basis(session, {c.component_id: c for c in window}, latest, now, domain=domain) if ids else {}
    rec = current_manifest(session)
    last = latest_run(session)
    any_obs = session.scalar(select(func.count()).select_from(ComponentObservation)) or 0
    return {
        "items": [component_row(c, latest, drift, now, advanced=advanced, relation_counts=rel_counts,
                                basis=basis.get(c.component_id)) for c in window],
        "total": len(filtered), "page": page, "page_size": page_size, "view": view, "facets": facets,
        "coverage": coverage(session, domain=domain, compact=True),
        "manifest": {"record_id": rec.id, "content_hash": rec.content_hash, "loaded_at": _iso(rec.created_at),
                     "manifest_version": rec.manifest_version} if rec else None,
        "source": "InstallManifest (DESIGN) + observasi bertanggal",
        "checked_at": _iso(last.finished_at) if last else None,
        "mode": "LIVE" if any_obs else "DESIGN",
        "domain": domain,
    }


# ---------------------------------------------------------------- coverage


def coverage(session: Session, *, domain: str | None, compact: bool = False) -> dict[str, Any]:
    now = utcnow()
    comps = _visible_components(session, domain)
    latest = latest_observations(session, list(comps)) if comps else {}
    observed_ids = {cid for (cid, dim), o in latest.items() if dim in OBSERVED_DIMENSIONS and not is_stale(o, now)}
    stale_ids = {cid for (cid, dim), o in latest.items() if dim in OBSERVED_DIMENSIONS and is_stale(o, now)}
    probeable = [c for c in comps.values() if is_probeable(c)]
    never = [c.component_id for c in probeable if not any((c.component_id, d) in latest for d in OBSERVED_DIMENSIONS)]
    not_probed = [c for c in comps.values() if not is_probeable(c)]
    external = [c for c in not_probed if (c.probe or {}).get("local_install") is False]
    candidates = [c for c in not_probed if c not in external]
    usage_seen = any(dim == "usage" for (_cid, dim) in latest)
    full = latest_run(session, full_only=True)
    last = latest_run(session)
    sources = list(full.sources) if full else []
    gaps: list[dict[str, Any]] = []

    def gap(code: str, severity: str, message: str) -> None:
        gaps.append({"code": code, "severity": severity, "message": message})

    if full is None:
        gap("NO_RECONCILE", "HIGH", "Belum ada rekonsiliasi penuh; status instalasi/konfigurasi belum diketahui. "
            "Jalankan: aeos inventory reconcile (atau tombol Segarkan).")
    by_source = {s.get("source"): s for s in sources}
    if full is not None:
        lock = by_source.get("frontend/package-lock.json")
        if lock is None or lock.get("status") != "READ":
            gap("NPM_LOCK_NOT_READ", "MEDIUM", "Lockfile frontend belum dibaca (frontend/package-lock.json).")
        py_lock = by_source.get("requirements.lock")
        if py_lock is None or py_lock.get("status") != "READ":
            gap("PY_LOCK_MISSING", "MEDIUM", "Lockfile Python (requirements.lock) tidak ada; versi paket Python hanya "
                "dibaca dari lingkungan interpreter yang berjalan, bukan dari lockfile yang dipin.")
        for s in sources:
            if s.get("status") == "ERROR":
                gap("SOURCE_ERROR", "MEDIUM", f"Sumber {s.get('source')} gagal dibaca: {(s.get('detail') or {}).get('error')}")
        transitive = (by_source.get("frontend/package-lock.json") or {}).get("detail", {}).get("transitive_packages")
        if transitive:
            gap("TRANSITIVE_PACKAGES", "INFO", f"{transitive} paket npm transitif tercatat di lockfile; tidak ditampilkan "
                "sebagai komponen tersendiri (buka detail Control Center untuk dependency view).")
    if never:
        gap("NEVER_PROBED", "MEDIUM", f"{len(never)} komponen yang dapat diprobe belum pernah diprobe.")
    if stale_ids - observed_ids:
        gap("STALE_OBSERVATIONS", "MEDIUM", f"{len(stale_ids - observed_ids)} komponen hanya memiliki observasi "
            "kedaluwarsa (STALE); status terakhir tidak dianggap terkini.")
    if candidates:
        gap("CANDIDATES_NOT_PROBED", "INFO", f"{len(candidates)} kandidat/rujukan tidak diprobe sebelum ada keputusan "
            "admission (status UNKNOWN, bukan 'tidak terpasang').")
    if external:
        gap("EXTERNAL_NOT_PROBED", "INFO", f"{len(external)} layanan eksternal (GitHub/Google/Atlassian/provider model) "
            "tidak diprobe oleh rekonsiliasi; status koneksi berasal dari Readiness Doctor.")
    if not usage_seen:
        gap("NO_USAGE_INSTRUMENTATION", "INFO", "Belum ada observasi penggunaan (run/broker/proses); kolom Penggunaan "
            "menampilkan NEVER_OBSERVED, bukan IN_USE.")
    gap("SCOPE_LIMIT", "INFO", "Hanya managed scope (repo platform, data dir, binary yang dinamai manifest) yang "
        "diperiksa; mesin dan akun lain tidak dipindai.")
    gap("DOMAIN_FILTER", "INFO", "Komponen khusus domain lain tidak ditampilkan pada sesi ini.")
    result: dict[str, Any] = {
        "totals": {
            "components": len(comps), "probeable": len(probeable), "observed_fresh": len(observed_ids & set(comps)),
            "stale_only": len((stale_ids - observed_ids) & set(comps)), "never_probed": len(never),
            "not_probed_candidates": len(candidates), "external_services": len(external),
        },
        "gaps": gaps,
        "last_reconciled_at": _iso(last.finished_at) if last else None,
        "last_full_reconciled_at": _iso(full.finished_at) if full else None,
    }
    if not compact:
        result["sources"] = sources
        result["last_run"] = run_view(last)
        result["source"] = "ReconciliationRun + ComponentObservation"
        result["checked_at"] = result["last_reconciled_at"]
        result["mode"] = "LIVE" if full else "DESIGN"
    return result


# ---------------------------------------------------------------- relations


def _relation_view(rel: ComponentRelation, comps: dict[str, ComponentReference], now: datetime,
                   *, status: str | None = None) -> dict[str, Any]:
    last = ensure_aware(rel.last_observed_at)
    stale = last is not None and now - last > OBSERVED_EDGE_TTL
    src, tgt = comps.get(rel.source_id), comps.get(rel.target_id)
    return {
        "id": rel.id, "source": rel.source_id, "target": rel.target_id, "relation": rel.relation, "lens": rel.lens,
        "source_name": src.friendly_name if src else rel.source_id,
        "target_name": tgt.friendly_name if tgt else rel.target_id,
        "status": status or rel.lens, "provenance": rel.provenance, "data_description": rel.data_description,
        "operation": rel.operation or "UNKNOWN", "boundary": rel.boundary, "identity_ref": rel.identity_ref,
        "direction": f"{rel.source_id} -> {rel.target_id}",
        "last_observed_at": _iso(last), "freshness": {"at": _iso(last), "stale": stale},
        "tombstoned_at": _iso(rel.tombstoned_at),
        "evidence": "relasi rancangan dari manifest (bukan traffic teramati)" if rel.lens == "DESIGN"
        else ("relasi teramati" if rel.lens == "OBSERVED" else "relasi konfigurasi"),
    }


def component_relations(session: Session, component_id: str, *, domain: str | None) -> dict[str, Any]:
    comps = _visible_components(session, domain, include_tombstoned=True)
    if component_id not in comps:
        raise NotFound("component not found")
    now = utcnow()
    outgoing, incoming = [], []
    q = select(ComponentRelation).where(
        (ComponentRelation.source_id == component_id) | (ComponentRelation.target_id == component_id)
    )
    for rel in session.scalars(q.order_by(ComponentRelation.lens, ComponentRelation.relation)):
        other = rel.target_id if rel.source_id == component_id else rel.source_id
        if other not in comps:
            continue  # other end belongs to the other domain: never leaked
        view = _relation_view(rel, comps, now)
        (outgoing if rel.source_id == component_id else incoming).append(view)
    return {"component_id": component_id, "outgoing": outgoing, "incoming": incoming,
            "items": outgoing + incoming, "source": "ComponentRelation (DESIGN/CONFIG/OBSERVED)",
            "checked_at": _iso(now), "mode": "DESIGN"}


# ---------------------------------------------------------------- detail card


def _claims(comp: ComponentReference, dims: dict[str, dict[str, Any]], checks: list[dict[str, Any]]
            ) -> list[dict[str, Any]]:
    claims = [{"claim": "Tercatat dalam rancangan (InstallManifest)", "level": "E1", "status": comp.design_label,
               "source": "install-manifest", "at": None}]
    inst = dims["installation"]
    if inst["observation_id"]:
        claims.append({"claim": f"Instalasi: {inst['value']}", "level": "E3" if not inst["stale"] else "E3 (STALE)",
                       "status": inst["effective"], "source": inst["source"], "at": inst["at"]})
    else:
        claims.append({"claim": "Instalasi", "level": None, "status": inst["value"], "source": inst["source"],
                       "at": None, "note": inst["reason"]})
    conn = dims["connectivity"]
    claims.append({"claim": "Konektivitas (bukan bukti izin/keamanan)", "level": "E3" if conn["observation_id"] else None,
                   "status": conn["effective"], "source": conn["source"], "at": conn["at"]})
    fn = dims["functional"]
    claims.append({"claim": "Fungsi terverifikasi untuk skenario tertentu", "level": "E2" if fn["observation_id"] else None,
                   "status": fn["effective"], "source": fn["source"], "at": fn["at"]})
    for c in checks:
        claims.append({"claim": f"Doctor {c['check_id']}", "level": "E3", "status": c["status"],
                       "source": "readiness doctor", "at": c["last_checked_at"]})
    return claims


def _cost_actual(session: Session, comp: ComponentReference) -> dict[str, Any]:
    from aeos.models.cost import UsageLedgerEntry

    providers = [p for p, cid in PROVIDER_COMPONENTS.items() if cid == comp.component_id]
    if not providers:
        return {"status": "UNKNOWN", "reason": "usage ledger belum menautkan komponen ini", "total_usd": None}
    rows = list(session.scalars(select(UsageLedgerEntry).where(UsageLedgerEntry.provider.in_(providers),
                                                               UsageLedgerEntry.mode == "LIVE")))
    if not rows:
        return {"status": "NO_USAGE", "reason": "belum ada entri usage ledger LIVE", "total_usd": None}
    known = [float(r.cost_usd) for r in rows if r.cost_usd is not None]
    unknown = sum(1 for r in rows if r.cost_usd is None)
    return {"status": "PARTIAL" if unknown else "KNOWN", "total_usd": round(sum(known), 6) if known else None,
            "entries": len(rows), "unknown_cost_entries": unknown,
            "estimates": sum(1 for r in rows if r.is_estimate), "source": "usage ledger"}


def _changes(session: Session, comp: ComponentReference) -> dict[str, Any]:
    from aeos.models.research import ChangeProposal, ResearchEntry

    cid = comp.component_id
    proposals = []
    for p in session.scalars(select(ChangeProposal).order_by(ChangeProposal.created_at.desc()).limit(500)):
        if cid in (p.existing_component_ids or []) or cid in (p.candidate_component_ids or []):
            proposals.append({"id": p.id, "kind": p.kind, "title": p.title, "status": p.status,
                              "created_at": _iso(p.created_at)})
    research = [{"id": r.id, "name": r.name, "lifecycle": r.lifecycle, "checked_at": _iso(r.checked_at)}
                for r in session.scalars(select(ResearchEntry).where(ResearchEntry.component_id == cid))]
    rec = current_manifest(session)
    return {
        "decision_refs": comp.decision_refs, "proposals": proposals, "research_entries": research,
        "manifest_hash": comp.manifest_hash,
        "manifest_record": {"id": rec.id, "loaded_at": _iso(rec.created_at)} if rec else None,
        "previous_accepted_configuration": {"value": None, "status": "UNKNOWN",
                                            "reason": "belum ada promotion/keputusan konfigurasi yang tercatat"},
        "propose_url": f"/proposals?component={cid}",
    }


def latest_checks_for(session: Session, component_id: str, domain: str | None) -> list[dict[str, Any]]:
    sub = (select(CheckResult.check_id, func.max(CheckResult.last_checked_at).label("m"))
           .where(CheckResult.component_id == component_id).group_by(CheckResult.check_id).subquery())
    q = select(CheckResult).join(sub, (CheckResult.check_id == sub.c.check_id)
                                 & (CheckResult.last_checked_at == sub.c.m))
    out = []
    for r in session.scalars(q):
        d = (r.scope or {}).get("domain")
        if d not in (None, domain):
            continue
        out.append({"check_id": r.check_id, "title": r.title, "group": r.group, "status": r.status,
                    "severity": r.severity, "summary": r.summary, "last_checked_at": _iso(r.last_checked_at),
                    "expires_at": _iso(r.expires_at)})
    return sorted(out, key=lambda x: x["check_id"])


def component_detail(session: Session, component_id: str, *, domain: str | None) -> dict[str, Any]:
    comps = _visible_components(session, domain, include_tombstoned=True)
    comp = comps.get(component_id)
    if comp is None:
        raise NotFound("component not found")
    now = utcnow()
    latest = latest_observations(session, [component_id])
    dims = dimension_values(comp, latest, now)
    rels = component_relations(session, component_id, domain=domain)
    checks = latest_checks_for(session, component_id, domain)
    obs_hist = [o for o in history(session, component_id, limit=40) if _obs_visible(o, domain)]
    instances = sorted({o.observed_location for o in obs_hist if o.dimension == "installation" and o.observed_location})
    declarations = [
        {"source": (o.detail or {}).get("source"), "observed_at": _iso(o.observed_at), "stale": is_stale(o, now),
         "packages": (o.detail or {}).get("packages"), "actions": (o.detail or {}).get("actions"),
         "recommendations": (o.detail or {}).get("recommendations"),
         "transitive_packages": (o.detail or {}).get("transitive_packages"),
         "package": (o.detail or {}).get("package"), "spec": (o.detail or {}).get("spec"),
         "version": o.observed_version}
        for (cid, dim), o in latest.items() if dim == "declaration" and cid == component_id
    ]
    used_by = [r for r in rels["incoming"] if r["relation"] in IMPACT_RELATIONS - {"governed_by"}]
    drift = _open_drift(session, [component_id]).get(component_id, [])
    domains = sorted(component_domains(comp))
    basis = config_basis(session, {component_id: comp}, latest, now, domain=domain).get(component_id, [])
    unknown = {"value": None, "status": "UNKNOWN"}
    probe = comp.probe or {}
    return {
        "component_id": component_id, "mode": comp.mode, "tombstoned_at": _iso(comp.tombstoned_at),
        "identity": {
            "component_id": component_id, "kind": comp.kind, "name": comp.name, "friendly_name": comp.friendly_name,
            "function": comp.function, "category": comp.category,
            "category_label": CATEGORY_LABELS.get(comp.category, comp.category),
            "source_kind": comp.source_kind, "upstream_identity": comp.upstream_identity or None,
            "upstream_identity_status": "UNVERIFIED" if not comp.upstream_identity else comp.identity_verified,
            "instances": instances or None,
            "instances_note": None if instances else "belum ada instance teramati",
        },
        "scope": {
            "level": (comp.scope or {}).get("level", "platform"), "domains": domains,
            "environment": (comp.scope or {}).get("environment", "local"), "owner": comp.owner,
            "maintainer": {**unknown, "reason": "maintainer operasional belum ditetapkan; tidak ditebak"},
            "authorization_scope": {"value": comp.access.get("tenant_target") if comp.access else None,
                                    "status": "DESIGN"},
        },
        "source": {
            "source_url": comp.source_url,
            "source_url_note": None if comp.source_url else "URL kanonik tidak tercantum di master; tidak ditebak",
            "source_refs": comp.source_refs, "identity_verified": comp.identity_verified,
            "publisher": {**unknown, "reason": "publisher/maintainer upstream belum diverifikasi"},
            "license": {"value": comp.license, "status": "UNKNOWN" if not comp.license else "STATED",
                        "reason": None if comp.license else "master tidak mencantumkan lisensi; tidak ditebak"},
            "retrieval_time": (comp.details or {}).get("source_checked_on"),
            "unresolved_identity": comp.identity_verified != "VERIFIED",
        },
        "versions": {
            **version_status(comp, latest),
            "config_revision": {**unknown, "reason": "revisi konfigurasi belum tercatat"},
            "lockfile_refs": [d["source"] for d in declarations if d.get("source")],
            "provenance_note": "Pin source commit tidak membuktikan binary di PATH dibangun dari commit itu (§37).",
        },
        "usage_role": {
            "usage_role": comp.usage_role, "requirement": comp.requirement, "design_label": comp.design_label,
            "why_selected": comp.why_selected, "placement": comp.placement,
            "used_by": [{"component_id": r["source"], "name": r["source_name"], "relation": r["relation"],
                         "lens": r["lens"]} for r in used_by],
            "consuming_tasks": {"items": [], "status": "UNKNOWN",
                                "reason": "belum ada run/task yang menautkan komponen ini"},
            "bootstrap_order": comp.bootstrap_order,
        },
        "lifecycle": dims,
        "config_lens": {"present": bool(basis), "basis": basis,
                        "note": "Konfigurasi berlaku hanya dengan dasar eksplisit; nilai default bukan enablement."},
        "access": {**(comp.access or {}), "expiry": {**unknown, "reason": "expiry akses/credential belum tercatat"},
                   "note": "Nilai credential tidak pernah ditampilkan; hanya referensi logis."},
        "relations": rels,
        "evidence": {
            "checks": checks,
            "observations": [{"id": o.id, "dimension": o.dimension, "status": o.status, "observer": o.observer,
                              "observed_version": o.observed_version, "observed_identity": o.observed_identity,
                              "location": o.observed_location, "operation_ref": o.operation_ref,
                              "observed_at": _iso(o.observed_at), "expires_at": _iso(o.expires_at),
                              "stale": is_stale(o, now), "detail": o.detail} for o in obs_hist],
            "claims": _claims(comp, dims, checks),
            "last_checked_at": max((c["last_checked_at"] for c in checks if c["last_checked_at"]), default=None),
            "last_observed_at": _iso(obs_hist[0].observed_at) if obs_hist else None,
            "last_used_at": dims["usage"]["at"] if dims["usage"]["value"] in ("IN_USE", "LAST_USED") else None,
            "drift": drift,
        },
        "dependencies": declarations,
        "cost": {"design": comp.cost, "actual": _cost_actual(session, comp)},
        "operations": {**(comp.operations or {}), "probe": probe, "capability_tests": comp.capability_tests},
        "changes": _changes(session, comp),
        "source_label": "InstallManifest + observasi + keputusan",
        "checked_at": _iso(now),
    }


# ---------------------------------------------------------------- system map


def _observed_status(cid: str, latest: dict[tuple[str, str], ComponentObservation], now: datetime
                     ) -> tuple[bool, str, str | None, bool]:
    """(present, status, at, stale) for the OBSERVED lens."""
    best: ComponentObservation | None = None
    for dim in ("usage", "connectivity", "installation", "declaration", "discovery"):
        o = latest.get((cid, dim))
        if o is not None and (best is None or o.observed_at > best.observed_at):
            best = o
    if best is None:
        return False, "UNKNOWN", None, False
    stale = is_stale(best, now)
    positive = best.status in ("INSTALLED", "CONNECTED", "IN_USE", "LAST_USED", "IDLE", "DECLARED", "UNREGISTERED")
    status = "STALE" if stale else best.status
    return positive and not stale, status, _iso(best.observed_at), stale


def system_map(session: Session, *, lens: str, domain: str | None, category: str | None = None) -> dict[str, Any]:
    lens = lens.upper()
    if lens not in LENSES:
        raise ValidationFailed(f"lens must be one of {', '.join(LENSES)}", code="INVALID_LENS")
    now = utcnow()
    comps = _visible_components(session, domain)
    if category:
        comps = {k: v for k, v in comps.items() if v.category == category}
    latest = latest_observations(session)
    drift = _open_drift(session)
    basis = config_basis(session, comps, latest, now, domain=domain) if lens == "CONFIG" else {}
    nodes: list[dict[str, Any]] = []
    present: dict[str, bool] = {}
    for cid, c in comps.items():
        node: dict[str, Any] = {
            "id": cid, "label": c.friendly_name or c.name, "name": c.name, "kind": c.kind, "category": c.category,
            "requirement": c.requirement, "design_label": c.design_label, "domains": sorted(component_domains(c)),
            "identity_verified": c.identity_verified,
            "drift": [{"drift_class": d["drift_class"], "label": d["label"], "severity": d["severity"]}
                      for d in drift.get(cid, [])],
            "function": c.function,
        }
        if lens == "DESIGN":
            node.update(present=True, status=c.design_label, at=None, stale=False, basis=["manifest"])
        elif lens == "CONFIG":
            b = basis.get(cid, [])
            st = "ENABLED" if c.enablement == "ENABLED" else ("CONFIGURED" if b else "DISABLED")
            node.update(present=bool(b), status=st, at=_iso(c.updated_at), stale=False, basis=b)
        else:
            ok, st, at, stale = _observed_status(cid, latest, now)
            node.update(present=ok, status=st, at=at, stale=stale,
                        basis=["observasi"] if at else [], unknown=at is None)
        present[cid] = bool(node["present"])
        nodes.append(node)
    if lens == "OBSERVED":
        for (cid, dim), o in latest.items():
            if not cid.startswith(UNREGISTERED_PREFIX) or dim != "discovery" or not _obs_visible(o, domain):
                continue
            if category and category != "tools/context":
                continue
            stale = is_stale(o, now)
            d = o.detail or {}
            nodes.append({"id": cid, "label": f"{d.get('name', cid)} (tidak terdaftar)", "name": d.get("name", cid),
                          "kind": "unregistered", "category": "tools/context", "requirement": "UNREGISTERED",
                          "design_label": "UNREGISTERED", "domains": [d.get("domain") or "PLATFORM"],
                          "identity_verified": "UNVERIFIED", "present": not stale,
                          "status": "STALE" if stale else "UNREGISTERED", "at": _iso(o.observed_at), "stale": stale,
                          "basis": [f"observasi {d.get('source')}"], "unknown": False,
                          "drift": [{"drift_class": "observed-but-unregistered",
                                     "label": DRIFT_LABELS["observed-but-unregistered"], "severity": "LOW"}],
                          "function": "Ditemukan di managed scope tanpa registrasi; perlu review."})
            present[cid] = not stale
    node_ids = {n["id"] for n in nodes}
    edges: list[dict[str, Any]] = []
    rel_rows = list(session.scalars(select(ComponentRelation).where(ComponentRelation.tombstoned_at.is_(None))))
    for rel in rel_rows:
        if rel.source_id not in node_ids or rel.target_id not in node_ids:
            continue
        if lens == "DESIGN" and rel.lens == "DESIGN":
            edges.append(_relation_view(rel, comps, now, status="DESIGN"))
        elif lens == "CONFIG":
            if rel.lens == "CONFIG":
                edges.append(_relation_view(rel, comps, now, status="CONFIGURED"))
            elif rel.lens == "DESIGN" and present.get(rel.source_id) and present.get(rel.target_id):
                v = _relation_view(rel, comps, now, status="CONFIGURED")
                v["evidence"] = "relasi rancangan; kedua ujung terkonfigurasi (traffic belum teramati)"
                edges.append(v)
        elif lens == "OBSERVED":
            if rel.lens == "OBSERVED":
                v = _relation_view(rel, comps, now, status="OBSERVED")
                if v["freshness"]["stale"]:
                    v["status"] = "STALE"
                edges.append(v)
            elif rel.lens == "DESIGN" and present.get(rel.source_id) and present.get(rel.target_id):
                v = _relation_view(rel, comps, now, status="UNKNOWN")
                v["evidence"] = "UNKNOWN: relasi tidak diinstrumentasi (hanya ada di rancangan)"
                edges.append(v)
    groups = []
    for cat in CATEGORIES:
        ids = [n["id"] for n in nodes if n["category"] == cat]
        if not ids:
            continue
        groups.append({
            "id": cat, "label": CATEGORY_LABELS[cat], "node_ids": ids,
            "counts": {"total": len(ids), "present": sum(1 for i in ids if present.get(i)),
                       "drift": sum(1 for n in nodes if n["category"] == cat and n["drift"])},
        })
    drift_items = [d for cid in node_ids for d in drift.get(cid, [])]
    last = latest_run(session)
    return {
        "lens": lens, "lens_label": LENS_LABELS[lens], "lens_source": LENS_SOURCES[lens],
        "nodes": nodes, "edges": edges, "groups": groups, "drift": drift_items,
        "drift_summary": {c: sum(1 for d in drift_items if d["drift_class"] == c) for c in DRIFT_CLASSES},
        "legend": _legend(lens), "coverage": coverage(session, domain=domain, compact=True),
        "source": LENS_SOURCES[lens], "checked_at": _iso(last.finished_at) if last else None,
        "mode": "DESIGN" if lens == "DESIGN" else "LIVE", "domain": domain,
        "note": "Peta logis ini adalah projection; koneksi ke node menunjukkan kemungkinan hubungan sesuai scope, "
                "bukan izin aktif (§36).",
    }


def _legend(lens: str) -> list[dict[str, str]]:
    base = [
        {"key": "DESIGN", "label": "Garis putus: relasi rancangan (manifest), belum membuktikan traffic"},
        {"key": "CONFIGURED", "label": "Garis tegas: kedua ujung terkonfigurasi/di-enable"},
        {"key": "OBSERVED", "label": "Garis beranimasi: relasi teramati dengan waktu"},
        {"key": "UNKNOWN", "label": "Abu-abu: tidak diinstrumentasi (UNKNOWN)"},
        {"key": "STALE", "label": "Kuning: observasi kedaluwarsa"},
        {"key": "DRIFT", "label": "Penanda drift: rancangan, konfigurasi dan observasi tidak sama"},
    ]
    return [*base, {"key": "LENS", "label": f"Lensa aktif: {LENS_LABELS[lens]}"}]


def _graph(session: Session, comps: dict[str, ComponentReference], *, lens: str = "DESIGN",
           relations: frozenset[str] = IMPACT_RELATIONS) -> list[ComponentRelation]:
    rows = session.scalars(select(ComponentRelation).where(ComponentRelation.tombstoned_at.is_(None),
                                                           ComponentRelation.lens == lens))
    return [r for r in rows if r.relation in relations and r.source_id in comps and r.target_id in comps]


def blast_radius(session: Session, component_id: str, *, domain: str | None, direction: str = "dependents",
                 max_depth: int = 6, lens: str = "DESIGN") -> dict[str, Any]:
    """Who is affected if *component_id* changes/fails (dependents), or what it relies on (dependencies)."""
    comps = _visible_components(session, domain)
    if component_id not in comps:
        raise NotFound("component not found")
    if direction not in ("dependents", "dependencies"):
        raise ValidationFailed("direction must be 'dependents' or 'dependencies'")
    lens = lens.upper()
    edges = _graph(session, comps, lens=lens if lens in LENSES else "DESIGN")
    adj: dict[str, list[ComponentRelation]] = {}
    for e in edges:
        # A depends on B (A -> B): a change in B affects A (dependents walk edges backwards).
        key = e.target_id if direction == "dependents" else e.source_id
        adj.setdefault(key, []).append(e)
    seen = {component_id: (0, [component_id], [])}
    queue: deque[str] = deque([component_id])
    while queue:
        cur = queue.popleft()
        depth, path, via = seen[cur]
        if depth >= max_depth:
            continue
        for e in adj.get(cur, []):
            nxt = e.source_id if direction == "dependents" else e.target_id
            if nxt in seen:
                continue
            seen[nxt] = (depth + 1, [*path, nxt], [*via, e.relation])
            queue.append(nxt)
    items = []
    for cid, (depth, path, via) in seen.items():
        if cid == component_id:
            continue
        c = comps[cid]
        items.append({"component_id": cid, "name": c.friendly_name or c.name, "category": c.category,
                      "requirement": c.requirement, "depth": depth, "path": path, "via": via})
    items.sort(key=lambda x: (x["depth"], x["component_id"]))
    edge_ids = {(e.source_id, e.target_id) for e in edges}
    used_edges = []
    for it in items:
        p = it["path"]
        for a, b in itertools.pairwise(p):
            pair = (b, a) if direction == "dependents" else (a, b)
            if pair in edge_ids:
                used_edges.append({"source": pair[0], "target": pair[1]})
    return {"root": component_id, "direction": direction, "lens": lens, "items": items, "edges": used_edges,
            "summary": {"affected": len(items),
                        "by_category": {cat: sum(1 for i in items if i["category"] == cat) for cat in CATEGORIES
                                        if any(i["category"] == cat for i in items)}},
            "note": "Dependensi transitif bukan koneksi jaringan langsung; dihitung dari relasi bertipe (§36).",
            "source": f"ComponentRelation lens {lens}", "checked_at": _iso(utcnow()), "mode": "DESIGN"}


def find_path(session: Session, source: str, target: str, *, domain: str | None, lens: str = "DESIGN"
              ) -> dict[str, Any]:
    comps = _visible_components(session, domain)
    for cid in (source, target):
        if cid not in comps:
            raise NotFound(f"component {cid} not found")
    rels = frozenset(IMPACT_RELATIONS | {"alternative_to", "replaces"})
    edges = _graph(session, comps, lens=lens.upper() if lens.upper() in LENSES else "DESIGN", relations=rels)

    def bfs(directed: bool) -> list[tuple[str, str, str]] | None:
        adj: dict[str, list[tuple[str, str]]] = {}
        for e in edges:
            adj.setdefault(e.source_id, []).append((e.target_id, e.relation))
            if not directed:
                adj.setdefault(e.target_id, []).append((e.source_id, e.relation))
        prev: dict[str, tuple[str, str] | None] = {source: None}
        q: deque[str] = deque([source])
        while q:
            cur = q.popleft()
            if cur == target:
                break
            for nxt, rel in adj.get(cur, []):
                if nxt not in prev:
                    prev[nxt] = (cur, rel)
                    q.append(nxt)
        if target not in prev:
            return None
        steps: list[tuple[str, str, str]] = []
        node = target
        while prev[node] is not None:
            p, rel = prev[node]  # type: ignore[misc]
            steps.append((p, node, rel))
            node = p
        return list(reversed(steps))

    steps = bfs(True)
    directed = True
    if steps is None:
        steps = bfs(False)
        directed = False
    if steps is None:
        return {"found": False, "source": source, "target": target, "path": [], "edges": [], "directed": None,
                "note": "Tidak ada jalur melalui relasi bertipe pada lensa ini."}
    path = [source] + [s[1] for s in steps]
    return {"found": True, "source": source, "target": target, "path": path, "directed": directed,
            "edges": [{"source": a, "target": b, "relation": r} for a, b, r in steps],
            "note": None if directed else "Jalur hanya ada bila arah relasi diabaikan."}


def drift_list(session: Session, *, domain: str | None, include_resolved: bool = False) -> dict[str, Any]:
    comps = _visible_components(session, domain, include_tombstoned=True)
    q = select(DriftFinding)
    if not include_resolved:
        q = q.where(DriftFinding.status == "OPEN")
    items = []
    for f in session.scalars(q.order_by(DriftFinding.last_detected_at.desc()).limit(1000)):
        if f.domain_scope not in ("PLATFORM", domain):
            continue
        if not f.component_id.startswith(UNREGISTERED_PREFIX) and f.component_id not in comps:
            continue
        v = drift_view(f)
        c = comps.get(f.component_id)
        v["component_name"] = c.friendly_name if c else f.component_id
        items.append(v)
    last = latest_run(session)
    return {"items": items, "by_class": {c: sum(1 for i in items if i["drift_class"] == c and i["status"] == "OPEN")
                                         for c in DRIFT_CLASSES},
            "labels": DRIFT_LABELS, "last_reconciled_at": _iso(last.finished_at) if last else None,
            "source": "DriftFinding (projection rekonsiliasi)", "checked_at": _iso(last.finished_at) if last else None,
            "mode": "LIVE" if last else "DESIGN",
            "note": "Drift bukan otomatis insiden keamanan; severity mengikuti policy dan bukti (§36)."}
