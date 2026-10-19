"""Owner-declared plan of a ChangeProposal (§39, §40).

The owner states, before the trial, what success means (acceptance criteria with metric/threshold), who
operates the result, and - for REPLACE/UPGRADE - the cutover order, retirement point, rollback/forward
recovery and the restore-tested backup. Those *bound* keys are part of the candidate hash, so changing
them after a decision re-binds every authority (§40 "authority terkait harus dinilai ulang").

Execution progress recorded later (REMOVE closure per item, canary evidence) is kept in the same record
but outside the hash: it documents what was done under the decision, it does not change what was approved.
Nothing here installs, activates or decides anything.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from aeos.core.errors import Conflict, ValidationFailed
from aeos.core.redaction import redact, redact_text
from aeos.models.evidence import EvidenceArtifact
from aeos.models.research import ChangeProposal, EvalSuite

BOUND_KEYS = ("acceptance", "replacement", "canary", "operations_owner")
PROGRESS_KEYS = ("removal", "canary_evidence_ids")
CLOSURE_STATUSES = ("OPEN", "CLOSED", "WAIVED")
MAX_CRITERIA = 20


def bound_part(plan: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (plan or {}).items() if k in BOUND_KEYS and v not in (None, "", [], {})}


def _text(v: Any, n: int, field: str) -> str:
    if v is None:
        return ""
    if not isinstance(v, (str, int, float)) or isinstance(v, bool):
        raise ValidationFailed(f"{field} must be text")
    return redact_text(str(v)).strip()[:n]


def _acceptance(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) > MAX_CRITERIA:
        raise ValidationFailed(f"acceptance must be a list of at most {MAX_CRITERIA} criteria")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValidationFailed(f"acceptance[{i}] must be an object")
        crit = _text(item.get("criterion"), 500, f"acceptance[{i}].criterion")
        if not crit:
            raise ValidationFailed(f"acceptance[{i}].criterion is required")
        cid = _text(item.get("id"), 32, f"acceptance[{i}].id") or f"AC{i + 1}"
        if cid in seen:
            raise ValidationFailed(f"acceptance id {cid} is duplicated")
        seen.add(cid)
        out.append({
            "id": cid, "criterion": crit,
            "metric": _text(item.get("metric"), 200, f"acceptance[{i}].metric"),
            "baseline_value": _text(item.get("baseline_value"), 100, f"acceptance[{i}].baseline_value") or "UNKNOWN",
            "threshold": _text(item.get("threshold"), 100, f"acceptance[{i}].threshold"),
            "suite": _text(item.get("suite"), 96, f"acceptance[{i}].suite") or None,
            "required": item.get("required") is not False,
        })
    return out


def _trusted_restore_evidence(session: Session, p: ChangeProposal, evidence_id: str) -> str:
    ev = session.get(EvidenceArtifact, evidence_id)
    if ev is None or ev.domain not in (None, p.domain):
        raise ValidationFailed(f"evidence {evidence_id} not found in this domain")
    if not ev.trusted or ev.mode != "LIVE" or (ev.kind != "RESTORE_TEST" and ev.origin != "restore-test"):
        raise ValidationFailed("backup_restore_evidence_id must be trusted LIVE restore-test evidence "
                               "(aeos ops backup restore-test)")
    return ev.id


def _replacement(session: Session, p: ChangeProposal, raw: Any) -> dict[str, Any]:
    if p.kind not in ("REPLACE", "UPGRADE", "REMOVE"):
        raise ValidationFailed("a replacement plan applies to REPLACE/UPGRADE/REMOVE proposals")
    if not isinstance(raw, dict):
        raise ValidationFailed("replacement must be an object")
    out: dict[str, Any] = {}
    for key, n in (("cutover_order", 2000), ("retire_old_version_after", 300), ("rollback_or_forward_recovery", 2000),
                   ("version_schema_data_compatibility", 2000), ("operations_owner", 128)):
        val = _text(raw.get(key), n, f"replacement.{key}")
        if val:
            out[key] = val
    if raw.get("coexistence_safe") is not None:
        if not isinstance(raw["coexistence_safe"], bool):
            raise ValidationFailed("replacement.coexistence_safe must be true/false")
        out["coexistence_safe"] = raw["coexistence_safe"]
    for key in ("backup_restore_evidence_id", "rollback_evidence_id"):
        if raw.get(key):
            out[key] = _trusted_restore_evidence(session, p, str(raw[key])) if key == "backup_restore_evidence_id" \
                else _trusted_any(session, p, str(raw[key]))
    return out


def _trusted_any(session: Session, p: ChangeProposal, evidence_id: str) -> str:
    ev = session.get(EvidenceArtifact, evidence_id)
    if ev is None or ev.domain not in (None, p.domain):
        raise ValidationFailed(f"evidence {evidence_id} not found in this domain")
    if not ev.trusted or ev.mode != "LIVE":
        raise ValidationFailed(f"evidence {evidence_id} is not trusted LIVE evidence")
    return ev.id


def _canary_evidence(session: Session, p: ChangeProposal, evidence_id: str) -> str:
    """§40 canary "evidence sebelum pelebaran": trusted evidence about THIS candidate (linked trial task/run/eval,
    verifier evidence naming the proposal, or an observation of its components) produced after the TRIAL decision."""
    from aeos.proposals import service

    ev = session.get(EvidenceArtifact, _trusted_any(session, p, evidence_id))
    why = service.evidence_binding_problem(session, p, ev, purpose="outcome")  # type: ignore[arg-type]
    if why:
        raise ValidationFailed(f"canary evidence {evidence_id}: {why}", code="EVIDENCE_NOT_FOR_CANDIDATE")
    if not service._after_trial(session, p, ev):  # type: ignore[arg-type]
        raise ValidationFailed(f"canary evidence {evidence_id} predates the TRIAL decision",
                               code="EVIDENCE_NOT_FOR_CANDIDATE")
    return ev.id  # type: ignore[union-attr]


def _canary(raw: Any) -> dict[str, Any] | None:
    if raw in (None, {}):
        return None
    if not isinstance(raw, dict):
        raise ValidationFailed("canary must be an object")
    out: dict[str, Any] = {k: _text(raw.get(k), 500, f"canary.{k}") for k in ("target", "cap", "observation_window")}
    stops = raw.get("stop_criteria")
    if isinstance(stops, str):
        stops = [s for s in (x.strip() for x in stops.splitlines()) if s]
    if not isinstance(stops, list) or not stops:
        raise ValidationFailed("canary.stop_criteria must list at least one stop criterion")
    missing = [k for k, v in out.items() if not v]
    if missing:
        raise ValidationFailed("a canary needs target, cap, observation_window and stop_criteria",
                               details={"missing": missing})
    out["stop_criteria"] = [_text(s, 300, "canary.stop_criteria") for s in stops][:20]
    out["widening_requires_evidence"] = True
    return out


def _removal(session: Session, p: ChangeProposal, raw: Any) -> dict[str, Any]:
    from aeos.proposals import review as R

    if p.kind != "REMOVE":
        raise ValidationFailed("removal closure applies to REMOVE proposals")
    if not isinstance(raw, dict):
        raise ValidationFailed("removal must map item ids to {status, note, evidence_id}")
    baseline, _bh, _x = R.compute_baseline(session, p)
    known = {i["id"] for i in (R.removal_plan(p, baseline) or {}).get("items", [])}
    out = dict((p.owner_plan or {}).get("removal") or {})
    for item_id, rec in raw.items():
        if item_id not in known:
            raise ValidationFailed(f"removal item {item_id!r} is not part of the current removal plan")
        if not isinstance(rec, dict) or rec.get("status") not in CLOSURE_STATUSES:
            raise ValidationFailed(f"removal[{item_id}].status must be one of {CLOSURE_STATUSES}")
        entry: dict[str, Any] = {"status": rec["status"], "note": _text(rec.get("note"), 1000, "removal.note")}
        if rec["status"] == "WAIVED" and len(entry["note"]) < 5:
            raise ValidationFailed(f"removal[{item_id}]: a WAIVED item needs the owner's reason (note)")
        if rec.get("evidence_id"):
            entry["evidence_id"] = _trusted_any(session, p, str(rec["evidence_id"]))
        out[item_id] = entry
    return out


def normalise(session: Session, p: ChangeProposal, patch: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(patch, dict):
        raise ValidationFailed("plan must be an object")
    unknown = sorted(set(patch) - set(BOUND_KEYS) - set(PROGRESS_KEYS))
    if unknown:
        raise ValidationFailed("unknown plan keys", details={"unknown": unknown,
                                                              "allowed": [*BOUND_KEYS, *PROGRESS_KEYS]})
    plan = dict(p.owner_plan or {})
    for key in ("acceptance", "replacement", "removal", "canary_evidence_ids"):
        if key in patch and patch[key] is None:
            plan.pop(key, None)
            patch = {k: v for k, v in patch.items() if k != key}
    if "acceptance" in patch:
        plan["acceptance"] = _acceptance(patch["acceptance"])
        suites = {a["suite"] for a in plan["acceptance"] if a.get("suite")}
        if suites:
            from sqlalchemy import select

            existing = set(session.scalars(select(EvalSuite.suite_key).where(EvalSuite.suite_key.in_(sorted(suites)))))
            if suites - existing:
                raise ValidationFailed("acceptance names unknown eval suites", details={"unknown": sorted(suites - existing)})
    if "replacement" in patch:
        plan["replacement"] = _replacement(session, p, patch["replacement"])
    if "canary" in patch:
        canary = _canary(patch["canary"])
        if canary is None:
            plan.pop("canary", None)
        else:
            plan["canary"] = canary
    if "operations_owner" in patch:
        owner = _text(patch["operations_owner"], 128, "operations_owner")
        if owner:
            plan["operations_owner"] = owner
        else:
            plan.pop("operations_owner", None)
    if "removal" in patch:
        plan["removal"] = _removal(session, p, patch["removal"])
    if "canary_evidence_ids" in patch:
        ids = patch["canary_evidence_ids"]
        if not isinstance(ids, list):
            raise ValidationFailed("canary_evidence_ids must be a list")
        if "canary" not in plan:
            raise ValidationFailed("record canary evidence only after a canary plan was declared")
        plan["canary_evidence_ids"] = sorted({_canary_evidence(session, p, str(i)) for i in ids})
    return redact(plan)


def update(session: Session, p: ChangeProposal, patch: dict[str, Any], *, actor_user_id: str) -> dict[str, Any]:
    """Store the owner plan. A change of a bound key re-reviews (new candidate hash; bound decisions are
    invalidated by the review). Progress keys never touch the hash."""
    from aeos.audit import service as audit
    from aeos.events import service as events
    from aeos.proposals import service

    if p.parent_id:
        raise Conflict("the plan belongs to the parent proposal")
    if p.status in service.OUTCOME_FINAL and set(patch) - {"removal", "canary_evidence_ids"}:
        raise Conflict(f"outcome already recorded ({p.status}); open a new proposal")
    if p.kind == "COMPARE":
        raise Conflict("COMPARE proposals have no trial/implementation plan")
    before = bound_part(p.owner_plan)
    new = normalise(session, p, patch)
    if p.status == "PROMOTION_APPROVED" and bound_part(new) != before:
        raise Conflict("promotion is approved for this exact plan; record the outcome or decide KEEP/REJECT before "
                       "changing what was approved", code="PLAN_LOCKED_BY_PROMOTION")
    p.owner_plan = new
    session.flush()
    rebound = bound_part(new) != before
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="proposal.plan.update",
                 target_type=service.TARGET_TYPE, target_id=p.id, project_id=p.project_id, domain=p.domain,
                 details={"keys": sorted(patch), "rebinds_candidate": rebound})
    events.emit(session, source="proposals", kind="proposal.plan.updated", entity_type=service.TARGET_TYPE,
                entity_id=p.id, project_id=p.project_id, domain=p.domain, payload={"keys": sorted(patch)})
    if rebound and p.review_package and p.status not in service.OUTCOME_FINAL:
        service.review(session, p, actor_user_id=actor_user_id, domain=p.domain)
    return {"rebinds_candidate": rebound, "owner_plan": p.owner_plan}
