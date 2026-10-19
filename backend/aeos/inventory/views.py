"""TechnologyInventoryView and SystemMapView projections (§13, §35-§38).

Pure read models over InstallManifest/ComponentReference (DESIGN), approved configuration records
(CONFIG) and dated observations (OBSERVED). They own no scheduler, credential or task state.

Rules kept here:
* the §38 dimensions stay separate (never one "active" badge), each with value, time and source;
* admission/lifecycle come from the Decision registry, enablement from approved configuration, usage
  from the run/lease/ledger/outbox records (``aeos.inventory.decisions`` / ``runtime``) — never seeds;
* absent data is UNKNOWN / NOT_APPLICABLE / NOT_CHECKED with a reason — never guessed;
* local paths are logical names; no credential values exist in any of the source records;
* PERSONAL and OFFICE components never mix: a session sees PLATFORM + its own domain only;
* nodes/edges that are not instrumented are labelled UNKNOWN, never invented.
"""

from __future__ import annotations

import itertools
import re
from collections import deque
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aeos.core.errors import NotFound, ValidationFailed
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.inventory import records as rec_views
from aeos.inventory import runtime as rt
from aeos.inventory.configlens import HARNESS_COMPONENTS, PROVIDER_COMPONENTS, config_basis
from aeos.inventory.decisions import DecisionState, decision_states
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

SECRET_REF_RE = re.compile(r"^(personal|workers|platform|office|projects)/[A-Za-z0-9._/-]{1,200}$")
MAINTAINER_PREFIX = "inventory.maintainer."
PROBE_LEVELS = {"binary": "E3", "python_runtime": "E3", "python_module": "E3", "python_package": "E3",
                "platform_module": "E3", "sqlite": "E3", "keyring_backend": "E3", "git_repo": "E1", "repo_file": "E1",
                "npm_package": "E1", "data_dir": "E1"}

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


WRITE_PROVIDER_COMPONENTS = {"google-drive": "google-drive", "github-issues": "github-issues",
                             "github-repo": "github", "confluence": "confluence", "jira": "jira",
                             "bitbucket": "bitbucket"}


def write_enabled_components(session: Session, domain: str | None) -> set[str]:
    """Components whose provider write enablement is effective in *domain* (flag + ACTIVE human decision)."""
    if domain is None:
        return set()
    try:
        from aeos.policy import enablement

        rows = enablement.status(session, domain)
    except Exception:  # policy module unavailable: nothing is enabled (fail closed)
        return set()
    return {WRITE_PROVIDER_COMPONENTS[r["provider"]] for r in rows
            if r.get("effective") and r.get("provider") in WRITE_PROVIDER_COMPONENTS}


def enablement_dim(comp: ComponentReference, basis: list[str] | None, *, write_enabled: bool = False
                   ) -> dict[str, Any]:
    """§38 Enablement = ENABLED/DISABLED in approved configuration (never proof of use).

    ENABLED needs a human-decided basis: an explicit component decision, an APPROVED/TRIAL route (route
    decision), an enabled agent definition, a project integration whose provider write enablement is effective
    (flag + ACTIVE WRITE_ENABLEMENT decision), or being part of the running released controller. An integration
    record alone is configuration (CONFIG lens: CONFIGURED), not enablement; a default setting is neither."""
    reasons = list(basis or [])
    decided = [r for r in reasons if r.startswith(("enablement", "model route", "agent definition"))]
    integrations = [r for r in reasons if r.startswith("integrasi")]
    released = [r for r in reasons if r.startswith("bagian dari released controller")]
    if comp.enablement == "ENABLED":
        d = _dim("ENABLED", "di-enable oleh keputusan yang tercatat", "decision registry", comp.updated_at)
    elif decided:
        d = _dim("ENABLED", "dasar konfigurasi yang diputuskan: " + "; ".join(decided[:3]), "approved config", None)
    elif integrations and write_enabled:
        d = _dim("ENABLED", "integrasi proyek + write enablement efektif (keputusan WRITE_ENABLEMENT)",
                 "approved config", None)
    elif released:
        d = _dim("ENABLED", "bagian dari released controller yang berjalan (terpasang, teramati)", "released controller",
                 None)
    elif integrations:
        d = _dim("DISABLED", "integrasi terkonfigurasi (lensa Konfigurasi), tetapi write enablement provider belum "
                 "efektif (butuh keputusan WRITE_ENABLEMENT)", "approved config", comp.updated_at)
    else:
        d = _dim("DISABLED", "belum di-enable dalam konfigurasi yang disetujui", "approved config", comp.updated_at)
    d["basis"] = reasons
    d["note"] = "Enablement bukan bukti dipakai (lihat Penggunaan)."
    return d


def _usage_dim(obs: ComponentObservation | None, fact: rt.UsageFact | None, now: datetime) -> dict[str, Any]:
    d_obs = _dim_from_obs(obs, now) if obs is not None else None
    if d_obs is not None and d_obs["stale"] and obs is not None and obs.status == "IN_USE":
        d_obs["effective"] = "STALE"
    if fact is None:
        return d_obs or _dim("NEVER_OBSERVED", "belum ada run/proses/operasi teramati", "belum ada observasi")
    d_fact = fact.as_dim()
    if d_obs is None or fact.status == "IN_USE":
        return d_fact
    if d_obs.get("effective") == "IN_USE":
        return d_obs
    fact_at, obs_at = ensure_aware(fact.at), ensure_aware(obs.observed_at) if obs is not None else None
    return d_fact if fact_at is not None and (obs_at is None or fact_at >= obs_at) else d_obs


def dimension_values(comp: ComponentReference, latest: dict[tuple[str, str], ComponentObservation],
                     now: datetime, *, state: DecisionState | None = None, basis: list[str] | None = None,
                     usage_fact: rt.UsageFact | None = None, write_enabled: bool = False) -> dict[str, dict[str, Any]]:
    cid = comp.component_id
    probe = comp.probe or {}
    out: dict[str, dict[str, Any]] = {}
    if state is not None:
        out["admission"] = _dim(state.admission, state.admission_reason, state.admission_source,
                                state.admission_at or comp.updated_at)
        out["admission"].update(decision_refs=list(state.decision_refs), scope=dict(state.scope))
    else:
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
    out["enablement"] = enablement_dim(comp, basis, write_enabled=write_enabled)
    out["usage"] = _usage_dim(latest.get((cid, "usage")), usage_fact, now)
    if state is not None:
        out["lifecycle_final"] = _dim(state.lifecycle, state.lifecycle_reason, state.lifecycle_source,
                                      state.lifecycle_at or comp.updated_at)
    else:
        out["lifecycle_final"] = _dim(
            "RETIRED" if comp.tombstoned_at else comp.lifecycle_final,
            "dihapus dari manifest (tombstone); histori dipertahankan" if comp.tombstoned_at else "status lifecycle",
            "decision registry", comp.tombstoned_at or comp.updated_at,
        )
    return out


def next_action(comp: ComponentReference, dims: dict[str, dict[str, Any]], drift: list[dict[str, Any]],
                version: dict[str, Any]) -> dict[str, Any]:
    """The one next step a human can take (§35 "tindakan berikutnya"), derived deterministically from the
    separate dimensions, drift and coverage; it never runs anything itself."""
    classes = {d["drift_class"] for d in drift}
    inst = dims["installation"]
    life = dims["lifecycle_final"]["value"]

    def act(code: str, label: str, command: str | None = None, link: str | None = None) -> dict[str, Any]:
        return {"code": code, "label": label, "command": command, "link": link}

    if comp.tombstoned_at is not None or life == "RETIRED":
        return act("HISTORY_ONLY", "Sudah dipensiunkan; histori saja")
    if life == "QUARANTINED" or dims["admission"]["value"] == "SUSPENDED":
        return act("REVIEW_SUSPENSION", "Ditangguhkan/karantina: tinjau keputusan keamanan", link="/decisions")
    if version.get("status") == "MISMATCH" or "resolved-version-mismatch" in classes:
        return act("RECONCILE_PIN", "Rekonsiliasi pin atau usulkan upgrade", "aeos inventory reconcile",
                   f"/proposals?component={comp.component_id}")
    if "planned-but-missing" in classes:
        return act("INSTALL_MANUALLY", "Pasang sesuai remedy Readiness (manual, tanpa auto-install)", link="/readiness")
    if is_probeable(comp) and inst["observation_id"] is None:
        return act("RUN_RECONCILE", "Jalankan aeos inventory reconcile", "aeos inventory reconcile")
    if inst.get("stale"):
        return act("REFRESH", "Segarkan observasi (kedaluwarsa)", "aeos inventory reconcile")
    if "enabled-but-unverified" in classes:
        return act("RUN_FUNCTIONAL_DOCTOR", "Jalankan aeos doctor --functional", "aeos doctor --functional",
                   "/readiness")
    if comp.requirement == "REFERENCE_ONLY":
        return act("NONE_REFERENCE", "Rujukan saja; bukan dependency aktif")
    if comp.requirement == "CANDIDATE" and dims["admission"]["value"] in ("DISCOVERED", "UNDER_REVIEW"):
        return act("REVIEW_CANDIDATE", "Tinjau lewat Usulkan perubahan (kandidat belum diadmisi)",
                   link=f"/proposals?component={comp.component_id}")
    if inst["value"] == "NOT_APPLICABLE" and dims["connectivity"]["value"] in ("NOT_CHECKED", "UNKNOWN"):
        return act("CHECK_CONNECTIVITY", "Periksa koneksi lewat Readiness Doctor", link="/readiness")
    if "installed-but-disabled" in classes:
        return act("DECIDE_ENABLEMENT", "Terpasang tetapi tidak aktif: putuskan enable atau biarkan",
                   link=f"/proposals?component={comp.component_id}")
    return act("MONITOR", "Tidak ada tindakan; pantau observasi")


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
                  relation_counts: dict[str, int] | None = None, basis: list[str] | None = None,
                  state: DecisionState | None = None, usage_fact: rt.UsageFact | None = None,
                  write_enabled: bool = False) -> dict[str, Any]:
    dims = dimension_values(comp, latest, now, state=state, basis=basis, usage_fact=usage_fact,
                            write_enabled=write_enabled)
    version = version_status(comp, latest)
    comp_drift = drift.get(comp.component_id, [])
    row: dict[str, Any] = {
        "config_lens": {"present": bool(basis), "basis": list(basis or [])},
        "component_id": comp.component_id, "name": comp.name, "friendly_name": comp.friendly_name,
        # §35: friendly name, short function, reason used and next action come before technical IDs
        "function": comp.function, "why_selected": comp.why_selected,
        "kind": comp.kind, "category": comp.category, "category_label": CATEGORY_LABELS.get(comp.category, comp.category),
        "domains": sorted(component_domains(comp)), "requirement": comp.requirement,
        "design_label": comp.design_label, "identity_verified": comp.identity_verified,
        "source_url": comp.source_url, "source_kind": comp.source_kind, "usage_role": comp.usage_role,
        "bootstrap_order": comp.bootstrap_order, "version": version,
        "dimensions": dims,
        "drift": [{"drift_class": d["drift_class"], "label": d["label"], "severity": d["severity"]}
                  for d in comp_drift],
        "next_action": next_action(comp, dims, comp_drift, version),
        "tombstoned_at": _iso(comp.tombstoned_at), "mode": comp.mode,
    }
    if advanced:
        row.update({
            "source_refs": comp.source_refs, "upstream_identity": comp.upstream_identity,
            "license": comp.license, "manifest_hash": comp.manifest_hash,
            "relations": (relation_counts or {}).get(comp.component_id, 0), "placement": comp.placement,
            "decision_refs": state.decision_refs if state is not None else comp.decision_refs,
            "approved_digest": (comp.details or {}).get("approved_digest"),
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
    allowed_project_ids: list[str] | None = None, project_id: str | None = None,
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
    window_comps = {c.component_id: c for c in window}
    basis = config_basis(session, window_comps, latest, now, domain=domain) if ids else {}
    states = decision_states(session, window_comps, domain=domain) if ids else {}
    usage, _edges, _runs = rt.collect(session, domain=domain, now=now)
    writable = write_enabled_components(session, domain)
    rec = current_manifest(session)
    last = latest_run(session)
    cov = coverage(session, domain=domain, compact=True, usage=usage)
    # LIVE only when a component visible in THIS domain has observations (no cross-domain signal).
    any_obs = (cov["totals"]["observed_fresh"] or 0) + (cov["totals"]["stale_only"] or 0)
    return {
        "items": [component_row(c, latest, drift, now, advanced=advanced, relation_counts=rel_counts,
                                basis=basis.get(c.component_id), state=states.get(c.component_id),
                                usage_fact=usage.get(c.component_id), write_enabled=c.component_id in writable)
                  for c in window],
        "total": len(filtered), "page": page, "page_size": page_size, "view": view, "facets": facets,
        "coverage": cov,
        # §37: Tools & Repos merges the other registries' records, each with its own source (never a 2nd authority)
        "project_repositories": rec_views.project_repositories(session, domain=domain,
                                                               allowed_project_ids=allowed_project_ids,
                                                               project_id=project_id),
        "project_services": rec_views.project_services(session, domain=domain, allowed_project_ids=allowed_project_ids,
                                                       project_id=project_id),
        "roles": rec_views.role_nodes(session),
        "routes": rec_views.route_nodes(session, domain=domain),
        "manifest": {"record_id": rec.id, "content_hash": rec.content_hash, "loaded_at": _iso(rec.created_at),
                     "manifest_version": rec.manifest_version} if rec else None,
        "source": "InstallManifest (DESIGN) + observasi bertanggal + Decision registry + run/ledger records",
        "checked_at": _iso(last.finished_at) if last else None,
        "mode": "LIVE" if any_obs else "DESIGN",
        "domain": domain,
    }


def enablement_for(session: Session, component_id: str, *, domain: str | None = None) -> dict[str, Any]:
    comp = session.get(ComponentReference, component_id)
    if comp is None:
        raise NotFound("component not found")
    now = utcnow()
    latest = latest_observations(session, [component_id])
    basis = config_basis(session, {component_id: comp}, latest, now, domain=domain).get(component_id, [])
    return enablement_dim(comp, basis, write_enabled=component_id in write_enabled_components(session, domain))


# ---------------------------------------------------------------- coverage


def coverage(session: Session, *, domain: str | None, compact: bool = False,
             usage: dict[str, rt.UsageFact] | None = None) -> dict[str, Any]:
    now = utcnow()
    comps = _visible_components(session, domain)
    latest = latest_observations(session, list(comps)) if comps else {}
    if usage is None:
        usage = rt.collect(session, domain=domain, now=now)[0]
    observed_ids = {cid for (cid, dim), o in latest.items() if dim in OBSERVED_DIMENSIONS and not is_stale(o, now)}
    stale_ids = {cid for (cid, dim), o in latest.items() if dim in OBSERVED_DIMENSIONS and is_stale(o, now)}
    probeable = [c for c in comps.values() if is_probeable(c)]
    never = [c.component_id for c in probeable if not any((c.component_id, d) in latest for d in OBSERVED_DIMENSIONS)]
    not_probed = [c for c in comps.values() if not is_probeable(c)]
    external = [c for c in not_probed if (c.probe or {}).get("local_install") is False]
    candidates = [c for c in not_probed if c not in external]
    usage_seen = any(dim == "usage" for (_cid, dim) in latest) or any(cid in comps for cid in usage)
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
                "sebagai komponen tersendiri (buka dependency view komponen control-center-frontend).")
        sbom = [s for s in sources if str(s.get("source") or "").startswith("sbom")]
        if not any(s.get("status") == "READ" for s in sbom):
            gap("SBOM_NOT_COMMITTED", "INFO", "Belum ada SBOM CycloneDX di sbom/; SBOM dapat dibuat dari lockfile dengan "
                "aeos inventory sbom (atau unduh dari Tools & Repos).")
        gap("IMAGES_NOT_MANAGED", "INFO", "Image container tidak dipetakan: AEOS tidak mengelola container "
            "runtime/registry (coverage gap, bukan 'tidak ada image').")
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
        gap("NO_USAGE_INSTRUMENTATION", "INFO", "Belum ada observasi penggunaan (run/lease, usage ledger broker/bridge, "
            "outbox publikasi) dalam 30 hari terakhir; kolom Penggunaan menampilkan NEVER_OBSERVED, bukan IN_USE.")
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
                   *, status: str | None = None, checks: dict[str, list[dict[str, Any]]] | None = None
                   ) -> dict[str, Any]:
    last = ensure_aware(rel.last_observed_at)
    stale = last is not None and now - last > OBSERVED_EDGE_TTL
    src, tgt = comps.get(rel.source_id), comps.get(rel.target_id)
    return {
        "id": rel.id, "source": rel.source_id, "target": rel.target_id, "relation": rel.relation, "lens": rel.lens,
        "source_name": src.friendly_name if src else rel.source_id,
        "target_name": tgt.friendly_name if tgt else rel.target_id,
        "status": status or rel.lens, "provenance": rel.provenance, "data_description": rel.data_description,
        "operation": rel.operation or "UNKNOWN", "boundary": rel.boundary, "identity_ref": rel.identity_ref,
        "direction": f"{rel.source_id} -> {rel.target_id}", "scope": rel.scope or {},
        "created_at": _iso(rel.created_at), "updated_at": _iso(rel.updated_at),
        "last_observed_at": _iso(last), "freshness": {"at": _iso(last), "stale": stale},
        "tombstoned_at": _iso(rel.tombstoned_at),
        "evidence": "relasi rancangan dari manifest (bukan traffic teramati)" if rel.lens == "DESIGN"
        else ("relasi teramati" if rel.lens == "OBSERVED" else "relasi konfigurasi"),
        # §36 edge card "hasil pengujian": the latest Doctor results about either end (target first)
        "tests": _edge_tests(rel.source_id, rel.target_id, checks),
    }


def _edge_tests(source: str, target: str, checks: dict[str, list[dict[str, Any]]] | None) -> list[dict[str, Any]]:
    if not checks:
        return []
    out = [{**c, "about": target} for c in checks.get(target, [])]
    out += [{**c, "about": source} for c in checks.get(source, [])]
    return out[:6]


def checks_by_component(session: Session, domain: str | None) -> dict[str, list[dict[str, Any]]]:
    """component_id -> latest Doctor results (platform rows + this domain's rows), for edge cards."""
    try:
        from aeos.doctor.runner import latest_results
    except (ImportError, ModuleNotFoundError):
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for r in latest_results(session).values():
        d = (r.scope or {}).get("domain")
        if not r.component_id or d not in (None, domain) or (r.scope or {}).get("project_id"):
            continue
        out.setdefault(r.component_id, []).append({
            "check_id": r.check_id, "status": r.status, "last_checked_at": _iso(r.last_checked_at),
            "expires_at": _iso(r.expires_at), "summary": (r.summary or "")[:160]})
    return out


def _fact_relation(e: rt.EdgeFact, comps: dict[str, ComponentReference], now: datetime,
                   checks: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """An OBSERVED edge seen in run/ledger/outbox records (traffic/event, §36)."""
    at = ensure_aware(e.at)
    stale = at is None or now - at > OBSERVED_EDGE_TTL
    src, tgt = comps.get(e.source_id), comps.get(e.target_id)
    return {
        "id": f"observed:{e.source_id}:{e.relation}:{e.target_id}", "source": e.source_id, "target": e.target_id,
        "relation": e.relation, "lens": "OBSERVED", "source_name": src.friendly_name if src else e.source_id,
        "target_name": tgt.friendly_name if tgt else e.target_id, "status": "STALE" if stale else "OBSERVED",
        "provenance": f"record:{e.refs.get('record')}", "data_description": e.source, "operation": "CALL",
        "boundary": None, "identity_ref": None, "direction": f"{e.source_id} -> {e.target_id}",
        "scope": {"refs": dict(e.refs)}, "created_at": None, "updated_at": None,
        "last_observed_at": _iso(at), "freshness": {"at": _iso(at), "stale": stale}, "tombstoned_at": None,
        "evidence": f"relasi teramati dari {e.source} (run/operasi bertanggal)",
        "tests": _edge_tests(e.source_id, e.target_id, checks),
    }


def component_relations(session: Session, component_id: str, *, domain: str | None, page: int = 1,
                        page_size: int = 200) -> dict[str, Any]:
    """Relations of one component (§36/§38 API): backend-paginated over the combined list (outgoing first, in a
    stable lens/relation order); ``outgoing``/``incoming`` are the split of the returned page."""
    comps = _visible_components(session, domain, include_tombstoned=True)
    if component_id not in comps:
        raise NotFound("component not found")
    now = utcnow()
    checks = checks_by_component(session, domain)
    outgoing, incoming = [], []
    q = select(ComponentRelation).where(
        (ComponentRelation.source_id == component_id) | (ComponentRelation.target_id == component_id)
    )
    for rel in session.scalars(q.order_by(ComponentRelation.lens, ComponentRelation.relation)):
        other = rel.target_id if rel.source_id == component_id else rel.source_id
        if other not in comps or not _relation_visible(rel, domain):
            continue  # other end (or the observed edge itself) belongs to the other domain: never leaked
        view = _relation_view(rel, comps, now, checks=checks)
        (outgoing if rel.source_id == component_id else incoming).append(view)
    for e in rt.collect(session, domain=domain, now=now)[1]:
        if component_id not in (e.source_id, e.target_id) or e.source_id not in comps or e.target_id not in comps:
            continue
        (outgoing if e.source_id == component_id else incoming).append(_fact_relation(e, comps, now, checks))
    page = max(1, page)
    page_size = max(1, min(page_size, 500))
    combined = [("out", v) for v in outgoing] + [("in", v) for v in incoming]
    window = combined[(page - 1) * page_size:page * page_size]
    out_page = [v for d, v in window if d == "out"]
    in_page = [v for d, v in window if d == "in"]
    return {"component_id": component_id, "outgoing": out_page, "incoming": in_page,
            "items": out_page + in_page, "total": len(combined), "total_outgoing": len(outgoing),
            "total_incoming": len(incoming), "page": page, "page_size": page_size,
            "source": "ComponentRelation (DESIGN/CONFIG/OBSERVED) + run/ledger records",
            "checked_at": _iso(now), "mode": "DESIGN"}


def _relation_visible(rel: ComponentRelation, domain: str | None) -> bool:
    d = (rel.scope or {}).get("domain")
    return d is None or d == domain


# ---------------------------------------------------------------- detail card


def _check_level(check_id: str, functional: bool) -> str:
    """§34: the evidence level of what a Doctor check observes (static E1 / controlled self-test E2 / runtime
    probe E3), from the check's own declaration — never a blanket E3."""
    try:
        from aeos.doctor.checks import REGISTRY, load_all

        load_all()
        spec = REGISTRY.get(check_id)
    except (ImportError, ModuleNotFoundError):
        spec = None
    return spec.level(functional) if spec is not None else "E1"


def _observation_level(session: Session, comp: ComponentReference, obs: ComponentObservation | None) -> str | None:
    """Level of one observation: its evidence artifact's level when linked, else by observer and probe kind
    (file/lockfile/manifest/config reads E1; executed/imported/health probes E3)."""
    if obs is None:
        return None
    if obs.evidence_id:
        from aeos.models.evidence import EvidenceArtifact

        ev = session.get(EvidenceArtifact, obs.evidence_id)
        if ev is not None and ev.evidence_level:
            return ev.evidence_level
    detail = obs.detail or {}
    if obs.observer == "doctor" and detail.get("check_id"):
        return _check_level(str(detail["check_id"]), False)
    if obs.dimension == "declaration" or obs.dimension == "discovery":
        return "E1"
    if obs.dimension == "configuration":
        return "E3" if detail.get("endpoint") == "loopback" else "E1"
    if obs.dimension == "connectivity":
        return "E3"
    if obs.dimension == "functional":
        return "E3" if obs.observer == "claude_bridge" else "E2"
    return PROBE_LEVELS.get(str((comp.probe or {}).get("type")), "E1")


def _claims(session: Session, comp: ComponentReference, dims: dict[str, dict[str, Any]],
            checks: list[dict[str, Any]], latest: dict[tuple[str, str], ComponentObservation],
            evals: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    cid = comp.component_id
    claims = [{"claim": "Tercatat dalam rancangan (InstallManifest)", "level": "E1", "status": comp.design_label,
               "source": "install-manifest", "at": None}]

    def level(dim: str) -> str | None:
        lvl = _observation_level(session, comp, latest.get((cid, dim)))
        return f"{lvl} (STALE)" if lvl and dims[dim].get("stale") else lvl

    inst = dims["installation"]
    if inst["observation_id"]:
        claims.append({"claim": f"Instalasi: {inst['value']}", "level": level("installation"),
                       "status": inst["effective"], "source": inst["source"], "at": inst["at"]})
    else:
        claims.append({"claim": "Instalasi", "level": None, "status": inst["value"], "source": inst["source"],
                       "at": None, "note": inst["reason"]})
    conf = dims["configuration"]
    if conf["observation_id"]:
        claims.append({"claim": "Konfigurasi (dokumen/konfigurasi hanya E1 tentang yang didokumentasikan)",
                       "level": level("configuration"), "status": conf["effective"], "source": conf["source"],
                       "at": conf["at"]})
    conn = dims["connectivity"]
    claims.append({"claim": "Konektivitas (bukan bukti izin/keamanan)",
                   "level": level("connectivity") if conn["observation_id"] else None,
                   "status": conn["effective"], "source": conn["source"], "at": conn["at"]})
    fn = dims["functional"]
    claims.append({"claim": "Fungsi terverifikasi untuk skenario tertentu",
                   "level": level("functional") if fn["observation_id"] else None,
                   "status": fn["effective"], "source": fn["source"], "at": fn["at"]})
    use = dims["usage"]
    if use["value"] in ("IN_USE", "LAST_USED", "STALE") and use.get("refs"):
        claims.append({"claim": "Penggunaan teramati (run/operasi)", "level": "E3", "status": use["effective"],
                       "source": use["source"], "at": use["at"], "refs": use.get("refs")})
    for c in checks:
        claims.append({"claim": f"Doctor {c['check_id']}", "level": _check_level(c["check_id"], bool(c.get("functional"))),
                       "status": c["status"], "source": "readiness doctor", "at": c["last_checked_at"]})
    for e in evals or []:
        claims.append({"claim": f"Evaluasi {e['suite_key']} ({e['route_key']})",
                       "level": "E2" if e.get("mode") == "LIVE" else "E0", "status": e["status"],
                       "source": f"eval_run:{e['id']}", "at": e.get("finished_at")})
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


def _cost_sources(session: Session, comp: ComponentReference, domain: str | None) -> dict[str, Any]:
    """§37 Biaya: free-tier expiry and the budget/quota records that bound this component (never guessed)."""
    from aeos.models.cost import Budget, QuotaState
    from aeos.models.routing import ModelRoute

    providers = [p for p, cid in PROVIDER_COMPONENTS.items() if cid == comp.component_id]
    design_free = (comp.cost or {}).get("free_until")
    routes = list(session.scalars(select(ModelRoute).where(ModelRoute.provider.in_(providers)))) if providers else []
    route_free = sorted(({"route_key": r.route_key, "free_until": _iso(r.free_until), "is_free": r.is_free}
                         for r in routes if r.free_until is not None), key=lambda x: x["free_until"] or "")
    if route_free:
        free = {"value": route_free[0]["free_until"], "status": "RECORDED", "source": "ModelRoute.free_until",
                "routes": route_free}
    elif design_free:
        free = {"value": design_free, "status": "DESIGN", "source": "install-manifest cost.free_until"}
    else:
        free = {"value": None, "status": "UNKNOWN" if (comp.cost or {}).get("model") == "FREE_TIER" else
                "NOT_APPLICABLE", "reason": "masa gratis tidak tercatat; tidak ditebak"
                if (comp.cost or {}).get("model") == "FREE_TIER" else "bukan komponen free-tier"}
    keys = {r.route_key for r in routes}
    budgets = [{"budget_id": b.id, "scope_type": b.scope_type, "scope_id": b.scope_id, "period": b.period,
                "limit_usd": float(b.limit_usd) if b.limit_usd is not None else None, "hard_stop": b.hard_stop,
                "decision_id": b.decision_id, "is_default": b.is_default}
               for b in session.scalars(select(Budget).where(Budget.status == "ACTIVE"))
               if (b.scope_type == "GLOBAL" or (b.scope_type == "ROUTE" and b.scope_id in keys))
               and b.domain in (None, domain)] if providers else []
    quotas = [{"provider": q.provider, "scope": q.scope, "state": q.state, "source": q.source,
               "observed_at": _iso(q.observed_at), "resets_at": _iso(q.resets_at)}
              for q in session.scalars(select(QuotaState).where(QuotaState.provider.in_(providers)))] if providers else []
    return {"free_expiry": free,
            "budget_source": {"items": budgets, "status": "RECORDED" if budgets else (
                "NOT_APPLICABLE" if not providers else "UNKNOWN"),
                "reason": None if budgets else ("bukan provider berbayar/terukur" if not providers else
                                                "belum ada budget aktif (reservasi memakai default $0 hard-stop)")},
            "quota_source": {"items": quotas, "status": "RECORDED" if quotas else (
                "NOT_APPLICABLE" if not providers else "UNKNOWN"),
                "reason": None if quotas else ("bukan provider" if not providers else "kuota belum teramati")}}


def _changes(session: Session, comp: ComponentReference, state: DecisionState | None = None,
             domain: str | None = None) -> dict[str, Any]:
    from aeos.models.research import ChangeProposal, ResearchEntry

    cid = comp.component_id
    proposals = []
    for p in session.scalars(select(ChangeProposal).order_by(ChangeProposal.created_at.desc()).limit(500)):
        if p.domain not in (None, domain):
            continue  # an OFFICE proposal never shows in a PERSONAL view (§23, §35)
        if cid in (p.existing_component_ids or []) or cid in (p.candidate_component_ids or []):
            proposals.append({"id": p.id, "kind": p.kind, "title": p.title, "status": p.status,
                              "created_at": _iso(p.created_at), "baseline_hash": p.baseline_hash,
                              "candidate_hash": p.candidate_hash, "candidate_pin": p.candidate_pin,
                              "outcome": ((p.outcome or {}).get("current") or {}).get("status"),
                              "role": "candidate" if cid in (p.candidate_component_ids or []) else "existing"})
    research = [{"id": r.id, "name": r.name, "lifecycle": r.lifecycle, "checked_at": _iso(r.checked_at)}
                for r in session.scalars(select(ResearchEntry).where(ResearchEntry.component_id == cid))]
    rec = current_manifest(session)
    prev = state.previous_accepted_configuration if state is not None else None
    return {
        "decision_refs": state.decision_refs if state is not None else comp.decision_refs,
        # §37 "approval target/hash": each decision with its exact binding hash and baseline/candidate hashes
        "decisions": state.decisions if state is not None else [],
        "proposals": proposals, "research_entries": research,
        "manifest_hash": comp.manifest_hash,
        "manifest_record": {"id": rec.id, "loaded_at": _iso(rec.created_at)} if rec else None,
        "previous_accepted_configuration": prev or {"value": None, "status": "UNKNOWN",
                                                    "reason": "belum ada outcome ACCEPTED dengan konfigurasi tercatat"},
        "propose_url": f"/proposals?component={cid}",
    }


def _config_revision(session: Session, comp: ComponentReference, latest: dict[tuple[str, str], ComponentObservation],
                     now: datetime) -> dict[str, Any]:
    """§37 config/schema/prompt/skill revision: the revision hash of the enabled agent definitions/roles that run on a
    harness component, else the latest configuration observation's version (e.g. manifest/registry/template hash)."""
    from aeos.models.workforce import AgentDefinition, RoleSkillPack

    harnesses = [h for h, cid in HARNESS_COMPONENTS.items() if cid == comp.component_id]
    if harnesses:
        agents = list(session.scalars(select(AgentDefinition).where(AgentDefinition.harness.in_(harnesses),
                                                                   AgentDefinition.enabled.is_(True))))
        if agents:
            roles = {r.role_key: r.revision_hash for r in session.scalars(select(RoleSkillPack).where(
                RoleSkillPack.role_key.in_([a.role_key for a in agents])))}
            return {"value": [{"agent_key": a.agent_key, "agent_revision_hash": a.revision_hash,
                               "role_key": a.role_key, "role_revision_hash": roles.get(a.role_key)} for a in agents],
                    "status": "RECORDED", "source": "AgentDefinition/RoleSkillPack revision_hash (enabled)"}
    conf = latest.get((comp.component_id, "configuration"))
    if conf is not None and (conf.observed_version or (conf.detail or {}).get("config_hash")):
        return {"value": conf.observed_version or (conf.detail or {}).get("config_hash"),
                "status": "STALE" if is_stale(conf, now) else "OBSERVED", "source": f"observasi:{conf.observer}",
                "aspect": (conf.detail or {}).get("aspect") or (conf.detail or {}).get("source"),
                "at": _iso(conf.observed_at)}
    return {"value": None, "status": "UNKNOWN", "reason": "revisi konfigurasi belum teramati"}


def _maintainer(session: Session, comp: ComponentReference) -> dict[str, Any]:
    from aeos.models.identity import SystemSetting

    row = session.get(SystemSetting, MAINTAINER_PREFIX + comp.component_id)
    v = row.value if row is not None and isinstance(row.value, dict) else None
    if not v or not v.get("name"):
        return {"value": None, "status": "UNKNOWN", "reason": "maintainer operasional belum ditetapkan; tidak ditebak"}
    return {"value": v.get("name"), "status": "SET", "set_by": v.get("set_by"), "set_at": v.get("set_at"),
            "source": "ditetapkan pemilik (teraudit)"}


def _authorization_scope(session: Session, comp: ComponentReference, domain: str | None) -> dict[str, Any]:
    """Effective grant/target scope from the Integration Registry and route eligibility (design text kept)."""
    from aeos.inventory.configlens import INTEGRATION_COMPONENTS
    from aeos.models.portfolio import ProjectIntegration
    from aeos.models.routing import ModelRoute

    design = (comp.access or {}).get("tenant_target")
    providers = [p for p, cid in INTEGRATION_COMPONENTS.items() if cid == comp.component_id]
    integrations = []
    if providers and domain is not None:
        integrations = [{"project_id": i.project_id, "provider": i.provider, "purpose": i.purpose,
                         "tenant_id": i.tenant_id, "root_id": i.root_id, "credential_ref": i.credential_ref}
                        for i in session.scalars(select(ProjectIntegration).where(
                            ProjectIntegration.provider.in_(providers), ProjectIntegration.domain == domain))]
    route_providers = [p for p, cid in PROVIDER_COMPONENTS.items() if cid == comp.component_id]
    routes = [{"route_key": r.route_key, "status": r.status, "eligible_domains": r.eligible_domains or [],
               "allowed_data_classes": r.allowed_data_classes or []}
              for r in session.scalars(select(ModelRoute).where(ModelRoute.provider.in_(route_providers),
                                                                ModelRoute.status.in_(("APPROVED", "TRIAL"))))
              if domain is None or not r.eligible_domains or domain in r.eligible_domains] if route_providers else []
    effective = bool(integrations or routes)
    return {"value": design, "design": design, "integrations": integrations, "routes": routes,
            "status": "EFFECTIVE" if effective else "DESIGN",
            "note": "Target/izin berlaku dari Integration Registry dan route yang disetujui; teks desain bukan izin."}


def _access_expiry(comp: ComponentReference, domain: str | None) -> dict[str, Any]:
    ref = str((comp.access or {}).get("identity_ref") or "").strip()
    first = ref.split(",")[0].split(" ")[0].strip()
    if not first or not SECRET_REF_RE.match(first):
        return {"value": None, "status": "NOT_APPLICABLE" if not ref else "UNKNOWN",
                "reason": "tanpa referensi credential" if not ref else
                "referensi bukan secret ref AEOS (mis. login client resmi); expiry dikelola pemiliknya"}
    ns = first.split("/", 1)[0]
    if (ns == "office" and domain != "OFFICE") or ns == "projects":
        return {"value": None, "status": "UNKNOWN", "reason": "referensi ber-scope domain/proyek; tidak ditampilkan"}
    try:
        from aeos.secrets import facade as secrets

        st = secrets.secret_status(first)
    except Exception as exc:  # store unavailable: UNKNOWN, never guessed
        return {"value": None, "status": "UNKNOWN", "ref": first,
                "reason": f"secret store tidak tersedia ({type(exc).__name__})"}
    if not st.exists:
        return {"value": None, "status": "MISSING", "ref": first, "reason": "credential belum disimpan"}
    exp = ensure_aware(st.expires_at)
    return {"value": _iso(exp), "status": "RECORDED" if exp else "NO_EXPIRY_RECORDED", "ref": first,
            "expired": bool(exp and exp <= utcnow()), "source": "secret store metadata (tanpa nilai)"}


def _admission_tests(session: Session, comp: ComponentReference, latest: dict[tuple[str, str], ComponentObservation],
                     now: datetime) -> list[dict[str, Any]]:
    """§25 "hasil tes": each declared capability test with the latest functional observation and evidence id."""
    func = latest.get((comp.component_id, "functional"))
    out = []
    for t in comp.capability_tests or []:
        out.append({"id": t.get("id"), "functional": t.get("functional"),
                    "result": (func.status if func is not None else "NOT_TESTED"),
                    "stale": is_stale(func, now) if func is not None else None,
                    "evidence_id": func.evidence_id if func is not None else None,
                    "at": _iso(func.observed_at) if func is not None else None})
    return out


def latest_checks_for(session: Session, component_id: str, domain: str | None) -> list[dict[str, Any]]:
    from aeos.models.doctor_ext import DoctorRun

    sub = (select(CheckResult.check_id, func.max(CheckResult.last_checked_at).label("m"))
           .where(CheckResult.component_id == component_id).group_by(CheckResult.check_id).subquery())
    q = select(CheckResult).join(sub, (CheckResult.check_id == sub.c.check_id)
                                 & (CheckResult.last_checked_at == sub.c.m))
    # per-project results are shown per project on Readiness (project scope), never mixed into the component card
    rows = [r for r in session.scalars(q) if (r.scope or {}).get("domain") in (None, domain)
            and not (r.scope or {}).get("project_id")]
    runs = {r.id: r.functional for r in session.scalars(select(DoctorRun).where(
        DoctorRun.id.in_([x.run_id for x in rows if x.run_id])))} if rows else {}
    out = []
    for r in rows:
        out.append({"check_id": r.check_id, "title": r.title, "group": r.group, "status": r.status,
                    "severity": r.severity, "summary": r.summary, "last_checked_at": _iso(r.last_checked_at),
                    "expires_at": _iso(r.expires_at), "functional": bool(runs.get(r.run_id or "")),
                    "project_id": (r.scope or {}).get("project_id")})
    return sorted(out, key=lambda x: x["check_id"])


def component_detail(session: Session, component_id: str, *, domain: str | None) -> dict[str, Any]:
    comps = _visible_components(session, domain, include_tombstoned=True)
    comp = comps.get(component_id)
    if comp is None:
        raise NotFound("component not found")
    now = utcnow()
    latest = latest_observations(session, [component_id])
    basis = config_basis(session, {component_id: comp}, latest, now, domain=domain).get(component_id, [])
    state = decision_states(session, {component_id: comp}, domain=domain)[component_id]
    usage, _edges, _runs = rt.collect(session, domain=domain, now=now)
    dims = dimension_values(comp, latest, now, state=state, basis=basis, usage_fact=usage.get(component_id),
                            write_enabled=component_id in write_enabled_components(session, domain))
    rels = component_relations(session, component_id, domain=domain, page_size=500)
    checks = latest_checks_for(session, component_id, domain)
    evals = rt.eval_runs(session, component_id, domain=domain)
    obs_hist = [o for o in history(session, component_id, limit=40) if _obs_visible(o, domain)]
    instances = sorted({o.observed_location for o in obs_hist if o.dimension == "installation" and o.observed_location})
    declarations = [
        {"source": (o.detail or {}).get("source"), "observed_at": _iso(o.observed_at), "stale": is_stale(o, now),
         "packages": (o.detail or {}).get("packages"), "actions": (o.detail or {}).get("actions"),
         "recommendations": (o.detail or {}).get("recommendations"),
         "transitive_packages": (o.detail or {}).get("transitive_packages"),
         "package": (o.detail or {}).get("package"), "spec": (o.detail or {}).get("spec"),
         "version": o.observed_version, "lockfile_sha256": (o.detail or {}).get("lockfile_sha256"),
         # §38 "digunakan oleh build X": a linked dependency is used by a build, not a running service
         "used_by_build": (o.detail or {}).get("used_by_build"),
         "dependencies_url": f"/api/inventory/components/{component_id}/dependencies"
         if component_id in ("dly-aeos-control-plane", "control-center-frontend") else None}
        for (cid, dim), o in latest.items() if dim == "declaration" and cid == component_id
    ]
    used_by = [r for r in rels["incoming"] if r["relation"] in IMPACT_RELATIONS - {"governed_by"}]
    drift = _open_drift(session, [component_id]).get(component_id, [])
    domains = sorted(component_domains(comp))
    unknown = {"value": None, "status": "UNKNOWN"}
    probe = comp.probe or {}
    details = comp.details or {}
    version = version_status(comp, latest)
    inst_obs = latest.get((component_id, "installation"))
    observed_digest = inst_obs.observed_identity if inst_obs is not None and str(
        inst_obs.observed_identity or "").startswith("sha256:") else None
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
            "maintainer": _maintainer(session, comp),
            "authorization_scope": _authorization_scope(session, comp, domain),
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
            **version,
            # §37: source repo, distribution artifact and installed executable are distinct identities
            "approved_digest": details.get("approved_digest"),
            "observed_digest": observed_digest,
            "digest_status": ("MATCH" if details.get("approved_digest") and observed_digest == details["approved_digest"]
                              else "MISMATCH" if details.get("approved_digest") and observed_digest
                              else "NO_APPROVED_DIGEST" if not details.get("approved_digest") else "UNKNOWN"),
            "distribution": details.get("distribution") or {**unknown, "reason": "artefak distribusi belum dicatat"},
            "signature": details.get("signature") or {**unknown, "result": "UNKNOWN",
                                                      "reason": "tanda tangan/provenance belum dicatat"},
            "tool_schema_hash": details.get("tool_schema_hash"),
            "config_revision": _config_revision(session, comp, latest, now),
            "lockfile_refs": [d["source"] for d in declarations if d.get("source")],
            "provenance_note": "Pin source commit tidak membuktikan binary di PATH dibangun dari commit itu (§37).",
        },
        "compatibility": {"ports": details.get("ports") or [], "config_refs": details.get("config_refs") or [],
                          "compatibility": details.get("compatibility") or {**unknown,
                                                                            "reason": "kompatibilitas belum dicatat"}},
        "usage_role": {
            "usage_role": comp.usage_role, "requirement": comp.requirement, "design_label": comp.design_label,
            "why_selected": comp.why_selected, "placement": comp.placement,
            "used_by": [{"component_id": r["source"], "name": r["source_name"], "relation": r["relation"],
                         "lens": r["lens"]} for r in used_by],
            "consuming_tasks": rt.consuming(session, component_id, domain=domain),
            "bootstrap_order": comp.bootstrap_order,
        },
        "lifecycle": dims,
        "admission": {"status": dims["admission"]["value"], "decision_refs": state.decision_refs,
                      "scope": state.scope, "capability_tests": _admission_tests(session, comp, latest, now)},
        "next_action": next_action(comp, dims, drift, version),
        "config_lens": {"present": bool(basis), "basis": basis,
                        "note": "Konfigurasi berlaku hanya dengan dasar eksplisit; nilai default bukan enablement."},
        "access": {**(comp.access or {}), "expiry": _access_expiry(comp, domain),
                   "note": "Nilai credential tidak pernah ditampilkan; hanya referensi logis."},
        "relations": rels,
        "evidence": {
            "checks": checks,
            "observations": [{"id": o.id, "dimension": o.dimension, "status": o.status, "observer": o.observer,
                              "observed_version": o.observed_version, "observed_identity": o.observed_identity,
                              "location": o.observed_location, "operation_ref": o.operation_ref,
                              "observed_at": _iso(o.observed_at), "expires_at": _iso(o.expires_at),
                              "stale": is_stale(o, now), "detail": o.detail} for o in obs_hist],
            "claims": _claims(session, comp, dims, checks, latest, evals),
            "eval_runs": evals,
            "last_checked_at": max((c["last_checked_at"] for c in checks if c["last_checked_at"]), default=None),
            "last_observed_at": _iso(obs_hist[0].observed_at) if obs_hist else None,
            "last_used_at": dims["usage"]["at"] if dims["usage"]["value"] in ("IN_USE", "LAST_USED") else None,
            "last_used_refs": dims["usage"].get("refs"),
            "drift": drift,
        },
        "dependencies": declarations,
        "cost": {"design": comp.cost, "actual": _cost_actual(session, comp), **_cost_sources(session, comp, domain)},
        "operations": {**(comp.operations or {}), "probe": probe, "capability_tests": comp.capability_tests},
        "changes": _changes(session, comp, state, domain),
        "source_label": "InstallManifest + observasi + keputusan + run/ledger records",
        "checked_at": _iso(now),
    }


def component_dependencies(session: Session, component_id: str, *, domain: str | None, page: int = 1,
                           page_size: int = 100, q: str | None = None, relation: str | None = None,
                           repo: Any = None) -> dict[str, Any]:
    """Paginated dependency view (§37: transitive packages explorable) of a component that owns a lockfile."""
    from aeos.core.paths import repo_root
    from aeos.inventory import dependencies as deps

    comps = _visible_components(session, domain, include_tombstoned=True)
    comp = comps.get(component_id)
    if comp is None:
        raise NotFound("component not found")
    if relation not in (None, "direct", "transitive"):
        raise ValidationFailed("relation must be direct or transitive")
    obs = latest_observations(session, [component_id], ("declaration",)).get((component_id, "declaration"))
    out = deps.dependency_page(repo or repo_root(), component_id, page=page, page_size=page_size, q=q,
                               relation=relation, observed_sha=(obs.detail or {}).get("lockfile_sha256") if obs else None,
                               build_name=comp.friendly_name or comp.name,
                               observed_at=_iso(obs.observed_at) if obs else None)
    out["component_id"] = component_id
    return out


# ---------------------------------------------------------------- system map


OBSERVED_PRIORITY = ("usage", "connectivity", "installation", "declaration", "discovery")
OBSERVED_POSITIVE = frozenset({"INSTALLED", "CONNECTED", "IN_USE", "LAST_USED", "IDLE", "DECLARED", "UNREGISTERED"})
INSTALL_NEGATIVE = frozenset({"NOT_INSTALLED", "REMOVED", "INSTALL_FAILED"})


def _observed_status(cid: str, latest: dict[tuple[str, str], ComponentObservation], now: datetime
                     ) -> tuple[bool, str, str | None, bool]:
    """(present, status, at, stale) for the OBSERVED lens.

    The most significant FRESH fact wins by dimension, not by timestamp: a lockfile/config
    declaration (read after the probes) never hides a fresh NOT_INSTALLED/UNREACHABLE result
    (§50 "Tool ditemukan dalam README atau lockfile ... bukan bukti proses aktif"). Only when every
    observation has expired is the newest one shown, as STALE.
    """
    fresh = {dim: o for dim in OBSERVED_PRIORITY
             if (o := latest.get((cid, dim))) is not None and not is_stale(o, now)}
    inst = fresh.get("installation")
    if inst is not None and inst.status in INSTALL_NEGATIVE:
        return False, inst.status, _iso(inst.observed_at), False
    for dim in OBSERVED_PRIORITY:
        o = fresh.get(dim)
        if o is not None:
            return o.status in OBSERVED_POSITIVE, o.status, _iso(o.observed_at), False
    expired = [o for dim in OBSERVED_PRIORITY if (o := latest.get((cid, dim))) is not None]
    if not expired:
        return False, "UNKNOWN", None, False
    newest = max(expired, key=lambda o: ensure_aware(o.observed_at) or now)
    return False, "STALE", _iso(newest.observed_at), True


def _usage_present(fact: rt.UsageFact | None, now: datetime) -> bool:
    if fact is None:
        return False
    if fact.status == "IN_USE":
        return True
    at = ensure_aware(fact.at)
    return fact.status == "LAST_USED" and at is not None and now - at <= OBSERVED_EDGE_TTL


def _record_node(item: dict[str, Any], *, label: str, kind: str, category: str, status: str, present: bool,
                 at: str | None, basis: list[str], domains: list[str], function: str) -> dict[str, Any]:
    return {"id": item["id"], "label": label, "name": label, "kind": kind, "category": category,
            "requirement": "RECORD", "design_label": "RECORD", "domains": domains, "identity_verified":
            item.get("identity_status", "UNVERIFIED"), "drift": [], "function": function, "present": present,
            "status": status, "at": at, "stale": False, "basis": basis, "unknown": at is None and not present,
            "record": True}


def _record_nodes(session: Session, lens: str, *, domain: str | None, allowed_project_ids: list[str] | None,
                  project_id: str | None, comps: dict[str, ComponentReference]
                  ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """§36 nodes for routes/roles/repositories/services (other registries' records) and their typed edges to the
    provider/harness components. DESIGN shows the approved plan's declared services of the selected project;
    CONFIG shows approved routes, enabled roles and project repositories; OBSERVED shows repositories with a
    recorded verification. Every node keeps its record source; nothing is invented."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    def edge(src: str, tgt: str, relation: str, status: str, evidence: str, at: str | None = None) -> None:
        edges.append({"id": f"record:{src}:{relation}:{tgt}", "source": src, "target": tgt, "relation": relation,
                      "lens": lens, "source_name": src, "target_name": comps[tgt].friendly_name if tgt in comps else tgt,
                      "status": status, "provenance": "record", "data_description": evidence, "operation": "UNKNOWN",
                      "boundary": None, "identity_ref": None, "direction": f"{src} -> {tgt}", "scope": {},
                      "created_at": None, "updated_at": None, "last_observed_at": at,
                      "freshness": {"at": at, "stale": False}, "tombstoned_at": None, "evidence": evidence,
                      "tests": []})

    if lens == "DESIGN" and project_id is not None:
        for s in rec_views.project_services(session, domain=domain, allowed_project_ids=allowed_project_ids,
                                            project_id=project_id):
            nodes.append(_record_node(s, label=f"{s['name']} ({s['project_name']})", kind="project_service",
                                      category="code/docs/CI", status="DECLARED", present=True, at=None,
                                      basis=[s["provenance"]], domains=[s["domain"]],
                                      function="Layanan yang dideklarasikan proyek (belum teramati berjalan)."))
    if lens == "CONFIG":
        for r in rec_views.route_nodes(session, domain=domain):
            nodes.append(_record_node(r, label=f"route {r['route_key']}", kind="route", category="workforce",
                                      status=r["status"], present=True, at=None, basis=[f"ModelRoute {r['status']}"],
                                      domains=[domain or "PLATFORM"], function=f"Route model {r['model_id']}"))
            if r.get("provider_component") in comps:
                edge(r["id"], r["provider_component"], "calls", "CONFIGURED", "route memanggil provider (konfigurasi)")
        for role in rec_views.role_nodes(session):
            nodes.append(_record_node(role, label=f"role {role['name']}", kind="role", category="workforce",
                                      status="ENABLED", present=True, at=None,
                                      basis=[f"agent {a['agent_key']} enabled" for a in role["agents"]],
                                      domains=["PLATFORM"], function="Role dengan agent definition aktif"))
            for h in role["harness_components"]:
                if h in comps:
                    edge(role["id"], h, "runs_with", "CONFIGURED", "agent definition aktif memakai harness ini")
    if lens in ("CONFIG", "OBSERVED"):
        for r in rec_views.project_repositories(session, domain=domain, allowed_project_ids=allowed_project_ids,
                                                project_id=project_id):
            observed = lens == "OBSERVED"
            ok = r["verification_status"] == "VERIFIED" and bool(r["last_verified_at"])
            if observed and not ok:
                status = "UNKNOWN"
            else:
                status = "VERIFIED" if observed else "CONFIGURED"
            nodes.append(_record_node(r, label=f"{r['full_name']} ({r['project_name']})", kind="repository",
                                      category="code/docs/CI", status=status, present=(not observed) or ok,
                                      at=r["last_verified_at"] if observed else None,
                                      basis=[f"RepositoryReference {r['identity'] or r['full_name']}"],
                                      domains=[r["domain"]], function="Repository proyek"))
            if r.get("provider_component") in comps:
                edge(r["id"], r["provider_component"], "stores_in", status if observed else "CONFIGURED",
                     "repository proyek di-host provider ini", r["last_verified_at"] if observed else None)
    return nodes, edges


def _snapshot_map(session: Session, snapshot_id: str, *, domain: str | None) -> dict[str, Any]:
    from aeos.inventory.snapshots import get_snapshot, snapshot_meta

    snap = get_snapshot(session, snapshot_id, domain=domain)
    refs = snap.refs or {}
    comps = _visible_components(session, domain, include_tombstoned=True)
    nodes = []
    for n in refs.get("nodes") or []:
        c = comps.get(n["id"])
        nodes.append({**n, "label": (c.friendly_name if c else None) or n["id"], "name": n["id"],
                      "domains": sorted(component_domains(c)) if c else [], "drift": [], "present": True,
                      "status": n.get("design_label") or "SNAPSHOT", "at": refs.get("taken_at"), "stale": False,
                      "basis": [f"snapshot {snap.id[:8]}"], "function": c.function if c else ""})
    edges = [{**e, "id": f"snap:{e['source']}:{e['relation']}:{e['target']}:{e['lens']}", "status": e["lens"],
              "direction": f"{e['source']} -> {e['target']}", "freshness": {"at": refs.get("taken_at"), "stale": False},
              "evidence": f"dari snapshot {snap.label}", "tests": []} for e in refs.get("edges") or []]
    groups = []
    for cat in CATEGORIES:
        ids = [n["id"] for n in nodes if n.get("category") == cat]
        if ids:
            groups.append({"id": cat, "label": CATEGORY_LABELS[cat], "node_ids": ids,
                           "counts": {"total": len(ids), "present": len(ids), "drift": 0}})
    meta = snapshot_meta(snap)
    return {"lens": "SNAPSHOT", "lens_label": f"Snapshot {snap.label}", "lens_source": "ArchitectureSnapshot (immutable)",
            "nodes": nodes, "edges": edges, "groups": groups, "drift": [], "drift_summary": {},
            "legend": _legend("DESIGN"), "coverage": None, "snapshot": meta, "source": "ArchitectureSnapshot",
            "checked_at": refs.get("taken_at"), "mode": f"SNAPSHOT {meta['created_at']}", "domain": domain,
            "note": "Snapshot adalah artefak rancangan/konfigurasi bertanggal; bukan live inventory (§36, §41)."}


def system_map(session: Session, *, lens: str, domain: str | None, category: str | None = None,
               project_id: str | None = None, environment: str | None = None, snapshot_id: str | None = None,
               allowed_project_ids: list[str] | None = None) -> dict[str, Any]:
    if snapshot_id:
        return _snapshot_map(session, snapshot_id, domain=domain)
    lens = lens.upper()
    if lens not in LENSES:
        raise ValidationFailed(f"lens must be one of {', '.join(LENSES)}", code="INVALID_LENS")
    if project_id is not None and not rec_views.visible_projects(session, domain=domain,
                                                                 allowed_project_ids=allowed_project_ids,
                                                                 project_id=project_id):
        raise NotFound("project not found")
    now = utcnow()
    comps = _visible_components(session, domain)
    all_comps = dict(comps)
    if category:
        comps = {k: v for k, v in comps.items() if v.category == category}
    if environment:
        comps = {k: v for k, v in comps.items() if (v.scope or {}).get("environment", "local") == environment}
    latest = latest_observations(session)
    drift = _open_drift(session)
    basis = config_basis(session, comps, latest, now, domain=domain) if lens == "CONFIG" else {}
    usage, fact_edges, active_runs = rt.collect(session, domain=domain, now=now)
    if project_id is not None:
        active_runs = [r for r in active_runs if r.get("project_id") == project_id]
    elif allowed_project_ids is not None:
        active_runs = [r for r in active_runs if r.get("project_id") in allowed_project_ids]
    checks = checks_by_component(session, domain)
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
            fact = usage.get(cid)
            if _usage_present(fact, now) and st not in INSTALL_NEGATIVE:
                ok, st, at, stale = True, fact.status, _iso(fact.at), False  # type: ignore[union-attr]
                node["usage_refs"] = dict(fact.refs)  # type: ignore[union-attr]
            node.update(present=ok, status=st, at=at, stale=stale,
                        basis=["observasi"] if at else [], unknown=at is None)
        runs = [r for r in active_runs if cid in r["components"]]
        if runs:
            # §36 zoom-in: authorized runs on the component they use (no fictional agent instances)
            node["runs"] = [{k: r[k] for k in ("run_id", "task_key", "status", "attempt", "lease_expires_at")}
                            for r in runs[:10]]
        present[cid] = bool(node["present"])
        nodes.append(node)
    rnodes, redges = _record_nodes(session, lens, domain=domain, allowed_project_ids=allowed_project_ids,
                                   project_id=project_id, comps=comps)
    if not category or category in ("workforce", "code/docs/CI"):
        for n in rnodes:
            if category and n["category"] != category:
                continue
            nodes.append(n)
            present[n["id"]] = bool(n["present"])
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
    edges: list[dict[str, Any]] = [e for e in redges if e["source"] in node_ids and e["target"] in node_ids]
    rel_rows = list(session.scalars(select(ComponentRelation).where(ComponentRelation.tombstoned_at.is_(None))))
    observed_keys: set[tuple[str, str, str]] = set()
    if lens == "OBSERVED":
        for e in fact_edges:
            if e.source_id in node_ids and e.target_id in node_ids:
                edges.append(_fact_relation(e, all_comps, now, checks))
                observed_keys.add((e.source_id, e.target_id, e.relation))
    for rel in rel_rows:
        if rel.source_id not in node_ids or rel.target_id not in node_ids or not _relation_visible(rel, domain):
            continue
        if lens == "DESIGN" and rel.lens == "DESIGN":
            edges.append(_relation_view(rel, comps, now, status="DESIGN", checks=checks))
        elif lens == "CONFIG":
            if rel.lens == "CONFIG":
                edges.append(_relation_view(rel, comps, now, status="CONFIGURED", checks=checks))
            elif rel.lens == "DESIGN" and present.get(rel.source_id) and present.get(rel.target_id):
                v = _relation_view(rel, comps, now, status="CONFIGURED", checks=checks)
                v["evidence"] = "relasi rancangan; kedua ujung terkonfigurasi (traffic belum teramati)"
                edges.append(v)
        elif lens == "OBSERVED":
            key = (rel.source_id, rel.target_id, rel.relation)
            if rel.lens == "OBSERVED":
                if key in observed_keys:
                    continue
                v = _relation_view(rel, comps, now, status="OBSERVED", checks=checks)
                if v["freshness"]["stale"]:
                    v["status"] = "STALE"
                edges.append(v)
                observed_keys.add(key)
            elif rel.lens == "DESIGN" and present.get(rel.source_id) and present.get(rel.target_id) \
                    and key not in observed_keys:
                v = _relation_view(rel, comps, now, status="UNKNOWN", checks=checks)
                v["evidence"] = "UNKNOWN: relasi tidak diinstrumentasi (hanya ada di rancangan)"
                edges.append(v)
    if lens == "OBSERVED":
        # an observed edge supersedes the uninstrumented DESIGN copy of the same relation
        edges = [e for e in edges if not (e["status"] == "UNKNOWN"
                                          and (e["source"], e["target"], e["relation"]) in observed_keys)]
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
        "legend": _legend(lens), "coverage": coverage(session, domain=domain, compact=True, usage=usage),
        "runs": active_runs[:50],
        "filters": {"project_id": project_id, "environment": environment, "category": category,
                    "environments": sorted({(c.scope or {}).get("environment", "local") for c in all_comps.values()})},
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


def drift_list(session: Session, *, domain: str | None, include_resolved: bool = False, page: int = 1,
               page_size: int = 200, drift_class: str | None = None) -> dict[str, Any]:
    from aeos.inventory.drift import drift_policy

    comps = _visible_components(session, domain, include_tombstoned=True)
    q = select(DriftFinding)
    if not include_resolved:
        q = q.where(DriftFinding.status == "OPEN")
    if drift_class:
        if drift_class not in DRIFT_CLASSES:
            raise ValidationFailed(f"drift_class must be one of {', '.join(DRIFT_CLASSES)}")
        q = q.where(DriftFinding.drift_class == drift_class)
    items = []
    for f in session.scalars(q.order_by(DriftFinding.last_detected_at.desc(), DriftFinding.id)):
        if f.domain_scope not in ("PLATFORM", domain):
            continue
        if not f.component_id.startswith(UNREGISTERED_PREFIX) and f.component_id not in comps:
            continue
        v = drift_view(f)
        c = comps.get(f.component_id)
        v["component_name"] = c.friendly_name if c else f.component_id
        items.append(v)
    last = latest_run(session)
    page = max(1, page)
    page_size = max(1, min(page_size, 500))
    policy = drift_policy()
    return {"items": items[(page - 1) * page_size:page * page_size], "total": len(items), "page": page,
            "page_size": page_size,
            "by_class": {c: sum(1 for i in items if i["drift_class"] == c and i["status"] == "OPEN")
                         for c in DRIFT_CLASSES},
            "labels": DRIFT_LABELS, "last_reconciled_at": _iso(last.finished_at) if last else None,
            "severity_policy": {"source": policy.get("source"), "severity": policy["severity"],
                                "warnings": policy.get("warnings") or []},
            "source": "DriftFinding (projection rekonsiliasi)", "checked_at": _iso(last.finished_at) if last else None,
            "mode": "LIVE" if last else "DESIGN",
            "note": "Drift bukan otomatis insiden keamanan; severity mengikuti policy dan bukti (§36)."}
