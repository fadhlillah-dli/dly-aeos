"""Incident records: timeline, evidence, analysis, corrective action, follow-up (§28, §33, §16).

§33: "Incident response menghentikan capability berisiko, menjaga bukti, menentukan owner dan blast
radius, memperbaiki root cause, lalu menambah regression/eval case." §16 Incident Analyst: timeline,
impact, hypotheses, evidenced root cause, corrective action; no root cause without support.

* An emergency stop opens an incident (status CONTAINED when every containment step succeeded, else
  OPEN) with an owner that is never empty (the declared owner, else the human who ran the stop) and a
  blast radius *derived from records*: affected projects, cancelled runs, broker side-effect targets of
  those runs (branches/PRs), revoked capability kinds, engines/routes and the inventory blast radius of
  their components. Derived data is stored per domain so Office details never reach a Personal view.
* A root cause can only be recorded together with at least one existing, trusted evidence artifact.
* ``resolve`` refuses until an evidenced root cause, a corrective action and a regression test that
  exists in the repository's test tree (or an existing EvalSuite case) are recorded.
* ``open_follow_up`` files a draft ChangeProposal through the proposals facade (human review decides);
  it never changes code, routes or policy by itself.
* ``render_report`` produces the INCIDENT_REPORT / POST_MORTEM document (Markdown) from the record.

Free text is redacted (secrets + PII) before it is stored. Services take a Session and never commit.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.errors import Conflict, NotFound, ValidationFailed
from aeos.core.paths import repo_root
from aeos.core.redaction import redact, redact_text
from aeos.core.timeutil import utcnow
from aeos.events import service as events
from aeos.models.operations_ext import OpsIncident

STATUSES = ("OPEN", "CONTAINED", "RESOLVED")
DOMAINS = ("PERSONAL", "OFFICE")


def _clean(text: str, limit: int = 4000) -> str:
    return redact_text((text or "").strip(), pii=True)[:limit]


def _entry(actor: str, kind: str, note: str, evidence_ids: list[str] | None = None) -> dict[str, Any]:
    return {"at": utcnow().isoformat(), "by": actor[:128], "kind": kind, "note": _clean(note, 2000),
            "evidence_ids": [str(e)[:36] for e in evidence_ids or []]}


def _get(session: Session, incident_id: str) -> OpsIncident:
    inc = session.get(OpsIncident, incident_id)
    if inc is None:
        raise NotFound("incident not found")
    return inc


def _touch(inc: OpsIncident, entry: dict[str, Any]) -> None:
    inc.timeline = [*(inc.timeline or []), entry]


# ----------------------------------------------------------------------------------------- blast radius


def derive_blast_radius(session: Session, runs: list[dict[str, Any]], grants: list[dict[str, Any]]) -> dict[str, Any]:
    """Blast radius from records, keyed by domain. ``runs``/``grants`` are snapshots taken *before* the
    containment steps changed them (run: id, project_id, domain, engine, model_route_key; grant: kind,
    project_id, domain)."""
    from aeos.models.broker_ext import BrokerSideEffect

    out: dict[str, Any] = {}
    run_ids = [r["id"] for r in runs]
    effects = list(session.scalars(select(BrokerSideEffect).where(BrokerSideEffect.run_id.in_(run_ids)))) \
        if run_ids else []
    for d in DOMAINS:
        mine = [r for r in runs if r.get("domain") == d]
        my_grants = [g for g in grants if g.get("domain") == d]
        components = sorted({c for r in mine for c in (r.get("engine"), r.get("model_route_key")) if c})
        my_run_ids = {r["id"] for r in mine}
        out[d] = {
            "projects": sorted({str(r["project_id"]) for r in mine} | {str(g["project_id"]) for g in my_grants}),
            "runs": sorted(my_run_ids),
            "side_effect_targets": [
                {"kind": e.kind, "status": e.status, "target": redact(e.target or {}),
                 "external_url": e.external_url}
                for e in effects if e.run_id in my_run_ids],
            "revoked_capability_kinds": sorted({str(g["kind"]) for g in my_grants}),
            "components": components,
            "component_blast_radius": _component_blast_radius(session, components, d),
        }
    return out


def _component_blast_radius(session: Session, component_ids: list[str], domain: str) -> dict[str, Any]:
    """Inventory dependents of each affected component (who is affected when it fails), or UNKNOWN."""
    found: dict[str, Any] = {}
    for cid in component_ids:
        try:
            from aeos.inventory import facade as inventory

            radius = inventory.blast_radius(session, cid, domain=domain, direction="dependents")
            found[cid] = sorted(i["component_id"] for i in radius.get("items") or [])
        except Exception:  # not an inventory component (e.g. a route key) or inventory unavailable
            found[cid] = "UNKNOWN"
    return found


def _referenced_evidence(inc: OpsIncident) -> set[str]:
    refs = {*(inc.evidence_ids or []), *(inc.root_cause_evidence_refs or [])}
    for e in inc.timeline or []:
        if isinstance(e, dict):
            refs.update(str(x) for x in e.get("evidence_ids") or [])
    return {r for r in refs if r}


def visible_evidence(session: Session, inc: OpsIncident, domain: str | None) -> set[str]:
    """Evidence ids of this incident that a session of *domain* may see: platform evidence (no domain) and
    evidence of that domain only. A platform-wide incident (emergency stop) references the containment
    evidence of BOTH domains; the other domain's ids are never shown (Office data never reaches Personal)."""
    from aeos.models.evidence import EvidenceArtifact

    refs = _referenced_evidence(inc)
    if not refs:
        return set()
    rows = session.execute(select(EvidenceArtifact.id, EvidenceArtifact.domain).where(EvidenceArtifact.id.in_(refs)))
    return {rid for rid, d in rows if d is None or (domain is not None and d == domain)}


def scoped(inc: OpsIncident, domain: str | None, session: Session | None = None) -> dict[str, Any]:
    """Incident view for a session domain: derived blast radius of other domains is removed, and (with a
    session) evidence ids of other domains are dropped from the evidence lists and the timeline."""
    derived = inc.derived_blast_radius or {}
    if domain is None:
        visible: dict[str, Any] = {}
    else:
        visible = {domain: derived.get(domain) or {}}
    if session is not None:
        allowed = visible_evidence(session, inc, domain)

        def keep(ids: list[str] | None) -> list[str]:
            return [i for i in ids or [] if i in allowed]
    else:
        def keep(ids: list[str] | None) -> list[str]:
            return list(ids or [])
    # entries bound to one domain (a follow-up filed in that domain) are shown in that domain only
    timeline = [{**e, "evidence_ids": keep(e.get("evidence_ids"))} if isinstance(e, dict) else e
                for e in inc.timeline or [] if not isinstance(e, dict) or e.get("domain") in (None, domain)]
    return {
        "id": inc.id, "source": inc.source, "source_ref": inc.source_ref, "title": inc.title, "status": inc.status,
        "domain": inc.domain, "project_id": inc.project_id, "owner": inc.owner,
        "declared_blast_radius": inc.declared_blast_radius, "derived_blast_radius": visible,
        "timeline": timeline, "impact": inc.impact, "hypotheses": inc.hypotheses or [],
        "root_cause": inc.root_cause, "root_cause_evidence_refs": keep(inc.root_cause_evidence_refs),
        "root_cause_ref": inc.root_cause_ref, "corrective_actions": inc.corrective_actions or [],
        "regression_test_ids": inc.regression_test_ids or [], "eval_case_refs": inc.eval_case_refs or [],
        "follow_ups": [f for f in inc.follow_ups or []
                       if not isinstance(f, dict) or f.get("domain") in (None, domain)],
        "evidence_ids": keep(inc.evidence_ids),
        "created_by": inc.created_by, "created_at": inc.created_at.isoformat() if inc.created_at else None,
        "resolved_at": inc.resolved_at.isoformat() if inc.resolved_at else None, "resolved_by": inc.resolved_by,
        "resolution_requirements": resolution_requirements(inc),
    }


# ----------------------------------------------------------------------------------------- lifecycle


def open_incident(
    session: Session, *, title: str, actor: str, owner: str = "", source: str = "manual",
    source_ref: str | None = None, status: str = "OPEN", domain: str | None = None, project_id: str | None = None,
    declared_blast_radius: str = "", derived_blast_radius: dict[str, Any] | None = None,
    evidence_ids: list[str] | None = None, note: str = "",
) -> OpsIncident:
    if status not in ("OPEN", "CONTAINED"):
        raise ValidationFailed("a new incident is OPEN or CONTAINED")
    if len((title or "").strip()) < 3:
        raise ValidationFailed("an incident needs a title")
    owner_v = _clean(owner, 200) or actor[:200]  # §33 the owner is always determined, never empty
    inc = OpsIncident(
        source=source[:32], source_ref=(source_ref or None) and str(source_ref)[:64], title=_clean(title, 256),
        status=status, domain=domain, project_id=project_id, owner=owner_v,
        declared_blast_radius=_clean(declared_blast_radius, 1000),
        derived_blast_radius=derived_blast_radius or {}, evidence_ids=list(evidence_ids or []),
        timeline=[_entry(actor, "opened", note or title, evidence_ids)], created_by=actor[:128],
        hypotheses=[], corrective_actions=[], regression_test_ids=[], eval_case_refs=[], follow_ups=[],
        root_cause_evidence_refs=[],
    )
    session.add(inc)
    session.flush()
    audit.record(session, actor_type="user" if not actor.startswith("cli") else "cli", actor_id=actor,
                 action="ops.incident.open", target_type="incident", target_id=inc.id, domain=domain,
                 project_id=project_id, details={"source": source, "source_ref": source_ref, "status": status})
    events.emit(session, source="operations", kind="ops.incident.opened", entity_type="incident",
                entity_id=inc.id, domain=domain, project_id=project_id, payload={"status": status})
    return inc


def add_timeline(session: Session, incident_id: str, *, actor: str, note: str,
                 evidence_ids: list[str] | None = None) -> OpsIncident:
    inc = _get(session, incident_id)
    _require_evidence(session, evidence_ids or [], required=False, domain=inc.domain)
    _touch(inc, _entry(actor, "note", note, evidence_ids))
    inc.evidence_ids = sorted({*(inc.evidence_ids or []), *(evidence_ids or [])})
    session.flush()
    return inc


def _require_evidence(session: Session, refs: list[str], *, required: bool,
                      domain: str | None = None) -> list[str]:
    """Each ref must exist (and be trusted when it supports a root cause). A domain incident may only cite
    evidence of its own domain or platform evidence, so the other domain's records never enter it."""
    from aeos.models.evidence import EvidenceArtifact

    refs = [str(r).strip() for r in refs if str(r).strip()]
    if required and not refs:
        raise ValidationFailed("a root cause needs at least one supporting evidence artifact (§16: no root cause "
                               "without support)", code="ROOT_CAUSE_UNSUPPORTED")
    for ref in refs:
        ev = session.get(EvidenceArtifact, ref)
        if ev is None or (domain is not None and ev.domain not in (None, domain)):
            raise ValidationFailed(f"evidence {ref} does not exist", code="EVIDENCE_NOT_FOUND")
        if required and not ev.trusted:
            raise ValidationFailed(f"evidence {ref} is not from a trusted collector; it cannot support a root "
                                   "cause", code="ROOT_CAUSE_UNSUPPORTED")
    return refs


def record_analysis(
    session: Session, incident_id: str, *, actor: str, impact: str = "", hypotheses: list[str] | None = None,
    root_cause: str = "", root_cause_evidence_refs: list[str] | None = None, root_cause_ref: str | None = None,
) -> OpsIncident:
    inc = _get(session, incident_id)
    if inc.status == "RESOLVED":
        raise Conflict("incident is resolved; open a new incident for new findings")
    if impact:
        inc.impact = _clean(impact)
    if hypotheses:
        inc.hypotheses = [*(inc.hypotheses or []), *[_clean(h, 1000) for h in hypotheses if h.strip()]]
    if root_cause:
        refs = _require_evidence(session, root_cause_evidence_refs or [], required=True, domain=inc.domain)
        inc.root_cause = _clean(root_cause)
        inc.root_cause_evidence_refs = refs
        inc.root_cause_ref = (root_cause_ref or None) and str(root_cause_ref)[:64]
    _touch(inc, _entry(actor, "analysis", root_cause or impact or "; ".join(hypotheses or []),
                       root_cause_evidence_refs))
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor, action="ops.incident.analysis", target_type="incident",
                 target_id=inc.id, domain=inc.domain, details={"root_cause": bool(root_cause),
                                                               "evidence_refs": inc.root_cause_evidence_refs})
    return inc


def add_corrective_action(session: Session, incident_id: str, *, actor: str, description: str,
                          ref: str | None = None) -> OpsIncident:
    inc = _get(session, incident_id)
    if len((description or "").strip()) < 5:
        raise ValidationFailed("describe the corrective action")
    inc.corrective_actions = [*(inc.corrective_actions or []),
                              {"description": _clean(description, 2000), "ref": (ref or None) and str(ref)[:64],
                               "by": actor[:128], "at": utcnow().isoformat()}]
    _touch(inc, _entry(actor, "corrective_action", description))
    session.flush()
    return inc


def _test_exists(test_id: str, root: Path) -> bool:
    path, _, name = test_id.partition("::")
    rel = Path(path.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] != "tests" or rel.suffix != ".py":
        return False
    file = root / rel
    if not file.is_file():
        return False
    if not name:
        return True
    func = name.split("[", 1)[0].split("::")[-1]
    try:
        tree = ast.parse(file.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return False
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func for n in ast.walk(tree))


def _eval_case_exists(session: Session, ref: str) -> bool:
    from aeos.models.research import EvalSuite

    suite_key, _, case_id = ref.partition("#")
    suite = session.scalar(select(EvalSuite).where(EvalSuite.suite_key == suite_key))
    if suite is None:
        return False
    if not case_id:
        return False
    return any(isinstance(c, dict) and str(c.get("id") or c.get("case_id") or c.get("key")) == case_id
               for c in suite.cases or [])


def link_regression(session: Session, incident_id: str, *, actor: str, test_ids: list[str] | None = None,
                    eval_case_refs: list[str] | None = None, root: Path | None = None) -> OpsIncident:
    """Link the regression test(s) (``tests/...py::test_name``) and/or eval cases (``suite_key#case_id``)
    that guard against a recurrence. Each must exist; nothing is created or changed in the suites."""
    inc = _get(session, incident_id)
    root = root or repo_root()
    tests_ok = []
    for t in test_ids or []:
        t = t.strip()
        if not _test_exists(t, root):
            raise ValidationFailed(f"regression test {t!r} does not exist in the repository test tree",
                                   code="REGRESSION_TEST_NOT_FOUND")
        tests_ok.append(t)
    cases_ok = []
    for c in eval_case_refs or []:
        c = c.strip()
        if not _eval_case_exists(session, c):
            raise ValidationFailed(f"eval case {c!r} does not exist (suite_key#case_id)", code="EVAL_CASE_NOT_FOUND")
        cases_ok.append(c)
    if not tests_ok and not cases_ok:
        raise ValidationFailed("give at least one regression test id or eval case")
    inc.regression_test_ids = sorted({*(inc.regression_test_ids or []), *tests_ok})
    inc.eval_case_refs = sorted({*(inc.eval_case_refs or []), *cases_ok})
    _touch(inc, _entry(actor, "regression_linked", ", ".join(tests_ok + cases_ok)))
    session.flush()
    return inc


def resolution_requirements(inc: OpsIncident) -> list[str]:
    missing = []
    if not inc.root_cause or not inc.root_cause_evidence_refs:
        missing.append("root_cause_with_evidence")
    if not inc.corrective_actions:
        missing.append("corrective_action")
    if not inc.regression_test_ids and not inc.eval_case_refs:
        missing.append("regression_test_or_eval_case")
    return missing


def resolve(session: Session, incident_id: str, *, actor: str, note: str = "") -> OpsIncident:
    inc = _get(session, incident_id)
    if inc.status == "RESOLVED":
        raise Conflict("incident is already resolved")
    missing = resolution_requirements(inc)
    if missing:
        raise ValidationFailed("the incident cannot be resolved yet: " + ", ".join(missing), code="INCIDENT_INCOMPLETE",
                               details={"missing": missing})
    inc.status = "RESOLVED"
    inc.resolved_at = utcnow()
    inc.resolved_by = actor[:128]
    _touch(inc, _entry(actor, "resolved", note or "resolved"))
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor, action="ops.incident.resolve", target_type="incident",
                 target_id=inc.id, domain=inc.domain,
                 details={"regression_test_ids": inc.regression_test_ids, "eval_case_refs": inc.eval_case_refs})
    events.emit(session, source="operations", kind="ops.incident.resolved", entity_type="incident",
                entity_id=inc.id, domain=inc.domain, payload={"status": "RESOLVED"})
    return inc


def open_follow_up(session: Session, incident_id: str, *, actor_user_id: str, domain: str,
                   component_ids: list[str], title: str = "") -> dict[str, Any]:
    """§05 step 15 / §33: operations results feed the lawful change process. Files a draft ChangeProposal
    (kind UPGRADE of the affected components) linked to the incident and its evidence; a human review
    decides everything after that."""
    inc = _get(session, incident_id)
    comps = sorted({c.strip() for c in component_ids if c and c.strip()})
    if not comps:
        raise ValidationFailed("name at least one affected inventory component")
    from aeos.proposals import facade as proposals

    visible = visible_evidence(session, inc, domain) & set(inc.evidence_ids or [])
    problem = (f"Incident {inc.id}: {inc.title}. Root cause: {inc.root_cause or 'UNKNOWN (not yet evidenced)'}. "
               f"Evidence: {', '.join(sorted(visible)) or 'none'}.")
    proposal = proposals.create_proposal(
        session, actor_user_id=actor_user_id, domain=domain, kind="UPGRADE",
        title=title or f"Corrective change for incident {inc.title}"[:200], sources=[], problem=problem,
        expected_benefit="Fix the incident root cause and add a regression/eval case (incident follow-up).",
        existing_component_ids=comps)
    inc.follow_ups = [*(inc.follow_ups or []), {"kind": "change_proposal", "id": proposal.id, "domain": domain,
                                                 "status": proposal.status, "at": utcnow().isoformat()}]
    _touch(inc, {**_entry(actor_user_id, "follow_up", f"change proposal {proposal.id} filed for review"),
                 "domain": domain})
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="ops.incident.follow_up",
                 target_type="incident", target_id=inc.id, domain=domain,
                 details={"proposal_id": proposal.id, "components": comps})
    return {"incident_id": inc.id, "proposal_id": proposal.id, "proposal_status": proposal.status}


def open_from_alert(session: Session, alert_id: str, *, actor: str, domain: str | None,
                    owner: str = "") -> tuple[OpsIncident, bool]:
    """§05 step 15 / §33: an operations result (an alert) opens an incident, which then feeds the lawful change
    process (follow-up proposal, regression/eval case). One incident per alert: (incident, created)."""
    from aeos.models.operations_ext import OpsAlert
    from aeos.operations._util import iso

    alert = session.get(OpsAlert, alert_id)
    if alert is None or (alert.domain is not None and alert.domain != domain):
        raise NotFound("alert not found")
    existing = session.scalar(select(OpsIncident).where(OpsIncident.source == "alert",
                                                        OpsIncident.source_ref == alert.id))
    if existing is not None:
        return existing, False
    inc = open_incident(
        session, title=f"Alert: {alert.title}", actor=actor, owner=owner, source="alert", source_ref=alert.id,
        domain=alert.domain, project_id=alert.project_id,
        note=f"{alert.severity} {alert.kind}: {alert.title} ({alert.occurrences}x, pertama {iso(alert.first_seen_at)})")
    return inc, True


def propose_eval_case(session: Session, incident_id: str, *, actor_user_id: str, domain: str, title: str,
                      expected: str) -> dict[str, Any]:
    """§16/§33 "menambah regression/eval case": file a CANDIDATE eval case from this incident's failure (its
    alert, else its first visible evidence) for human review; the incident records it as a follow-up."""
    from aeos.evaluation import facade as evaluation

    inc = _get(session, incident_id)
    if inc.source == "alert" and inc.source_ref:
        source_type, source_id = "alert", inc.source_ref
    else:
        visible = sorted(visible_evidence(session, inc, domain) & set(inc.evidence_ids or []))
        if not visible:
            raise ValidationFailed("the incident has no alert or evidence to derive an eval case from; add evidence "
                                   "to its timeline first", code="EVAL_CASE_NO_SOURCE")
        source_type, source_id = "evidence", visible[0]
    cand = evaluation.propose_case_from_failure(session, source_type=source_type, source_id=source_id,
                                                actor_user_id=actor_user_id, title=title, expected=expected,
                                                viewer_domain=domain)
    inc.follow_ups = [*(inc.follow_ups or []), {"kind": "eval_case_candidate", "id": cand.id, "domain": domain,
                                                 "status": cand.status, "at": utcnow().isoformat()}]
    _touch(inc, {**_entry(actor_user_id, "follow_up", f"eval case candidate {cand.id} filed for review"),
                 "domain": domain})
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="ops.incident.eval_case",
                 target_type="incident", target_id=inc.id, domain=domain,
                 details={"candidate_id": cand.id, "source_type": source_type, "source_id": source_id})
    return {"incident_id": inc.id, "candidate_id": cand.id, "candidate_status": cand.status}


def list_incidents(session: Session, *, domain: str | None, status: str | None = None,
                   limit: int = 100) -> list[dict[str, Any]]:
    q = select(OpsIncident).order_by(OpsIncident.created_at.desc()).limit(min(limit, 500))
    if status:
        q = q.where(OpsIncident.status == status)
    rows = list(session.scalars(q))
    # platform-wide incidents (domain None) are listed for every domain; domain incidents only for theirs
    return [scoped(i, domain, session) for i in rows if i.domain is None or i.domain == domain]


def incidents_for_project(session: Session, project: Any) -> list[dict[str, Any]]:
    """Scoped views (project domain) of the incidents that concern *project*, oldest first: incidents recorded
    for it; platform-wide incidents (emergency stop) whose blast radius derived from records names it; and, for
    the platform project (DLY AEOS documents itself), every platform-wide incident. Read-only."""
    out = []
    for inc in session.scalars(select(OpsIncident).order_by(OpsIncident.created_at, OpsIncident.id)):
        if inc.domain is not None and inc.domain != project.domain:
            continue
        if inc.project_id is not None:
            if inc.project_id != project.id:
                continue
        else:
            affected = ((inc.derived_blast_radius or {}).get(project.domain) or {}).get("projects") or []
            if not (getattr(project, "is_platform", False) or project.id in affected):
                continue
        out.append(scoped(inc, project.domain, session))
    return out


def get_incident(session: Session, incident_id: str, *, domain: str | None) -> dict[str, Any]:
    inc = _get(session, incident_id)
    if inc.domain is not None and inc.domain != domain:
        raise NotFound("incident not found")
    return scoped(inc, domain, session)


# ----------------------------------------------------------------------------------------- report


def render_report(view: dict[str, Any], *, kind: str = "INCIDENT_REPORT") -> str:
    """INCIDENT_REPORT (while open) or POST_MORTEM (resolved) from a scoped incident view (§28)."""
    if kind not in ("INCIDENT_REPORT", "POST_MORTEM"):
        raise ValidationFailed("kind must be INCIDENT_REPORT or POST_MORTEM")
    title = "Post-mortem" if kind == "POST_MORTEM" else "Laporan insiden"
    lines = [f"# {title}: {view['title']}", "",
             f"* Incident: `{view['id']}` ({view['source']} {view.get('source_ref') or ''})".rstrip(),
             f"* Status: {view['status']}", f"* Owner: {view['owner']}",
             f"* Dibuka: {view.get('created_at')}", f"* Diselesaikan: {view.get('resolved_at') or 'belum'}", "",
             "## Timeline", ""]
    for e in view.get("timeline") or []:
        ev = f" (evidence: {', '.join(e.get('evidence_ids') or [])})" if e.get("evidence_ids") else ""
        lines.append(f"* {e.get('at')} — {e.get('kind')} oleh {e.get('by')}: {e.get('note')}{ev}")
    lines += ["", "## Dampak dan blast radius", "", view.get("impact") or "_Belum dicatat._", "",
              f"Dideklarasikan: {view.get('declared_blast_radius') or '-'}", ""]
    for d, br in (view.get("derived_blast_radius") or {}).items():
        lines.append(f"Diturunkan dari catatan ({d}): proyek {len(br.get('projects') or [])}, run "
                     f"{len(br.get('runs') or [])}, side effect {len(br.get('side_effect_targets') or [])}, "
                     f"capability {', '.join(br.get('revoked_capability_kinds') or []) or '-'}, komponen "
                     f"{', '.join(br.get('components') or []) or '-'}")
    lines += ["", "## Analisis", "", "Hipotesis:"]
    lines += [f"* {h}" for h in view.get("hypotheses") or []] or ["* _Belum ada._"]
    rc = view.get("root_cause")
    lines += ["", f"Root cause: {rc if rc else 'UNKNOWN — belum didukung bukti'}"]
    if view.get("root_cause_evidence_refs"):
        lines.append(f"Bukti: {', '.join(view['root_cause_evidence_refs'])}")
    lines += ["", "## Tindakan korektif", ""]
    lines += [f"* {a.get('description')} {('(' + a['ref'] + ')') if a.get('ref') else ''}".rstrip()
              for a in view.get("corrective_actions") or []] or ["* _Belum ada._"]
    lines += ["", "## Regression / eval case", ""]
    lines += [f"* `{t}`" for t in (view.get("regression_test_ids") or []) + (view.get("eval_case_refs") or [])] \
        or ["* _Belum ada — insiden tidak dapat ditutup._"]
    lines += ["", "## Tindak lanjut", ""]
    lines += [f"* {f.get('kind')} `{f.get('id')}` ({f.get('status')})" for f in view.get("follow_ups") or []] \
        or ["* _Belum ada._"]
    missing = view.get("resolution_requirements") or []
    if missing:
        lines += ["", f"_Belum dapat ditutup: {', '.join(missing)}._"]
    return "\n".join(lines) + "\n"
