"""Records a ChangeProposal is connected to (§40 "menautkan ... TaskSpec, evidence dan publication/release
records", §41 "Hubungkan setiap perubahan dengan proposal/ADR, approval, source/config hashes, task/release,
runtime observations serta publication revisions").

Every link is validated against the real record and the proposal's domain (a link never reveals or uses
another domain's record). Finalization after an outcome triggers the inventory reconciliation of the
affected components and computes the inventory/docs/ADR/ownership state from those records - "PENDING"
is replaced by what was actually done, and what is missing is named.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.enums import Domain
from aeos.core.errors import AeosError, ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.events import service as events
from aeos.models.research import ChangeProposal

LINK_KINDS = ("task_ids", "eval_run_ids", "publication_ids", "release_ids", "adr_keys", "observation_ids")
ADR_OK = ("ACCEPTED", "PROPOSED")
PUBLISHED_OK = ("PUBLISHED",)
PUBLICATION_PROBLEM = ("PARTIAL", "CONFLICT", "FAILED")


def _clean(ids: list[str] | None) -> list[str]:
    out = []
    for i in ids or []:
        s = str(i or "").strip()
        if s and s not in out:
            out.append(s[:128])
    return out


def validate(session: Session, p: ChangeProposal, kind: str, ids: list[str], *,
             allowed_project_ids: list[str] | None = None) -> list[str]:
    from aeos.models.decisions import ArchitectureDecisionRecord
    from aeos.models.inventory import ComponentObservation
    from aeos.models.planning import TaskSpec
    from aeos.models.publishing import Publication
    from aeos.models.research import EvalRun

    out: list[str] = []
    for i in _clean(ids):
        if kind == "task_ids":
            t = session.get(TaskSpec, i)
            if t is None or t.domain != p.domain or (allowed_project_ids is not None and t.project_id not in allowed_project_ids):
                raise ValidationFailed(f"task {i} not found in this domain")
            from aeos.proposals.trial import proposal_id_for_task

            owner = proposal_id_for_task(t)
            if owner is not None and owner != p.id:
                # a trial TaskSpec of ANOTHER proposal is about another candidate: its evidence never opens this
                # proposal's gates (§40 "bukti harus tentang kandidat itu")
                raise ValidationFailed(f"task {i} belongs to the trial of proposal {owner}; link it there",
                                       code="TASK_OF_OTHER_PROPOSAL")
            out.append(t.id)
        elif kind == "eval_run_ids":
            r = session.get(EvalRun, i)
            if r is None or r.submitted_domain not in (None, p.domain):
                raise ValidationFailed(f"eval run {i} not found in this domain")
            out.append(r.id)
        elif kind == "publication_ids":
            pub = session.get(Publication, i)
            if pub is None or pub.domain != p.domain or \
                    (allowed_project_ids is not None and pub.project_id not in allowed_project_ids):
                raise ValidationFailed(f"publication {i} not found in this domain")
            out.append(pub.id)
        elif kind == "release_ids":
            if p.domain == Domain.OFFICE.value:
                raise ValidationFailed("controller releases are platform (PERSONAL) records; not linkable from OFFICE")
            from aeos.core.config import get_settings
            from aeos.operations.release import load_provenance

            try:
                prov = load_provenance(get_settings(), i)
            except AeosError as exc:
                raise ValidationFailed(f"release {i} not found") from exc
            out.append(str(prov["release_id"]))
        elif kind == "adr_keys":
            adr = session.scalar(select(ArchitectureDecisionRecord).where(ArchitectureDecisionRecord.adr_key == i))
            if adr is None or adr.status not in ADR_OK:
                raise ValidationFailed(f"ADR {i} not found or not ACCEPTED/PROPOSED")
            out.append(adr.adr_key)
        elif kind == "observation_ids":
            o = session.get(ComponentObservation, i)
            targets = set(p.existing_component_ids or []) | set(p.candidate_component_ids or [])
            if o is None or o.component_id not in targets:
                raise ValidationFailed(f"observation {i} is not about a component of this proposal")
            out.append(o.id)
        else:
            raise ValidationFailed(f"unknown link kind {kind}")
    return out


def add_links(session: Session, p: ChangeProposal, *, actor_user_id: str, allowed_project_ids: list[str] | None = None,
              **kinds: list[str] | None) -> dict[str, Any]:
    if p.parent_id:
        raise ValidationFailed("link records on the parent proposal")
    added: dict[str, list[str]] = {}
    links = dict(p.links or {})
    for kind in LINK_KINDS:
        ids = validate(session, p, kind, list(kinds.get(kind) or []), allowed_project_ids=allowed_project_ids)
        if not ids:
            continue
        added[kind] = ids
        if kind == "task_ids":
            p.task_ids = sorted({*(p.task_ids or []), *ids})
        else:
            links[kind] = sorted({*(links.get(kind) or []), *ids})
    if not added:
        raise ValidationFailed("nothing to link", details={"kinds": list(LINK_KINDS)})
    p.links = links
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="proposal.links.add",
                 target_type="change_proposal", target_id=p.id, project_id=p.project_id, domain=p.domain,
                 details=added)
    events.emit(session, source="proposals", kind="proposal.linked", entity_type="change_proposal", entity_id=p.id,
                project_id=p.project_id, domain=p.domain, payload={k: len(v) for k, v in added.items()})
    return added


def link_views(session: Session, p: ChangeProposal) -> dict[str, Any]:
    from aeos.models.planning import TaskSpec
    from aeos.models.publishing import Publication
    from aeos.models.workforce import TaskExecution

    links = p.links or {}
    tasks = []
    for t in session.scalars(select(TaskSpec).where(TaskSpec.id.in_(list(p.task_ids or [])))):
        te = session.scalar(select(TaskExecution).where(TaskExecution.task_id == t.id))
        tasks.append({"id": t.id, "task_key": t.task_key, "title": t.title, "plan_version_id": t.plan_version_id,
                      "state": te.state if te else "NOT_ENQUEUED"})
    pubs = [{"id": x.id, "status": x.status, "plan_version_id": x.plan_version_id}
            for x in session.scalars(select(Publication).where(Publication.id.in_(list(links.get("publication_ids") or []))))]
    return {"tasks": tasks, "publications": pubs, **{k: list(links.get(k) or []) for k in LINK_KINDS if k not in
                                                     ("task_ids", "publication_ids")},
            "plan_version_ids": list(links.get("plan_version_ids") or [])}


def docs_published(session: Session, p: ChangeProposal) -> dict[str, str]:
    from aeos.models.publishing import Publication

    ids = list((p.links or {}).get("publication_ids") or [])
    if not ids:
        return {"status": "UNKNOWN", "note": "Belum ada publikasi dokumen tertaut; status dari modul publisher"}
    statuses = [x.status for x in session.scalars(select(Publication).where(Publication.id.in_(ids)))]
    if any(s in PUBLICATION_PROBLEM for s in statuses):
        worst = "CONFLICT" if "CONFLICT" in statuses else "PARTIAL"
        return {"status": worst, "note": f"Publikasi tertaut: {', '.join(statuses)} (sebagian gagal tidak hijau)"}
    if statuses and all(s in PUBLISHED_OK for s in statuses):
        return {"status": "PUBLISHED", "note": f"{len(statuses)} publikasi PUBLISHED (publisher)"}
    return {"status": "PENDING", "note": f"Publikasi tertaut: {', '.join(statuses) or 'tidak ditemukan'}"}


def _reconcile(session: Session, p: ChangeProposal, component_ids: list[str]) -> dict[str, Any]:
    try:
        from aeos.inventory.manifest import current_manifest
        from aeos.inventory.reconcile import reconcile

        if current_manifest(session) is None:
            return {"status": "PENDING", "reason": "install manifest belum dimuat; jalankan `aeos inventory reconcile`",
                    "runs": []}
        from aeos.core.config import get_settings

        settings = get_settings()
    except (ImportError, ModuleNotFoundError) as exc:  # pragma: no cover - inventory ships with the foundation
        return {"status": "PENDING", "reason": f"inventory unavailable ({exc})", "runs": []}
    runs = []
    for cid in component_ids:
        try:
            run = reconcile(session, settings, component_id=cid, actor=f"proposal:{p.id}", trigger="proposal-outcome")
            runs.append({"component_id": cid, "run_id": run.id, "status": run.status,
                         "component": (run.component_status or {}).get(cid, {}).get("status")})
        except AeosError as exc:
            runs.append({"component_id": cid, "run_id": None, "status": "FAILED", "reason": exc.code})
        except Exception as exc:
            runs.append({"component_id": cid, "run_id": None, "status": "FAILED", "reason": type(exc).__name__})
    ok = runs and all(r["status"] in ("COMPLETED",) for r in runs)
    return {"status": "DONE" if ok else ("PARTIAL" if runs else "PENDING"), "runs": runs,
            "reason": None if ok else "Rekonsiliasi belum lengkap untuk semua komponen terdampak"}


def finalize(session: Session, p: ChangeProposal, entry: dict[str, Any]) -> dict[str, Any]:
    """§40 Finalization: inventory reconciled, docs/ADR linked, ownership and rollback history connected."""
    from aeos.inventory import facade as inventory

    targets = sorted({*(p.existing_component_ids or []), *(p.candidate_component_ids or [])})
    registered = [c for c in targets if inventory.get_component(session, c) is not None]
    inv = _reconcile(session, p, registered) if registered else {
        "status": "PENDING", "runs": [], "reason": "tidak ada komponen terdampak yang terdaftar di inventaris"}
    adr_keys = list((p.links or {}).get("adr_keys") or [])
    docs = docs_published(session, p)
    owner_plan = p.owner_plan or {}
    ops_owner = owner_plan.get("operations_owner") or (owner_plan.get("replacement") or {}).get("operations_owner")
    manifest_owners = sorted({str(c.owner) for c in (inventory.get_component(session, x) for x in registered) if c})
    ownership = {"operations_owner": ops_owner or "UNKNOWN", "manifest_owners": manifest_owners,
                 "status": ("UNKNOWN" if not ops_owner else "MATCH" if manifest_owners and all(o == ops_owner for o in manifest_owners)
                            else "MISMATCH"),
                 "note": "Pemilik di install manifest diperbarui lewat review manifest (desired state), bukan oleh outcome"}
    missing = [name for name, ok in (("inventory", inv["status"] == "DONE"), ("ADR", bool(adr_keys)),
                                     ("docs", docs["status"] == "PUBLISHED")) if not ok]
    fin = {"inventory": inv, "adr": {"status": "LINKED" if adr_keys else "PENDING", "keys": adr_keys},
           "docs": docs, "ownership": ownership, "rollback_history": "outcome.history (tidak dihapus)",
           "at": utcnow().isoformat()}
    entry["finalization"] = fin
    entry["inventory_docs_update"] = "DONE" if not missing else "PENDING: " + ", ".join(missing)
    return fin
