"""§39 review package: the 10 items a human receives, computed deterministically from records.

Baseline/impact come from the inventory (ComponentReference + typed relations + observations);
source identity from the research registry; anything not derivable is UNKNOWN — never guessed.
The optional Claude recommendation (item 8) receives only generic public metadata and is DATA,
never owner approval (§19, §39). Every record read here is scoped to the proposal's domain
(snapshots, inventory components/relations/observations, usage, drift): one domain's records never shape
the other domain's package, baseline hash or outcome (§23, §35, §41).
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aeos.core.enums import Domain
from aeos.core.errors import AeosError
from aeos.core.hashing import content_hash
from aeos.core.timeutil import utcnow
from aeos.models.decisions import ArchitectureSnapshot
from aeos.models.inventory import ComponentObservation, ComponentReference, ComponentRelation
from aeos.models.research import ChangeProposal, EvalSuite, ResearchEntry
from aeos.research import advisories

UNKNOWN = "UNKNOWN"
# §50 "Nama repo ambigu atau sumber tidak terverifikasi -> UNVERIFIED dan blocked admission";
# "Source repo rename/redirect/fork -> tidak mewarisi trust otomatis".
ADMISSION_BLOCKING_IDENTITY = ("UNVERIFIED", "CHANGED", "NOT_FOUND")
REVIEW_MODEL = "fable"  # §15/§39: highest Claude per policy; the bridge records the resolved model
KIND_QUESTIONS = {
    "ADD": "Capability baru apa yang dibutuhkan dan mengapa komponen yang ada tidak cukup?",
    "REPLACE": "Komponen mana yang diganti, siapa consumer-nya, dan kapan versi lama boleh dihentikan?",
    "UPGRADE": "Apa delta versi/publisher/terms/schema dari komponen yang sama?",
    "REMOVE": "Dependency, data, identity, job dan support apa yang harus ditutup?",
    "COMPARE": "Apakah cukup menjadi referensi atau perlu uji lebih lanjut? Belum ada izin install/aktivasi.",
}
CONSUMER_RELATIONS = ("depends_on", "calls", "reads", "implements", "runs_with", "verifies", "publishes_to")
DATA_RELATIONS = ("writes", "stores_in")
POLICY_RELATIONS = ("governed_by",)
CATEGORY_SUITES = {
    "worker-engine": ["eval.small-bugfix", "eval.feature-fe-api", "eval.provider-quota-interruption", "eval.crash-recovery"],
    "inference-route": ["eval.small-bugfix", "eval.feature-fe-api", "eval.provider-quota-interruption"],
    "gateway": ["eval.provider-quota-interruption"],
    "retrieval": ["eval.small-bugfix", "eval.feature-fe-api"],
    "metering": ["eval.provider-quota-interruption"],
    "skills": ["eval.independent-readonly-reviews", "eval.docs-only-change"],
    "role-method": ["eval.small-bugfix", "eval.feature-fe-api"],
    "orchestration": ["eval.crash-recovery", "eval.self-build-controller-change"],
    "fleet": ["eval.crash-recovery", "eval.self-build-controller-change"],
    "mcp-runtime": ["eval.independent-readonly-reviews"],
    "model": ["eval.small-bugfix", "eval.feature-fe-api", "eval.docs-only-change"],
}
# Research category -> inventory component kinds that already provide that capability (ADD overlap).
CATEGORY_KINDS = {
    "worker-engine": ("worker-engine",), "inference-route": ("model-provider", "model-gateway"),
    "gateway": ("model-gateway",), "retrieval": ("mcp-server",), "mcp-runtime": ("mcp-server",),
    "skills": ("skill-library",), "role-method": ("skill-library", "method-reference"),
    "orchestration": ("orchestrator",), "fleet": ("orchestrator",), "workflow": ("orchestrator",),
    "secrets": ("secret-store",), "observability": ("observability",), "evaluation": ("evaluation", "test-tool"),
    "model": ("model-provider",), "metering": ("observability",), "security-tools": ("test-tool",),
}
DATA_CLASSES = ("PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED")
LEDGER_WINDOW = timedelta(days=30)
_PERCENT = re.compile(r"[~±+-]?\d+(?:[.,]\d+)?\s*(?:[-\u2013]|sampai|to)?\s*\d*(?:[.,]\d+)?\s*%")

CLAUDE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["review", "verify", "verdict", "recommendation", "rationale"],
    "additionalProperties": False,
    "properties": {
        "review": {"type": "object", "required": ["summary"], "properties": {
            "summary": {"type": "string"}, "upstream_downstream": {"type": "string"},
            "risks": {"type": "array", "items": {"type": "string"}},
            "contradictions": {"type": "array", "items": {"type": "string"}},
        }},
        "verify": {"type": "object", "required": ["claims"], "properties": {
            "claims": {"type": "array", "items": {"type": "object", "required": ["claim", "status"], "properties": {
                "claim": {"type": "string"}, "evidence": {"type": "string"},
                "status": {"enum": ["SUPPORTED", "UNSUPPORTED", "UNKNOWN"]}}}},
            "unknowns": {"type": "array", "items": {"type": "string"}},
        }},
        "verdict": {"enum": ["PASS-STATIC", "NOT-PROVEN", "BLOCKED", "FAIL"]},
        "recommendation": {"enum": ["KEEP", "ADD", "REPLACE", "TRIAL", "REJECT", "DEFER", "REQUEST_MORE_EVIDENCE",
                                    "REFERENCE_ONLY"]},
        "rationale": {"type": "string"},
    },
}


def _norm_kind(kind: str | None) -> str:
    return (kind or "").strip().lower().replace("_", "-")


def _tokens(text: str | None) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t}


# --------------------------------------------------------------------------- snapshots (domain-scoped)


def snapshot_in_domain(snap: ArchitectureSnapshot, domain: str | None) -> bool:
    """Same scope rule as ``aeos.inventory.snapshots``: a snapshot belongs to the domain it was taken in."""
    return ((snap.refs or {}).get("scope") or {}).get("domain") == domain


def latest_snapshot(session: Session, domain: str | None) -> ArchitectureSnapshot | None:
    for snap in session.scalars(select(ArchitectureSnapshot).order_by(ArchitectureSnapshot.created_at.desc())):
        if snapshot_in_domain(snap, domain):
            return snap
    return None


# --------------------------------------------------------------------------- inventory records (domain-scoped)
# The same scope rules as the inventory views (§23, §35, §38): a PLATFORM component is shared, a PERSONAL-only
# or OFFICE-only component exists only in its own domain, and a relation/observation tagged with a domain is
# only that domain's record. A proposal reads the inventory exclusively through these helpers, so an OFFICE
# record never shapes a PERSONAL package, baseline hash or outcome (and vice versa).


def component_in_domain(comp: ComponentReference | None, domain: str | None) -> bool:
    from aeos.inventory import facade as inventory

    return comp is not None and inventory.component_visible(comp, domain)


def domain_component(session: Session, component_id: str, domain: str | None) -> ComponentReference | None:
    """The inventory component as the proposal's domain sees it (None when it belongs to the other domain)."""
    comp = session.get(ComponentReference, component_id)
    return comp if component_in_domain(comp, domain) else None


def relation_in_domain(session: Session, rel: ComponentRelation, domain: str | None) -> bool:
    """A relation is this domain's when it is not tagged with another domain and neither registered end is a
    component of the other domain (an unregistered end, e.g. a control-plane module, carries no domain)."""
    from aeos.inventory import facade as inventory

    if not inventory.relation_visible(rel, domain):
        return False
    for end in (rel.source_id, rel.target_id):
        comp = session.get(ComponentReference, end)
        if comp is not None and not inventory.component_visible(comp, domain):
            return False
    return True


def observation_in_domain(obs: ComponentObservation, domain: str | None) -> bool:
    from aeos.inventory import facade as inventory

    return inventory.observation_visible(obs, domain)


# --------------------------------------------------------------------------- baseline / candidate


def _relations(session: Session, component_id: str, domain: str | None
               ) -> tuple[list[ComponentRelation], list[ComponentRelation]]:
    inbound = [r for r in session.scalars(select(ComponentRelation).where(ComponentRelation.target_id == component_id))
               if relation_in_domain(session, r, domain)]
    outbound = [r for r in session.scalars(select(ComponentRelation).where(ComponentRelation.source_id == component_id))
                if relation_in_domain(session, r, domain)]
    return inbound, outbound


def _latest_observations(session: Session, component_id: str, domain: str | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    rows = session.scalars(select(ComponentObservation).where(ComponentObservation.component_id == component_id)
                           .order_by(ComponentObservation.observed_at.desc()))
    for o in rows:
        if o.dimension in out or not observation_in_domain(o, domain):
            continue
        out[o.dimension] = {"status": o.status, "observed_version": o.observed_version, "observer": o.observer,
                            "observed_at": o.observed_at.isoformat() if o.observed_at else None}
    return out


def _rel_view(r: ComponentRelation) -> dict[str, Any]:
    return {"source_id": r.source_id, "target_id": r.target_id, "relation": r.relation, "lens": r.lens,
            "provenance": r.provenance, "data": r.data_description, "boundary": r.boundary,
            "identity_ref": r.identity_ref}


def _known_issues(session: Session, component_id: str, domain: str | None) -> list[dict[str, Any]]:
    """§39 item 3 "kekurangan yang sudah tercatat": registry limitations of the component's entries and
    open inventory drift findings visible in this domain (never the other domain's findings)."""
    out: list[dict[str, Any]] = []
    for e in session.scalars(select(ResearchEntry).where(ResearchEntry.component_id == component_id)):
        if e.submitted_domain not in (None, domain) or not (e.limitations or "").strip():
            continue
        out.append({"source": "research_registry", "ref": e.entry_key or e.id, "issue": e.limitations[:400]})
    try:
        from aeos.models.inventory_ext import DriftFinding

        scopes = ["PLATFORM", *([domain] if domain else [])]
        for f in session.scalars(select(DriftFinding).where(DriftFinding.component_id == component_id,
                                                            DriftFinding.status == "OPEN",
                                                            DriftFinding.domain_scope.in_(scopes))):
            out.append({"source": "inventory_drift", "ref": f.id, "issue": f"{f.drift_class}: {f.summary}"[:400],
                        "severity": f.severity})
    except (ImportError, ModuleNotFoundError):  # pragma: no cover - inventory ships with the foundation
        pass
    return out


def component_baseline(session: Session, component_id: str, *, domain: str | None = None) -> dict[str, Any]:
    # A component of the other domain is "not in this domain's inventory": same answer as a missing one, so
    # the package never reveals that it exists (§23).
    comp = domain_component(session, component_id, domain)
    if comp is None:
        return {"component_id": component_id, "status": UNKNOWN,
                "reason": "komponen tidak ditemukan di inventaris; baseline UNKNOWN"}
    inbound, outbound = _relations(session, component_id, domain)
    obs = _latest_observations(session, component_id, domain)
    return {
        "component_id": comp.component_id, "status": "KNOWN", "name": comp.name, "friendly_name": comp.friendly_name,
        "kind": comp.kind, "category": comp.category, "function": comp.function or UNKNOWN,
        "usage_role": comp.usage_role, "requirement": comp.requirement, "design_label": comp.design_label,
        "approved_pin": comp.approved_pin or UNKNOWN, "manifest_hash": comp.manifest_hash or UNKNOWN,
        "admission_status": comp.admission_status, "enablement": comp.enablement, "lifecycle_final": comp.lifecycle_final,
        "source_url": comp.source_url, "upstream_identity": comp.upstream_identity,
        "identity_verified": comp.identity_verified, "owner": comp.owner, "license": comp.license or UNKNOWN,
        "access": comp.access or {}, "cost": comp.cost or {}, "operations": comp.operations or {},
        "capability_tests": comp.capability_tests or [], "decision_refs": comp.decision_refs or [], "mode": comp.mode,
        "consumers": [_rel_view(r) for r in inbound], "dependencies": [_rel_view(r) for r in outbound],
        "observations": obs, "known_issues": _known_issues(session, component_id, domain),
        "evidence_note": ("Observasi terakhir per dimensi (§38); desain/katalog bukan bukti instalasi."
                          if obs else "Belum ada observasi; status runtime UNKNOWN."),
    }


FINGERPRINT_FIELDS = ("component_id", "approved_pin", "manifest_hash", "admission_status", "enablement",
                      "lifecycle_final", "design_label", "access", "requirement")


def baseline_fingerprint(b: dict[str, Any]) -> dict[str, Any]:
    if b.get("status") != "KNOWN":
        return {"component_id": b["component_id"], "status": b.get("status")}
    rels = sorted((r["source_id"], r["target_id"], r["relation"], r["lens"]) for r in b["consumers"] + b["dependencies"]
                  if r["lens"] in ("DESIGN", "CONFIG"))
    observed = {d: (b["observations"].get(d) or {}).get("observed_version") for d in ("installation", "configuration")}
    return {k: b.get(k) for k in FINGERPRINT_FIELDS} | {"relations": rels, "observed_versions": observed}


_baseline_fingerprint = baseline_fingerprint  # kept for existing importers


def compute_baseline(session: Session, p: ChangeProposal) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    comps = [component_baseline(session, cid, domain=p.domain) for cid in sorted(set(p.existing_component_ids or []))]
    fingerprint: dict[str, Any] = {"kind": p.kind, "components": [baseline_fingerprint(b) for b in comps]}
    snapshot = None
    if not comps:
        # Only a snapshot taken in the proposal's own domain is its architecture baseline (§23, §35, §41).
        snap = latest_snapshot(session, p.domain)
        snapshot = {"id": snap.id, "label": snap.label, "snapshot_hash": snap.snapshot_hash} if snap else None
        fingerprint["architecture_snapshot"] = snapshot["snapshot_hash"] if snapshot else None
    return comps, content_hash(fingerprint), {"architecture_snapshot": snapshot}


def research_entries(session: Session, p: ChangeProposal) -> list[ResearchEntry]:
    ids = p.research_entry_ids or []
    return [e for e in (session.get(ResearchEntry, i) for i in ids) if e is not None]


def compute_candidate(session: Session, p: ChangeProposal, children: list[ChangeProposal]) -> tuple[dict[str, Any], str]:
    entries = research_entries(session, p)
    material = sorted(
        (e.identity or "", e.identity_status if e.identity_status in ("CHANGED", "NOT_FOUND") else "",
         (e.upstream or {}).get("observed_identity") or "") for e in entries)
    cand = {
        "kind": p.kind, "identities": sorted(p.canonical_identities or []), "candidate_pin": p.candidate_pin or UNKNOWN,
        "candidate_component_ids": sorted(p.candidate_component_ids or []), "identity_state": material,
        # a part's candidate digest is bound too (appended only when recorded: existing hashes stay unchanged)
        "parts": sorted((c.id, c.kind, tuple(sorted(c.canonical_identities or [])), c.candidate_pin or UNKNOWN,
                         *((c.candidate_digest,) if c.candidate_digest else ())) for c in children),
    }
    if p.candidate_digest:
        # §25: the structured candidate digest is part of what the owner approves (added only when recorded, so
        # candidates without one keep their existing hash)
        cand["candidate_digest"] = p.candidate_digest
    from aeos.proposals.plan import bound_part

    bound = bound_part(p.owner_plan)
    if bound:
        # Acceptance, owner, cutover/rollback and canary plans are part of what the owner approves: changing
        # them re-binds every decision (§40 "authority terkait harus dinilai ulang"). Execution progress
        # (REMOVE closure, canary evidence) is recorded outside the hash.
        cand["owner_plan"] = bound
    return cand, content_hash(cand)


# --------------------------------------------------------------------------- items


def _advisory(e: ResearchEntry | None, pin: str | None) -> dict[str, Any]:
    if e is None:
        return advisories.summary(None, pin)
    return advisories.summary(e.upstream or {}, pin or e.source_version)


def _source_identity(session: Session, p: ChangeProposal) -> list[dict[str, Any]]:
    by_identity = {e.identity: e for e in research_entries(session, p)}
    out = []
    for ident in p.canonical_identities or []:
        e = by_identity.get(ident)
        up = (e.upstream or {}) if e else {}
        if e is None:
            status = "UNVERIFIED"
        elif e.identity_status == "CHECKED":
            status = "VERIFIED_LIVE"
        else:
            status = e.identity_status
        adv = _advisory(e, p.candidate_pin)
        out.append({
            "identity": ident, "status": status, "research_entry_id": e.id if e else None,
            "research_lifecycle": e.lifecycle if e else None, "evidence_level": e.evidence_level if e else "E0",
            "official_repo": up.get("html_url") or ((e.urls or [None])[0] if e else None) or UNKNOWN,
            "release_or_commit": up.get("latest_tag") or UNKNOWN,
            "latest_release": up.get("latest_release") or UNKNOWN,
            "maintainer": up.get("owner") or (e.maintainer if e and e.maintainer else UNKNOWN),
            "license": up.get("license_spdx") or (e.license if e and e.license else UNKNOWN),
            "distribution": (e.distribution if e and e.distribution else UNKNOWN),
            "terms": e.data_terms if e and e.data_terms else UNKNOWN,
            "terms_checked_at": e.terms_checked_at.isoformat() if e and e.terms_checked_at else None,
            "checked_at": e.checked_at.isoformat() if e and e.checked_at else None,
            "research_date": e.research_date if e else None,
            "advisories": adv,
            "flags": [f for f, on in (("IDENTITY_CHANGED", status == "CHANGED"), ("ARCHIVED", bool(up.get("archived"))),
                                      ("FORK", bool(up.get("fork"))), ("NOT_FOUND", status == "NOT_FOUND"),
                                      ("NOT_CHECKED", not (e and e.checked_at)),
                                      ("ADVISORIES_AFFECTED", adv["status"] == "AFFECTED"),
                                      ("ADVISORIES_OPEN", adv["status"] in ("AFFECTED", "UNKNOWN")),
                                      ("ADVISORIES_NOT_CHECKED", adv["status"] in ("NOT_CHECKED", "ERROR")),
                                      ("TERMS_NOT_CHECKED", not (e and e.terms_checked_at))) if on],
            "note": ("Trust tidak diwariskan dari identitas lama." if status == "CHANGED" else
                     "Dinyatakan pada master (riset 17-18 Okt 2026), belum dicek live." if status == "STATED_BY_MASTER"
                     else ""),
        })
    return out


def source_identity_gate(session: Session, p: ChangeProposal) -> dict[str, Any]:
    """Live admission gate for TRIAL/PROMOTION: every candidate source identity must be stated by the
    master, configured by the owner, internal, or checked live - never UNVERIFIED/CHANGED/NOT_FOUND - and
    the candidate version must not lie inside a published vulnerable range (§40 "known security advisories")."""
    rows = _source_identity(session, p)
    blocking = [{"identity": s["identity"], "status": s["status"]} for s in rows
                if s["status"] in ADMISSION_BLOCKING_IDENTITY]
    blocking += [{"identity": s["identity"], "status": "ADVISORY_AFFECTED", "advisories": s["advisories"]["affected"]}
                 for s in rows if s["advisories"]["status"] == "AFFECTED"]
    remedy = None
    if blocking:
        remedy = ("Tambahkan URL kanonik di Research & Technology Registry lalu jalankan Check Upstream "
                  "(atau perbaiki sumber usulan), kemudian review ulang. Tidak ada URL/lisensi/versi yang ditebak.")
        if any(b["status"] == "ADVISORY_AFFECTED" for b in blocking):
            remedy += " Untuk advisory: pin versi yang sudah di-patch lalu review ulang."
    return {"status": "BLOCKED" if blocking else "OK", "blocking": blocking, "remedy": remedy}


def _alternatives(p: ChangeProposal, has_existing: bool) -> list[dict[str, Any]]:
    compare = p.kind == "COMPARE"
    return [
        {"option": "KEEP_EXISTING", "applicable": True,
         "summary": "Pertahankan konfigurasi saat ini; baseline tetap berlaku.",
         "tradeoffs": "Tanpa biaya migrasi; masalah/tujuan usulan tetap terbuka.",
         "unknowns": ["Besarnya masalah pada baseline belum diukur"] if not has_existing else []},
        {"option": "SMALL_CONFIG_FIX", "applicable": has_existing,
         "summary": "Perbaikan konfigurasi kecil pada komponen yang ada.",
         "tradeoffs": "Risiko rendah; mungkin tidak memenuhi capability baru.",
         "unknowns": [] if has_existing else ["Tidak ada komponen lama untuk dikonfigurasi"]},
        {"option": "ADD", "applicable": p.kind in ("ADD", "COMPARE"),
         "summary": "Tambahkan kandidat berdampingan dengan scope terbatas.",
         "tradeoffs": "Menambah dependency, proses dan permukaan risiko; manfaat harus dibuktikan.",
         "unknowns": ["Manfaat terhadap baseline belum terbukti", "Biaya operasi UNKNOWN"]},
        {"option": "REPLACE", "applicable": has_existing and p.kind in ("REPLACE", "UPGRADE", "COMPARE"),
         "summary": "Ganti komponen lama setelah consumer dipindahkan.",
         "tradeoffs": "Memerlukan migration/consumer tests dan rencana rollback/forward recovery.",
         "unknowns": ["Kompatibilitas consumer", "Rollback data"]},
        {"option": "TRIAL", "applicable": not compare,
         "summary": "Uji terisolasi terbatas (sandbox sekali pakai) sebelum keputusan promotion.",
         "tradeoffs": "Butuh pin/hash kandidat, budget/attempt/time caps dan verifier independen.",
         "unknowns": ["Hasil eksperimen belum ada"] if not compare else
                     ["COMPARE tidak mencakup install; ajukan ADD/REPLACE/UPGRADE untuk trial"]},
        {"option": "REJECT", "applicable": True, "summary": "Tolak; baseline tetap berlaku dan alasan dicatat.",
         "tradeoffs": "Tidak ada perubahan; kandidat tetap tercatat di registry.", "unknowns": []},
    ]


def _dimension(items: list[str], known_source: str | None, unknown_reason: str) -> dict[str, Any]:
    if items:
        return {"status": "KNOWN", "items": items, "source": known_source}
    return {"status": UNKNOWN, "items": [], "source": None, "reason": unknown_reason}


def _related_component(session: Session, cid: str, domain: str | None) -> ComponentReference | None:
    return domain_component(session, cid, domain)


def _route(session: Session, key: str) -> Any:
    from aeos.models.routing import ModelRoute

    return session.scalar(select(ModelRoute).where(ModelRoute.route_key == key))


def _is_ci(comp: ComponentReference | None) -> bool:
    """A CI system from its inventory record (category + name tokens), not a substring of an id."""
    if comp is None:
        return False
    toks = _tokens(f"{comp.component_id} {comp.name}")
    return "CI" in (comp.category or "") and bool(toks & {"ci", "actions", "pipelines", "pipeline"})


def _is_docs(comp: ComponentReference | None) -> bool:
    if comp is None:
        return False
    toks = _tokens(f"{comp.component_id} {comp.name}")
    return bool(toks & {"docs", "confluence", "drive", "publisher", "documentation"})


def _rollback_reference(p: ChangeProposal) -> str | None:
    rp = (p.owner_plan or {}).get("replacement") or {}
    return rp.get("backup_restore_evidence_id") or rp.get("rollback_evidence_id")


def _impact(session: Session, p: ChangeProposal, baseline: list[dict[str, Any]]) -> dict[str, Any]:
    known = [b for b in baseline if b.get("status") == "KNOWN"]
    consumers, data, policy, secrets, egress, ci, docs, ops, pins = [], [], [], [], [], [], [], [], []
    tools, identity, resources, hooks = [], [], [], []
    for b in known:
        for r in b["consumers"]:
            if r["relation"] in CONSUMER_RELATIONS:
                consumers.append(f"{r['source_id']} {r['relation']} {b['component_id']} ({r['lens']})")
            if r["relation"] in DATA_RELATIONS:
                data.append(f"{r['source_id']} {r['relation']} {b['component_id']}")
            if _is_ci(_related_component(session, r["source_id"], p.domain)):
                ci.append(f"{r['source_id']} {r['relation']} {b['component_id']}")
            if r.get("identity_ref"):
                identity.append(f"{r['source_id']} -> {b['component_id']}: identity {r['identity_ref']} (referensi)")
        for r in b["dependencies"]:
            target = _related_component(session, r["target_id"], p.domain)
            if r["relation"] in DATA_RELATIONS:
                data.append(f"{b['component_id']} {r['relation']} {r['target_id']}: {r['data'] or 'data UNKNOWN'}")
            if r["relation"] in POLICY_RELATIONS:
                policy.append(f"{b['component_id']} governed_by {r['target_id']}")
            if r["boundary"]:
                policy.append(f"{b['component_id']} -> {r['target_id']} melalui boundary {r['boundary']}")
            if _is_ci(target):
                ci.append(f"{b['component_id']} {r['relation']} {r['target_id']}")
            if r["relation"] == "publishes_to" or _is_docs(target):
                docs.append(f"{b['component_id']} {r['relation']} {r['target_id']}")
            if r.get("identity_ref"):
                identity.append(f"{b['component_id']} -> {r['target_id']}: identity {r['identity_ref']} (referensi)")
        acc = b.get("access") or {}
        for key in ("credential_ref", "credential_refs", "secrets", "identity_ref"):
            if acc.get(key):
                secrets.append(f"{b['component_id']}: {key} (referensi, tanpa nilai)")
        if acc.get("identity_ref"):
            identity.append(f"{b['component_id']}: identity_ref {acc['identity_ref']} (referensi)")
        if acc.get("egress"):
            egress.append(f"{b['component_id']}: egress {acc.get('egress')}")
        if acc.get("tools") or acc.get("paths"):
            policy.append(f"{b['component_id']}: tools/paths terbatas {acc.get('tools') or ''} {acc.get('paths') or ''}".strip())
        if acc.get("tools") or acc.get("operations"):
            tools.append(f"{b['component_id']}: tools {acc.get('tools') or []} operasi {acc.get('operations') or []}")
        for t in b.get("capability_tests") or []:
            name = t.get("name") or t.get("id") if isinstance(t, dict) else str(t)
            tools.append(f"{b['component_id']}: capability test {name}")
        for key in ("resources", "hardware", "resource"):
            if acc.get(key):
                resources.append(f"{b['component_id']}: {key} {acc.get(key)}")
        for key in ("hooks", "prompts", "memory"):
            if acc.get(key):
                hooks.append(f"{b['component_id']}: {key} {acc.get(key)}")
        route = _route(session, b["component_id"])
        if route is not None:  # a model route as the changed component: its tool set and prompt/context config
            if route.tool_set:
                tools.append(f"{route.route_key}: tool set {route.tool_set}")
            for label, val in (("prompt_revision", route.prompt_revision), ("context_strategy", route.context_strategy)):
                if val:
                    hooks.append(f"{route.route_key}: {label} {val}")
        if b.get("operations"):
            ops.append(f"{b['component_id']}: {', '.join(sorted(b['operations']))}")
        pins.append(f"{b['component_id']}: konfigurasi diterima sebelumnya pin={b['approved_pin']}")
    if isinstance((p.limits or {}).get("resource"), str) and p.limits["resource"] != UNKNOWN:
        resources.append(f"batas resource usulan: {p.limits['resource']}")
    unknown_comp = [b["component_id"] for b in baseline if b.get("status") != "KNOWN"]
    replacing = p.kind in ("REPLACE", "UPGRADE", "REMOVE")
    tested = _rollback_reference(p)
    rollback: dict[str, Any]
    if tested:
        rollback = {"status": "KNOWN", "items": [f"Prosedur rollback/restore teruji: bukti {tested}"],
                    "source": "owner_plan.replacement (bukti terpercaya)", "targets": pins}
    else:
        rollback = {"status": UNKNOWN, "items": [], "source": None, "targets": pins,
                    "reason": "Belum ada prosedur rollback/restore yang teruji; pin lama hanya target rollback, "
                              "bukan bukti bahwa rollback tersedia" + (
                                  ". Rollback kode tidak menjamin rollback data" if replacing else "")}
    src = "inventory relations/access"
    return {
        "contracts": _dimension(consumers, src, "Consumer/kontrak belum terpetakan di inventaris"),
        "schema_api_tools": _dimension(tools, src, "Schema/API/nama tool kandidat belum dianalisis; UNKNOWN"),
        "hooks_prompts_memory": _dimension(hooks, src, "Hooks/prompt/memory yang tersentuh UNKNOWN; perubahan "
                                                       "ini adalah system change yang perlu eval/decision"),
        "identity": _dimension(identity, src, "Identity/credential reference yang terdampak UNKNOWN"),
        "resource_hardware": _dimension(resources, src, "Kebutuhan resource/hardware kandidat UNKNOWN"),
        "state_data_migration": _dimension(data, src, "Relasi data/state belum terpetakan; migration UNKNOWN"
                                           + ("; backup + restore test diperlukan" if replacing else "")),
        "own_auth_cap_enf": _dimension(policy, src, "Policy/OWN-AUTH-CAP-ENF kandidat belum direview"),
        "secrets": _dimension(secrets, src, "Kebutuhan credential kandidat UNKNOWN; tidak ada credential baru dari review"),
        "egress": _dimension(egress, src, "Egress kandidat UNKNOWN; tidak ada egress baru dari review"),
        "ci": _dimension(ci, "inventory relations ke komponen CI", "Dampak CI UNKNOWN (tidak ada relasi ke komponen CI)"),
        "docs": _dimension(docs, "inventory relations ke target dokumentasi",
                           "Target docs/ADR belum terpetakan di inventaris; pembaruan docs dilacak pada hasil (item 10)"),
        "operations": _dimension(ops, src, "Runbook restart/uninstall/recovery kandidat UNKNOWN"),
        "rollback": rollback,
        "unmapped_components": unknown_comp,
    }


def owner_acceptance(p: ChangeProposal) -> list[dict[str, Any]]:
    return [dict(a) for a in (p.owner_plan or {}).get("acceptance") or [] if isinstance(a, dict)]


def measurable_acceptance(p: ChangeProposal) -> list[dict[str, Any]]:
    return [a for a in owner_acceptance(p) if a.get("metric") and a.get("threshold") not in (None, "")]


def _experiment_plan(session: Session, p: ChangeProposal, baseline_hash: str, candidate_hash: str,
                     categories: list[str]) -> dict[str, Any]:
    from aeos.evaluation.definitions import ISOLATION

    if p.kind == "COMPARE":
        return {"type": "REFERENCE_REVIEW", "install": False,
                "note": "COMPARE/RESEARCH: review sumber dan baseline saja; tidak ada install, aktivasi, kenaikan budget "
                        "atau pengiriman data proyek. Trial memerlukan proposal ADD/REPLACE/UPGRADE terpisah.",
                "pins": {"baseline_hash": baseline_hash, "candidate_hash": candidate_hash}}
    suggested: list[str] = []
    for c in categories:
        for s in CATEGORY_SUITES.get(c, []):
            if s not in suggested:
                suggested.append(s)
    existing = {s.suite_key for s in session.scalars(select(EvalSuite))}
    limits = p.limits or {}
    declared = owner_acceptance(p)
    canary = (p.owner_plan or {}).get("canary")
    plan: dict[str, Any] = {
        "type": "CONTROLLED_TRIAL", "install": "hanya di sandbox trial setelah TRIAL_APPROVE",
        "pins": {"baseline_hash": baseline_hash, "candidate_pin": p.candidate_pin or UNKNOWN,
                 "candidate_digest": p.candidate_digest or "NOT_RECORDED",
                 "candidate_hash": candidate_hash,
                 "note": None if p.candidate_pin else "Pin/digest kandidat wajib sebelum TRIAL_APPROVE"},
        "suites": [{"suite_key": s, "available": s in existing} for s in suggested] or UNKNOWN,
        "comparison": "Baseline vs candidate pada tugas representatif dengan verifier/suite dan budget setara; "
                      "negative/edge/scope-escape/recovery cases sesuai impact",
        "isolation": ISOLATION,
        "limits": {k: limits.get(k, UNKNOWN) for k in ("cost_usd", "max_minutes", "max_attempts", "data_class", "resource")},
        "acceptance": [
            "Requirement yang ditetapkan sebelum eksekusi terpenuhi", "Compatibility dengan consumer terbukti",
            "Scope adherence dan privacy/secret behavior lulus", "Correctness, recovery/cancel teruji",
            "Overhead operasional terukur", "Manfaat diukur terhadap accepted outcomes dan oversight, bukan output lebih pendek",
        ],
        "owner_acceptance": declared or UNKNOWN,
        "acceptance_status": ("DECLARED" if measurable_acceptance(p) else
                              "NOT_MEASURABLE" if declared else "NOT_DECLARED"),
        "stop_conditions": ["Budget/attempt/time cap tercapai", "Pelanggaran scope/permission", "Secret terdeteksi pada output",
                            "Gate security/authority/evidence gagal", "Owner membatalkan"],
        "collector_verifier": "Verifier independen (trusted collector); worker tidak menilai hasilnya sendiri",
        "canary": "Hanya jika aman dan reversible; bila tidak, sandbox/staging dan cutover berizin (tanpa dual authority)",
        "canary_plan": canary or None,
    }
    return plan


def replacement_plan(p: ChangeProposal, baseline: list[dict[str, Any]]) -> dict[str, Any] | None:
    """§40 "Rencana penggantian dan rollback" for REPLACE/UPGRADE/REMOVE; owner-declared values (owner_plan)
    replace UNKNOWN; anything not declared stays UNKNOWN."""
    if p.kind not in ("REPLACE", "UPGRADE", "REMOVE"):
        return None
    owner = (p.owner_plan or {}).get("replacement") or {}
    known = [b for b in baseline if b.get("status") == "KNOWN"]
    consumers = sorted({r["source_id"] for b in known for r in b["consumers"]})
    creds: list[str] = []
    for b in known:
        acc = b.get("access") or {}
        refs = acc.get("credential_refs") if isinstance(acc.get("credential_refs"), list) else []
        for ref in [acc.get("credential_ref"), acc.get("identity_ref"), *refs]:
            if ref:
                creds.append(f"{b['component_id']}: {ref}")
    egress = [f"{b['component_id']}: {(b.get('access') or {}).get('egress')}" for b in known
              if (b.get("access") or {}).get("egress")]
    data_state = any(r["relation"] in DATA_RELATIONS for b in known for r in b["consumers"] + b["dependencies"])
    restore = owner.get("backup_restore_evidence_id")
    named_owners = sorted({str(b["owner"]) for b in known if str(b.get("owner") or "").strip()})
    ops_owner = owner.get("operations_owner") or (p.owner_plan or {}).get("operations_owner")
    return {
        "previous_accepted_configuration": [{"component_id": b["component_id"], "approved_pin": b["approved_pin"],
                                             "manifest_hash": b["manifest_hash"]} for b in known] or UNKNOWN,
        "version_schema_data_compatibility": owner.get("version_schema_data_compatibility") or UNKNOWN,
        "backup_and_restore_evidence": (f"Bukti restore teruji: {restore}" if restore else
                                        "Diperlukan sebelum cutover bila ada state/data; belum ada bukti restore"),
        "data_state_relations": data_state,
        "adapter_consumer_changes": consumers or UNKNOWN,
        "credential_egress_changes": ({"credential_refs_to_revoke_after_cutover": creds, "egress": egress}
                                      if creds or egress else UNKNOWN),
        "cutover_order": owner.get("cutover_order") or UNKNOWN,
        "retire_old_version_after": owner.get("retire_old_version_after") or UNKNOWN,
        "coexistence": "Hanya bila aman; tidak menjalankan dua scheduler/vault/credential authority aktif",
        "coexistence_safe": owner.get("coexistence_safe") if owner.get("coexistence_safe") is not None else UNKNOWN,
        "success_stop_criteria": "Acceptance trial + stop conditions pada rencana eksperimen",
        "operations_owner": ([ops_owner] if ops_owner else named_owners) or UNKNOWN,
        "rollback_or_forward_recovery": owner.get("rollback_or_forward_recovery") or UNKNOWN,
        "notes": ["Rollback kode tidak menjamin rollback data; migration irreversible memerlukan keputusan owner",
                  "Credential komponen lama dicabut setelah consumer sah dipindahkan; revoke/uninstall bukan efek review",
                  "Jika berhasil, komponen lama ditandai deprecated/retired, tidak dihapus dari history"],
    }


def removal_plan(p: ChangeProposal, baseline: list[dict[str, Any]]) -> dict[str, Any] | None:
    """§39 REMOVE/RETIRE: dependency, data, identity, job and support items that must be closed. Each item
    is OPEN until the owner records it CLOSED or WAIVED (``owner_plan.removal``)."""
    if p.kind != "REMOVE":
        return None
    closure = (p.owner_plan or {}).get("removal") or {}
    items: list[dict[str, Any]] = []

    def add(item_id: str, kind: str, text: str) -> None:
        if any(i["id"] == item_id for i in items):
            return
        rec = closure.get(item_id) if isinstance(closure.get(item_id), dict) else {}
        items.append({"id": item_id, "kind": kind, "item": text, "status": rec.get("status") or "OPEN",
                      "note": rec.get("note") or "", "evidence_id": rec.get("evidence_id")})

    for b in baseline:
        cid = b["component_id"]
        if b.get("status") != "KNOWN":
            add(f"unknown:{cid}", "unknown", f"{cid} tidak terpetakan di inventaris; dependency/data/identity UNKNOWN")
            continue
        for r in b["consumers"]:
            if r["relation"] in CONSUMER_RELATIONS:
                add(f"consumer:{r['source_id']}:{r['relation']}", "dependency",
                    f"Pindahkan/putuskan consumer {r['source_id']} ({r['relation']} {cid})")
            if r["relation"] in DATA_RELATIONS:
                add(f"data:{r['source_id']}:{cid}", "data", f"Retensi/penghapusan data yang ditulis {r['source_id']} ke {cid}")
        for r in b["dependencies"]:
            if r["relation"] in DATA_RELATIONS:
                add(f"data:{cid}:{r['target_id']}", "data",
                    f"Retensi/penghapusan data {cid} di {r['target_id']}: {r['data'] or 'data UNKNOWN'}")
            if r.get("identity_ref"):
                add(f"identity:{r['identity_ref']}", "identity", f"Cabut identity/credential ref {r['identity_ref']} "
                                                                 "(referensi, tanpa nilai)")
        acc = b.get("access") or {}
        refs = acc.get("credential_refs") if isinstance(acc.get("credential_refs"), list) else []
        for ref in [acc.get("credential_ref"), acc.get("identity_ref"), *refs]:
            if ref:
                add(f"identity:{ref}", "identity", f"Cabut identity/credential ref {ref} (referensi, tanpa nilai)")
        operations = b.get("operations") or {}
        for key in ("uninstall", "retention", "recovery"):
            if operations.get(key):
                add(f"job:{cid}:{key}", "job", f"{key} {cid}: {operations[key]}")
        add(f"support:{cid}", "support", f"Tutup support/runbook {cid} (status {operations.get('support_status', UNKNOWN)})")
    open_items = [i["id"] for i in items if i["status"] not in ("CLOSED", "WAIVED")]
    return {"items": items, "open": open_items, "status": "OPEN" if open_items else "CLOSED",
            "rule": "Semua item harus CLOSED atau WAIVED (keputusan owner) sebelum outcome ACCEPTED."}


def upgrade_delta(p: ChangeProposal, baseline: list[dict[str, Any]], entries: list[ResearchEntry]) -> dict[str, Any] | None:
    """§39 UPGRADE: explicit delta of version/publisher/license/terms/schema for the same component."""
    if p.kind != "UPGRADE":
        return None
    out = []
    for b in baseline:
        if b.get("status") != "KNOWN":
            out.append({"component_id": b["component_id"], "status": UNKNOWN, "reason": b.get("reason")})
            continue
        e = next((x for x in entries if x.component_id == b["component_id"]), entries[0] if entries else None)
        up = (e.upstream or {}) if e else {}
        publisher_to = up.get("owner") or (up.get("observed_identity") if up.get("observed_identity") else None)
        publisher_from = b.get("upstream_identity")
        license_to = up.get("license_spdx") or (e.license if e and e.license else None)
        dep = up.get("dependency_diff") if isinstance(up.get("dependency_diff"), dict) else None
        dep_ok = dep and dep.get("head") == p.candidate_pin

        def pair(a: Any, b2: Any) -> dict[str, Any]:
            a = a if a not in (None, "", UNKNOWN) else UNKNOWN
            b2 = b2 if b2 not in (None, "", UNKNOWN) else UNKNOWN
            return {"from": a, "to": b2, "changed": UNKNOWN if UNKNOWN in (a, b2) else a != b2}

        out.append({
            "component_id": b["component_id"], "status": "KNOWN",
            "version": pair(b.get("approved_pin"), p.candidate_pin),
            "publisher": pair(publisher_from, publisher_to),
            "license": pair(b.get("license"), license_to),
            "terms": {"from": UNKNOWN, "to": (e.data_terms if e and e.data_terms else UNKNOWN),
                      "checked_at": e.terms_checked_at.isoformat() if e and e.terms_checked_at else None},
            "schema": ({"status": "DEPENDENCY_DIFF", "base": dep.get("base"), "head": dep.get("head"),
                        "checked_at": dep.get("checked_at"),
                        "changed_files": [m["path"] for m in dep.get("manifests") or [] if m.get("status") != "UNCHANGED"]}
                       if dep_ok else {"status": UNKNOWN,
                                       "note": "Belum ada contract/dependency diff untuk pin kandidat ini"}),
        })
    return {"components": out, "source": "inventory (baseline) + research registry (kandidat)"}


def overlap_components(session: Session, p: ChangeProposal, entries: list[ResearchEntry]) -> list[dict[str, Any]]:
    """§39 ADD "mengapa komponen yang ada tidak cukup?": existing inventory components of the same kind."""
    if p.kind not in ("ADD", "COMPARE"):
        return []
    kinds: set[str] = set()
    for e in entries:
        kinds |= set(CATEGORY_KINDS.get(e.category, ()))
        kinds.add(_norm_kind(e.category))
    if not kinds:
        return []
    out = []
    for c in session.scalars(select(ComponentReference).order_by(ComponentReference.component_id)):
        if c.tombstoned_at is not None or _norm_kind(c.kind) not in kinds or not component_in_domain(c, p.domain):
            continue
        if c.component_id in (p.candidate_component_ids or []):
            continue
        out.append({"component_id": c.component_id, "name": c.name, "kind": c.kind, "enablement": c.enablement,
                    "admission_status": c.admission_status, "function": c.function or UNKNOWN})
    return out[:25]


def security_privacy(session: Session, p: ChangeProposal, *, data_class: str | None = None) -> dict[str, Any]:
    """§39 "Security/Privacy menilai batas data/akses": code assessment of the candidate's data/access
    boundary. PASS / UNKNOWN / BLOCKED per dimension; a BLOCKED dimension blocks TRIAL/PROMOTE."""
    dims: dict[str, dict[str, Any]] = {}
    dc = (data_class or (p.limits or {}).get("data_class") or "").upper()
    office = p.domain == Domain.OFFICE.value
    office_pol: dict[str, Any] = {}
    if office:
        try:
            from aeos.policy import facade as policy

            office_pol = policy.office_policy(session) or {}
        except Exception:
            office_pol = {}
    if dc not in DATA_CLASSES:
        dims["data_class"] = {"status": UNKNOWN, "note": "Kelas data trial belum dinyatakan"}
    elif dc == "RESTRICTED":
        dims["data_class"] = {"status": "BLOCKED", "note": "Data RESTRICTED tidak boleh masuk trial kandidat"}
    elif dc == "CONFIDENTIAL":
        dims["data_class"] = {"status": UNKNOWN,
                              "note": "CONFIDENTIAL hanya lewat route yang data terms-nya mengizinkan; cek saat admission"}
    else:
        dims["data_class"] = {"status": "PASS", "note": f"Kelas data {dc}"}
    if office:
        allowed = office_pol.get("external_worker_allowed") is True
        dims["office_eligibility"] = {"status": "PASS" if allowed else "BLOCKED",
                                      "note": "OFFICE default deny: external_worker_allowed=false sampai ada otorisasi "
                                              "perusahaan" if not allowed else "Otorisasi OFFICE tercatat"}
    else:
        dims["office_eligibility"] = {"status": "NOT_APPLICABLE", "note": "Proposal PERSONAL"}
    cands = [c for c in (domain_component(session, cid, p.domain) for cid in p.candidate_component_ids or []) if c]
    if not cands:
        dims["egress"] = {"status": UNKNOWN, "note": "Kandidat belum terdaftar di inventaris; egress UNKNOWN"}
        dims["credentials"] = {"status": UNKNOWN, "note": "Kandidat belum terdaftar; kebutuhan credential UNKNOWN"}
    else:
        hosts = [str(h) for c in cands for h in ((c.access or {}).get("egress") or [])]
        wild = [h for h in hosts if "*" in h or h.strip().lower() in ("any", "all", "internet")]
        dims["egress"] = ({"status": "BLOCKED", "note": f"Egress tak terbatas: {wild}"} if wild else
                          {"status": "PASS", "items": hosts, "note": "Egress eksplisit dari inventaris"} if hosts else
                          {"status": "PASS", "items": [], "note": "Tidak ada egress tercatat (default deny)"})
        refs: list[str] = []
        for c in cands:
            acc = c.access or {}
            more = acc.get("credential_refs") if isinstance(acc.get("credential_refs"), list) else []
            refs += [str(r) for r in [acc.get("credential_ref"), acc.get("identity_ref"), *more] if r]
        try:
            from aeos.core.redaction import redact_text

            leaked = [r for r in refs if redact_text(r) != r]
        except Exception:
            leaked = []
        dims["credentials"] = ({"status": "BLOCKED", "note": "Nilai menyerupai secret pada access record"} if leaked else
                               {"status": "PASS", "items": refs, "note": "Hanya referensi credential (tanpa nilai)"})
    entries = research_entries(session, p)
    if not entries:
        dims["terms"] = {"status": UNKNOWN, "note": "Tidak ada entri registry; data/terms UNKNOWN"}
        dims["advisories"] = {"status": UNKNOWN, "note": "Tidak ada entri registry; advisory UNKNOWN"}
    else:
        missing = [e.name for e in entries if not (e.data_terms or "").strip()]
        dims["terms"] = ({"status": UNKNOWN, "note": f"Data terms belum tercatat: {', '.join(missing)}"} if missing else
                         {"status": "PASS", "note": "Data terms tercatat di registry"})
        advs = [_advisory(e, p.candidate_pin) for e in entries]
        if any(a["status"] == "AFFECTED" for a in advs):
            dims["advisories"] = {"status": "BLOCKED", "note": "Versi kandidat berada dalam rentang advisory rentan",
                                  "items": sorted({i for a in advs for i in a["affected"]})}
        elif all(a["status"] in ("NONE", "NOT_AFFECTED") for a in advs):
            dims["advisories"] = {"status": "PASS", "note": "Advisory dicek; tidak mencakup versi kandidat"}
        else:
            dims["advisories"] = {"status": UNKNOWN, "note": "Advisory belum dicek atau rentang tidak dapat dievaluasi"}
    statuses = [d["status"] for d in dims.values()]
    overall = "BLOCKED" if "BLOCKED" in statuses else UNKNOWN if UNKNOWN in statuses else "PASS"
    return {"status": overall, "dimensions": dims, "performed_by": "kode (inventaris + registry + policy OFFICE)",
            "rule": "BLOCKED memblokir TRIAL/PROMOTE; UNKNOWN tetap terlihat dan bukan PASS."}


def review_contributors(package: dict[str, Any]) -> list[dict[str, str]]:
    """§39 "Pembagian pekerjaan review": roles are activated as needed; most work is code, not LLM."""
    claude = package.get("claude_review", {}).get("status", "NOT_REQUESTED")
    sp = (package.get("security_privacy") or {}).get("status", UNKNOWN)
    return [
        {"role": "Research / Supply-chain Analyst",
         "performed_by": "kode: registry riset + Check Upstream + advisory + dependency diff (metadata publik)"},
        {"role": "Integration / Architecture specialist", "performed_by": "kode: relasi & consumer dari inventaris"},
        {"role": "Security / Privacy",
         "performed_by": f"kode: batas data/akses/egress/credential/advisory kandidat — status {sp}; "
                         "reviewer manusia dapat ditambahkan"},
        {"role": "FinOps", "performed_by": "kode: ledger 30 hari (ACTUAL) + harga ModelRoute (ESTIMATE); UNKNOWN bila tidak ada data"},
        {"role": "Model Evaluation", "performed_by": "kode: saran suite; hasil hanya dari EvalRun tercatat"},
        {"role": "Claude tertinggi (checkpoint)", "performed_by": f"opsional pra-trial — status {claude}; checkpoint bukti "
                                                                 "wajib sebelum PROMOTE; findings bukan approval"},
        {"role": "Owner", "performed_by": "manusia: tujuan, biaya, residual risk dan target (keputusan terikat hash)"},
    ]


def _ledger_actual(session: Session, component_ids: list[str], domain: str | None) -> dict[str, Any] | None:
    from aeos.models.cost import UsageLedgerEntry

    if not component_ids:
        return None
    since = utcnow() - LEDGER_WINDOW
    rows = list(session.scalars(select(UsageLedgerEntry).where(
        UsageLedgerEntry.at >= since, UsageLedgerEntry.mode == "LIVE",
        or_(UsageLedgerEntry.provider.in_(component_ids), UsageLedgerEntry.route_key.in_(component_ids)),
        or_(UsageLedgerEntry.domain.is_(None), UsageLedgerEntry.domain == domain))))
    if not rows:
        return None
    by_cat: dict[str, list[Any]] = {}
    for r in rows:
        by_cat.setdefault(r.category, []).append(r)
    return {"rows": rows, "by_category": by_cat}


def _actual_item(rows: list[Any], label: str) -> dict[str, Any]:
    known = [float(r.cost_usd) for r in rows if r.cost_usd is not None]
    unknown = len(rows) - len(known)
    qty = [float(r.quantity) for r in rows if r.quantity is not None]
    tokens = sum(int(r.input_tokens or 0) + int(r.output_tokens or 0) for r in rows)
    item: dict[str, Any] = {"value": round(sum(known), 6) if known else None, "basis": "ACTUAL",
                            "note": f"{label}: {len(rows)} entri ledger LIVE 30 hari"
                                    + (f"; {unknown} entri tanpa biaya (UNKNOWN) tidak dijumlahkan" if unknown else ""),
                            "entries": len(rows), "unknown_entries": unknown, "source": "usage_ledger (30 hari)"}
    if qty:
        item["quantity"] = round(sum(qty), 3)
        item["unit"] = next((r.unit for r in rows if r.unit), None)
    if tokens:
        item["quantity"] = tokens
        item["unit"] = "tokens"
    return item


def _route_estimate(session: Session, p: ChangeProposal) -> dict[str, Any] | None:
    from aeos.models.routing import ModelRoute

    keys = sorted(set(p.candidate_component_ids or []))
    if not keys:
        return None
    routes = list(session.scalars(select(ModelRoute).where(ModelRoute.route_key.in_(keys))))
    if not routes:
        return None
    prices = []
    for r in routes:
        pr = r.pricing or {}
        if r.is_free == "YES":
            prices.append(f"{r.route_key}: gratis (is_free=YES"
                          + (f", sampai {r.free_until.isoformat()}" if r.free_until else "") + ")")
        elif "input_per_mtok" in pr or "output_per_mtok" in pr:
            prices.append(f"{r.route_key}: input {pr.get('input_per_mtok')} / output {pr.get('output_per_mtok')} USD per 1M token")
        elif "prompt" in pr or "completion" in pr:
            prices.append(f"{r.route_key}: prompt {pr.get('prompt')} / completion {pr.get('completion')} USD per token")
    if not prices:
        return None
    free = all(r.is_free == "YES" for r in routes)
    checked = [r.pricing_checked_at.isoformat() for r in routes if r.pricing_checked_at]
    return {"value": 0.0 if free else None, "basis": "ESTIMATE",
            "note": "Harga per unit dari ModelRoute.pricing (" + "; ".join(prices) + "); total bergantung volume trial",
            "source": "model_routes.pricing", "pricing_checked_at": max(checked) if checked else None}


def _cost(session: Session, p: ChangeProposal, baseline: list[dict[str, Any]]) -> dict[str, Any]:
    limits = p.limits or {}
    known_costs = {b["component_id"]: b.get("cost") for b in baseline if b.get("status") == "KNOWN" and b.get("cost")}

    def unknown(note: str) -> dict[str, Any]:
        return {"value": None, "basis": UNKNOWN, "note": note}

    cash: dict[str, Any] = _route_estimate(session, p) or unknown("Belum ada pricing/ledger untuk kandidat")
    if cash["basis"] == UNKNOWN and isinstance(limits.get("cost_usd"), (int, float)):
        cash = {"value": None, "basis": UNKNOWN, "cap_usd": limits["cost_usd"],
                "note": "Batas yang dinyatakan pengusul; bukan estimasi biaya"}
    actual = _ledger_actual(session, sorted(set(p.existing_component_ids or [])), p.domain)
    by_cat = actual["by_category"] if actual else {}
    spend_rows = [r for cat, rows in by_cat.items() if cat not in ("COMPUTE", "CI", "STORAGE", "HUMAN_TIME") for r in rows]

    def cat_item(cats: tuple[str, ...], label: str, note: str) -> dict[str, Any]:
        rows = [r for c in cats for r in by_cat.get(c, [])]
        return _actual_item(rows, label) if rows else unknown(note)

    return {
        "cash": cash,
        "baseline_cash_30d": _actual_item(spend_rows, "Pemakaian baseline") if spend_rows else
        unknown("Belum ada pemakaian baseline tercatat di ledger (30 hari)"),
        "compute": cat_item(("COMPUTE",), "Compute baseline", "Compute kandidat belum diukur"),
        "ci": cat_item(("CI",), "Menit CI baseline", "Menit CI belum diukur"),
        "storage": cat_item(("STORAGE",), "Storage baseline", "Storage belum diukur"),
        "tokens": _actual_item(spend_rows, "Token baseline") if any((r.input_tokens or r.output_tokens) for r in spend_rows)
        else unknown("Token/context belum diukur"),
        "oversight": unknown("Konsumsi review Claude belum diukur (bukan dolar)"),
        "operations": cat_item(("HUMAN_TIME",), "Waktu operator", "Usaha operasi belum diperkirakan"),
        "migration": unknown("Usaha migrasi belum diperkirakan") if p.kind in ("REPLACE", "UPGRADE", "REMOVE")
        else {"value": None, "basis": "NOT_APPLICABLE", "note": "Tidak ada migrasi untuk ADD/COMPARE"},
        "baseline_cost_records": known_costs or UNKNOWN,
        "rule": "Actual/estimate/UNKNOWN dibedakan; persentase penghematan hanya dengan metode dan baseline terukur.",
    }


def allowed_actions(p: ChangeProposal) -> list[str]:
    if p.status in ("ACCEPTED", "ROLLED_BACK", "FAILED", "PARTIAL") or p.parent_id:
        return []
    if not p.review_package:
        return []
    base = ["KEEP", "REJECT", "DEFER", "REQUEST_MORE_EVIDENCE", "REFERENCE_ONLY"]
    if p.kind == "COMPARE":
        return base
    if p.status in ("REVIEWED",):
        return ["TRIAL_APPROVE", *base]
    if p.status in ("TRIAL_APPROVED", "IN_TRIAL", "PROMOTION_PENDING"):
        return ["PROMOTE", *base]
    return base


SCOPE_FIELDS = ["target", "environment", "provider", "data_class", "budget_usd", "max_minutes", "max_attempts",
                "stop_conditions", "rollback_scope"]


def _human_decision(p: ChangeProposal, baseline_hash: str, candidate_hash: str,
                    identity_gate: dict[str, Any], sp: dict[str, Any]) -> dict[str, Any]:
    blocked = identity_gate["status"] == "BLOCKED" and p.kind != "COMPARE"
    blocked_note = ([f"Identitas sumber belum terverifikasi ({', '.join(b['identity'] for b in identity_gate['blocking'])}): "
                     "TRIAL/PROMOTE diblokir. " + str(identity_gate["remedy"])] if blocked else [])
    nexts = (["Pin candidate (versi/digest) lalu review ulang"] if not p.candidate_pin and p.kind != "COMPARE" else [])
    if p.kind != "COMPARE" and not measurable_acceptance(p):
        nexts.append("Tetapkan acceptance terukur (metrik + ambang) di Rencana pemilik sebelum TRIAL_APPROVE")
    if p.kind != "COMPARE" and (not (p.problem or "").strip() or not (p.expected_benefit or "").strip()):
        nexts.append("Isi masalah dan manfaat yang diharapkan; tanpa itu manfaat tidak dapat diuji")
    if p.kind != "COMPARE" and sp.get("status") == "BLOCKED":
        nexts.append("Security/Privacy BLOCKED: perbaiki batas data/akses kandidat sebelum TRIAL")
    return {
        "required": True,
        "bind": {"baseline_hash": baseline_hash, "candidate_hash": candidate_hash},
        "scope_fields": list(SCOPE_FIELDS),
        "options": {
            "TRIAL_APPROVE": "Izin uji terisolasi dalam scope; BUKAN promotion, tidak mengubah baseline aktif",
            "PROMOTE": "Promotion kandidat; keputusan terpisah dari trial, memerlukan bukti trial terpercaya",
            "KEEP": "Pertahankan baseline", "REJECT": "Tolak kandidat", "DEFER": "Tunda",
            "REQUEST_MORE_EVIDENCE": "Minta bukti tambahan", "REFERENCE_ONLY": "Simpan sebagai referensi/pola saja",
        },
        "allowed_now": allowed_actions(p),
        "rules": [
            "Keputusan terikat baseline_hash + candidate_hash (+ provider/data class/budget/target)",
            "Perubahan hash/target/scope membatalkan binding; keputusan harus diulang",
            "TRIAL hanya di environment sandbox/staging (canary hanya dengan rencana canary); wajib batas waktu, "
            "attempt, stop conditions dan rollback scope",
            "Budget tidak naik dari keputusan ini; kenaikan budget memerlukan keputusan BUDGET terpisah",
            "Pengusul/implementer/model tidak dapat memberi approval",
        ],
        "source_identity_gate": identity_gate,
        "next_actions": nexts + blocked_note + ["Pilih keputusan dan scope; sistem merekam Decision terikat hash"],
    }


def _outcome_placeholder() -> dict[str, Any]:
    return {"status": "PENDING", "evidence": [], "accepted_configuration": None, "partial_failures": [],
            "rollback": None, "inventory_docs_update": "PENDING",
            "note": "Diisi setelah implementasi: ACCEPTED / ROLLED_BACK / FAILED / PARTIAL; rollback bukan acceptance."}


# --------------------------------------------------------------------------- Claude (optional)


def _public_metadata(p: ChangeProposal, package: dict[str, Any]) -> dict[str, Any]:
    """Only generic public metadata: no problem text, no attachments, no project names/data."""
    public = [s for s in package["source_identity"] if not s["identity"].startswith("name:")]
    return {
        "proposal_kind": p.kind,
        "public_sources": [s["identity"] for s in public],
        # Free-text names (name:...) may be internal project/tool names: never sent (§39 public research).
        "source_status": [{"identity": s["identity"], "status": s["status"], "release": s["release_or_commit"],
                           "license": s["license"]} for s in public],
        "unnamed_sources": len(package["source_identity"]) - len(public),
        "baseline_components": [{"component_id": b["component_id"], "name": b.get("name"), "approved_pin": b.get("approved_pin"),
                                 "status": b.get("status")} for b in package["baseline"]["components"]],
        "impact_status": {k: v["status"] for k, v in package["impact"].items() if isinstance(v, dict) and "status" in v},
        "experiment_type": package["experiment_plan"].get("type"),
    }


def claude_review(p: ChangeProposal, package: dict[str, Any], *, domain: str | None) -> dict[str, Any]:
    label = "Rekomendasi Claude adalah DATA review; findings bukan persetujuan pemilik"
    if domain == Domain.OFFICE.value:
        return {"status": "NOT_RUN", "label": label,
                "reason": "OFFICE: metadata usulan tidak dikirim ke Claude tanpa otorisasi data/provider kantor (§23)"}
    meta = _public_metadata(p, package)
    meta_hash = content_hash(meta)
    prompt = (
        "You review a proposed change to the DLY AEOS control plane. Use ONLY the JSON metadata below. "
        "Return REVIEW / VERIFY / VERDICT. Claims without evidence are UNKNOWN. You cannot approve anything; "
        "your output is advisory data for the human owner.\n\n" + json.dumps(meta, ensure_ascii=True)
    )
    try:
        from aeos.claude_bridge.facade import ClaudeRequest, run_structured

        res = run_structured(ClaudeRequest(purpose="review.proposal", role="proposal-reviewer", model=REVIEW_MODEL,
                                           prompt=prompt, json_schema=CLAUDE_SCHEMA, project_id=None, domain=domain,
                                           timeout_seconds=600, allowed_tools=[]))
    except (ImportError, ModuleNotFoundError):
        return {"status": "NOT_RUN", "label": label, "sent_metadata": sorted(meta), "metadata_hash": meta_hash,
                "reason": "ClaudeNativeBridge belum tersedia; gunakan paket manual Claude Code resmi (tanpa Claude API)"}
    except AeosError as exc:
        return {"status": "NOT_RUN", "label": label, "metadata_hash": meta_hash, "reason": f"{exc.code}: {exc.message}"}
    base = {"label": label, "model_requested": res.model_requested, "model_resolved": res.model_resolved,
            "mode": res.mode, "sent_metadata": sorted(meta), "metadata_hash": meta_hash, "at": utcnow().isoformat()}
    if not res.ok or not isinstance(res.structured, dict):
        return base | {"status": "QUOTA_WAIT" if res.quota_exhausted else "FAILED",
                       "reason": res.error_code or res.error or "no structured result"}
    import jsonschema

    try:
        jsonschema.validate(res.structured, CLAUDE_SCHEMA)
    except jsonschema.ValidationError as exc:
        return base | {"status": "FAILED", "reason": f"schema mismatch: {exc.message[:200]}"}
    s = res.structured
    return base | {"status": "COMPLETED", "review": s["review"], "verify": s["verify"], "verdict": s["verdict"],
                   "recommendation": s["recommendation"], "rationale": s["rationale"]}


# --------------------------------------------------------------------------- package


def _carry_claude(previous: dict[str, Any] | None, meta_hash: str) -> dict[str, Any] | None:
    """§41/§42: a completed Claude review is reused while the public metadata it judged is unchanged
    (same hash); once that input changes the old result is shown as STALE, never as current."""
    if previous and previous.get("status") == "STALE":
        return previous  # stays STALE until a new Claude review replaces it
    if not previous or previous.get("status") != "COMPLETED":
        return None
    if previous.get("metadata_hash") == meta_hash:
        return {**previous, "reused": True,
                "reuse_note": "Dipakai ulang: metadata publik (hash) sama dengan review Claude sebelumnya; "
                              "tidak ada panggilan Claude baru."}
    return {"status": "STALE", "label": previous.get("label"), "previous_at": previous.get("at"),
            "previous_verdict": previous.get("verdict"), "metadata_hash": previous.get("metadata_hash"),
            "reason": "Metadata publik berubah sejak review Claude terakhir; hasil lama tidak berlaku. "
                      "Minta rekomendasi ulang bila diperlukan."}


def build_package(session: Session, p: ChangeProposal, children: list[ChangeProposal], *, with_claude: bool,
                  domain: str | None, previous_claude: dict[str, Any] | None = None) -> tuple[dict[str, Any], str, str]:
    baseline, baseline_hash, extra = compute_baseline(session, p)
    _cand, candidate_hash = compute_candidate(session, p, children)
    entries = research_entries(session, p)
    categories = sorted({e.category for e in entries})
    relevance = [{"name": e.name, "placement": e.placement, "relevance": e.relevance, "lifecycle": e.lifecycle}
                 for e in entries]
    impact = _impact(session, p, baseline)
    sp = security_privacy(session, p)
    package: dict[str, Any] = {
        "summary": {
            "kind": p.kind, "decision_question": KIND_QUESTIONS[p.kind], "problem": p.problem or UNKNOWN,
            "expected_benefit": p.expected_benefit or UNKNOWN, "relevance": relevance or UNKNOWN,
            "plain_language": plain_language(p, baseline, entries, impact),
            "attachments": [{"attachment_id": a, "note": "lampiran adalah DATA tidak tepercaya"} for a in p.attachment_ids or []],
        },
        "source_identity": _source_identity(session, p),
        "baseline": {"components": baseline, "baseline_hash": baseline_hash, **extra,
                     "overlap": overlap_components(session, p, entries),
                     "note": None if baseline else "Tidak ada komponen lama (ADD/COMPARE); baseline = konfigurasi berlaku"},
        "alternatives": _alternatives(p, bool(p.existing_component_ids)),
        "impact": impact,
        "experiment_plan": _experiment_plan(session, p, baseline_hash, candidate_hash, categories),
        "replacement_plan": replacement_plan(p, baseline),
        "removal_plan": removal_plan(p, baseline),
        "upgrade_delta": upgrade_delta(p, baseline, entries),
        "security_privacy": sp,
        "cost": _cost(session, p, baseline),
        "claude_review": {"status": "NOT_REQUESTED",
                          "label": "Rekomendasi Claude adalah DATA review; findings bukan persetujuan pemilik"},
        "human_decision": _human_decision(p, baseline_hash, candidate_hash, source_identity_gate(session, p), sp),
        "outcome": (p.outcome or {}).get("current") or _outcome_placeholder(),
        "parts": [{"child_id": c.id, "title": c.title, "kind": c.kind} for c in children],
        "unknown_fields": p.unknown_fields or [],
        "generated_at": utcnow().isoformat(),
        "generated_by": "deterministic review builder (kode, bukan LLM)",
    }
    carried = None if domain == Domain.OFFICE.value else _carry_claude(previous_claude,
                                                                        content_hash(_public_metadata(p, package)))
    if carried is not None and carried["status"] == "COMPLETED":
        package["claude_review"] = carried
    elif with_claude:
        package["claude_review"] = claude_review(p, package, domain=domain)
    elif carried is not None:
        package["claude_review"] = carried  # STALE: shown as history, never as the current recommendation
    package["review_contributors"] = review_contributors(package)
    return package, baseline_hash, candidate_hash


def _no_unmeasured_percent(text: str) -> str:
    """§39: percentages are shown only with a measurement method and baseline. Registry text is a claim,
    so any percentage in it is masked in the plain-language report."""
    return _PERCENT.sub("[persentase klaim disembunyikan: belum diukur]", text)


def plain_language(p: ChangeProposal, baseline: list[dict[str, Any]], entries: list[ResearchEntry],
                   impact: dict[str, Any] | None = None) -> list[str]:
    """Candidate-specific trade-offs in plain Indonesian ("menambah X, tetapi Y")."""
    out: list[str] = []
    impact = impact or {}
    costs: list[str] = []
    if p.kind == "ADD":
        costs.append("menambah dependency dan proses baru")
    if p.kind in ("REPLACE", "UPGRADE"):
        n = len((impact.get("contracts") or {}).get("items") or [])
        costs.append(f"{n} consumer harus diuji/dipindahkan" if n else "consumer yang terdampak belum terpetakan")
    if (impact.get("egress") or {}).get("status") == "KNOWN":
        costs.append("menyentuh egress yang tercatat")
    if (impact.get("secrets") or {}).get("status") == "KNOWN":
        costs.append("memerlukan credential reference")
    for e in entries:
        gain = (e.relevance or e.placement or "").strip()
        if gain:
            line = f"{e.name}: {gain[:220]}"
            if costs:
                line += f" — tetapi {', '.join(costs)}"
            out.append(_no_unmeasured_percent(line))
        if (e.limitations or "").strip():
            out.append(_no_unmeasured_percent(f"Batasan {e.name}: {e.limitations.strip()[:220]}"))
    if p.kind in ("ADD", "REPLACE", "UPGRADE"):
        out.append("Manfaat terhadap baseline belum terbukti sampai trial terkendali; belum terbukti mengurangi "
                   "kebutuhan review Claude.")
    if p.kind == "REMOVE":
        out.append("Menutup dependency/data/identity/job; histori dan bukti tetap disimpan.")
    if p.kind == "COMPARE":
        out.append("Hanya review/perbandingan: tidak ada instalasi, aktivasi, kenaikan budget atau pengiriman data proyek.")
    if any(b.get("status") != "KNOWN" for b in baseline):
        out.append("Sebagian komponen lama belum terpetakan di inventaris; dampaknya UNKNOWN.")
    if any(e.identity_status in ("UNVERIFIED", "CHANGED", "NOT_FOUND") for e in entries) or not entries:
        out.append("Identitas sumber belum terverifikasi; admission diblokir sampai diverifikasi.")
    out.append("Persentase penghematan/keunggulan tidak ditampilkan tanpa metode pengukuran dan baseline.")
    return out


_plain_language = plain_language  # kept for existing importers


def review_hash(package: dict[str, Any]) -> str:
    stable = {k: v for k, v in package.items()
              if k not in ("generated_at", "claude_review", "human_decision", "review_contributors")}
    return content_hash(stable)
