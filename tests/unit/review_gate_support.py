"""Claude checkpoint review records bound to a head commit, recorded the way ``aeos.verification.review.review_run``
records them (not a test module).

The broker publishes a worker run (push or pull request, GitHub or Bitbucket) only when the run's latest Claude
checkpoint review is a PASS bound to exactly the head being published (owner rule R44, ADR-0132). A broker test that
expects a publication to proceed past that gate records such a review with ``record_claude_review``.
"""

from __future__ import annotations

from typing import Any

from aeos.core.hashing import content_hash
from aeos.models.evidence import ReviewRecord
from aeos.models.workforce import WorkerRun
from aeos.verification import collector
from aeos.verification import publication_review as _gate  # noqa: F401 - loaded with the real modules, before any fake
from aeos.verification.review import PACKAGER_IDENTITY, REVIEWER_ROLE


def record_claude_review(db: Any, run: WorkerRun, *, head: str | None = None, verdict: str = "PASS-CONTROLLED",
                         status: str = "COMPLETED", mode: str = "LIVE", model_requested: str = "opus",
                         model_resolved: str | None = "claude-opus-5-5", reviewer_kind: str = "CLAUDE",
                         findings: list[dict[str, Any]] | None = None, artifact_hash: str | None = None,
                         non_regression: dict[str, Any] | None = None, diff_truncated: bool = False) -> ReviewRecord:
    """A checkpoint package (trusted ``REVIEW_PACKAGE`` evidence naming *head*, default the run's head) and the
    Claude review of it, as the native bridge path writes them. The default reviewer is Opus (owner rule 2026-10-28:
    the checkpoint review is Opus, never Fable). *artifact_hash* is the verified artifact the review is bound to (the
    verifier report's, when acceptance must accept it); *diff_truncated* is the package's own flag. Flushed, not
    committed."""
    head = head or run.head_commit or ""
    artifact_hash = artifact_hash or content_hash({"tree": head, "run": run.id})
    package = {"package_version": "1", "purpose": "review.checkpoint",
               "run": {"id": run.id, "branch": run.branch, "head_commit": head},
               "artifact": {"artifact_hash": artifact_hash}, "diff_truncated": diff_truncated}
    package_hash = content_hash(package)
    ev = collector.collect_evidence(
        db, kind="REVIEW_PACKAGE", origin="collector", collector_identity=PACKAGER_IDENTITY,
        claim=f"Checkpoint review package for run {run.id} at {head[:12]}", content=package, evidence_level="E1",
        subject_type="run", subject_id=run.id, project_id=run.project_id, domain=run.domain, run_id=run.id,
        task_id=run.task_id, environment={"commit": head, "artifact_hash": artifact_hash, "package_hash": package_hash})
    rec = ReviewRecord(
        target_type="worker_run", target_id=run.id, target_hash=artifact_hash, project_id=run.project_id,
        domain=run.domain, run_id=run.id, task_id=run.task_id, reviewer_kind=reviewer_kind,
        reviewer_identity=f"claude-code:{REVIEWER_ROLE}", model_requested=model_requested,
        model_resolved=model_resolved, verdict=verdict, status=status, package_hash=package_hash,
        package_evidence_id=ev.id, evidence_refs=[ev.id], findings=list(findings or []),
        non_regression=dict(non_regression or {}), mode=mode)
    db.add(rec)
    db.flush()
    return rec
