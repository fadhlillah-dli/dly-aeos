"""ChangeProposal service (§39, §40, §41, §50, ADR-0082).

* Submitting or reviewing a proposal never installs, activates, changes routes/policy, raises a
  budget or grants credentials. COMPARE ("review repo") can never be trial-approved or promoted.
* Duplicates are linked through canonical source identity within the SAME domain only — Office and
  Personal contexts are never merged.
* TRIAL_APPROVE and PROMOTE are different human decisions, each recorded through the Decision
  registry and bound to ``baseline_hash`` + ``candidate_hash`` + scope (provider/data class/budget/
  target). Any change of baseline/candidate invalidates the binding; a stale binding is refused.
* Outcomes are ACCEPTED / ROLLED_BACK / FAILED / PARTIAL; a rollback is never recorded as acceptance
  and the history keeps every outcome with its evidence.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core.enums import Domain
from aeos.core.errors import Blocked, Conflict, NotFound, PolicyDenied, ValidationFailed
from aeos.core.hashing import content_hash
from aeos.core.redaction import redact, redact_text
from aeos.core.timeutil import ensure_aware, utcnow
from aeos.events import service as events
from aeos.models.audit import AuditEvent
from aeos.models.decisions import ArchitectureSnapshot, Decision
from aeos.models.evidence import EvidenceArtifact
from aeos.models.research import ChangeProposal, ResearchEntry
from aeos.proposals import review as R
from aeos.research import service as research
from aeos.research.identity import IdentityError, canonical_identity

KINDS = ("ADD", "REPLACE", "UPGRADE", "REMOVE", "COMPARE")
ACTIONS = ("TRIAL_APPROVE", "PROMOTE", "KEEP", "REJECT", "DEFER", "REQUEST_MORE_EVIDENCE", "REFERENCE_ONLY")
OUTCOMES = ("ACCEPTED", "ROLLED_BACK", "FAILED", "PARTIAL")
ACTION_DECISION_KIND = {"TRIAL_APPROVE": "TRIAL", "PROMOTE": "PROMOTION", "KEEP": "REJECTION", "REJECT": "REJECTION",
                        "DEFER": "REVISION_REQUEST", "REQUEST_MORE_EVIDENCE": "REVISION_REQUEST",
                        "REFERENCE_ONLY": "APPROVAL"}
ACTION_STATUS = {"TRIAL_APPROVE": "TRIAL_APPROVED", "PROMOTE": "PROMOTION_APPROVED", "KEEP": "KEEP",
                 "REJECT": "REJECTED", "DEFER": "DEFERRED", "REQUEST_MORE_EVIDENCE": "REQUEST_MORE_EVIDENCE",
                 "REFERENCE_ONLY": "REFERENCE_ONLY"}
ACTION_OPERATIONS = {"TRIAL_APPROVE": ["trial.sandbox"], "PROMOTE": ["promote.candidate"]}
OUTCOME_FINAL = ("ACCEPTED", "ROLLED_BACK", "FAILED", "PARTIAL")
CLOSING_OUTCOMES = ("ROLLED_BACK", "FAILED")  # close the proposal; a retry needs a new proposal
# Outcome transitions: ACCEPTED may later be rolled back/fail/turn partial; PARTIAL may be completed.
OUTCOME_TRANSITIONS = {None: OUTCOMES, "ACCEPTED": ("ROLLED_BACK", "FAILED", "PARTIAL"), "PARTIAL": OUTCOMES,
                       "ROLLED_BACK": (), "FAILED": ()}
RESEARCH_SYNC = {"REJECT": "REJECTED", "DEFER": "DEFERRED", "REFERENCE_ONLY": "REFERENCE_ONLY"}
SCOPE_REQUIRED = ("target", "environment", "provider", "data_class", "budget_usd")
DATA_ROLLBACK = ("DONE", "NOT_POSSIBLE", "NOT_APPLICABLE", "UNKNOWN")
LIMIT_KEYS = ("cost_usd", "max_minutes", "max_attempts", "data_class", "resource")
TARGET_TYPE = "change_proposal"


class StaleBinding(Conflict):
    """Baseline/candidate changed since review; related decisions were invalidated (commit, then raise)."""

    code = "STALE_BINDING"


# --------------------------------------------------------------------------- helpers


def _policy() -> Any:
    try:
        from aeos.policy import facade as policy

        return policy
    except (ImportError, ModuleNotFoundError) as exc:  # pragma: no cover - facade ships with the foundation
        raise Blocked("decision registry unavailable", code="DECISION_REGISTRY_UNAVAILABLE") from exc


def _record_decision(session: Session, **kw: Any) -> Decision:
    try:
        return _policy().record_decision(session, **kw)
    except (ImportError, ModuleNotFoundError) as exc:
        raise Blocked("decision registry (aeos.policy) unavailable; nothing was decided (fail closed)",
                      code="DECISION_REGISTRY_UNAVAILABLE",
                      details={"reasons": ["policy decision registry not available"]}) from exc


def _invalidate(session: Session, p: ChangeProposal, reason: str) -> int:
    """Invalidate ACTIVE decisions bound to this proposal. Fails closed when decisions exist but the
    registry cannot invalidate them."""
    active = [d for d in _decisions(session, p) if d.status == "ACTIVE"]
    if not active:
        return 0
    try:
        return int(_policy().invalidate_decisions(session, target_type=TARGET_TYPE, target_id=p.id, reason=reason) or 0)
    except (ImportError, ModuleNotFoundError) as exc:
        raise Blocked("cannot invalidate stale decisions: decision registry unavailable (fail closed)",
                      code="DECISION_REGISTRY_UNAVAILABLE") from exc


def _decisions(session: Session, p: ChangeProposal) -> list[Decision]:
    return list(session.scalars(select(Decision).where(Decision.target_type == TARGET_TYPE, Decision.target_id == p.id)
                                .order_by(Decision.created_at)))


def normalise_scope(scope: dict[str, Any] | None) -> dict[str, Any]:
    s = dict(redact(scope or {}))
    if "budget_usd" in s and s["budget_usd"] is not None:
        try:
            s["budget_usd"] = float(s["budget_usd"])
        except (TypeError, ValueError) as exc:
            raise ValidationFailed("scope.budget_usd must be a number") from exc
        if s["budget_usd"] < 0:
            raise ValidationFailed("scope.budget_usd must be >= 0")
    return {k: s[k] for k in sorted(s)}


def binding_hash(proposal_id: str, action: str, baseline_hash: str, candidate_hash: str, scope: dict[str, Any]) -> str:
    return content_hash({"proposal_id": proposal_id, "action": action, "baseline_hash": baseline_hash,
                         "candidate_hash": candidate_hash, "scope": scope})


def _children(session: Session, p: ChangeProposal) -> list[ChangeProposal]:
    return list(session.scalars(select(ChangeProposal).where(ChangeProposal.parent_id == p.id)
                                .order_by(ChangeProposal.created_at)))


def current_hashes(session: Session, p: ChangeProposal) -> tuple[str, str]:
    _b, bh, _x = R.compute_baseline(session, p)
    _c, ch = R.compute_candidate(session, p, _children(session, p))
    return bh, ch


def get_proposal(session: Session, proposal_id: str, *, domain: str | None,
                 allowed_project_ids: list[str] | None = None) -> ChangeProposal:
    p = session.get(ChangeProposal, proposal_id)
    if p is None or p.domain != domain:
        raise NotFound("proposal not found")  # cross-domain is a 404 (§35)
    if p.project_id and allowed_project_ids is not None and p.project_id not in allowed_project_ids:
        raise NotFound("proposal not found")
    return p


# --------------------------------------------------------------------------- create


def _identities(sources: list[str]) -> tuple[list[str], list[str]]:
    out: list[str] = []
    refs: list[str] = []
    for raw in sources:
        s = (raw or "").strip()
        if not s:
            continue
        try:
            ident = canonical_identity(s)
        except IdentityError as exc:
            raise ValidationFailed(f"invalid source {s[:80]!r}: {exc}") from exc
        refs.append(redact_text(s)[:512])
        if ident not in out:
            out.append(ident)
    return out, refs


def _root(session: Session, p: ChangeProposal) -> ChangeProposal:
    seen = {p.id}
    while p.duplicate_of:
        nxt = session.get(ChangeProposal, p.duplicate_of)
        if nxt is None or nxt.id in seen:
            break
        seen.add(nxt.id)
        p = nxt
    return p


def find_duplicate(session: Session, identities: list[str], *, domain: str,
                   family_id: str | None = None) -> ChangeProposal | None:
    """Earliest proposal in the SAME domain sharing a canonical identity (parts of one parent are
    explicit components of a single proposal, not duplicates of each other)."""
    if not identities:
        return None
    wanted = set(identities)
    rows = session.scalars(select(ChangeProposal).where(ChangeProposal.domain == domain).order_by(ChangeProposal.created_at))
    for row in rows:
        if family_id and (row.id == family_id or row.parent_id == family_id):
            continue
        if wanted & set(row.canonical_identities or []):
            return _root(session, row)
    return None


def _validate_attachments(session: Session, attachment_ids: list[str], domain: str) -> list[str]:
    from aeos.intake import facade as intake

    out = []
    for aid in attachment_ids:
        a = intake.get_attachment(session, aid)
        if a is None or a.domain != domain:
            raise ValidationFailed(f"attachment {aid} not found in this domain")
        if a.scan_status == "REJECTED":
            raise ValidationFailed(f"attachment {aid} was rejected by intake scanning")
        out.append(a.id)
    return out


def _limits(limits: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    lim = dict(redact(limits or {}))
    unknown = []
    for k in LIMIT_KEYS:
        if lim.get(k) in (None, ""):
            lim[k] = R.UNKNOWN
            unknown.append(f"limits.{k}")
    return lim, unknown


def create_proposal(
    session: Session, *, actor_user_id: str, domain: str, kind: str, title: str | None, sources: list[str],
    problem: str = "", expected_benefit: str = "", scope_type: str = "PLATFORM", project_id: str | None = None,
    existing_component_ids: list[str] | None = None, candidate_component_ids: list[str] | None = None,
    limits: dict[str, Any] | None = None, attachment_ids: list[str] | None = None, candidate_pin: str | None = None,
    parts: list[dict[str, Any]] | None = None, parent_id: str | None = None, emit: bool = True,
) -> ChangeProposal:
    kind = (kind or "").upper()
    if kind not in KINDS:
        raise ValidationFailed(f"kind must be one of {KINDS}")
    if scope_type not in ("PLATFORM", "PROJECT"):
        raise ValidationFailed("scope_type must be PLATFORM or PROJECT")
    if scope_type == "PROJECT" and not project_id:
        raise ValidationFailed("project_id is required for a PROJECT-scoped proposal")
    identities, refs = _identities(sources or [])
    existing = sorted({c.strip() for c in existing_component_ids or [] if c and c.strip()})
    if not identities and not existing and not parts:
        raise ValidationFailed("give at least one repo/docs/model/tool URL or name, or an existing component")
    if kind in ("REPLACE", "UPGRADE", "REMOVE") and not existing:
        raise ValidationFailed(f"{kind} needs the existing component it changes")
    resolved = [research.resolve_alias(session, i) for i in identities]
    lim, unknown = _limits(limits)
    if not candidate_pin and kind != "COMPARE":
        unknown.append("candidate_pin")
    for f in ("license", "maintainer", "distribution", "terms"):
        unknown.append(f"source.{f}")
    from aeos.inventory import facade as inventory

    for cid in existing:
        if inventory.get_component(session, cid) is None:
            unknown.append(f"existing_component:{cid} (tidak ada di inventaris)")
    atts = _validate_attachments(session, attachment_ids or [], domain)
    entry_ids = []
    for ident in resolved:
        e = research.find_by_identity(session, ident, domain=domain)
        if e is not None:
            entry_ids.append(e.id)
    duplicate = find_duplicate(session, resolved, domain=domain, family_id=parent_id)
    fallback_title = refs[0] if refs else (existing[0] if existing else f"{kind} (bagian majemuk)")
    p = ChangeProposal(
        kind=kind, title=redact_text((title or "").strip() or fallback_title)[:256],
        parent_id=parent_id, submitted_by=actor_user_id, scope_type=scope_type, project_id=project_id, domain=domain,
        source_refs=refs, problem=redact_text(problem or "")[:8000], expected_benefit=redact_text(expected_benefit or "")[:8000],
        existing_component_ids=existing, candidate_component_ids=sorted(set(candidate_component_ids or [])),
        limits=lim, candidate_pin=redact_text(candidate_pin)[:128] if candidate_pin else None, status="SUBMITTED",
        review_package={}, decision_ids=[], task_ids=[], evidence_ids=[], outcome={}, attachment_ids=atts,
        canonical_identities=resolved, duplicate_of=duplicate.id if duplicate else None, parts=[],
        unknown_fields=unknown, research_entry_ids=entry_ids,
    )
    session.add(p)
    session.flush()
    if atts:
        from aeos.intake import facade as intake

        for aid in atts:  # provenance link (.md briefs etc., U-11); the attachment stays untrusted DATA
            intake.link_attachment(session, aid, proposal_id=p.id)
    if parts:
        _create_parts(session, p, parts, actor_user_id=actor_user_id)
    if emit:
        audit.record(session, actor_type="user", actor_id=actor_user_id, action="proposal.create", target_type=TARGET_TYPE,
                     target_id=p.id, project_id=project_id, domain=domain,
                     details={"kind": kind, "identities": resolved, "duplicate_of": p.duplicate_of,
                              "installed": False, "activated": False, "budget_changed": False})
        events.emit(session, source="proposals", kind="proposal.created", entity_type=TARGET_TYPE, entity_id=p.id,
                    project_id=project_id, domain=domain,
                    payload={"kind": kind, "status": p.status, "duplicate_of": p.duplicate_of})
    return p


def _create_parts(session: Session, parent: ChangeProposal, parts: list[dict[str, Any]], *, actor_user_id: str) -> None:
    n = len(parts)
    deps: list[list[int]] = []
    for i, part in enumerate(parts):
        d = part.get("depends_on") or []
        if not all(isinstance(x, int) and 0 <= x < n and x != i for x in d):
            raise ValidationFailed(f"part {i}: depends_on must reference other part indexes")
        deps.append(sorted(set(d)))
    # reject dependency cycles (an explicit, acyclic set of parts — §39)
    state = [0] * n

    def visit(i: int) -> None:
        if state[i] == 1:
            raise ValidationFailed("parts have a dependency cycle")
        if state[i] == 2:
            return
        state[i] = 1
        for j in deps[i]:
            visit(j)
        state[i] = 2

    for i in range(n):
        visit(i)
    children = []
    for part in parts:
        child = create_proposal(
            session, actor_user_id=actor_user_id, domain=parent.domain or "", kind=part.get("kind") or parent.kind,
            title=part.get("title"), sources=part.get("sources") or [], problem=part.get("problem") or parent.problem,
            expected_benefit=part.get("expected_benefit") or "", scope_type=parent.scope_type,
            project_id=parent.project_id, existing_component_ids=part.get("existing_component_ids") or [],
            limits=parent.limits, candidate_pin=part.get("candidate_pin"), parent_id=parent.id, emit=False,
        )
        children.append(child)
    parent.parts = [{"child_id": c.id, "title": c.title, "kind": c.kind,
                     "depends_on": [children[j].id for j in deps[i]]} for i, c in enumerate(children)]
    union = list(parent.canonical_identities or [])
    for c in children:
        union += [i for i in c.canonical_identities or [] if i not in union]
    parent.canonical_identities = union
    parent.research_entry_ids = sorted({*(parent.research_entry_ids or []),
                                        *[e for c in children for e in (c.research_entry_ids or [])]})
    session.flush()


# --------------------------------------------------------------------------- review


def review(session: Session, p: ChangeProposal, *, actor_user_id: str, with_claude: bool = False,
           candidate_pin: str | None = None, domain: str | None = None) -> ChangeProposal:
    if p.status in OUTCOME_FINAL:
        raise Conflict(f"proposal outcome already recorded ({p.status}); open a new proposal")
    if p.parent_id:
        raise Conflict("review the parent proposal; parts are reviewed together")
    if candidate_pin is not None:
        pin = redact_text(candidate_pin.strip())[:128]
        p.candidate_pin = pin or None
        p.unknown_fields = [u for u in (p.unknown_fields or []) if u != "candidate_pin" or not pin]
    children = _children(session, p)
    _refresh_research_links(session, p, children)
    package, bh, ch = R.build_package(session, p, children, with_claude=with_claude, domain=domain or p.domain,
                                      previous_claude=(p.review_package or {}).get("claude_review"))
    changed = (p.baseline_hash is not None and p.baseline_hash != bh) or (p.candidate_hash is not None and p.candidate_hash != ch)
    invalidated = 0
    if changed:
        invalidated = _invalidate(session, p, "baseline/candidate changed at re-review")
        package["binding_note"] = f"Baseline/candidate berubah; {invalidated} keputusan terkait dibatalkan."
    p.review_package = package
    p.review_hash = R.review_hash(package)
    p.baseline_hash = bh
    p.candidate_hash = ch
    p.reviewed_at = utcnow()
    # An unchanged re-review keeps an approved trial/promotion; anything else returns to REVIEWED.
    keep = p.status in ("TRIAL_APPROVED", "IN_TRIAL", "PROMOTION_PENDING", "PROMOTION_APPROVED") and not changed
    if not keep:
        p.status = "REVIEWED"
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="proposal.review", target_type=TARGET_TYPE,
                 target_id=p.id, project_id=p.project_id, domain=p.domain,
                 details={"baseline_hash": bh, "candidate_hash": ch, "review_hash": p.review_hash,
                          "claude": package["claude_review"]["status"], "invalidated_decisions": invalidated})
    events.emit(session, source="proposals", kind="proposal.reviewed", entity_type=TARGET_TYPE, entity_id=p.id,
                project_id=p.project_id, domain=p.domain, entity_version=p.review_hash,
                payload={"status": p.status, "baseline_hash": bh, "candidate_hash": ch})
    return p


def _refresh_research_links(session: Session, p: ChangeProposal, children: list[ChangeProposal]) -> None:
    """Link registry entries (visible in the proposal's domain) for every canonical identity, so an entry
    added or checked after submission is part of the review. Changed links change the candidate hash."""
    ids = list(p.research_entry_ids or [])
    for ident in p.canonical_identities or []:
        e = research.find_by_identity(session, ident, domain=p.domain)
        if e is not None and e.id not in ids:
            ids.append(e.id)
    for c in children:
        ids += [i for i in c.research_entry_ids or [] if i not in ids]
    if sorted(ids) != sorted(p.research_entry_ids or []):
        p.research_entry_ids = sorted(ids)


# --------------------------------------------------------------------------- decisions


def _active_decision(session: Session, p: ChangeProposal, kind: str) -> Decision | None:
    rows = [d for d in _decisions(session, p) if d.kind == kind and d.status == "ACTIVE"]
    return rows[-1] if rows else None


def _last_closing_outcome_at(p: ChangeProposal) -> str | None:
    hist = [h for h in (p.outcome or {}).get("history") or [] if h.get("status") in CLOSING_OUTCOMES]
    return hist[-1]["at"] if hist else None


def decide(session: Session, p: ChangeProposal, *, actor_user_id: str, action: str, baseline_hash: str,
           candidate_hash: str, scope: dict[str, Any] | None, rationale: str) -> Decision:
    action = (action or "").upper()
    if action not in ACTIONS:
        raise ValidationFailed(f"action must be one of {ACTIONS}")
    if p.parent_id:
        raise Conflict("decide on the parent proposal; parts must not get conflicting approvals")
    if not p.review_package or not p.baseline_hash or not p.candidate_hash:
        raise Blocked("a review package is required before a decision", code="REVIEW_REQUIRED")
    if p.status in OUTCOME_FINAL:
        raise Conflict(f"outcome already recorded ({p.status})")
    if not (rationale or "").strip():
        raise ValidationFailed("rationale is required")
    if p.kind == "COMPARE" and action in ("TRIAL_APPROVE", "PROMOTE"):
        raise PolicyDenied("COMPARE/review never installs or activates; open an ADD/REPLACE/UPGRADE proposal to trial",
                           code="COMPARE_NO_INSTALL")
    if baseline_hash != p.baseline_hash or candidate_hash != p.candidate_hash:
        raise StaleBinding("submitted hashes do not match the reviewed package; reload and decide on the current review",
                           details={"reviewed": {"baseline_hash": p.baseline_hash, "candidate_hash": p.candidate_hash},
                                    "submitted": {"baseline_hash": baseline_hash, "candidate_hash": candidate_hash}})
    live_b, live_c = current_hashes(session, p)
    if live_b != p.baseline_hash or live_c != p.candidate_hash:
        n = _invalidate(session, p, "baseline/candidate changed after review")
        p.status = "UNDER_REVIEW"
        session.flush()
        raise StaleBinding("baseline/candidate/target changed after review; binding refused, re-review required",
                           details={"reviewed": {"baseline_hash": p.baseline_hash, "candidate_hash": p.candidate_hash},
                                    "current": {"baseline_hash": live_b, "candidate_hash": live_c},
                                    "invalidated_decisions": n})
    norm = normalise_scope(scope)
    if action in ("TRIAL_APPROVE", "PROMOTE"):
        missing = [k for k in SCOPE_REQUIRED if norm.get(k) in (None, "")]
        if missing:
            raise ValidationFailed("scope must bind target/environment/provider/data_class/budget_usd",
                                   details={"missing": missing})
        if not p.candidate_pin:
            raise Blocked("candidate pin/digest is UNKNOWN; pin the candidate and re-review first",
                          code="CANDIDATE_NOT_PINNED")
        gate = R.source_identity_gate(session, p)
        if gate["status"] != "OK":
            raise Blocked("source identity UNVERIFIED/CHANGED/NOT_FOUND; admission blocked until verified (§50)",
                          code="SOURCE_IDENTITY_UNVERIFIED", details=gate)
    if action == "TRIAL_APPROVE" and p.status != "REVIEWED":
        raise Conflict(f"TRIAL_APPROVE needs status REVIEWED (current {p.status})")
    if action == "PROMOTE":
        if p.status not in ("TRIAL_APPROVED", "IN_TRIAL", "PROMOTION_PENDING"):
            raise Conflict(f"PROMOTE needs an approved trial (current {p.status}); trial approval is not promotion")
        trial = _active_decision(session, p, "TRIAL")
        if trial is None or (trial.details or {}).get("baseline_hash") != p.baseline_hash or \
                (trial.details or {}).get("candidate_hash") != p.candidate_hash:
            raise Blocked("no ACTIVE trial decision bound to the current hashes", code="TRIAL_DECISION_MISSING")
        if not _trusted_evidence(session, p, p.evidence_ids or []):
            raise Blocked("NOT-PROVEN: no trusted trial evidence attached; promotion refused", code="NOT_PROVEN")

    if action != "PROMOTE":
        # One proposal never holds contradictory ACTIVE decisions (§39 "jangan membuat beberapa
        # approval yang saling bertentangan"): KEEP/REJECT/DEFER/... supersede an earlier trial, and a
        # TRIAL_APPROVE (only reachable from REVIEWED, so no trial/promotion is active) supersedes an
        # earlier KEEP/REJECT/DEFER/REFERENCE_ONLY. PROMOTE keeps the trial it builds on.
        _invalidate(session, p, f"superseded by {action}")
    bind = binding_hash(p.id, action, p.baseline_hash, p.candidate_hash, norm)
    decision = _record_decision(
        session, kind=ACTION_DECISION_KIND[action], actor_user_id=actor_user_id, target_type=TARGET_TYPE,
        target_id=p.id, target_version=p.review_hash, target_hash=bind,
        allowed_operations=ACTION_OPERATIONS.get(action, ["record.decision"]), auth_scope=norm,
        project_id=p.project_id, domain=p.domain, rationale=redact_text(rationale),
        details={"action": action, "baseline_hash": p.baseline_hash, "candidate_hash": p.candidate_hash,
                 "proposal_kind": p.kind, "scope": norm, "budget_raised": False, "installed": False},
    )
    if decision is None or decision.target_hash != bind:
        raise Blocked("decision was not recorded with the exact binding", code="DECISION_NOT_BOUND")
    p.decision_ids = [*(p.decision_ids or []), decision.id]
    p.status = ACTION_STATUS[action]
    if action in RESEARCH_SYNC:
        _sync_research(session, p, RESEARCH_SYNC[action], decision.id)
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action=f"proposal.decision.{action.lower()}",
                 target_type=TARGET_TYPE, target_id=p.id, project_id=p.project_id, domain=p.domain, reason=rationale,
                 details={"decision_id": decision.id, "binding_hash": bind, "baseline_hash": p.baseline_hash,
                          "candidate_hash": p.candidate_hash, "scope": norm})
    events.emit(session, source="proposals", kind="proposal.decision", entity_type=TARGET_TYPE, entity_id=p.id,
                project_id=p.project_id, domain=p.domain,
                payload={"action": action, "decision_id": decision.id, "status": p.status})
    return decision


def verify_binding(session: Session, p: ChangeProposal, *, action: str) -> Decision | None:
    """For consumers (e.g. a scheduler before a trial run): the ACTIVE decision for *action* only if
    it is still bound to the live baseline/candidate; otherwise decisions are invalidated -> None."""
    kind = ACTION_DECISION_KIND[action]
    d = _active_decision(session, p, kind)
    if d is None:
        return None
    live_b, live_c = current_hashes(session, p)
    det = d.details or {}
    if det.get("baseline_hash") != live_b or det.get("candidate_hash") != live_c:
        _invalidate(session, p, "binding stale at verification")
        return None
    expected = binding_hash(p.id, action, det.get("baseline_hash", ""), det.get("candidate_hash", ""),
                            normalise_scope(d.auth_scope))
    return d if expected == d.target_hash else None


def _syncable(e: ResearchEntry | None, p: ChangeProposal) -> bool:
    """Registry rows a proposal decision may update: rows of the same domain, and shared (platform)
    rows only from a non-OFFICE proposal - Office decisions never leak into the shared registry that
    Personal views read (§23, §35)."""
    if e is None or e.category == research.REFERENCE_CATEGORY:
        return False
    if e.submitted_domain is None:
        return p.domain != Domain.OFFICE.value
    return e.submitted_domain == p.domain


def _sync_research(session: Session, p: ChangeProposal, lifecycle: str, decision_id: str) -> None:
    for eid in p.research_entry_ids or []:
        e = session.get(ResearchEntry, eid)
        if e is None or not _syncable(e, p) or e.lifecycle == "ENABLED":
            continue
        e.history = [*(e.history or []), {"at": utcnow().isoformat(), "event": "OWNER_DECISION", "from": e.lifecycle,
                                          "to": lifecycle, "proposal_id": p.id, "decision_id": decision_id}]
        e.lifecycle = lifecycle
        e.owner_decision_id = decision_id


# --------------------------------------------------------------------------- evidence & outcome


def _trusted_evidence(session: Session, p: ChangeProposal, ids: list[str]) -> list[EvidenceArtifact]:
    out = []
    for i in ids:
        ev = session.get(EvidenceArtifact, i)
        if ev is not None and ev.trusted and ev.mode == "LIVE" and (ev.domain in (None, p.domain)):
            out.append(ev)
    return out


def attach_evidence(session: Session, p: ChangeProposal, *, evidence_ids: list[str], actor_user_id: str) -> ChangeProposal:
    if p.status in OUTCOME_FINAL:
        raise Conflict("outcome already recorded")
    for i in evidence_ids:
        ev = session.get(EvidenceArtifact, i)
        if ev is None or ev.domain not in (None, p.domain):
            raise ValidationFailed(f"evidence {i} not found in this domain")
        if not ev.trusted or ev.mode != "LIVE":
            raise ValidationFailed(f"evidence {i} is not trusted LIVE evidence (worker/mock material cannot support a gate)")
    p.evidence_ids = sorted({*(p.evidence_ids or []), *evidence_ids})
    if p.status == "TRIAL_APPROVED":
        p.status = "PROMOTION_PENDING"
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="proposal.evidence.attach",
                 target_type=TARGET_TYPE, target_id=p.id, project_id=p.project_id, domain=p.domain,
                 details={"evidence_ids": evidence_ids})
    events.emit(session, source="proposals", kind="proposal.evidence", entity_type=TARGET_TYPE, entity_id=p.id,
                project_id=p.project_id, domain=p.domain, payload={"evidence_ids": evidence_ids, "status": p.status})
    return p


def _snapshot(session: Session, sid: str | None) -> dict[str, Any] | None:
    if not sid:
        return None
    s = session.get(ArchitectureSnapshot, sid)
    if s is None:
        raise ValidationFailed(f"architecture snapshot {sid} not found")
    return {"id": s.id, "label": s.label, "snapshot_hash": s.snapshot_hash,
            "created_at": s.created_at.isoformat() if s.created_at else None}


def record_outcome(
    session: Session, p: ChangeProposal, *, actor_user_id: str, status: str, evidence_ids: list[str] | None = None,
    accepted_configuration: dict[str, Any] | None = None, partial_failures: list[Any] | None = None,
    rollback: dict[str, Any] | None = None, notes: str = "", before_snapshot_id: str | None = None,
    after_snapshot_id: str | None = None,
) -> ChangeProposal:
    status = (status or "").upper()
    if status not in OUTCOMES:
        raise ValidationFailed(f"status must be one of {OUTCOMES}")
    if p.kind == "COMPARE":
        raise PolicyDenied("COMPARE proposals have no implementation outcome (nothing was installed)",
                           code="COMPARE_NO_INSTALL")
    if p.parent_id:
        raise Conflict("record the outcome on the parent proposal (PARTIAL lists failed parts)")
    previous = ((p.outcome or {}).get("current") or {}).get("status")
    if status not in OUTCOME_TRANSITIONS.get(previous, ()):
        raise Conflict(f"outcome {previous} cannot change to {status}; open a new proposal")
    ev_ids = list(evidence_ids or [])
    trusted = _trusted_evidence(session, p, ev_ids)
    if len(trusted) != len(ev_ids):
        raise ValidationFailed("outcome evidence must be trusted LIVE evidence in this domain")
    promotion = _active_decision(session, p, "PROMOTION")
    if status == "ACCEPTED":
        if rollback:
            raise ValidationFailed("a rollback is never recorded as acceptance; use ROLLED_BACK")
        if promotion is None or (promotion.details or {}).get("candidate_hash") != p.candidate_hash:
            raise Blocked("ACCEPTED requires an ACTIVE PROMOTION decision bound to this candidate",
                          code="PROMOTION_DECISION_MISSING")
        closed_at = _last_closing_outcome_at(p)
        created = ensure_aware(promotion.created_at)
        if closed_at and created is not None and created.isoformat() <= closed_at:
            raise Blocked("the promotion decision predates a rollback/failure; a new promotion decision is required",
                          code="PROMOTION_DECISION_STALE")
        if not trusted:
            raise Blocked("NOT-PROVEN: ACCEPTED requires trusted evidence", code="NOT_PROVEN")
        if not accepted_configuration:
            raise ValidationFailed("accepted_configuration is required for ACCEPTED")
    if status == "PARTIAL" and not partial_failures:
        raise ValidationFailed("PARTIAL must list the failed parts/targets")
    rb: dict[str, Any] | None = None
    if status == "ROLLED_BACK":
        rb = dict(redact(rollback or {}))
        data_rb = str(rb.get("data_rollback") or "UNKNOWN").upper()
        if data_rb not in DATA_ROLLBACK:
            raise ValidationFailed(f"rollback.data_rollback must be one of {DATA_ROLLBACK}")
        rb["data_rollback"] = data_rb
        rb.setdefault("code_reverted", False)
        rb["note"] = "Rollback kode tidak menjamin rollback data; baseline sebelumnya tetap berlaku."
    entry = {
        "status": status, "accepted": status == "ACCEPTED", "at": utcnow().isoformat(), "actor": actor_user_id,
        "evidence_ids": ev_ids, "accepted_configuration": redact(accepted_configuration) if status == "ACCEPTED" else None,
        "partial_failures": redact(partial_failures or []), "rollback": rb, "notes": redact_text(notes or "")[:4000],
        "before_snapshot": _snapshot(session, before_snapshot_id), "after_snapshot": _snapshot(session, after_snapshot_id),
        "promotion_decision_id": promotion.id if promotion else None,
        "inventory_docs_update": "PENDING - inventory direkonsiliasi dari observasi; ADR/docs delta diperlukan",
    }
    hist = [*((p.outcome or {}).get("history") or []), entry]
    p.outcome = {"current": entry, "history": hist}
    p.evidence_ids = sorted({*(p.evidence_ids or []), *ev_ids})
    p.status = status
    if status in CLOSING_OUTCOMES:
        try:
            _invalidate(session, p, f"outcome {status}")
        except Blocked:
            entry["decision_invalidation"] = "PENDING (decision registry unavailable)"
    _sync_outcome_research(session, p, status, promotion.id if promotion else None)
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action=f"proposal.outcome.{status.lower()}",
                 target_type=TARGET_TYPE, target_id=p.id, project_id=p.project_id, domain=p.domain, reason=notes,
                 details={"status": status, "accepted": status == "ACCEPTED", "evidence_ids": ev_ids})
    events.emit(session, source="proposals", kind="proposal.outcome", entity_type=TARGET_TYPE, entity_id=p.id,
                project_id=p.project_id, domain=p.domain, payload={"status": status, "accepted": status == "ACCEPTED"})
    return p


def _sync_outcome_research(session: Session, p: ChangeProposal, status: str, decision_id: str | None) -> None:
    for eid in p.research_entry_ids or []:
        e = session.get(ResearchEntry, eid)
        if e is None or not _syncable(e, p):
            continue
        if status == "ACCEPTED":
            e.history = [*(e.history or []), {"at": utcnow().isoformat(), "event": "PROMOTED", "from": e.lifecycle,
                                              "to": "ENABLED", "proposal_id": p.id, "decision_id": decision_id}]
            e.lifecycle = "ENABLED"
            e.owner_decision_id = decision_id
        elif e.lifecycle == "ENABLED":
            prev = next((h.get("from") for h in reversed(e.history or [])
                         if h.get("event") == "PROMOTED" and h.get("proposal_id") == p.id), "DISCOVERED")
            e.history = [*(e.history or []), {"at": utcnow().isoformat(), "event": status, "from": "ENABLED",
                                              "to": prev, "proposal_id": p.id}]
            e.lifecycle = prev


# --------------------------------------------------------------------------- views


def _decision_view(d: Decision) -> dict[str, Any]:
    det = d.details or {}
    return {"id": d.id, "kind": d.kind, "action": det.get("action"), "status": d.status, "actor_user_id": d.actor_user_id,
            "actor_kind": d.actor_kind, "target_hash": d.target_hash, "target_version": d.target_version,
            "baseline_hash": det.get("baseline_hash"), "candidate_hash": det.get("candidate_hash"),
            "scope": d.auth_scope or {}, "allowed_operations": d.allowed_operations or [], "rationale": d.rationale,
            "created_at": d.created_at.isoformat() if d.created_at else None,
            "invalidated_at": d.invalidated_at.isoformat() if d.invalidated_at else None,
            "invalidation_reason": d.invalidation_reason}


def delivery_dimensions(session: Session, p: ChangeProposal) -> dict[str, dict[str, str]]:
    """§41: APPROVED / IMPLEMENTED / ACTIVE / VERIFIED / ARCHITECTURE_SOURCE_UPDATED / DOCS_PUBLISHED are
    shown separately; nothing is inferred from another (an approval is not activation)."""
    decisions = _decisions(session, p)
    promotion = next((d for d in reversed(decisions) if d.kind == "PROMOTION"), None)
    cur = (p.outcome or {}).get("current") or {}
    trusted = _trusted_evidence(session, p, p.evidence_ids or [])
    from aeos.inventory import facade as inventory

    enabled = [c for c in (inventory.get_component(session, cid) for cid in p.candidate_component_ids or [])
               if c is not None and c.enablement == "ENABLED"]
    # §41 "kapan aktif": when the inventory first observed a candidate as ENABLED (never the approval time)
    activated_at = None
    if enabled:
        from sqlalchemy import func as _f

        from aeos.models.inventory import ComponentObservation

        first = session.scalar(select(_f.min(ComponentObservation.observed_at)).where(
            ComponentObservation.component_id.in_([c.component_id for c in enabled]),
            ComponentObservation.dimension == "enablement", ComponentObservation.status == "ENABLED"))
        activated_at = first.isoformat() if first else None

    def dim(status: str, note: str, **extra: Any) -> dict[str, Any]:
        return {"status": status, "note": note, **extra}

    return {
        "APPROVED": dim("YES" if promotion and promotion.status == "ACTIVE" else
                        (promotion.status if promotion else "NO"), "Keputusan PROMOTION terikat hash"),
        "IMPLEMENTED": dim(cur.get("status") or "UNKNOWN", "Outcome yang dicatat manusia dengan bukti"),
        "ACTIVE": dim("ENABLED_IN_CONFIG" if enabled else "UNKNOWN",
                      "Dari konfigurasi/observasi inventaris, bukan dari approval", at=activated_at),
        "VERIFIED": dim("EVIDENCE_ATTACHED" if trusted else "NOT-PROVEN", "Bukti terpercaya dari verifier/collector"),
        "ARCHITECTURE_SOURCE_UPDATED": dim("YES" if cur.get("after_snapshot") else "UNKNOWN",
                                           "ArchitectureSnapshot sesudah perubahan"),
        "DOCS_PUBLISHED": dim("UNKNOWN", "Status publikasi dokumen berasal dari modul publisher"),
    }


def proposal_view(session: Session, p: ChangeProposal, *, detail: bool = False) -> dict[str, Any]:
    v: dict[str, Any] = {
        "id": p.id, "kind": p.kind, "title": p.title, "status": p.status, "domain": p.domain, "scope_type": p.scope_type,
        "project_id": p.project_id, "parent_id": p.parent_id, "duplicate_of": p.duplicate_of,
        "source_refs": p.source_refs or [], "canonical_identities": p.canonical_identities or [],
        "existing_component_ids": p.existing_component_ids or [], "submitted_by": p.submitted_by,
        "baseline_hash": p.baseline_hash, "candidate_hash": p.candidate_hash, "candidate_pin": p.candidate_pin,
        "reviewed_at": p.reviewed_at.isoformat() if p.reviewed_at else None,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        "parts_count": len(p.parts or []), "outcome_status": ((p.outcome or {}).get("current") or {}).get("status"),
        "allowed_actions": R.allowed_actions(p),
    }
    if detail:
        dups = list(session.scalars(select(ChangeProposal).where(ChangeProposal.duplicate_of == p.id,
                                                                 ChangeProposal.domain == p.domain)))
        package = dict(p.review_package or {})
        if package.get("human_decision"):
            package["human_decision"] = {**package["human_decision"], "allowed_now": R.allowed_actions(p)}
        v.update({
            "problem": p.problem, "expected_benefit": p.expected_benefit, "limits": p.limits or {},
            "candidate_component_ids": p.candidate_component_ids or [], "attachment_ids": p.attachment_ids or [],
            "unknown_fields": p.unknown_fields or [], "parts": p.parts or [], "review_package": package,
            "review_hash": p.review_hash, "decisions": [_decision_view(d) for d in _decisions(session, p)],
            "evidence_ids": p.evidence_ids or [], "task_ids": p.task_ids or [], "outcome": p.outcome or {},
            "duplicates": [{"id": d.id, "title": d.title, "status": d.status} for d in dups],
            "research_entries": [research.entry_view(e) for e in R.research_entries(session, p)],
            "delivery_dimensions": delivery_dimensions(session, p),
            "guarantees": {"installs": False, "activates": False, "raises_budget": False, "sends_project_data": False,
                           "note": "Usulan/review tidak memasang, mengaktifkan, menaikkan budget atau mengirim data proyek."},
        })
    return v


def list_proposals(session: Session, *, domain: str, allowed_project_ids: list[str] | None, status: str | None = None,
                   kind: str | None = None, q: str | None = None, component_id: str | None = None) -> list[dict[str, Any]]:
    stmt = select(ChangeProposal).where(ChangeProposal.domain == domain).order_by(ChangeProposal.created_at.desc())
    if status:
        stmt = stmt.where(ChangeProposal.status == status.upper())
    if kind:
        stmt = stmt.where(ChangeProposal.kind == kind.upper())
    if allowed_project_ids is not None:
        stmt = stmt.where(or_(ChangeProposal.project_id.is_(None), ChangeProposal.project_id.in_(allowed_project_ids)))
    rows = list(session.scalars(stmt))
    if component_id:
        rows = [r for r in rows if component_id in (r.existing_component_ids or []) + (r.candidate_component_ids or [])]
    if q:
        needle = q.lower()
        rows = [r for r in rows if needle in (r.title + " " + " ".join(r.canonical_identities or [])).lower()]
    return [proposal_view(session, r) for r in rows]


def timeline(session: Session, p: ChangeProposal) -> dict[str, Any]:
    ids = [p.id, *[c.id for c in _children(session, p)]]
    audits = list(session.scalars(select(AuditEvent).where(AuditEvent.target_type == TARGET_TYPE,
                                                           AuditEvent.target_id.in_(ids)).order_by(AuditEvent.seq)))
    items: list[dict[str, Any]] = [
        {"at": a.at.isoformat() if a.at else None, "type": "audit", "action": a.action, "actor": a.actor_id,
         "result": a.result, "target_id": a.target_id, "details": a.details or {}} for a in audits
    ]
    for d in _decisions(session, p):
        items.append({"at": d.created_at.isoformat() if d.created_at else None, "type": "decision",
                      "action": (d.details or {}).get("action"), "actor": d.actor_user_id, "result": d.status,
                      "target_id": p.id, "details": _decision_view(d)})
    items.sort(key=lambda i: i["at"] or "")
    current = (p.outcome or {}).get("current") or {}
    baseline_components = (p.review_package or {}).get("baseline", {}).get("components", [])
    after = None
    if current.get("status") == "ACCEPTED":
        after = {"candidate_hash": p.candidate_hash, "candidate_pin": p.candidate_pin,
                 "accepted_configuration": current.get("accepted_configuration")}
    return {
        "proposal_id": p.id, "items": items, "outcome_history": (p.outcome or {}).get("history") or [],
        "before_after": {
            "before": {"baseline_hash": p.baseline_hash,
                       "components": [{k: b.get(k) for k in ("component_id", "name", "approved_pin", "status")}
                                      for b in baseline_components]},
            "after": after,
            "status": current.get("status") or p.status,
            "note": None if after else "Belum ada konfigurasi diterima; baseline tetap berlaku.",
            "snapshots": {"before": current.get("before_snapshot"), "after": current.get("after_snapshot")},
        },
        "source": "audit_events + decisions + outcome history", "checked_at": utcnow().isoformat(),
    }
