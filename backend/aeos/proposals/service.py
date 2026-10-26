"""ChangeProposal service (§39, §40, §41, §50, ADR-0082).

* Submitting or reviewing a proposal never installs, activates, changes routes/policy, raises a
  budget or grants credentials. COMPARE ("review repo") can never be trial-approved or promoted.
* Duplicates are linked through canonical source identity within the SAME domain only — Office and
  Personal contexts are never merged; linked duplicates never hold conflicting ACTIVE decisions.
* TRIAL_APPROVE and PROMOTE are different human decisions, each recorded through the Decision
  registry and bound to ``baseline_hash`` + ``candidate_hash`` + scope (provider/data class/budget/
  target/caps/stop+rollback scope). Any change of baseline/candidate - or a run outside the decided
  scope - invalidates the binding; a stale binding is refused at every consumer: trial launch, every
  scheduler attempt, evidence attachment, PROMOTE and the ACCEPTED outcome.
* Evidence must be about THIS candidate (trial TaskSpec/run/eval run, or verifier evidence naming this
  proposal and candidate hash) and created after the TRIAL decision; anything else cannot open a gate.
* Outcomes are ACCEPTED / ROLLED_BACK / FAILED / PARTIAL; a rollback is never recorded as acceptance,
  ACCEPTED requires the lawful configuration change in the inventory and pre-declared acceptance met,
  and the history keeps every outcome with its evidence.
"""

from __future__ import annotations

import re
from datetime import datetime
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
from aeos.proposals.trial import TRIAL_OPERATIONS
from aeos.research import service as research
from aeos.research.facade import canonical_identity
from aeos.research.identity import IdentityError

KINDS = ("ADD", "REPLACE", "UPGRADE", "REMOVE", "COMPARE")
ACTIONS = ("TRIAL_APPROVE", "PROMOTE", "KEEP", "REJECT", "DEFER", "REQUEST_MORE_EVIDENCE", "REFERENCE_ONLY")
OUTCOMES = ("ACCEPTED", "ROLLED_BACK", "FAILED", "PARTIAL")
ACTION_DECISION_KIND = {"TRIAL_APPROVE": "TRIAL", "PROMOTE": "PROMOTION", "KEEP": "REJECTION", "REJECT": "REJECTION",
                        "DEFER": "REVISION_REQUEST", "REQUEST_MORE_EVIDENCE": "REVISION_REQUEST",
                        "REFERENCE_ONLY": "APPROVAL"}
ACTION_STATUS = {"TRIAL_APPROVE": "TRIAL_APPROVED", "PROMOTE": "PROMOTION_APPROVED", "KEEP": "KEEP",
                 "REJECT": "REJECTED", "DEFER": "DEFERRED", "REQUEST_MORE_EVIDENCE": "REQUEST_MORE_EVIDENCE",
                 "REFERENCE_ONLY": "REFERENCE_ONLY"}
# One authorization covers a clear series of operations (§40); consumers check each against the decision.
PROMOTION_OPERATIONS = ["promote.candidate", "inventory.config.apply", "release.promote.candidate"]
ROLLBACK_OPERATIONS = ["rollback.candidate", "restore.baseline"]
ACTION_OPERATIONS = {"TRIAL_APPROVE": list(TRIAL_OPERATIONS), "PROMOTE": PROMOTION_OPERATIONS}
OUTCOME_FINAL = ("ACCEPTED", "ROLLED_BACK", "FAILED", "PARTIAL")
CLOSING_OUTCOMES = ("ROLLED_BACK", "FAILED")  # close the proposal; a retry needs a new proposal
# Outcome transitions: ACCEPTED may later be rolled back/fail/turn partial; PARTIAL may be completed.
OUTCOME_TRANSITIONS = {None: OUTCOMES, "ACCEPTED": ("ROLLED_BACK", "FAILED", "PARTIAL"), "PARTIAL": OUTCOMES,
                       "ROLLED_BACK": (), "FAILED": ()}
RESEARCH_SYNC = {"REJECT": "REJECTED", "DEFER": "DEFERRED", "REFERENCE_ONLY": "REFERENCE_ONLY"}
SCOPE_REQUIRED = ("target", "environment", "provider", "data_class", "budget_usd")
TRIAL_SCOPE_REQUIRED = (*SCOPE_REQUIRED, "max_minutes", "max_attempts", "stop_conditions", "rollback_scope")
PROMOTE_SCOPE_REQUIRED = (*SCOPE_REQUIRED, "stop_conditions", "rollback_scope")
TRIAL_ENVIRONMENTS = ("sandbox", "staging", "canary")
TRIAL_STATUSES = ("TRIAL_APPROVED", "IN_TRIAL", "PROMOTION_PENDING")
DATA_ROLLBACK = ("DONE", "NOT_POSSIBLE", "NOT_APPLICABLE", "UNKNOWN")
LIMIT_KEYS = ("cost_usd", "max_minutes", "max_attempts", "data_class", "resource")
ADR_REQUIRED_KINDS = ("ADD", "REPLACE", "REMOVE")
# Audit actions that prove a stop/rollback/recovery actually ran (§40 node "Stop, rollback atau recovery").
ROLLBACK_AUDIT_ACTIONS = ("ops.release.rollback", "ops.backup.restore", "inventory.manifest.load")
# Baseline fields a promoted change may lawfully move (config applied / old component retired).
ALLOWED_POST_PROMOTION = {
    "UPGRADE": {"approved_pin", "manifest_hash", "observed_versions", "admission_status", "enablement",
                "design_label", "requirement", "lifecycle_final"},
    "REPLACE": {"admission_status", "enablement", "lifecycle_final", "design_label", "requirement", "relations",
                "manifest_hash"},
    "REMOVE": {"admission_status", "enablement", "lifecycle_final", "design_label", "requirement", "relations",
               "manifest_hash", "observed_versions"},
}
# Inventory observation statuses that positively show the activated configuration (§38 dimensions).
GOOD_OBSERVATION = ("INSTALLED", "CONFIGURED", "CONNECTED", "VERIFIED", "IN_USE", "LAST_USED", "IDLE")
TARGET_TYPE = "change_proposal"
_STOPWORDS = {"yang", "untuk", "dengan", "dari", "pada", "agar", "lebih", "tidak", "atau", "this", "that", "with",
              "from", "into", "akan", "bisa", "dalam"}


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


def _positive_int(v: Any, field: str) -> int:
    if isinstance(v, bool):
        raise ValidationFailed(f"scope.{field} must be a whole number > 0")
    try:
        f = float(v)
    except (TypeError, ValueError) as exc:
        raise ValidationFailed(f"scope.{field} must be a whole number > 0") from exc
    if f <= 0 or f != int(f):
        raise ValidationFailed(f"scope.{field} must be a whole number > 0")
    return int(f)


def normalise_scope(scope: dict[str, Any] | None) -> dict[str, Any]:
    s = dict(redact(scope or {}))
    if "budget_usd" in s and s["budget_usd"] is not None:
        try:
            s["budget_usd"] = float(s["budget_usd"])
        except (TypeError, ValueError) as exc:
            raise ValidationFailed("scope.budget_usd must be a number") from exc
        if s["budget_usd"] < 0:
            raise ValidationFailed("scope.budget_usd must be >= 0")
    for k in ("max_minutes", "max_attempts"):
        if s.get(k) not in (None, ""):
            s[k] = _positive_int(s[k], k)
    if s.get("stop_conditions") not in (None, ""):
        raw = s["stop_conditions"]
        items = re.split(r"[\n;]", raw) if isinstance(raw, str) else raw
        if not isinstance(items, list):
            raise ValidationFailed("scope.stop_conditions must be a list or text")
        s["stop_conditions"] = [str(x).strip()[:300] for x in items if str(x).strip()][:20]
    if isinstance(s.get("rollback_scope"), str):
        s["rollback_scope"] = s["rollback_scope"].strip()[:1000]
    if isinstance(s.get("environment"), str):
        s["environment"] = s["environment"].strip().lower()
    if isinstance(s.get("data_class"), str):
        s["data_class"] = s["data_class"].strip().upper()
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


def duplicate_family(session: Session, p: ChangeProposal) -> list[ChangeProposal]:
    """The canonical proposal and every proposal linked to it as a duplicate, in the same domain."""
    root = _root(session, p)
    out = [root]
    for row in session.scalars(select(ChangeProposal).where(ChangeProposal.domain == p.domain,
                                                            ChangeProposal.duplicate_of.is_not(None))):
        if row.id != root.id and _root(session, row).id == root.id:
            out.append(row)
    return out


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


_CANDIDATE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def candidate_digest_value(raw: str | None) -> str | None:
    """§25 "pinned version/digest": a structured ``sha256:<64 hex>`` (normalised to lower case) or None. Anything
    else is refused - a digest is never free text."""
    if raw is None or not str(raw).strip():
        return None
    value = str(raw).strip().lower()
    if not _CANDIDATE_DIGEST.match(value):
        raise ValidationFailed("candidate_digest must be 'sha256:<64 hex>' (the digest of the candidate artifact)",
                               code="CANDIDATE_DIGEST_INVALID")
    return value


def create_proposal(
    session: Session, *, actor_user_id: str, domain: str, kind: str, title: str | None, sources: list[str],
    problem: str = "", expected_benefit: str = "", scope_type: str = "PLATFORM", project_id: str | None = None,
    existing_component_ids: list[str] | None = None, candidate_component_ids: list[str] | None = None,
    limits: dict[str, Any] | None = None, attachment_ids: list[str] | None = None, candidate_pin: str | None = None,
    parts: list[dict[str, Any]] | None = None, parent_id: str | None = None, emit: bool = True,
    provenance: dict[str, Any] | None = None, candidate_digest: str | None = None,
) -> ChangeProposal:
    digest = candidate_digest_value(candidate_digest)
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

    for cid in existing:  # a component of the other domain is "not in this domain's inventory" (§23)
        if not R.component_in_domain(inventory.get_component(session, cid), domain):
            unknown.append(f"existing_component:{cid} (tidak ada di inventaris)")
    candidates = sorted({c.strip() for c in candidate_component_ids or [] if c and c.strip()})
    for cid in candidates:
        if not R.component_in_domain(inventory.get_component(session, cid), domain):
            unknown.append(f"candidate_component:{cid} (belum terdaftar di inventaris)")
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
        parent_id=parent_id, submitted_by=actor_user_id[:64], scope_type=scope_type, project_id=project_id, domain=domain,
        source_refs=refs, problem=redact_text(problem or "")[:8000], expected_benefit=redact_text(expected_benefit or "")[:8000],
        existing_component_ids=existing, candidate_component_ids=candidates,
        limits=lim, candidate_pin=redact_text(candidate_pin)[:128] if candidate_pin else None,
        candidate_digest=digest, status="SUBMITTED",
        review_package={}, decision_ids=[], task_ids=[], evidence_ids=[], outcome={}, attachment_ids=atts,
        canonical_identities=resolved, duplicate_of=duplicate.id if duplicate else None, parts=[],
        unknown_fields=unknown, research_entry_ids=entry_ids, owner_plan={},
        links={"provenance": redact(provenance)} if provenance else {}, trial={}, checkpoint_review={},
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
        audit.record(session, actor_type="user" if not provenance else "system", actor_id=actor_user_id,
                     action="proposal.create", target_type=TARGET_TYPE,
                     target_id=p.id, project_id=project_id, domain=domain,
                     details={"kind": kind, "identities": resolved, "duplicate_of": p.duplicate_of,
                              "installed": False, "activated": False, "budget_changed": False,
                              "provenance": redact(provenance) if provenance else None})
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
            candidate_digest=part.get("candidate_digest"),
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


def file_suggestion(session: Session, *, source_kind: str, source_id: str, domain: str, project_id: str | None,
                    kind: str, title: str | None, sources: list[str], rationale: str,
                    existing_component_ids: list[str] | None = None) -> ChangeProposal:
    """§24: a skill/memory/tool suggestion from Hermes or another worker enters proposal review as an
    untrusted SUBMITTED proposal. It never becomes policy, memory or a decision by itself."""
    if source_kind not in ("worker", "hermes", "reviewer"):
        raise ValidationFailed("source_kind must be worker, hermes or reviewer")
    if not source_id:
        raise ValidationFailed("source_id (run/review id) is required for provenance")
    note = ("Saran dari " + source_kind + " " + source_id[:36] + " - DATA tidak tepercaya, bukan kebijakan aktif. "
            + redact_text(rationale or "")[:2000])
    return create_proposal(
        session, actor_user_id=f"{source_kind}:{source_id[:36]}", domain=domain, kind=kind, title=title,
        sources=sources, problem=note, expected_benefit="", scope_type="PROJECT" if project_id else "PLATFORM",
        project_id=project_id, existing_component_ids=existing_component_ids,
        provenance={"source_kind": source_kind, "source_id": source_id, "trusted": False},
    )


# --------------------------------------------------------------------------- review


def review(session: Session, p: ChangeProposal, *, actor_user_id: str, with_claude: bool = False,
           candidate_pin: str | None = None, domain: str | None = None,
           candidate_digest: str | None = None) -> ChangeProposal:
    if p.status in OUTCOME_FINAL:
        raise Conflict(f"proposal outcome already recorded ({p.status}); open a new proposal")
    if p.parent_id:
        raise Conflict("review the parent proposal; parts are reviewed together")
    if p.status == "PROMOTION_APPROVED":
        # The promoted baseline -> candidate change is being applied; a re-review would re-bind the package to
        # the half-applied state. Record the outcome, or KEEP/REJECT to change course (§40).
        raise Conflict("promotion is approved; record the outcome (ACCEPTED/FAILED/PARTIAL) or decide KEEP/REJECT",
                       code="PROMOTION_APPROVED_NO_REREVIEW")
    if candidate_pin is not None:
        pin = redact_text(candidate_pin.strip())[:128]
        p.candidate_pin = pin or None
        p.unknown_fields = [u for u in (p.unknown_fields or []) if u != "candidate_pin" or not pin]
    if candidate_digest is not None:  # "" clears it; the candidate hash re-binds either way
        p.candidate_digest = candidate_digest_value(candidate_digest)
    children = _children(session, p)
    _refresh_research_links(session, p, children)
    package, bh, ch = R.build_package(session, p, children, with_claude=with_claude, domain=domain or p.domain,
                                      previous_claude=(p.review_package or {}).get("claude_review"))
    changed = (p.baseline_hash is not None and p.baseline_hash != bh) or (p.candidate_hash is not None and p.candidate_hash != ch)
    invalidated = 0
    if changed:
        invalidated = _invalidate(session, p, "baseline/candidate changed at re-review")
        if invalidated:
            package["binding_note"] = f"Baseline/candidate berubah; {invalidated} keputusan terkait dibatalkan."
    p.review_package = package
    p.review_hash = R.review_hash(package)
    p.baseline_hash = bh
    p.candidate_hash = ch
    p.reviewed_at = utcnow()
    # An unchanged re-review keeps an approved trial; anything else returns to REVIEWED.
    keep = p.status in ("TRIAL_APPROVED", "IN_TRIAL", "PROMOTION_PENDING") and not changed
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


# --------------------------------------------------------------------------- binding


def _active_decision(session: Session, p: ChangeProposal, kind: str) -> Decision | None:
    rows = [d for d in _decisions(session, p) if d.kind == kind and d.status == "ACTIVE"]
    return rows[-1] if rows else None


def post_promotion_drift(session: Session, p: ChangeProposal) -> list[str]:
    """Baseline changes after PROMOTE that are NOT the promoted change itself (§40): the reviewed baseline
    (stored package) is compared field by field with the live inventory; only the fields the promoted
    change lawfully moves (pin/enablement of the upgraded component, retirement of a replaced/removed one)
    may differ. ADD/COMPARE bind to the latest same-domain snapshot: a newer snapshot (the "after"
    snapshot) is the expected effect."""
    if not p.existing_component_ids:
        return []
    stored = ((p.review_package or {}).get("baseline") or {}).get("components") or []
    if not stored:
        return ["reviewed baseline not available; re-review required"]
    allowed = ALLOWED_POST_PROMOTION.get(p.kind, set())
    problems = []
    for b in stored:
        before = R.baseline_fingerprint(b)
        after = R.baseline_fingerprint(R.component_baseline(session, b["component_id"], domain=p.domain))
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        illegal = [k for k in changed if k not in allowed]
        if illegal:
            problems.append(f"{b['component_id']}: {', '.join(illegal)} changed outside the promoted change")
    return problems


def binding_problems(session: Session, p: ChangeProposal, *, action: str,
                     after_promotion: bool = False) -> tuple[Decision | None, list[str]]:
    """The ACTIVE decision for *action* and why it is no longer bound to the live records (empty = bound)."""
    from aeos.proposals import trial

    kind = ACTION_DECISION_KIND[action]
    d = _active_decision(session, p, kind)
    if d is None:
        return None, []
    det = d.details or {}
    live_b, live_c = current_hashes(session, p)
    problems: list[str] = []
    if det.get("candidate_hash") != live_c:
        problems.append("candidate (pin/identity/plan) changed after the decision")
    if det.get("baseline_hash") != live_b:
        problems += post_promotion_drift(session, p) if after_promotion else ["baseline changed after the decision"]
    if action == "TRIAL_APPROVE":
        problems += trial.scope_drift(session, p, d)
    expected = binding_hash(p.id, action, det.get("baseline_hash", ""), det.get("candidate_hash", ""),
                            normalise_scope(d.auth_scope))
    if expected != d.target_hash:
        problems.append("decision binding hash does not match its recorded scope")
    return d, problems


def verify_binding(session: Session, p: ChangeProposal, *, action: str, after_promotion: bool = False) -> Decision | None:
    """For consumers (scheduler, release promotion, evidence, outcome): the ACTIVE decision for *action* only
    if it is still bound to the live baseline/candidate/scope; otherwise decisions are invalidated -> None."""
    d, problems = binding_problems(session, p, action=action, after_promotion=after_promotion)
    if d is None:
        return None
    if problems:
        _invalidate(session, p, "binding stale at verification: " + "; ".join(problems)[:300])
        return None
    return d


def _stale(session: Session, p: ChangeProposal, problems: list[str], where: str) -> StaleBinding:
    n = _invalidate(session, p, f"binding stale at {where}: " + "; ".join(problems)[:300])
    p.status = "UNDER_REVIEW"
    session.flush()
    return StaleBinding(f"binding stale at {where}; re-review and decide again",
                        details={"problems": problems, "invalidated_decisions": n})


# --------------------------------------------------------------------------- evidence binding


def _task_ids_of_runs(session: Session, run_ids: list[str]) -> dict[str, str | None]:
    from aeos.models.workforce import WorkerRun

    if not run_ids:
        return {}
    return {r.id: r.task_id for r in session.scalars(select(WorkerRun).where(WorkerRun.id.in_(run_ids)))}


def evidence_binding_problem(session: Session, p: ChangeProposal, ev: EvidenceArtifact, *, purpose: str = "gate") -> str | None:
    """None when *ev* is about this candidate: produced for a TaskSpec/run linked to the proposal, for an
    eval run linked to it, or by a trusted collector naming this proposal and its candidate hash. For an
    outcome, component observation evidence about the proposal's components also counts (§40 activation)."""
    task_ids = set(p.task_ids or [])
    if ev.task_id and ev.task_id in task_ids:
        return None
    if ev.run_id and _task_ids_of_runs(session, [ev.run_id]).get(ev.run_id) in task_ids and task_ids:
        return None
    if ev.subject_type == "eval_run" and ev.subject_id in ((p.links or {}).get("eval_run_ids") or []):
        return None
    if ev.subject_type == TARGET_TYPE and ev.subject_id == p.id and \
            (ev.environment or {}).get("candidate_hash") == p.candidate_hash:
        return None
    if purpose == "outcome" and ev.subject_type == "component" and \
            ev.subject_id in {*(p.existing_component_ids or []), *(p.candidate_component_ids or [])}:
        return None
    return ("evidence is not about this candidate: link the trial TaskSpec/eval run first, or use verifier evidence "
            "naming this proposal and its candidate hash")


def _after_trial(session: Session, p: ChangeProposal, ev: EvidenceArtifact) -> bool:
    trial = _active_decision(session, p, "TRIAL")
    created = ensure_aware(trial.created_at) if trial else None
    at = ensure_aware(ev.created_at)
    return created is None or (at is not None and at >= created)


def bound_evidence(session: Session, p: ChangeProposal, ids: list[str], *, purpose: str = "gate") -> list[EvidenceArtifact]:
    """Trusted LIVE evidence in this domain that is about this candidate (and, for gates, produced after the
    TRIAL decision)."""
    out = []
    for i in ids:
        ev = session.get(EvidenceArtifact, i)
        if ev is None or not ev.trusted or ev.mode != "LIVE" or ev.domain not in (None, p.domain):
            continue
        if evidence_binding_problem(session, p, ev, purpose=purpose):
            continue
        if purpose == "gate" and not _after_trial(session, p, ev):
            continue
        out.append(ev)
    return out


def _trusted_evidence(session: Session, p: ChangeProposal, ids: list[str]) -> list[EvidenceArtifact]:
    """Kept for the delivery dimensions: trusted LIVE evidence in this domain bound to this candidate."""
    return bound_evidence(session, p, ids, purpose="outcome")


# --------------------------------------------------------------------------- decisions


def _last_closing_outcome_at(p: ChangeProposal) -> str | None:
    hist = [h for h in (p.outcome or {}).get("history") or [] if h.get("status") in CLOSING_OUTCOMES]
    return hist[-1]["at"] if hist else None


def provider_eligibility(session: Session, p: ChangeProposal, provider: str, data_class: str) -> dict[str, Any]:
    """§40 trial admission "model/provider eligibility": ELIGIBLE (an active route of the provider passes the
    routing hard filter), NOT_ACTIVATED (a route of the provider exists for this domain/data class but is not
    activated yet: the scheduler waits), INELIGIBLE or UNKNOWN_PROVIDER (refused)."""
    from aeos.models.routing import ModelRoute

    routes = [r for r in session.scalars(select(ModelRoute).where(ModelRoute.provider == provider))
              if r.pool in ("WORKER", "LOCAL")]
    if not routes:
        return {"status": "UNKNOWN_PROVIDER", "routes": [],
                "reason": f"no WORKER/LOCAL route of provider {provider!r} in the model registry"}
    try:
        from aeos.routing import facade as routing

        decision = routing.eligible_routes(session, domain=p.domain or "", data_classification=data_class)
        eligible = [k for k in decision.eligible if any(r.route_key == k for r in routes)]
    except (ImportError, ModuleNotFoundError, ValidationFailed):  # pragma: no cover - routing ships with foundation
        eligible = []
    if eligible:
        return {"status": "ELIGIBLE", "routes": eligible}
    structural = [r.route_key for r in routes if p.domain in (r.eligible_domains or [])
                  and data_class in (r.allowed_data_classes or []) and r.status != "DISABLED"
                  and r.mode not in ("MOCK", "DEMO")]
    if structural:
        return {"status": "NOT_ACTIVATED", "routes": structural,
                "reason": "route exists for this domain/data class but is not activated; trial admission waits"}
    return {"status": "INELIGIBLE", "routes": [r.route_key for r in routes],
            "reason": f"no route of {provider} allows domain {p.domain} with data class {data_class}"}


def _benefit_problems(p: ChangeProposal) -> list[str]:
    problems = []
    if not (p.problem or "").strip():
        problems.append("problem/goal is empty")
    if not (p.expected_benefit or "").strip():
        problems.append("expected benefit is empty")
    if not R.measurable_acceptance(p):
        problems.append("no measurable acceptance criterion (metric + threshold) declared before the trial")
    return problems


def _admission_scope(session: Session, p: ChangeProposal, norm: dict[str, Any], action: str) -> dict[str, Any]:
    required = TRIAL_SCOPE_REQUIRED if action == "TRIAL_APPROVE" else PROMOTE_SCOPE_REQUIRED
    missing = [k for k in required if norm.get(k) in (None, "", [])]
    if missing:
        raise ValidationFailed("scope must bind " + "/".join(required), details={"missing": missing})
    dc = norm["data_class"]
    if dc not in R.DATA_CLASSES:
        raise ValidationFailed(f"scope.data_class must be one of {R.DATA_CLASSES}")
    if dc == "RESTRICTED":
        raise PolicyDenied("RESTRICTED data never enters a candidate trial or promotion scope", code="DATA_CLASS_RESTRICTED")
    if action == "TRIAL_APPROVE":
        env = norm["environment"]
        if env not in TRIAL_ENVIRONMENTS:
            raise ValidationFailed("a trial runs in a disposable sandbox or staging (canary only with a canary plan); "
                                   f"environment {env!r} is not allowed", code="TRIAL_ENVIRONMENT_NOT_ALLOWED",
                                   details={"allowed": list(TRIAL_ENVIRONMENTS)})
        if env == "canary":
            rp = (p.owner_plan or {}).get("replacement") or {}
            if not (p.owner_plan or {}).get("canary"):
                raise Blocked("canary needs a declared canary plan (target, cap, observation window, stop criteria)",
                              code="CANARY_PLAN_REQUIRED")
            if p.kind == "REPLACE" and rp.get("coexistence_safe") is not True:
                raise Blocked("canary is not a safe method when coexistence is not declared safe; use sandbox/staging "
                              "and an authorised cutover (§40)", code="CANARY_NOT_SAFE")
    elig = provider_eligibility(session, p, str(norm["provider"]), dc)
    if elig["status"] in ("UNKNOWN_PROVIDER", "INELIGIBLE"):
        raise Blocked(f"provider {norm['provider']} is not eligible for this trial: {elig.get('reason')}",
                      code="PROVIDER_NOT_ELIGIBLE", details=elig)
    sp = R.security_privacy(session, p, data_class=dc)
    if sp["status"] == "BLOCKED":
        raise Blocked("Security/Privacy assessment BLOCKED for the candidate's data/access boundary",
                      code="SECURITY_PRIVACY_BLOCKED", details=sp)
    benefit = _benefit_problems(p)
    if benefit:
        raise Blocked("the benefit is not testable: " + "; ".join(benefit), code="BENEFIT_NOT_TESTABLE",
                      details={"problems": benefit})
    return {"provider_eligibility": elig, "security_privacy": sp["status"]}


def _promote_gates(session: Session, p: ChangeProposal, norm: dict[str, Any]) -> dict[str, Any]:
    from aeos.proposals import checkpoint

    if (p.trial or {}).get("status") == "STOPPED":
        raise Blocked("the trial was stopped by the owner; relaunch it or decide KEEP/REJECT", code="TRIAL_STOPPED")
    trial, problems = binding_problems(session, p, action="TRIAL_APPROVE")
    if trial is None or (trial.details or {}).get("baseline_hash") != p.baseline_hash or \
            (trial.details or {}).get("candidate_hash") != p.candidate_hash:
        raise Blocked("no ACTIVE trial decision bound to the current hashes", code="TRIAL_DECISION_MISSING")
    if problems:
        raise _stale(session, p, problems, "PROMOTE (trial scope)")
    if not bound_evidence(session, p, p.evidence_ids or []):
        raise Blocked("NOT-PROVEN: no trusted evidence about this candidate (trial TaskSpec/run/eval) produced after "
                      "the TRIAL decision; promotion refused", code="NOT_PROVEN")
    if not p.task_ids:
        raise Blocked("no TaskSpec is linked to this proposal (launch the trial or link the implementing task)",
                      code="TASK_REQUIRED")
    ok, code, msg, rec = checkpoint.gate(session, p)
    if not ok:
        raise Blocked(msg, code=code, details={"checkpoint": rec})
    plan = p.owner_plan or {}
    rp = R.replacement_plan(p, ((p.review_package or {}).get("baseline") or {}).get("components") or []) or {}
    owner = plan.get("operations_owner") or (plan.get("replacement") or {}).get("operations_owner") or (
        rp.get("operations_owner") if rp.get("operations_owner") not in (None, R.UNKNOWN) else None)
    if not owner:
        raise Blocked("name the operations owner of the result in the owner plan before promotion", code="OWNER_REQUIRED")
    details: dict[str, Any] = {"checkpoint": {k: rec.get(k) for k in ("status", "verdict", "mode", "package_hash")}
                               if rec else None}
    if p.kind in ("REPLACE", "UPGRADE"):
        # §39 REPLACE "kapan versi lama boleh dihentikan": a replacement names when the old version may retire.
        needed = ("cutover_order", "rollback_or_forward_recovery",
                  *(("retire_old_version_after",) if p.kind == "REPLACE" else ()))
        missing = [k for k in needed if rp.get(k) in (None, R.UNKNOWN)]
        if missing:
            raise Blocked("the replacement plan is incomplete: " + ", ".join(missing), code="REPLACEMENT_PLAN_INCOMPLETE",
                          details={"missing": missing})
        if rp.get("data_state_relations") and not (plan.get("replacement") or {}).get("backup_restore_evidence_id"):
            raise Blocked("the component holds state/data: reference a restore-tested backup before promotion",
                          code="RESTORE_EVIDENCE_REQUIRED")
        details["cutover"] = {"order": rp.get("cutover_order"), "coexistence_safe": rp.get("coexistence_safe"),
                              "authorised_by_this_decision": True}
    return details


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
    # §39: linked duplicates of one canonical identity never carry contradictory ACTIVE decisions. A duplicate
    # whose outcome is recorded (ACCEPTED/ROLLED_BACK/FAILED/PARTIAL) is a finished change: its decisions are
    # history (and a ROLLBACK record), so a new proposal for the same source - e.g. the next UPGRADE - can be
    # decided; otherwise one accepted or rolled-back change would block that identity forever.
    for other in duplicate_family(session, p):
        if other.id == p.id or other.status in OUTCOME_FINAL:
            continue
        if any(d.status == "ACTIVE" and d.kind != "ROLLBACK" for d in _decisions(session, other)):
            root = _root(session, p)
            raise Conflict(f"a linked duplicate already holds an ACTIVE decision; decide on the canonical proposal "
                           f"{root.id} (or revoke the other decision first)", code="DUPLICATE_DECISION_CONFLICT",
                           details={"canonical_proposal_id": root.id, "conflicting_proposal_id": other.id})
    norm = normalise_scope(scope)
    extra: dict[str, Any] = {}
    if action in ("TRIAL_APPROVE", "PROMOTE"):
        if not p.candidate_pin:
            raise Blocked("candidate pin/digest is UNKNOWN; pin the candidate and re-review first",
                          code="CANDIDATE_NOT_PINNED")
        digest_gate = candidate_digest_gate(session, p)
        if digest_gate["required"] and not p.candidate_digest:
            raise Blocked("the changed component is digest-pinned in the reviewed install-manifest; record the "
                          "candidate's sha256 digest (candidate_digest) and re-review first (§25)",
                          code="CANDIDATE_DIGEST_REQUIRED", details=digest_gate)
        gate = R.source_identity_gate(session, p)
        if gate["status"] != "OK":
            raise Blocked("source identity UNVERIFIED/CHANGED/NOT_FOUND or the pin is affected by a published "
                          "advisory; admission blocked (§40, §50)", code="SOURCE_IDENTITY_UNVERIFIED", details=gate)
        extra = _admission_scope(session, p, norm, action)
    if action == "TRIAL_APPROVE" and p.status != "REVIEWED":
        raise Conflict(f"TRIAL_APPROVE needs status REVIEWED (current {p.status})")
    if action == "PROMOTE":
        if p.status not in TRIAL_STATUSES:
            raise Conflict(f"PROMOTE needs an approved trial (current {p.status}); trial approval is not promotion")
        extra |= _promote_gates(session, p, norm)

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
                 "proposal_kind": p.kind, "scope": norm, "budget_raised": False, "installed": False, **extra},
    )
    if decision is None or decision.target_hash != bind:
        raise Blocked("decision was not recorded with the exact binding", code="DECISION_NOT_BOUND")
    p.decision_ids = [*(p.decision_ids or []), decision.id]
    p.status = ACTION_STATUS[action]
    if action in RESEARCH_SYNC:
        _sync_research(session, p, RESEARCH_SYNC[action], decision.id)
    if action == "TRIAL_APPROVE" and p.kind != "REMOVE":  # adoption stages; a removal is not a candidate eval
        _sync_research(session, p, "CANARY" if norm.get("environment") == "canary" else "CONTROLLED_EVAL",
                       decision.id, only_from=("SOURCE_VERIFIED",))
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action=f"proposal.decision.{action.lower()}",
                 target_type=TARGET_TYPE, target_id=p.id, project_id=p.project_id, domain=p.domain, reason=rationale,
                 details={"decision_id": decision.id, "binding_hash": bind, "baseline_hash": p.baseline_hash,
                          "candidate_hash": p.candidate_hash, "scope": norm})
    events.emit(session, source="proposals", kind="proposal.decision", entity_type=TARGET_TYPE, entity_id=p.id,
                project_id=p.project_id, domain=p.domain,
                payload={"action": action, "decision_id": decision.id, "status": p.status})
    return decision


def _syncable(e: ResearchEntry | None, p: ChangeProposal) -> bool:
    """Registry rows a proposal decision may update: rows of the same domain, and shared (platform)
    rows only from a non-OFFICE proposal - Office decisions never leak into the shared registry that
    Personal views read (§23, §35)."""
    if e is None or e.category == research.REFERENCE_CATEGORY:
        return False
    if e.submitted_domain is None:
        return p.domain != Domain.OFFICE.value
    return e.submitted_domain == p.domain


def _sync_research(session: Session, p: ChangeProposal, lifecycle: str, decision_id: str | None, *,
                   only_from: tuple[str, ...] | None = None, event: str = "OWNER_DECISION") -> None:
    for eid in p.research_entry_ids or []:
        e = session.get(ResearchEntry, eid)
        if e is None or not _syncable(e, p) or e.lifecycle == "ENABLED" or e.lifecycle == lifecycle:
            continue
        if only_from is not None and e.lifecycle not in only_from:
            continue
        e.history = [*(e.history or []), {"at": utcnow().isoformat(), "event": event, "from": e.lifecycle,
                                          "to": lifecycle, "proposal_id": p.id, "decision_id": decision_id}]
        e.lifecycle = lifecycle
        if decision_id:
            e.owner_decision_id = decision_id


def _append_test_results(session: Session, p: ChangeProposal, rows: list[dict[str, Any]]) -> None:
    """§47 "test results": this platform's own controlled/runtime results for the candidate (E2/E3)."""
    if not rows:
        return
    for eid in p.research_entry_ids or []:
        e = session.get(ResearchEntry, eid)
        if e is None or not _syncable(e, p):
            continue
        e.test_results = [*(e.test_results or []), *rows][-100:]


# --------------------------------------------------------------------------- evidence & outcome


def attach_evidence(session: Session, p: ChangeProposal, *, evidence_ids: list[str], actor_user_id: str) -> ChangeProposal:
    if p.status in OUTCOME_FINAL:
        raise Conflict("outcome already recorded")
    evs: list[EvidenceArtifact] = []
    for i in evidence_ids:
        ev = session.get(EvidenceArtifact, i)
        if ev is None or ev.domain not in (None, p.domain):
            raise ValidationFailed(f"evidence {i} not found in this domain")
        if not ev.trusted or ev.mode != "LIVE":
            raise ValidationFailed(f"evidence {i} is not trusted LIVE evidence (worker/mock material cannot support a gate)")
        why = evidence_binding_problem(session, p, ev, purpose="gate")
        if why:
            raise ValidationFailed(f"evidence {i}: {why}", code="EVIDENCE_NOT_FOR_CANDIDATE")
        if not _after_trial(session, p, ev):
            raise ValidationFailed(f"evidence {i} predates the TRIAL decision; it is not trial evidence",
                                   code="EVIDENCE_NOT_FOR_CANDIDATE")
        evs.append(ev)
    if p.status not in TRIAL_STATUSES:
        raise Conflict(f"trial evidence is attached during an approved trial (current {p.status})")
    _d, problems = binding_problems(session, p, action="TRIAL_APPROVE")
    if _d is None or problems:
        raise _stale(session, p, problems or ["no ACTIVE TRIAL decision"], "evidence attachment")
    p.evidence_ids = sorted({*(p.evidence_ids or []), *evidence_ids})
    if p.status in ("TRIAL_APPROVED", "IN_TRIAL"):
        p.status = "PROMOTION_PENDING"
    if p.kind != "REMOVE":
        _sync_research(session, p, "RECOMMENDED_FOR_PROMOTION", None, only_from=("CONTROLLED_EVAL", "CANARY"),
                       event="TRIAL_EVIDENCE")
    now = utcnow().isoformat()
    _append_test_results(session, p, [{"at": now, "proposal_id": p.id, "evidence_id": ev.id, "kind": ev.kind,
                                       "collector": ev.collector_identity, "claim": redact_text(ev.claim or "")[:200],
                                       "candidate_pin": p.candidate_pin, "level": "E2 (controlled trial, platform ini)"}
                                      for ev in evs])
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action="proposal.evidence.attach",
                 target_type=TARGET_TYPE, target_id=p.id, project_id=p.project_id, domain=p.domain,
                 details={"evidence_ids": evidence_ids})
    events.emit(session, source="proposals", kind="proposal.evidence", entity_type=TARGET_TYPE, entity_id=p.id,
                project_id=p.project_id, domain=p.domain, payload={"evidence_ids": evidence_ids, "status": p.status})
    return p


def _snapshot(session: Session, sid: str | None, p: ChangeProposal) -> dict[str, Any] | None:
    if not sid:
        return None
    s = session.get(ArchitectureSnapshot, sid)
    if s is None or not R.snapshot_in_domain(s, p.domain):
        raise ValidationFailed(f"architecture snapshot {sid} not found in this domain")
    return {"id": s.id, "label": s.label, "snapshot_hash": s.snapshot_hash,
            "created_at": s.created_at.isoformat() if s.created_at else None}


def _config_targets(p: ChangeProposal, cfg: dict[str, Any]) -> list[tuple[str, str | None]]:
    # The lawful configuration is the PROMOTED candidate: without an explicit pin the candidate pin is checked
    # (never "any pin" - an already-enabled component at its old pin is not the accepted upgrade, §47).
    pin = cfg.get("approved_pin") or (p.candidate_pin if p.kind != "REMOVE" else None)
    allowed = {*(p.candidate_component_ids or []), *((p.existing_component_ids or []) if p.kind in ("UPGRADE", "REMOVE")
                                                      else [])}
    if cfg.get("component_id"):
        cid = str(cfg["component_id"])
        if cid not in allowed:
            raise ValidationFailed(f"accepted_configuration.component_id {cid} is not a candidate component of this "
                                   "proposal (candidate_component_ids, or the existing component for UPGRADE/REMOVE)")
        return [(cid, str(pin) if pin else None)]
    if p.candidate_component_ids:
        return [(c, str(pin) if pin else None) for c in p.candidate_component_ids]
    if p.kind in ("UPGRADE", "REMOVE"):
        return [(c, str(pin) if pin else None) for c in p.existing_component_ids or []]
    return []


def _digest_targets(p: ChangeProposal) -> list[str]:
    if p.kind in ("REMOVE", "COMPARE"):
        return []
    return sorted({*(p.candidate_component_ids or []),
                   *((p.existing_component_ids or []) if p.kind in ("UPGRADE", "REPLACE") else [])})


def candidate_digest_gate(session: Session, p: ChangeProposal) -> dict[str, Any]:
    """§25 tool admission "pinned version/digest": a candidate digest is REQUIRED before TRIAL_APPROVE/PROMOTE when a
    component the proposal changes is digest-pinned for this platform in the reviewed install-manifest (the owner
    holds that component to an exact executable, so its replacement must name one too). Without a digest-pinned
    baseline it stays optional and is shown as NOT_RECORDED - never invented."""
    from aeos.inventory import facade as inventory

    pinned: list[dict[str, Any]] = []
    platform = None
    for cid in _digest_targets(p):
        comp = inventory.get_component(session, cid)
        if not R.component_in_domain(comp, p.domain):
            continue
        sel = inventory.approved_digest(cid, session=session)
        platform = sel["platform"]
        if sel["digest"]:
            pinned.append({"component_id": cid, "approved_digest": sel["digest"]})
    return {"required": bool(pinned), "digest_pinned_components": pinned, "platform": platform,
            "candidate_digest": p.candidate_digest,
            "status": "RECORDED" if p.candidate_digest else ("MISSING" if pinned else "NOT_RECORDED")}


def candidate_digest_problems(session: Session, p: ChangeProposal, since: datetime | None,
                              targets: list[str] | None = None) -> list[str]:
    """ACCEPTED with a candidate digest: the executable this domain observed installed after the promotion must be
    exactly that digest, and a digest the reviewed manifest records for this platform must be it too.

    Only the ACCEPTED component is compared (*targets*: the accepted configuration's component, else the candidate
    components, or the upgraded component itself) - never the old component a REPLACE retires, whose bytes are
    not the candidate's. A candidate digest names one artifact, so several targets need
    ``accepted_configuration.component_id`` to say which one it belongs to."""
    from aeos.inventory import facade as inventory

    if not p.candidate_digest:
        return []
    if targets is None:
        targets = list(p.candidate_component_ids or []) or (
            list(p.existing_component_ids or []) if p.kind == "UPGRADE" else [])
    if len(targets) > 1:
        return [f"candidate_digest names one artifact but the accepted configuration covers {sorted(targets)}; set "
                "accepted_configuration.component_id to the component the digest belongs to"]
    problems: list[str] = []
    for cid in targets:
        comp = inventory.get_component(session, cid)
        if not R.component_in_domain(comp, p.domain):
            continue
        sel = inventory.approved_digest(cid, session=session)
        if sel["digest"] and sel["digest"] != p.candidate_digest:
            problems.append(f"{cid} approved_digest for {sel['platform']} {sel['digest'][:19]}... != candidate_digest "
                            f"{p.candidate_digest[:19]}... (manifest not applied)")
        obs = _latest_observation(session, cid, ("installation",), since, domain=p.domain)
        observed = str(getattr(obs, "observed_identity", "") or "") if obs is not None else ""
        if not observed.startswith("sha256:"):
            problems.append(f"{cid}: no installation observation with an executable digest after the promotion; the "
                            "candidate_digest cannot be compared (reconcile the inventory first)")
        elif observed != p.candidate_digest:
            problems.append(f"{cid}: observed executable digest {observed[:19]}... != candidate_digest "
                            f"{p.candidate_digest[:19]}... (not the reviewed candidate artifact)")
    return problems


def _latest_observation(session: Session, cid: str, dimensions: tuple[str, ...], since: datetime | None, *,
                        domain: str | None) -> Any:
    """The latest observation of *cid* in *dimensions* that belongs to *domain* (untagged or tagged with it),
    if it is newer than *since*. Another domain's observation is never this domain's evidence (§23, §38)."""
    from aeos.models.inventory import ComponentObservation

    since = ensure_aware(since)
    for o in session.scalars(select(ComponentObservation).where(ComponentObservation.component_id == cid,
                                                                ComponentObservation.dimension.in_(dimensions))
                             .order_by(ComponentObservation.observed_at.desc())):
        if not R.observation_in_domain(o, domain):
            continue
        at = ensure_aware(o.observed_at)
        if since is None or (at is not None and at > since):
            return o
        return None
    return None


def observed_at_pin(session: Session, cid: str, pin: str | None, since: datetime | None, *,
                    domain: str | None) -> bool:
    """The latest installation or configuration observation of *domain* after *since* shows the component
    INSTALLED/CONFIGURED at *pin* (§38 observed lens; desired state alone is not activation)."""
    for dim, ok in (("installation", "INSTALLED"), ("configuration", "CONFIGURED")):
        o = _latest_observation(session, cid, (dim,), since, domain=domain)
        if o is not None and o.status == ok and (not pin or (o.observed_version or "").lstrip("v") == pin.lstrip("v")):
            return True
    return False


def config_applied_problems(session: Session, p: ChangeProposal, cfg: dict[str, Any],
                            since: datetime | None = None) -> list[str]:
    """§47 "ENABLED hanya dicatat setelah keputusan dan perubahan konfigurasi yang sah": the inventory must show
    the accepted configuration - the reviewed install manifest declares the accepted pin AND the component is
    enabled by decision or observed (installed/configured at that version) after the promotion - never a
    hand-typed dict alone. For REMOVE the component must be retired, tombstoned or observed removed."""
    from aeos.inventory import facade as inventory

    targets = _config_targets(p, cfg)
    if not targets:
        return ["no inventory component records the accepted configuration; register the candidate in the install "
                "manifest (candidate_component_ids) first"]
    problems = []
    for cid, pin in targets:
        comp = inventory.get_component(session, cid)
        if not R.component_in_domain(comp, p.domain):
            problems.append(f"{cid} is not registered in the inventory")
            continue
        assert comp is not None
        if p.kind == "REMOVE":
            gone = _latest_observation(session, cid, ("installation",), since, domain=p.domain)
            if not (comp.tombstoned_at or comp.lifecycle_final in ("RETIRED", "DEPRECATED")
                    or (gone is not None and gone.status in ("NOT_INSTALLED", "REMOVED"))):
                problems.append(f"{cid} is still {comp.enablement}/{comp.lifecycle_final} and not observed removed "
                                "(removal not applied)")
            continue
        if pin and comp.approved_pin != pin:
            problems.append(f"{cid} approved_pin {comp.approved_pin} != accepted {pin} (manifest not applied)")
        if pin and p.candidate_pin and pin != p.candidate_pin:
            problems.append(f"accepted pin {pin} differs from the promoted candidate pin {p.candidate_pin}")
        if comp.enablement != "ENABLED" and not observed_at_pin(session, cid, pin, since, domain=p.domain):
            problems.append(f"{cid} is neither enabled by decision nor observed installed/configured"
                            + (f" at {pin}" if pin else "") + " after the promotion (configuration not applied)")
    if p.kind != "REMOVE":
        problems += candidate_digest_problems(session, p, since, [cid for cid, _pin in targets])
    return problems


def _observed_after(session: Session, p: ChangeProposal, since: datetime | None, evs: list[EvidenceArtifact],
                    cfg: dict[str, Any]) -> bool:
    from aeos.models.inventory import ComponentObservation

    since = ensure_aware(since)
    if since is None:
        return False
    if any((ensure_aware(e.created_at) or since) > since for e in evs):
        return True
    try:
        targets = [c for c, _ in _config_targets(p, cfg)]
    except ValidationFailed:
        targets = []
    for o in session.scalars(select(ComponentObservation).where(ComponentObservation.component_id.in_(targets))):
        if not R.observation_in_domain(o, p.domain):
            continue  # another domain's observation is never evidence for this proposal's outcome
        at = ensure_aware(o.observed_at)
        if at is not None and at > since and str(o.status).upper() in GOOD_OBSERVATION:
            return True
    return False


def _acceptance_met(session: Session, p: ChangeProposal, results: dict[str, Any] | None, evs: list[EvidenceArtifact],
                    waiver_decision_id: str | None) -> dict[str, Any]:
    declared = R.owner_acceptance(p)
    results = dict(redact(results or {}))
    bound_ids = {e.id for e in evs} | {e.id for e in bound_evidence(session, p, p.evidence_ids or [], purpose="outcome")}
    rows = []
    for a in declared:
        raw = results.get(a["id"])
        given: dict[str, Any] = raw if isinstance(raw, dict) else {}  # absent/non-object result -> UNKNOWN
        status = str(given.get("status") or "UNKNOWN").upper()
        if status not in ("PASS", "FAIL", "UNKNOWN"):
            raise ValidationFailed(f"acceptance_results[{a['id']}].status must be PASS, FAIL or UNKNOWN")
        ev = given.get("evidence_id")
        if status == "PASS" and ev not in bound_ids:
            status = "UNKNOWN"  # a PASS without bound trusted evidence is not a pass (§34)
        rows.append({"id": a["id"], "criterion": a["criterion"], "required": a.get("required", True), "status": status,
                     "evidence_id": ev, "observed": redact_text(str(given.get("observed") or ""))[:200]})
    unmet = [r["id"] for r in rows if r["required"] and r["status"] != "PASS"]
    if not declared:
        unmet = ["NOT_DECLARED"]
    waiver = None
    if unmet:
        if waiver_decision_id:
            d = session.get(Decision, waiver_decision_id)
            if d is None or d.kind != "RISK_ACCEPTANCE" or d.status != "ACTIVE" or d.target_type != TARGET_TYPE or \
                    d.target_id != p.id or d.domain not in (None, p.domain):
                raise ValidationFailed("waiver_decision_id must be an ACTIVE RISK_ACCEPTANCE decision for this proposal")
            waiver = d.id
        else:
            raise Blocked("acceptance declared before the trial is not met: " + ", ".join(unmet),
                          code="ACCEPTANCE_NOT_MET", details={"criteria": rows, "unmet": unmet})
    return {"criteria": rows, "unmet": unmet, "waiver_decision_id": waiver}


def _rollback_reference(session: Session, p: ChangeProposal, rb: dict[str, Any], since: datetime | None) -> dict[str, Any]:
    raw_ref = rb.get("reference")
    ref: dict[str, Any] = raw_ref if isinstance(raw_ref, dict) else {}  # no reference object -> not referenced
    aid = str(ref.get("audit_event_id") or "")
    if not aid:
        raise ValidationFailed("ROLLED_BACK must reference the rollback/restore operation that actually ran "
                               "(rollback.reference.audit_event_id: ops.release.rollback, ops.backup.restore or "
                               "inventory.manifest.load)", code="ROLLBACK_NOT_REFERENCED")
    ev = session.scalar(select(AuditEvent).where(AuditEvent.event_id == aid))
    if ev is None or ev.domain not in (None, p.domain):
        raise ValidationFailed(f"audit event {aid} not found in this domain", code="ROLLBACK_NOT_REFERENCED")
    if ev.action not in ROLLBACK_AUDIT_ACTIONS or ev.result != "SUCCESS":
        raise ValidationFailed(f"audit event {aid} ({ev.action} {ev.result}) is not a successful rollback/restore",
                               code="ROLLBACK_NOT_REFERENCED")
    since = ensure_aware(since)
    if since is None:
        raise ValidationFailed("nothing was trialled or promoted under this proposal, so nothing can be rolled back; "
                               "record FAILED or decide KEEP/REJECT", code="ROLLBACK_NOT_REFERENCED")
    at = ensure_aware(ev.at)
    if at is None or at < since:
        raise ValidationFailed("the referenced rollback ran before the promotion/trial it would undo; it is not this "
                               "rollback",
                               code="ROLLBACK_NOT_REFERENCED")
    out: dict[str, Any] = {"audit_event_id": aid, "action": ev.action, "at": at.isoformat() if at else None}
    if ev.action == "inventory.manifest.load" and p.kind in ("UPGRADE", "REPLACE"):
        prev = {b["component_id"]: b.get("approved_pin") for b in
                ((p.review_package or {}).get("baseline") or {}).get("components") or [] if b.get("status") == "KNOWN"}
        def live_pin(cid: str) -> str:
            comp = R.domain_component(session, cid, p.domain)
            return (comp.approved_pin if comp is not None else None) or R.UNKNOWN

        wrong = [f"{cid} pin {live_pin(cid)} != {pin}" for cid, pin in prev.items() if live_pin(cid) != (pin or R.UNKNOWN)]
        if wrong:
            raise ValidationFailed("the manifest reload did not restore the previous accepted configuration: "
                                   + "; ".join(wrong), code="ROLLBACK_NOT_APPLIED")
        out["restored_pins"] = prev
    if rb.get("data_rollback") == "DONE":
        rid = str(rb.get("restore_evidence_id") or "")
        rev = session.get(EvidenceArtifact, rid) if rid else None
        if rev is None or not rev.trusted or rev.mode != "LIVE" or rev.domain not in (None, p.domain) or \
                (rev.kind != "RESTORE_TEST" and rev.origin != "restore-test"):
            raise ValidationFailed("data_rollback DONE needs restore_evidence_id: trusted restore-test evidence",
                                   code="ROLLBACK_NOT_REFERENCED")
        out["restore_evidence_id"] = rev.id
    return out


def record_outcome(
    session: Session, p: ChangeProposal, *, actor_user_id: str, status: str, evidence_ids: list[str] | None = None,
    accepted_configuration: dict[str, Any] | None = None, partial_failures: list[Any] | None = None,
    rollback: dict[str, Any] | None = None, notes: str = "", before_snapshot_id: str | None = None,
    after_snapshot_id: str | None = None, acceptance_results: dict[str, Any] | None = None,
    waiver_decision_id: str | None = None, adr_keys: list[str] | None = None,
    reconciled_failures: list[Any] | None = None,
) -> ChangeProposal:
    from aeos.proposals import links as L

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
    trusted = bound_evidence(session, p, ev_ids, purpose="outcome")
    if len(trusted) != len(ev_ids):
        raise ValidationFailed("outcome evidence must be trusted LIVE evidence in this domain about this candidate")
    if adr_keys:
        L.add_links(session, p, actor_user_id=actor_user_id, adr_keys=adr_keys)
    promotion = _active_decision(session, p, "PROMOTION")
    entry_extra: dict[str, Any] = {}
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
        _d, problems = binding_problems(session, p, action="PROMOTE", after_promotion=True)
        if problems:
            raise _stale(session, p, problems, "ACCEPTED outcome")
        if not trusted:
            raise Blocked("NOT-PROVEN: ACCEPTED requires trusted evidence", code="NOT_PROVEN")
        if not accepted_configuration:
            raise ValidationFailed("accepted_configuration is required for ACCEPTED")
        cfg_problems = config_applied_problems(session, p, accepted_configuration, promotion.created_at)
        if cfg_problems:
            raise Blocked("the accepted configuration is not applied in the inventory: " + "; ".join(cfg_problems),
                          code="CONFIG_NOT_APPLIED", details={"problems": cfg_problems})
        if not _observed_after(session, p, promotion.created_at, trusted, accepted_configuration):
            raise Blocked("no observation/evidence after activation: record the post-activation observation "
                          "(inventory probe or verifier evidence) before ACCEPTED", code="OBSERVATION_REQUIRED")
        entry_extra["acceptance"] = _acceptance_met(session, p, acceptance_results, trusted, waiver_decision_id)
        if p.kind in ADR_REQUIRED_KINDS and not (p.links or {}).get("adr_keys"):
            raise Blocked("link the ADR that records this architecture change before ACCEPTED", code="ADR_REQUIRED")
        if p.kind == "REMOVE":
            rem = R.removal_plan(p, ((p.review_package or {}).get("baseline") or {}).get("components") or []) or {}
            if rem.get("open"):
                raise Blocked("REMOVE closure items are still open: " + ", ".join(rem["open"][:10]),
                              code="REMOVAL_NOT_CLOSED", details={"open": rem["open"]})
        if p.kind == "REPLACE" and not (promotion.details or {}).get("cutover"):
            raise Blocked("the cutover was not authorised by the promotion decision", code="CUTOVER_NOT_AUTHORISED")
        trial_d = _active_decision(session, p, "TRIAL")
        trial_env = (p.trial or {}).get("environment") or ((trial_d.auth_scope or {}).get("environment") if trial_d
                                                           else None)
        if (p.owner_plan or {}).get("canary") and trial_env == "canary" and \
                not (p.owner_plan or {}).get("canary_evidence_ids"):
            raise Blocked("the canary plan requires canary observation evidence before widening/ACCEPTED",
                          code="CANARY_EVIDENCE_REQUIRED")
    if status == "PARTIAL" and not partial_failures:
        raise ValidationFailed("PARTIAL must list the failed parts/targets")
    reconciliation: list[dict[str, Any]] = []
    if previous == "PARTIAL" and status == "ACCEPTED":
        # §40 "tombol 'selesai' tidak membuka gate": completing a PARTIAL outcome needs every listed failed part
        # reconciled with a resolution and trusted evidence (it is never a plain status flip).
        reconciliation = _reconcile_partial(session, p, reconciled_failures or [])
    rb: dict[str, Any] | None = None
    if status == "ROLLED_BACK":
        rb = dict(redact(rollback or {}))
        data_rb = str(rb.get("data_rollback") or "UNKNOWN").upper()
        if data_rb not in DATA_ROLLBACK:
            raise ValidationFailed(f"rollback.data_rollback must be one of {DATA_ROLLBACK}")
        rb["data_rollback"] = data_rb
        rb.setdefault("code_reverted", False)
        since = promotion.created_at if promotion else None
        if since is None:
            last_acc = [h for h in (p.outcome or {}).get("history") or [] if h.get("status") == "ACCEPTED"]
            since = datetime.fromisoformat(last_acc[-1]["at"]) if last_acc else None
        if since is None:
            # no promotion/acceptance: what is rolled back is the trial, so the rollback must postdate the
            # TRIAL decision (an old, unrelated rollback/restore is never "this" rollback)
            trial_d = _active_decision(session, p, "TRIAL") or next(
                (d for d in reversed(_decisions(session, p)) if d.kind == "TRIAL"), None)
            since = trial_d.created_at if trial_d else None
        rb["reference"] = _rollback_reference(session, p, rb, since)
        rb["note"] = "Rollback kode tidak menjamin rollback data; baseline sebelumnya tetap berlaku."
    after_snap = _snapshot(session, after_snapshot_id, p)
    if after_snap and promotion is not None and status == "ACCEPTED":
        at = ensure_aware(datetime.fromisoformat(after_snap["created_at"])) if after_snap.get("created_at") else None
        if at is None or at < ensure_aware(promotion.created_at):  # type: ignore[operator]
            raise ValidationFailed("the after snapshot must be taken after the promotion decision")
    entry = {
        "status": status, "accepted": status == "ACCEPTED", "at": utcnow().isoformat(), "actor": actor_user_id,
        "evidence_ids": ev_ids, "accepted_configuration": redact(accepted_configuration) if status == "ACCEPTED" else None,
        "partial_failures": redact(partial_failures or []), "rollback": rb, "notes": redact_text(notes or "")[:4000],
        "before_snapshot": _snapshot(session, before_snapshot_id, p), "after_snapshot": after_snap,
        "promotion_decision_id": promotion.id if promotion else None,
        "inventory_docs_update": "PENDING", **entry_extra,
    }
    if reconciliation:
        entry["reconciled_failures"] = reconciliation
    p.evidence_ids = sorted({*(p.evidence_ids or []), *ev_ids})
    p.status = status
    if status in CLOSING_OUTCOMES:
        try:
            _invalidate(session, p, f"outcome {status}")
        except Blocked:
            entry["decision_invalidation"] = "PENDING (decision registry unavailable)"
    if status == "ROLLED_BACK" and rb is not None:
        rbind = binding_hash(p.id, "ROLLBACK", p.baseline_hash or "", p.candidate_hash or "",
                             {"reference": rb["reference"], "data_rollback": rb["data_rollback"]})
        d = _record_decision(
            session, kind="ROLLBACK", actor_user_id=actor_user_id, target_type=TARGET_TYPE, target_id=p.id,
            target_version=p.review_hash, target_hash=rbind, allowed_operations=list(ROLLBACK_OPERATIONS),
            auth_scope={"reference": rb["reference"], "data_rollback": rb["data_rollback"]}, project_id=p.project_id,
            domain=p.domain, rationale=redact_text(notes or "rollback"),
            details={"action": "ROLLBACK", "baseline_hash": p.baseline_hash, "candidate_hash": p.candidate_hash,
                     "accepted_is_success": False})
        p.decision_ids = [*(p.decision_ids or []), d.id]
        entry["rollback_decision_id"] = d.id
    if status in ("ACCEPTED", "ROLLED_BACK", "PARTIAL"):
        L.finalize(session, p, entry)
    hist = [*((p.outcome or {}).get("history") or []), entry]
    p.outcome = {"current": entry, "history": hist}
    _sync_outcome_research(session, p, status, promotion.id if promotion else None)
    _append_test_results(session, p, [{"at": entry["at"], "proposal_id": p.id, "outcome": status,
                                       "evidence_ids": ev_ids, "candidate_pin": p.candidate_pin,
                                       "level": "E3 (runtime outcome, platform ini)" if status == "ACCEPTED" else
                                       f"outcome {status}"}])
    session.flush()
    audit.record(session, actor_type="user", actor_id=actor_user_id, action=f"proposal.outcome.{status.lower()}",
                 target_type=TARGET_TYPE, target_id=p.id, project_id=p.project_id, domain=p.domain, reason=notes,
                 details={"status": status, "accepted": status == "ACCEPTED", "evidence_ids": ev_ids})
    events.emit(session, source="proposals", kind="proposal.outcome", entity_type=TARGET_TYPE, entity_id=p.id,
                project_id=p.project_id, domain=p.domain, payload={"status": status, "accepted": status == "ACCEPTED"})
    return p


def _failure_label(item: Any) -> str:
    if isinstance(item, dict):
        for k in ("part", "target", "title", "name", "id"):
            if str(item.get(k) or "").strip():
                return str(item[k]).strip()
        return str(redact(item))
    return str(item).strip()


def _reconcile_partial(session: Session, p: ChangeProposal, reconciled: list[Any]) -> list[dict[str, Any]]:
    """Every failed part of the current PARTIAL outcome needs ``{part, resolution, evidence_ids}`` with trusted
    LIVE evidence before the outcome may become ACCEPTED (ValidationFailed/Blocked otherwise)."""
    failures = [_failure_label(f) for f in ((p.outcome or {}).get("current") or {}).get("partial_failures") or []]
    by_part: dict[str, dict[str, Any]] = {}
    for r in reconciled:
        if isinstance(r, dict) and str(r.get("part") or "").strip():
            by_part[str(r["part"]).strip()] = r
    missing = [f for f in failures if f not in by_part]
    if missing:
        raise ValidationFailed("PARTIAL -> ACCEPTED needs a reconciliation for every failed part",
                               code="PARTIAL_NOT_RECONCILED", details={"unreconciled": missing})
    out: list[dict[str, Any]] = []
    for f in failures:
        r = by_part[f]
        ev = [str(x) for x in r.get("evidence_ids") or []]
        resolution = redact_text(str(r.get("resolution") or "")).strip()
        if not resolution or not ev or len(_trusted_evidence(session, p, ev)) != len(ev):
            raise Blocked(f"NOT-PROVEN: failed part {f!r} needs a resolution and trusted LIVE evidence",
                          code="PARTIAL_NOT_RECONCILED", details={"part": f})
        out.append({"part": f, "resolution": resolution[:1000], "evidence_ids": ev})
    return out


def _sync_outcome_research(session: Session, p: ChangeProposal, status: str, decision_id: str | None) -> None:
    for eid in p.research_entry_ids or []:
        e = session.get(ResearchEntry, eid)
        if e is None or not _syncable(e, p):
            continue
        if status == "ACCEPTED" and p.kind == "REMOVE":
            # §47 ENABLED only after a lawful configuration change that ENABLES the candidate: an accepted removal
            # retires the component, so its entry leaves ENABLED (kept as a dated reference, history intact).
            to = "REFERENCE_ONLY" if e.lifecycle == "ENABLED" else e.lifecycle
            e.history = [*(e.history or []), {"at": utcnow().isoformat(), "event": "RETIRED", "from": e.lifecycle,
                                              "to": to, "proposal_id": p.id, "decision_id": decision_id,
                                              "config": "removed (inventory)"}]
            e.lifecycle = to
            e.owner_decision_id = decision_id
        elif status == "ACCEPTED":
            e.history = [*(e.history or []), {"at": utcnow().isoformat(), "event": "PROMOTED", "from": e.lifecycle,
                                              "to": "ENABLED", "proposal_id": p.id, "decision_id": decision_id,
                                              "config": "applied (inventory)"}]
            e.lifecycle = "ENABLED"
            e.owner_decision_id = decision_id
        elif p.kind == "REMOVE":
            # A removal that failed/was rolled back leaves the component in place: restore what the accepted
            # removal changed (never demote an entry this proposal did not retire).
            hit = next((h for h in reversed(e.history or [])
                        if h.get("event") == "RETIRED" and h.get("proposal_id") == p.id), None)
            if hit and e.lifecycle == hit.get("to") and hit.get("from") not in (None, e.lifecycle):
                e.history = [*(e.history or []), {"at": utcnow().isoformat(), "event": status, "from": e.lifecycle,
                                                  "to": hit["from"], "proposal_id": p.id}]
                e.lifecycle = hit["from"]
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
    from aeos.inventory import facade as inventory
    from aeos.proposals import links as L

    decisions = _decisions(session, p)
    promotion = next((d for d in reversed(decisions) if d.kind == "PROMOTION"), None)
    cur = (p.outcome or {}).get("current") or {}
    trusted = _trusted_evidence(session, p, p.evidence_ids or [])
    targets = [*(p.candidate_component_ids or []), *((p.existing_component_ids or []) if p.kind == "UPGRADE" else [])]
    enabled: list[Any] = []
    observed: list[Any] = []
    for c in (inventory.get_component(session, cid) for cid in targets):
        if c is None or not R.component_in_domain(c, p.domain) or \
                (p.kind == "UPGRADE" and p.candidate_pin and c.approved_pin != p.candidate_pin):
            continue
        if c.enablement == "ENABLED":
            enabled.append(c)
            continue
        if promotion is not None and observed_at_pin(session, c.component_id, p.candidate_pin, promotion.created_at,
                                                     domain=p.domain):
            observed.append(c)
    after = cur.get("after_snapshot") or {}
    arch_ok = False
    if after and promotion is not None:
        snap = session.get(ArchitectureSnapshot, after.get("id"))
        arch_ok = bool(snap and R.snapshot_in_domain(snap, p.domain) and ensure_aware(snap.created_at)
                       and ensure_aware(promotion.created_at)
                       and ensure_aware(snap.created_at) >= ensure_aware(promotion.created_at))  # type: ignore[operator]
    # §41 "kapan aktif": when the inventory first observed a candidate as ENABLED (never the approval time)
    activated_at = None
    if enabled:
        from aeos.models.inventory import ComponentObservation

        firsts = [o.observed_at for o in session.scalars(select(ComponentObservation).where(
            ComponentObservation.component_id.in_([c.component_id for c in enabled]),
            ComponentObservation.dimension == "enablement", ComponentObservation.status == "ENABLED"))
            if o.observed_at is not None and R.observation_in_domain(o, p.domain)]
        first = min(firsts, key=lambda at: ensure_aware(at) or at) if firsts else None
        activated_at = first.isoformat() if first else None

    def dim(status: str, note: str, **extra: Any) -> dict[str, Any]:
        return {"status": status, "note": note, **extra}

    implemented = cur.get("status") or "UNKNOWN"
    docs = L.docs_published(session, p)
    return {
        "APPROVED": dim("YES" if promotion and promotion.status == "ACTIVE" else
                        (promotion.status if promotion else "NO"), "Keputusan PROMOTION terikat hash"),
        "IMPLEMENTED": dim(implemented, "Outcome yang dicatat manusia dengan bukti"
                           + ("; sebagian target gagal" if implemented == "PARTIAL" else "")),
        "ACTIVE": dim("ENABLED_IN_CONFIG" if enabled else "OBSERVED_IN_CONFIG" if observed else "UNKNOWN",
                      "Dari konfigurasi/observasi inventaris, bukan dari approval", at=activated_at),
        "VERIFIED": dim("EVIDENCE_ATTACHED" if trusted else "NOT-PROVEN", "Bukti terpercaya tentang kandidat ini"),
        "ARCHITECTURE_SOURCE_UPDATED": dim("YES" if arch_ok else "UNKNOWN",
                                           "ArchitectureSnapshot domain ini, diambil sesudah keputusan PROMOTION"),
        "DOCS_PUBLISHED": dim(docs["status"], docs["note"]),
    }


def _goal_tokens(p: ChangeProposal) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", f"{p.title} {p.problem}".lower()) if len(t) >= 4 and t not in _STOPWORDS}


def possible_duplicates(session: Session, p: ChangeProposal) -> list[dict[str, Any]]:
    """§39 "Duplikat ... tujuan": same-domain proposals with the same component+kind or an overlapping goal.
    A suggestion only - never merged automatically."""
    family = {x.id for x in duplicate_family(session, p)} | {p.id}
    mine = _goal_tokens(p)
    comps = set(p.existing_component_ids or [])
    out = []
    # Same domain AND the same project scope (platform proposals or this proposal's project): a viewer of this
    # proposal never learns titles of proposals in projects outside its access (§35).
    scope = or_(ChangeProposal.project_id.is_(None), ChangeProposal.project_id == p.project_id) if p.project_id \
        else ChangeProposal.project_id.is_(None)
    for row in session.scalars(select(ChangeProposal).where(ChangeProposal.domain == p.domain,
                                                            ChangeProposal.id != p.id,
                                                            ChangeProposal.parent_id.is_(None), scope)
                               .order_by(ChangeProposal.created_at.desc()).limit(500)):
        if row.id in family or row.parent_id == p.id:
            continue
        reason = None
        if comps and row.kind == p.kind and comps & set(row.existing_component_ids or []):
            reason = f"jenis {p.kind} pada komponen yang sama ({', '.join(sorted(comps & set(row.existing_component_ids or [])))})"
        else:
            other = _goal_tokens(row)
            if mine and other:
                jac = len(mine & other) / len(mine | other)
                if jac >= 0.5:
                    reason = f"tujuan/judul mirip ({round(jac * 100)}% token sama)"
        if reason:
            out.append({"id": row.id, "title": row.title, "status": row.status, "kind": row.kind, "reason": reason})
        if len(out) >= 10:
            break
    return out


def proposal_view(session: Session, p: ChangeProposal, *, detail: bool = False) -> dict[str, Any]:
    v: dict[str, Any] = {
        "id": p.id, "kind": p.kind, "title": p.title, "status": p.status, "domain": p.domain, "scope_type": p.scope_type,
        "project_id": p.project_id, "parent_id": p.parent_id, "duplicate_of": p.duplicate_of,
        "source_refs": p.source_refs or [], "canonical_identities": p.canonical_identities or [],
        "existing_component_ids": p.existing_component_ids or [], "submitted_by": p.submitted_by,
        "baseline_hash": p.baseline_hash, "candidate_hash": p.candidate_hash, "candidate_pin": p.candidate_pin,
        "candidate_digest": p.candidate_digest,
        "reviewed_at": p.reviewed_at.isoformat() if p.reviewed_at else None,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        "parts_count": len(p.parts or []), "outcome_status": ((p.outcome or {}).get("current") or {}).get("status"),
        "allowed_actions": R.allowed_actions(p),
    }
    if detail:
        from aeos.proposals import checkpoint
        from aeos.proposals import links as L

        dups = list(session.scalars(select(ChangeProposal).where(ChangeProposal.duplicate_of == p.id,
                                                                 ChangeProposal.domain == p.domain)))
        # progress recorded after the review (REMOVE closure, credential revocation) is shown as it stands now
        package = R.apply_progress(p, dict(p.review_package or {}))
        if package.get("human_decision"):
            package["human_decision"] = {**package["human_decision"], "allowed_now": R.allowed_actions(p)}
        cp = checkpoint.current(p)
        cp_view = None
        if cp is not None:
            _ok, code, msg, _rec = checkpoint.gate(session, p) if p.status in TRIAL_STATUSES else (None, None, None, None)
            cp_view = {k: v2 for k, v2 in cp.items() if k not in ("package",)} | {"gate": code, "gate_note": msg}
            if cp.get("status") == "MANUAL_PACKAGE":
                cp_view["package"] = cp.get("package")
        v.update({
            "problem": p.problem, "expected_benefit": p.expected_benefit, "limits": p.limits or {},
            "candidate_component_ids": p.candidate_component_ids or [], "attachment_ids": p.attachment_ids or [],
            "unknown_fields": p.unknown_fields or [], "parts": p.parts or [], "review_package": package,
            "review_hash": p.review_hash, "decisions": [_decision_view(d) for d in _decisions(session, p)],
            "evidence_ids": p.evidence_ids or [], "task_ids": p.task_ids or [], "outcome": p.outcome or {},
            "duplicates": [{"id": d.id, "title": d.title, "status": d.status} for d in dups],
            "possible_duplicates": possible_duplicates(session, p),
            "research_entries": [research.entry_view(e) for e in R.research_entries(session, p)],
            "delivery_dimensions": delivery_dimensions(session, p),
            "owner_plan": p.owner_plan or {}, "links": L.link_views(session, p), "trial": p.trial or {},
            "checkpoint_review": cp_view,
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
    from aeos.proposals import links as L

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
                 "candidate_digest": p.candidate_digest,
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
        "links": L.link_views(session, p),
        "trial": {k: (p.trial or {}).get(k) for k in ("status", "launched_at", "plan_id", "task_ids", "stopped_at")},
        "source": "audit_events + decisions + outcome history + links", "checked_at": utcnow().isoformat(),
    }
