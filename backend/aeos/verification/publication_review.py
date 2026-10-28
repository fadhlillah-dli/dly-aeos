"""Publication review gate: a worker candidate leaves the machine only after a Claude review PASS of that exact head.

Owner rule R44 (2026-10-28, ADR-0132), paraphrased: all OpenCode and OpenRouter work must pass a Claude review; a worker
has no right to commit, push, let alone merge, as the architecture says (verbatim in the owner requests register).

The trusted broker asks ``publication_review`` before every remote write of a worker run (the push of its ``aeos/*``
branch and pull-request creation, GitHub and Bitbucket). It allows the write only when the run's latest checkpoint
review (``review.checkpoint`` through the official Claude Code bridge, or the manual-package result of the same
package) is all of:

* by the Claude checkpoint reviewer (reviewer kind ``CLAUDE``), COMPLETED, and not a MOCK;
* bound to exactly the head commit being published: its checkpoint package evidence (``REVIEW_PACKAGE``, recorded by
  the trusted collector for this run; evidence is append-only) carries the head the reviewer saw in
  ``environment.commit`` and in the stored package itself (read only while its bytes match the recorded hash), and
  the review's own package hash and artifact hash - a review of an older head never covers a newer one;
* of a package that showed the reviewer the whole diff: a package whose diff excerpt was truncated
  (``diff_truncated``, more than ``DIFF_EXCERPT_CHARS``) never counts - the task is split instead;
* a PASS verdict (``PASS_VERDICTS``) that its own HIGH/CRITICAL findings do not contradict (``clamp_verdict``, the
  rule a review is recorded with);
* answered by an Opus model: the resolved model's family must be in ``PUBLICATION_REVIEWER_FAMILIES`` whatever was
  requested (owner rule 2026-10-28: the checkpoint review is Opus, never Fable), and the identity must also satisfy
  the review policy (``_model_policy_problem``, the check acceptance makes).

The broker adds one check of its own that needs git (``git_broker.push_from_mirror``): the pushed history is exactly
the runtime's single candidate commit on the run's base, the diff the reviewer saw (reason ``HISTORY``).

Anything else refuses with ``REVIEW_REQUIRED`` (fail closed). The gate adds to the broker's other checks (run state,
runtime head, quarantine release, grant, interlock, commit identity); it never replaces one. It reads recorded
evidence and approves nothing: merge stays a human action (U-09).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy.orm import Session

from aeos.core.commit_identity import valid_sha
from aeos.models.evidence import EvidenceArtifact, ReviewRecord
from aeos.models.planning import TaskSpec
from aeos.models.workforce import WorkerRun
from aeos.verification import collector
from aeos.verification.review import (
    DEFAULT_REVIEW_MODEL,
    PASS_VERDICTS,
    _model_policy_problem,
    clamp_verdict,
    latest_review,
    model_family,
)

REVIEW_REQUIRED = "REVIEW_REQUIRED"
CHECKPOINT_REVIEWER_KIND = "CLAUDE"
PACKAGE_KIND = "REVIEW_PACKAGE"
# Owner rule 2026-10-28: the checkpoint review that opens publication is Opus, never Fable. An explicit allow-list of
# resolved model families, independent of what the review requested (a Fable request never validates itself).
PUBLICATION_REVIEWER_FAMILIES: frozenset[str] = frozenset({"opus"})


@dataclass(frozen=True)
class PublicationReview:
    """The gate's answer for one head. ``reason`` is PASS or why the write is refused: NO_HEAD, NO_REVIEW, NOT_CLAUDE,
    NOT_COMPLETED, MOCK, UNBOUND, OTHER_HEAD, TRUNCATED, NOT_PASS, FINDINGS, MODEL (the broker adds HISTORY and
    REVIEW_GATE_UNAVAILABLE)."""

    allowed: bool
    reason: str
    message: str
    head_commit: str
    review_id: str | None = None
    reviewed_head: str | None = None
    verdict: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReviewBinding:
    """What a checkpoint review provably looked at: the head commit and whether its diff excerpt was cut."""

    head: str
    diff_truncated: bool


def review_binding(session: Session, rec: ReviewRecord) -> ReviewBinding | None:
    """The head commit a checkpoint review is bound to, from its own checkpoint package evidence: the recorded
    environment and the stored package (verified against its content hash) must name the same head, and the
    environment must carry this review's package and artifact hash. None = not provably bound (fail closed)."""
    if not rec.package_evidence_id:
        return None
    ev = session.get(EvidenceArtifact, rec.package_evidence_id)
    if ev is None or ev.kind != PACKAGE_KIND or not ev.trusted or not rec.run_id or ev.run_id != rec.run_id:
        return None
    env = ev.environment if isinstance(ev.environment, dict) else {}
    if not rec.package_hash or env.get("package_hash") != rec.package_hash:
        return None
    if not rec.target_hash or env.get("artifact_hash") != rec.target_hash:
        return None
    commit = str(env.get("commit") or "")
    package = collector.read_json(ev)
    run_part = package.get("run") if isinstance(package, dict) else None
    if not valid_sha(commit) or not isinstance(run_part, dict) or run_part.get("head_commit") != commit:
        return None
    truncated = package.get("diff_truncated") if isinstance(package, dict) else None
    if not isinstance(truncated, bool):
        return None
    return ReviewBinding(head=commit, diff_truncated=truncated)


def reviewed_head(session: Session, rec: ReviewRecord) -> str | None:
    """The head commit a checkpoint review is bound to (None = not provably bound to any head)."""
    binding = review_binding(session, rec)
    return binding.head if binding is not None else None


def publication_review(session: Session, run: WorkerRun, head_commit: str | None) -> PublicationReview:
    """Whether *run* may publish *head_commit* (the exact commit the broker would push or open a PR for)."""
    head = str(head_commit or "")

    def refuse(reason: str, why: str, rec: ReviewRecord | None = None,
               bound: str | None = None) -> PublicationReview:
        return PublicationReview(
            allowed=False, reason=reason, message=f"no Claude review PASS for head {head[:12] or 'NONE'}: {why}",
            head_commit=head, review_id=rec.id if rec is not None else None, reviewed_head=bound,
            verdict=rec.verdict if rec is not None else None)

    if not valid_sha(head):
        return refuse("NO_HEAD", "the run has no runtime-recorded head commit")
    rec = latest_review(session, run.id)
    if rec is None:
        return refuse("NO_REVIEW", "no Claude checkpoint review exists for this run")
    if rec.reviewer_kind != CHECKPOINT_REVIEWER_KIND:
        return refuse("NOT_CLAUDE", f"the latest checkpoint review is by {rec.reviewer_kind!r}, not the Claude "
                                    "checkpoint reviewer", rec)
    if rec.status != "COMPLETED":
        return refuse("NOT_COMPLETED", f"the latest Claude checkpoint review is {rec.status}, not COMPLETED", rec)
    if rec.mode == "MOCK":
        return refuse("MOCK", "the latest Claude checkpoint review is a MOCK", rec)
    binding = review_binding(session, rec)
    if binding is None:
        return refuse("UNBOUND", "the latest Claude checkpoint review is not bound to a recorded head commit (no "
                                 "trusted, intact checkpoint package evidence with this review's package and artifact "
                                 "hash)", rec)
    bound = binding.head
    if bound != head:
        return refuse("OTHER_HEAD", f"the latest Claude checkpoint review covers head {bound[:12]}, not this head; "
                                    "request a review of this head", rec, bound)
    if binding.diff_truncated:
        return refuse("TRUNCATED", "the reviewer saw only part of the diff (the checkpoint package excerpt was "
                                   "truncated); split the task into smaller changes and review each one", rec, bound)
    if rec.verdict not in PASS_VERDICTS:
        return refuse("NOT_PASS", f"the latest Claude checkpoint review verdict is {rec.verdict}", rec, bound)
    clamped, _notes = clamp_verdict(rec.verdict, has_controlled_evidence=True, has_staging_evidence=True,
                                    has_production_evidence=True, findings=list(rec.findings or []))
    if clamped not in PASS_VERDICTS:
        return refuse("FINDINGS", f"the review's {rec.verdict} is contradicted by its own HIGH/CRITICAL findings",
                      rec, bound)
    family = model_family(rec.model_resolved)
    if family not in PUBLICATION_REVIEWER_FAMILIES:
        return refuse("MODEL", f"the review was answered by {rec.model_resolved or 'an UNKNOWN model'} (family "
                               f"{family or 'UNKNOWN'}); only an Opus review opens publication (owner rule "
                               "2026-10-28: the checkpoint review is Opus, never Fable)", rec, bound)
    task = session.get(TaskSpec, run.task_id) if run.task_id else None
    policy = dict(task.reviewer_policy or {}) if task is not None else {}
    problem = _model_policy_problem(policy, rec.model_resolved, rec.model_requested or DEFAULT_REVIEW_MODEL)
    if problem:
        return refuse("MODEL", f"the reviewer model does not satisfy the Claude review policy ({problem})", rec,
                      bound)
    return PublicationReview(allowed=True, reason="PASS", message=f"Claude review {rec.verdict} for head {head[:12]}",
                             head_commit=head, review_id=rec.id, reviewed_head=bound, verdict=rec.verdict)
