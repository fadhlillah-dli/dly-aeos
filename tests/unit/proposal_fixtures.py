"""Shared builders for the ChangeProposal tests (not a test module).

They create the real records the gates read: inventory components/relations, a worker route of the decided
provider, a TaskSpec linked to the proposal, verifier evidence produced for that task after the TRIAL
decision, restore-test evidence and the owner plan (acceptance, owner, replacement plan)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from aeos.core.hashing import content_hash
from aeos.models.evidence import EvidenceArtifact
from aeos.models.inventory import ComponentReference, ComponentRelation
from aeos.models.planning import Plan, PlanVersion, TaskSpec
from aeos.models.portfolio import Project
from aeos.models.routing import ModelRoute
from aeos.proposals import links, plan, service
from aeos.research import service as research

SCOPE: dict[str, Any] = {"target": "platform:dly-aeos", "environment": "sandbox", "provider": "opencode-zen",
                         "data_class": "INTERNAL", "budget_usd": 0, "max_minutes": 60, "max_attempts": 2,
                         "stop_conditions": ["cap waktu/attempt/budget tercapai", "pelanggaran scope"],
                         "rollback_scope": "hapus sandbox trial; baseline tetap aktif"}
ACCEPTANCE = [{"criterion": "Suite small-bugfix lulus pada kandidat", "metric": "final_acceptance_rate",
               "threshold": ">= 0.8", "baseline_value": "0.7"}]


def route(db, provider: str = "opencode-zen", key: str | None = None, domains: tuple[str, ...] = ("PERSONAL",)) -> ModelRoute:
    r = db.scalar(select(ModelRoute).where(ModelRoute.route_key == (key or f"{provider}/test-free")))
    if r is None:
        r = ModelRoute(route_key=key or f"{provider}/test-free", pool="WORKER", provider=provider, model_id="m-free",
                       status="CANDIDATE", eligible_domains=list(domains), allowed_data_classes=["PUBLIC", "INTERNAL"],
                       mode="LIVE")
        db.add(r)
        db.commit()
    return r


def component(db, cid: str = "opencode", pin: str = "1.3.0", *, seed: bool = True, data: bool = True,
              enablement: str = "DISABLED") -> ComponentReference:
    if db.get(ComponentReference, cid) is not None:
        return db.get(ComponentReference, cid)
    db.add(ComponentReference(component_id=cid, kind="worker-engine", name=cid.title(), approved_pin=pin,
                              enablement=enablement, owner="owner",
                              access={"egress": ["opencode.ai"], "credential_ref": "workers/openrouter"},
                              operations={"restart": "aeos worker restart"}))
    db.add(ComponentRelation(source_id="workforce", target_id=cid, relation="calls", lens="DESIGN",
                             provenance="master §36"))
    if data:
        db.add(ComponentRelation(source_id=cid, target_id="evidence-store", relation="writes", lens="DESIGN",
                                 data_description="run artifacts"))
    db.commit()
    if seed:
        research.seed_registry(db)  # master-stated identities (e.g. github:anomalyco/opencode) pass the gate
        db.commit()
    route(db)
    return db.get(ComponentReference, cid)


def restore_evidence(db, domain: str | None = None) -> EvidenceArtifact:
    ev = EvidenceArtifact(kind="RESTORE_TEST", origin="restore-test", collector_identity="restore-test", trusted=True,
                          content_hash="sha256:" + "c" * 64, domain=domain, mode="LIVE", claim="restore test passed",
                          subject_type="backup", subject_id="bk-1")
    db.add(ev)
    db.commit()
    return ev


def owner_plan(db, p, **extra: Any) -> None:
    patch: dict[str, Any] = {"acceptance": ACCEPTANCE, "operations_owner": "owner"}
    if p.kind in ("REPLACE", "UPGRADE"):
        patch["replacement"] = {"cutover_order": "1. pasang kandidat di samping; 2. pindahkan workforce; 3. retire lama",
                                "rollback_or_forward_recovery": "kembalikan pin lama lewat manifest; data run tetap",
                                "backup_restore_evidence_id": restore_evidence(db).id,
                                "retire_old_version_after": "14 hari tanpa regresi"}
    patch.update(extra)
    plan.update(db, p, patch, actor_user_id="owner")
    db.commit()


def create(db, kind: str = "REPLACE", sources: tuple[str, ...] | list[str] = ("https://github.com/anomalyco/opencode",),
           domain: str = "PERSONAL", **kw: Any):
    params = {"existing_component_ids": ["opencode"] if kind in ("REPLACE", "UPGRADE", "REMOVE") else [],
              "title": None, "problem": "Butuh engine yang lebih stabil", "expected_benefit": "kurangi repair"} | kw
    p = service.create_proposal(db, actor_user_id="owner", domain=domain, kind=kind, sources=list(sources), **params)
    db.commit()
    return p


def review(db, p, **kw: Any):
    service.review(db, p, actor_user_id="owner", **kw)
    db.commit()
    return p


def decide(db, p, action: str, **kw: Any):
    d = service.decide(db, p, actor_user_id="owner", action=action, baseline_hash=kw.pop("bh", p.baseline_hash),
                       candidate_hash=kw.pop("ch", p.candidate_hash), scope=kw.pop("scope", SCOPE),
                       rationale=kw.pop("rationale", f"owner {action}"))
    db.commit()
    return d


def project(db, domain: str = "PERSONAL") -> Project:
    key = f"proj-{domain.lower()}"
    proj = db.scalar(select(Project).where(Project.project_key == key))
    if proj is None:
        proj = Project(project_key=key, name=f"Proyek {domain}", domain=domain)
        db.add(proj)
        db.commit()
    return proj


def task(db, p, *, domain: str | None = None, data_class: str = "INTERNAL", link: bool = True) -> TaskSpec:
    """A TaskSpec of an (illustrative) implementing plan, linked to the proposal through the links API."""
    proj = project(db, domain or p.domain)
    pl = Plan(project_id=proj.id, domain=proj.domain, title="Trial plan", state="APPROVED")
    db.add(pl)
    db.flush()
    content = {"tasks": [{"key": "T1"}]}
    pv = PlanVersion(plan_id=pl.id, project_id=proj.id, domain=proj.domain, version=1, content=content,
                     manifest_hash=content_hash(content))
    db.add(pv)
    db.flush()
    fields = {"plan_version_id": pv.id, "plan_version_hash": pv.manifest_hash, "project_id": proj.id,
              "domain": proj.domain, "task_key": "T1", "title": "trial candidate", "goal": "trial",
              "data_classification": data_class}
    t = TaskSpec(**fields, spec_hash=content_hash(fields), input_artifacts=[f"change_proposal:{p.id}"])
    db.add(t)
    db.commit()
    if link:
        links.add_links(db, p, actor_user_id="owner", task_ids=[t.id])
        db.commit()
    return t


def evidence(db, *, task_spec: TaskSpec | None = None, trusted: bool = True, domain: str | None = "PERSONAL",
             mode: str = "LIVE", claim: str = "trial suite passed", **kw: Any) -> EvidenceArtifact:
    ev = EvidenceArtifact(kind="TEST_REPORT", origin="verifier" if trusted else "worker",
                          collector_identity="verifier" if trusted else "worker", trusted=trusted,
                          content_hash="sha256:" + "a" * 64, domain=domain, mode=mode, claim=claim,
                          task_id=task_spec.id if task_spec else None, **kw)
    db.add(ev)
    db.commit()
    return ev


MANUAL_PASS = {"review": {"summary": "bukti konsisten dengan acceptance"},
               "verify": {"claims": [{"claim": "suite lulus", "status": "SUPPORTED"}]},
               "verdict": "PASS", "rationale": "verifier report covers AC1"}


def checkpoint_pass(db, p) -> dict[str, Any]:
    from aeos.proposals import checkpoint

    rec = checkpoint.record(db, p, actor_user_id="owner", manual_result=MANUAL_PASS)
    db.commit()
    return rec


def trialled(db, kind: str = "UPGRADE", **kw: Any):
    """Proposal reviewed with an owner plan, TRIAL approved, a linked TaskSpec and bound verifier evidence."""
    component(db)
    p = create(db, kind=kind, candidate_pin="1.4.0", **kw)
    owner_plan(db, p)
    review(db, p)
    decide(db, p, "TRIAL_APPROVE")
    t = task(db, p)
    ev = evidence(db, task_spec=t)
    service.attach_evidence(db, p, evidence_ids=[ev.id], actor_user_id="owner")
    db.commit()
    return p, t, ev


def promoted(db, kind: str = "UPGRADE", **kw: Any):
    p, t, ev = trialled(db, kind=kind, **kw)
    checkpoint_pass(db, p)
    decide(db, p, "PROMOTE")
    return p, t, ev


def apply_config(db, cid: str = "opencode", pin: str = "1.4.0") -> None:
    """What the reviewed install-manifest change does after promotion: the pin is applied and enabled."""
    comp = db.get(ComponentReference, cid)
    comp.approved_pin = pin
    comp.enablement = "ENABLED"
    db.commit()
