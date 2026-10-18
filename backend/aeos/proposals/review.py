"""§39 review package: the 10 items a human receives, computed deterministically from records.

Baseline/impact come from the inventory (ComponentReference + typed relations + observations);
source identity from the research registry; anything not derivable is UNKNOWN — never guessed.
The optional Claude recommendation (item 8) receives only generic public metadata and is DATA,
never owner approval (§19, §39).
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.enums import Domain
from aeos.core.errors import AeosError
from aeos.core.hashing import content_hash
from aeos.core.timeutil import utcnow
from aeos.models.decisions import ArchitectureSnapshot
from aeos.models.inventory import ComponentObservation, ComponentReference, ComponentRelation
from aeos.models.research import ChangeProposal, EvalSuite, ResearchEntry

UNKNOWN = "UNKNOWN"
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


# --------------------------------------------------------------------------- baseline / candidate


def _relations(session: Session, component_id: str) -> tuple[list[ComponentRelation], list[ComponentRelation]]:
    inbound = list(session.scalars(select(ComponentRelation).where(ComponentRelation.target_id == component_id)))
    outbound = list(session.scalars(select(ComponentRelation).where(ComponentRelation.source_id == component_id)))
    return inbound, outbound


def _latest_observations(session: Session, component_id: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    rows = session.scalars(select(ComponentObservation).where(ComponentObservation.component_id == component_id)
                           .order_by(ComponentObservation.observed_at.desc()))
    for o in rows:
        if o.dimension in out:
            continue
        out[o.dimension] = {"status": o.status, "observed_version": o.observed_version, "observer": o.observer,
                            "observed_at": o.observed_at.isoformat() if o.observed_at else None}
    return out


def _rel_view(r: ComponentRelation) -> dict[str, Any]:
    return {"source_id": r.source_id, "target_id": r.target_id, "relation": r.relation, "lens": r.lens,
            "provenance": r.provenance, "data": r.data_description, "boundary": r.boundary}


def component_baseline(session: Session, component_id: str) -> dict[str, Any]:
    try:
        from aeos.inventory import facade as inventory

        comp: ComponentReference | None = inventory.get_component(session, component_id)
    except (ImportError, ModuleNotFoundError):  # pragma: no cover - facade ships with the foundation
        comp = None
    if comp is None:
        return {"component_id": component_id, "status": UNKNOWN,
                "reason": "komponen tidak ditemukan di inventaris; baseline UNKNOWN"}
    inbound, outbound = _relations(session, component_id)
    obs = _latest_observations(session, component_id)
    return {
        "component_id": comp.component_id, "status": "KNOWN", "name": comp.name, "friendly_name": comp.friendly_name,
        "kind": comp.kind, "function": comp.function or UNKNOWN, "usage_role": comp.usage_role,
        "requirement": comp.requirement, "design_label": comp.design_label, "approved_pin": comp.approved_pin or UNKNOWN,
        "manifest_hash": comp.manifest_hash or UNKNOWN, "admission_status": comp.admission_status,
        "enablement": comp.enablement, "lifecycle_final": comp.lifecycle_final, "source_url": comp.source_url,
        "upstream_identity": comp.upstream_identity, "identity_verified": comp.identity_verified, "owner": comp.owner,
        "license": comp.license or UNKNOWN, "access": comp.access or {}, "cost": comp.cost or {},
        "operations": comp.operations or {}, "decision_refs": comp.decision_refs or [], "mode": comp.mode,
        "consumers": [_rel_view(r) for r in inbound], "dependencies": [_rel_view(r) for r in outbound],
        "observations": obs,
        "evidence_note": ("Observasi terakhir per dimensi (§38); desain/katalog bukan bukti instalasi."
                          if obs else "Belum ada observasi; status runtime UNKNOWN."),
    }


def _baseline_fingerprint(b: dict[str, Any]) -> dict[str, Any]:
    if b.get("status") != "KNOWN":
        return {"component_id": b["component_id"], "status": b.get("status")}
    rels = sorted((r["source_id"], r["target_id"], r["relation"], r["lens"]) for r in b["consumers"] + b["dependencies"]
                  if r["lens"] in ("DESIGN", "CONFIG"))
    observed = {d: (b["observations"].get(d) or {}).get("observed_version") for d in ("installation", "configuration")}
    return {k: b.get(k) for k in ("component_id", "approved_pin", "manifest_hash", "admission_status", "enablement",
                                  "lifecycle_final", "design_label", "access", "requirement")} | {
        "relations": rels, "observed_versions": observed}


def compute_baseline(session: Session, p: ChangeProposal) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    comps = [component_baseline(session, cid) for cid in sorted(set(p.existing_component_ids or []))]
    fingerprint: dict[str, Any] = {"kind": p.kind, "components": [_baseline_fingerprint(b) for b in comps]}
    snapshot = None
    if not comps:
        snap = session.scalar(select(ArchitectureSnapshot).order_by(ArchitectureSnapshot.created_at.desc()))
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
        "parts": sorted((c.id, c.kind, tuple(sorted(c.canonical_identities or [])), c.candidate_pin or UNKNOWN)
                        for c in children),
    }
    return cand, content_hash(cand)


# --------------------------------------------------------------------------- items


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
        out.append({
            "identity": ident, "status": status, "research_entry_id": e.id if e else None,
            "research_lifecycle": e.lifecycle if e else None, "evidence_level": e.evidence_level if e else "E0",
            "official_repo": up.get("html_url") or ((e.urls or [None])[0] if e else None) or UNKNOWN,
            "release_or_commit": up.get("latest_tag") or UNKNOWN,
            "latest_release": up.get("latest_release") or UNKNOWN,
            "maintainer": up.get("owner") or (e.maintainer if e and e.maintainer else UNKNOWN),
            "license": up.get("license_spdx") or (e.license if e and e.license else UNKNOWN),
            "distribution": UNKNOWN, "terms": e.data_terms if e and e.data_terms else UNKNOWN,
            "checked_at": e.checked_at.isoformat() if e and e.checked_at else None,
            "research_date": e.research_date if e else None,
            "flags": [f for f, on in (("IDENTITY_CHANGED", status == "CHANGED"), ("ARCHIVED", bool(up.get("archived"))),
                                      ("FORK", bool(up.get("fork"))), ("NOT_FOUND", status == "NOT_FOUND"),
                                      ("NOT_CHECKED", not (e and e.checked_at))) if on],
            "note": ("Trust tidak diwariskan dari identitas lama." if status == "CHANGED" else
                     "Dinyatakan pada master (riset 17-18 Okt 2026), belum dicek live." if status == "STATED_BY_MASTER"
                     else ""),
        })
    return out


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


def _impact(p: ChangeProposal, baseline: list[dict[str, Any]]) -> dict[str, Any]:
    known = [b for b in baseline if b.get("status") == "KNOWN"]
    consumers, data, policy, secrets, egress, ci, ops, pins = [], [], [], [], [], [], [], []
    for b in known:
        for r in b["consumers"]:
            if r["relation"] in CONSUMER_RELATIONS:
                consumers.append(f"{r['source_id']} {r['relation']} {b['component_id']} ({r['lens']})")
            if r["relation"] in DATA_RELATIONS:
                data.append(f"{r['source_id']} {r['relation']} {b['component_id']}")
        for r in b["dependencies"]:
            if r["relation"] in DATA_RELATIONS:
                data.append(f"{b['component_id']} {r['relation']} {r['target_id']}: {r['data'] or 'data UNKNOWN'}")
            if r["relation"] in POLICY_RELATIONS:
                policy.append(f"{b['component_id']} governed_by {r['target_id']}")
            if r["boundary"]:
                policy.append(f"{b['component_id']} -> {r['target_id']} melalui boundary {r['boundary']}")
            if "ci" in r["target_id"].lower():
                ci.append(f"{b['component_id']} {r['relation']} {r['target_id']}")
        acc = b.get("access") or {}
        for key in ("credential_ref", "credential_refs", "secrets", "identity_ref"):
            if acc.get(key):
                secrets.append(f"{b['component_id']}: {key} (referensi, tanpa nilai)")
        if acc.get("egress"):
            egress.append(f"{b['component_id']}: egress {acc.get('egress')}")
        if acc.get("tools") or acc.get("paths"):
            policy.append(f"{b['component_id']}: tools/paths terbatas {acc.get('tools') or ''} {acc.get('paths') or ''}".strip())
        if b.get("operations"):
            ops.append(f"{b['component_id']}: {', '.join(sorted(b['operations']))}")
        pins.append(f"{b['component_id']}: konfigurasi diterima sebelumnya pin={b['approved_pin']}")
    unknown_comp = [b["component_id"] for b in baseline if b.get("status") != "KNOWN"]
    replacing = p.kind in ("REPLACE", "UPGRADE", "REMOVE")
    rollback_items = list(pins)
    if replacing:
        rollback_items.append("Rollback kode tidak menjamin rollback data; status data rollback harus dinyatakan")
    if p.kind == "ADD":
        rollback_items.append("Nonaktifkan/lepas kandidat; credential kandidat dicabut setelah consumer dipindahkan")
    src = "inventory relations/access"
    return {
        "contracts": _dimension(consumers, src, "Consumer/kontrak belum terpetakan di inventaris"),
        "state_data_migration": _dimension(data, src, "Relasi data/state belum terpetakan; migration UNKNOWN"
                                           + ("; backup + restore test diperlukan" if replacing else "")),
        "own_auth_cap_enf": _dimension(policy, src, "Policy/OWN-AUTH-CAP-ENF kandidat belum direview"),
        "secrets": _dimension(secrets, src, "Kebutuhan credential kandidat UNKNOWN; tidak ada credential baru dari review"),
        "egress": _dimension(egress, src, "Egress kandidat UNKNOWN; tidak ada egress baru dari review"),
        "ci": _dimension(ci, src, "Dampak CI UNKNOWN"),
        "docs": {"status": "KNOWN", "items": ["Inventory, ADR/decision dan dokumentasi terdampak diperbarui saat outcome"],
                 "source": "§41"},
        "operations": _dimension(ops, src, "Runbook restart/uninstall/recovery kandidat UNKNOWN"),
        "rollback": _dimension(rollback_items, src, "Rencana rollback UNKNOWN"),
        "unmapped_components": unknown_comp,
    }


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
    plan: dict[str, Any] = {
        "type": "CONTROLLED_TRIAL", "install": "hanya di sandbox trial setelah TRIAL_APPROVE",
        "pins": {"baseline_hash": baseline_hash, "candidate_pin": p.candidate_pin or UNKNOWN,
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
        "stop_conditions": ["Budget/attempt/time cap tercapai", "Pelanggaran scope/permission", "Secret terdeteksi pada output",
                            "Gate security/authority/evidence gagal", "Owner membatalkan"],
        "collector_verifier": "Verifier independen (trusted collector); worker tidak menilai hasilnya sendiri",
        "canary": "Hanya jika aman dan reversible; bila tidak, sandbox/staging dan cutover berizin (tanpa dual authority)",
    }
    return plan


def replacement_plan(p: ChangeProposal, baseline: list[dict[str, Any]]) -> dict[str, Any] | None:
    """§40 "Rencana penggantian dan rollback" for REPLACE/UPGRADE/REMOVE; unknown items stay UNKNOWN."""
    if p.kind not in ("REPLACE", "UPGRADE", "REMOVE"):
        return None
    known = [b for b in baseline if b.get("status") == "KNOWN"]
    consumers = sorted({r["source_id"] for b in known for r in b["consumers"]})
    creds = [b["component_id"] for b in known if any((b.get("access") or {}).get(k) for k in
                                                       ("credential_ref", "credential_refs", "secrets"))]
    return {
        "previous_accepted_configuration": [{"component_id": b["component_id"], "approved_pin": b["approved_pin"],
                                             "manifest_hash": b["manifest_hash"]} for b in known] or UNKNOWN,
        "version_schema_data_compatibility": UNKNOWN,
        "backup_and_restore_evidence": "Diperlukan sebelum cutover bila ada state/data; belum ada bukti restore",
        "adapter_consumer_changes": consumers or UNKNOWN,
        "credential_egress_changes": ({"credential_refs_to_revoke_after_cutover": creds} if creds else UNKNOWN),
        "cutover_order": UNKNOWN,
        "coexistence": "Hanya bila aman; tidak menjalankan dua scheduler/vault/credential authority aktif",
        "success_stop_criteria": "Acceptance trial + stop conditions pada rencana eksperimen",
        "operations_owner": sorted({str(b.get("owner") or "owner") for b in known}) or UNKNOWN,
        "rollback_or_forward_recovery": UNKNOWN,
        "notes": ["Rollback kode tidak menjamin rollback data; migration irreversible memerlukan keputusan owner",
                  "Credential komponen lama dicabut setelah consumer sah dipindahkan; revoke/uninstall bukan efek review",
                  "Jika berhasil, komponen lama ditandai deprecated/retired, tidak dihapus dari history"],
    }


def review_contributors(package: dict[str, Any]) -> list[dict[str, str]]:
    """§39 "Pembagian pekerjaan review": roles are activated as needed; most work is code, not LLM."""
    claude = package.get("claude_review", {}).get("status", "NOT_REQUESTED")
    return [
        {"role": "Research / Supply-chain Analyst", "performed_by": "kode: registry riset + Check Upstream (metadata publik)"},
        {"role": "Integration / Architecture specialist", "performed_by": "kode: relasi & consumer dari inventaris"},
        {"role": "Security / Privacy", "performed_by": "BELUM DIJALANKAN — review keamanan/akses data manusia atau reviewer terpisah diperlukan"},
        {"role": "FinOps", "performed_by": "kode: ledger/pricing; UNKNOWN bila data tidak ada"},
        {"role": "Model Evaluation", "performed_by": "kode: saran suite; hasil hanya dari EvalRun tercatat"},
        {"role": "Claude tertinggi (checkpoint)", "performed_by": f"opsional — status {claude}; findings bukan approval"},
        {"role": "Owner", "performed_by": "manusia: tujuan, biaya, residual risk dan target (keputusan terikat hash)"},
    ]


def _cost(p: ChangeProposal, baseline: list[dict[str, Any]]) -> dict[str, Any]:
    limits = p.limits or {}
    known_costs = {b["component_id"]: b.get("cost") for b in baseline if b.get("status") == "KNOWN" and b.get("cost")}

    def unknown(note: str) -> dict[str, Any]:
        return {"value": None, "basis": UNKNOWN, "note": note}

    cash: dict[str, Any] = unknown("Belum ada pricing/ledger untuk kandidat")
    if isinstance(limits.get("cost_usd"), (int, float)):
        cash = {"value": None, "basis": UNKNOWN, "cap_usd": limits["cost_usd"],
                "note": "Batas yang dinyatakan pengusul; bukan estimasi biaya"}
    return {
        "cash": cash, "compute": unknown("Compute kandidat belum diukur"), "ci": unknown("Menit CI belum diukur"),
        "storage": unknown("Storage belum diukur"), "tokens": unknown("Token/context belum diukur"),
        "oversight": unknown("Konsumsi review Claude belum diukur (bukan dolar)"),
        "operations": unknown("Usaha operasi belum diperkirakan"),
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


def _human_decision(p: ChangeProposal, baseline_hash: str, candidate_hash: str) -> dict[str, Any]:
    return {
        "required": True,
        "bind": {"baseline_hash": baseline_hash, "candidate_hash": candidate_hash},
        "scope_fields": ["target", "environment", "provider", "data_class", "budget_usd", "max_minutes", "max_attempts"],
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
            "Budget tidak naik dari keputusan ini; kenaikan budget memerlukan keputusan BUDGET terpisah",
            "Pengusul/implementer/model tidak dapat memberi approval",
        ],
        "next_actions": (["Pin candidate (versi/digest) lalu review ulang"] if not p.candidate_pin and p.kind != "COMPARE"
                         else []) + ["Pilih keputusan dan scope; sistem merekam Decision terikat hash"],
    }


def _outcome_placeholder() -> dict[str, Any]:
    return {"status": "PENDING", "evidence": [], "accepted_configuration": None, "partial_failures": [],
            "rollback": None, "inventory_docs_update": "PENDING",
            "note": "Diisi setelah implementasi: ACCEPTED / ROLLED_BACK / FAILED / PARTIAL; rollback bukan acceptance."}


# --------------------------------------------------------------------------- Claude (optional)


def _public_metadata(p: ChangeProposal, package: dict[str, Any]) -> dict[str, Any]:
    """Only generic public metadata: no problem text, no attachments, no project names/data."""
    return {
        "proposal_kind": p.kind,
        "public_sources": [s["identity"] for s in package["source_identity"] if not s["identity"].startswith("name:")],
        "source_status": [{"identity": s["identity"], "status": s["status"], "release": s["release_or_commit"],
                           "license": s["license"]} for s in package["source_identity"]],
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
        return {"status": "NOT_RUN", "label": label, "sent_metadata": sorted(meta),
                "reason": "ClaudeNativeBridge belum tersedia; gunakan paket manual Claude Code resmi (tanpa Claude API)"}
    except AeosError as exc:
        return {"status": "NOT_RUN", "label": label, "reason": f"{exc.code}: {exc.message}"}
    base = {"label": label, "model_requested": res.model_requested, "model_resolved": res.model_resolved,
            "mode": res.mode, "sent_metadata": sorted(meta), "at": utcnow().isoformat()}
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


def build_package(session: Session, p: ChangeProposal, children: list[ChangeProposal], *, with_claude: bool,
                  domain: str | None) -> tuple[dict[str, Any], str, str]:
    baseline, baseline_hash, extra = compute_baseline(session, p)
    _cand, candidate_hash = compute_candidate(session, p, children)
    entries = research_entries(session, p)
    categories = sorted({e.category for e in entries})
    relevance = [{"name": e.name, "placement": e.placement, "relevance": e.relevance, "lifecycle": e.lifecycle}
                 for e in entries]
    package: dict[str, Any] = {
        "summary": {
            "kind": p.kind, "decision_question": KIND_QUESTIONS[p.kind], "problem": p.problem or UNKNOWN,
            "expected_benefit": p.expected_benefit or UNKNOWN, "relevance": relevance or UNKNOWN,
            "plain_language": _plain_language(p, baseline, entries),
            "attachments": [{"attachment_id": a, "note": "lampiran adalah DATA tidak tepercaya"} for a in p.attachment_ids or []],
        },
        "source_identity": _source_identity(session, p),
        "baseline": {"components": baseline, "baseline_hash": baseline_hash, **extra,
                     "note": None if baseline else "Tidak ada komponen lama (ADD/COMPARE); baseline = konfigurasi berlaku"},
        "alternatives": _alternatives(p, bool(p.existing_component_ids)),
        "impact": _impact(p, baseline),
        "experiment_plan": _experiment_plan(session, p, baseline_hash, candidate_hash, categories),
        "replacement_plan": replacement_plan(p, baseline),
        "cost": _cost(p, baseline),
        "claude_review": {"status": "NOT_REQUESTED",
                          "label": "Rekomendasi Claude adalah DATA review; findings bukan persetujuan pemilik"},
        "human_decision": _human_decision(p, baseline_hash, candidate_hash),
        "outcome": (p.outcome or {}).get("current") or _outcome_placeholder(),
        "parts": [{"child_id": c.id, "title": c.title, "kind": c.kind} for c in children],
        "unknown_fields": p.unknown_fields or [],
        "generated_at": utcnow().isoformat(),
        "generated_by": "deterministic review builder (kode, bukan LLM)",
    }
    if with_claude:
        package["claude_review"] = claude_review(p, package, domain=domain)
    package["review_contributors"] = review_contributors(package)
    return package, baseline_hash, candidate_hash


def _plain_language(p: ChangeProposal, baseline: list[dict[str, Any]], entries: list[ResearchEntry]) -> list[str]:
    out: list[str] = []
    if p.kind in ("ADD", "REPLACE", "UPGRADE"):
        out.append("Menambah/mengubah dependency; manfaat terhadap baseline belum terbukti sampai trial terkendali.")
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


def review_hash(package: dict[str, Any]) -> str:
    stable = {k: v for k, v in package.items()
              if k not in ("generated_at", "claude_review", "human_decision", "review_contributors")}
    return content_hash(stable)
