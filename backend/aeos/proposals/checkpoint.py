"""Claude checkpoint review of the trial evidence (§39 "Claude tertinggi menilai konsistensi dan evidence
pada checkpoint"; §40 flow node "Claude review bukti" before "Kriteria dan authority terpenuhi").

The package holds the candidate/baseline hashes, the owner's pre-declared acceptance and summaries of the
trusted evidence bound to this candidate. Its hash binds the review: attaching other evidence or changing
the candidate makes the record STALE. Paths, honest by construction:

* ``with_claude`` -> official ClaudeNativeBridge (never the Claude API); OFFICE is never sent (§23).
* ``manual_result`` -> the owner ran the package in official Claude Code and submits the structured result
  (manual fallback, mode MANUAL_FALLBACK).
* ``acknowledge_not_proven`` -> only after the bridge could not run for this exact package (or for OFFICE):
  the owner records NOT-PROVEN explicitly. It is shown as NOT-PROVEN, never as a pass.

A verdict is DATA for the owner, not approval. PROMOTE needs a current record that is not FAIL/BLOCKED and
not waiting for quota.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.enums import Domain
from aeos.core.errors import AeosError, Conflict, ValidationFailed
from aeos.core.hashing import content_hash
from aeos.core.redaction import redact, redact_text
from aeos.core.timeutil import utcnow
from aeos.events import service as events
from aeos.models.research import ChangeProposal

CHECKPOINT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["review", "verify", "verdict", "rationale"],
    "additionalProperties": False,
    "properties": {
        "review": {"type": "object", "required": ["summary"], "properties": {
            "summary": {"type": "string"}, "consistency": {"type": "string"},
            "risks": {"type": "array", "items": {"type": "string"}},
        }},
        "verify": {"type": "object", "required": ["claims"], "properties": {
            "claims": {"type": "array", "items": {"type": "object", "required": ["claim", "status"], "properties": {
                "claim": {"type": "string"}, "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "status": {"enum": ["SUPPORTED", "UNSUPPORTED", "UNKNOWN"]}}}},
            "unknowns": {"type": "array", "items": {"type": "string"}},
        }},
        "verdict": {"enum": ["PASS", "NOT-PROVEN", "BLOCKED", "FAIL"]},
        "rationale": {"type": "string"},
    },
}
GATE_OK = ("COMPLETED", "NOT_PROVEN_ACKNOWLEDGED")
UNAVAILABLE = ("NOT_RUN", "QUOTA_WAIT", "FAILED")
LABEL = "Checkpoint Claude atas bukti adalah DATA review; verdict bukan persetujuan pemilik"


def evidence_package(session: Session, p: ChangeProposal) -> dict[str, Any]:
    from aeos.proposals import review as R
    from aeos.proposals import service

    evs = service.bound_evidence(session, p, p.evidence_ids or [], purpose="gate")
    return {
        "proposal_kind": p.kind, "candidate_pin": p.candidate_pin, "candidate_hash": p.candidate_hash,
        "baseline_hash": p.baseline_hash,
        "acceptance": [{k: a.get(k) for k in ("id", "criterion", "metric", "threshold", "baseline_value", "suite")}
                       for a in R.owner_acceptance(p)],
        "evidence": [{"id": e.id, "kind": e.kind, "origin": e.origin, "collector": e.collector_identity,
                      "level": e.evidence_level, "claim": redact_text(e.claim or "")[:300],
                      "content_hash": e.content_hash, "task_id": e.task_id, "run_id": e.run_id,
                      "created_at": e.created_at.isoformat() if e.created_at else None} for e in evs],
        "trial": {k: (p.trial or {}).get(k) for k in ("status", "environment", "caps")},
    }


def current(p: ChangeProposal) -> dict[str, Any] | None:
    return (p.checkpoint_review or {}).get("current")


def gate(session: Session, p: ChangeProposal) -> tuple[bool, str, str, dict[str, Any] | None]:
    rec = current(p)
    pkg_hash = content_hash(evidence_package(session, p))
    if rec is None:
        return False, "CHECKPOINT_REVIEW_REQUIRED", "a Claude checkpoint review of the trial evidence is required " \
                                                    "before PROMOTE (or an explicit NOT-PROVEN record)", None
    if rec.get("package_hash") != pkg_hash or rec.get("candidate_hash") != p.candidate_hash:
        return False, "CHECKPOINT_REVIEW_STALE", "the checkpoint review covered other evidence/candidate; " \
                                                 "review the current evidence again", rec
    if rec.get("status") == "QUOTA_WAIT":
        return False, "CHECKPOINT_REVIEW_WAITING", "checkpoint review waits for Claude quota; the gate is not skipped", rec
    if rec.get("status") not in GATE_OK:
        return False, "CHECKPOINT_REVIEW_REQUIRED", f"checkpoint review status {rec.get('status')}; run it or record " \
                                                    "the manual result / NOT-PROVEN", rec
    if rec.get("verdict") in ("FAIL", "BLOCKED"):
        return False, "CHECKPOINT_REVIEW_FAILED", f"checkpoint verdict {rec.get('verdict')}: promotion refused", rec
    return True, "OK", "", rec


def _validate(structured: Any) -> dict[str, Any]:
    import jsonschema

    try:
        jsonschema.validate(structured, CHECKPOINT_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise ValidationFailed(f"checkpoint result does not match the schema: {exc.message[:200]}") from exc
    return dict(redact(structured))


def _bridge(p: ChangeProposal, pkg: dict[str, Any]) -> dict[str, Any]:
    from aeos.proposals.review import REVIEW_MODEL

    if p.domain == Domain.OFFICE.value:
        return {"status": "NOT_RUN", "reason": "OFFICE: bukti tidak dikirim ke Claude tanpa otorisasi data kantor (§23)"}
    prompt = ("You are the highest-tier Claude checkpoint reviewer for a DLY AEOS change proposal trial. Judge only "
              "whether the evidence below is consistent and sufficient for the pre-declared acceptance criteria. "
              "Return REVIEW / VERIFY / VERDICT (PASS, NOT-PROVEN, BLOCKED or FAIL). Claims without evidence are "
              "UNKNOWN. You cannot approve anything; your verdict is advisory data for the human owner.\n\n"
              + json.dumps(pkg, ensure_ascii=True, default=str))
    try:
        from aeos.claude_bridge.facade import ClaudeRequest, run_structured

        res = run_structured(ClaudeRequest(purpose="review.proposal.checkpoint", role="proposal-checkpoint-reviewer",
                                           model=REVIEW_MODEL, prompt=prompt, json_schema=CHECKPOINT_SCHEMA,
                                           project_id=None, domain=p.domain, timeout_seconds=900, allowed_tools=[]))
    except (ImportError, ModuleNotFoundError):
        return {"status": "NOT_RUN", "reason": "ClaudeNativeBridge belum tersedia; jalankan paket di Claude Code resmi "
                                               "lalu kirim hasilnya (tanpa Claude API)"}
    except AeosError as exc:
        return {"status": "NOT_RUN", "reason": f"{exc.code}: {exc.message}"}
    base = {"model_requested": res.model_requested, "model_resolved": res.model_resolved, "mode": res.mode}
    if not res.ok or not isinstance(res.structured, dict):
        return base | {"status": "QUOTA_WAIT" if res.quota_exhausted else "FAILED",
                       "reason": res.error_code or res.error or "no structured result"}
    try:
        s = _validate(res.structured)
    except ValidationFailed as exc:
        return base | {"status": "FAILED", "reason": exc.message}
    return base | {"status": "COMPLETED", **{k: s[k] for k in ("review", "verify", "verdict", "rationale")}}


def record(session: Session, p: ChangeProposal, *, actor_user_id: str, with_claude: bool = False,
           manual_result: dict[str, Any] | None = None, acknowledge_not_proven: bool = False,
           rationale: str = "") -> dict[str, Any]:
    from aeos.proposals import service

    if p.status not in service.TRIAL_STATUSES:
        raise Conflict(f"the evidence checkpoint runs during an approved trial (current {p.status})")
    pkg = evidence_package(session, p)
    if not pkg["evidence"]:
        raise Conflict("no trusted evidence about this candidate is attached yet", code="NOT_PROVEN")
    pkg_hash = content_hash(pkg)
    prev = current(p)
    now = utcnow().isoformat()
    rec: dict[str, Any] = {"package_hash": pkg_hash, "candidate_hash": p.candidate_hash, "label": LABEL,
                           "evidence_ids": [e["id"] for e in pkg["evidence"]], "at": now, "actor": actor_user_id}
    if manual_result is not None:
        s = _validate(manual_result)
        rec |= {"status": "COMPLETED", "mode": "MANUAL_FALLBACK", **{k: s[k] for k in ("review", "verify", "verdict",
                                                                                        "rationale")}}
    elif with_claude:
        rec |= {"mode": "NATIVE"} | _bridge(p, pkg)
    elif acknowledge_not_proven:
        same = prev is not None and prev.get("package_hash") == pkg_hash and prev.get("status") in UNAVAILABLE
        if not (same or p.domain == Domain.OFFICE.value):
            raise Conflict("NOT-PROVEN may be recorded only after the Claude checkpoint could not run for this exact "
                           "evidence package (or for OFFICE); request the review first",
                           code="CHECKPOINT_NOT_ATTEMPTED")
        if len((rationale or "").strip()) < 10:
            raise ValidationFailed("recording NOT-PROVEN needs the owner's rationale (min 10 characters)")
        rec |= {"status": "NOT_PROVEN_ACKNOWLEDGED", "mode": "OWNER_RECORD", "verdict": "NOT-PROVEN",
                "rationale": redact_text(rationale)[:2000],
                "reason": "Checkpoint Claude tidak dapat dijalankan; pemilik mencatat NOT-PROVEN secara eksplisit"}
    else:
        rec |= {"status": "MANUAL_PACKAGE", "mode": "MANUAL_FALLBACK", "package": pkg, "schema": CHECKPOINT_SCHEMA,
                "reason": "Jalankan paket ini di Claude Code resmi lalu kirim hasil terstruktur (manual_result)"}
    hist = [*((p.checkpoint_review or {}).get("history") or []),
            {k: rec.get(k) for k in ("status", "verdict", "mode", "at", "actor", "package_hash")}]
    p.checkpoint_review = {"current": rec, "history": hist[-50:]}
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="proposal.checkpoint.record",
                 target_type=service.TARGET_TYPE, target_id=p.id, project_id=p.project_id, domain=p.domain,
                 details={"status": rec.get("status"), "verdict": rec.get("verdict"), "mode": rec.get("mode"),
                          "package_hash": pkg_hash})
    events.emit(session, source="proposals", kind="proposal.checkpoint", entity_type=service.TARGET_TYPE, entity_id=p.id,
                project_id=p.project_id, domain=p.domain, payload={"status": rec.get("status"),
                                                                    "verdict": rec.get("verdict")})
    return rec
