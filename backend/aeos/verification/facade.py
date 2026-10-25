"""Cross-module contract for evidence and acceptance (§34). Owned by ``aeos.verification``.

Only collector/verifier identities may write trusted evidence. Worker-provided material is stored
with ``trusted=False`` and never upgrades a verdict on its own.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from aeos.models.evidence import AcceptanceRecord, EvidenceArtifact

TRUSTED_COLLECTORS = frozenset({"verifier", "collector", "doctor", "reconciler", "provider-ci", "restore-test"})


def collect_evidence(
    session: Session, *, kind: str, origin: str, collector_identity: str, claim: str,
    content: bytes | str | dict[str, Any] | None = None, evidence_level: str = "E0",
    subject_type: str = "", subject_id: str = "", project_id: str | None = None, domain: str | None = None,
    run_id: str | None = None, task_id: str | None = None, environment: dict[str, Any] | None = None,
    classification: str = "INTERNAL", mode: str = "LIVE", retention_policy: str = "default",
) -> EvidenceArtifact:
    """Persist content (redacted) under the evidence dir and record a hashed EvidenceArtifact.
    ``trusted`` is derived from ``origin`` ∈ TRUSTED_COLLECTORS — callers cannot set it.
    ``retention_policy`` (additive): one of ``collector.RETENTION_POLICIES`` (default/legal-hold/short)."""
    kwargs = dict(locals())  # captured before the lazy import adds 'impl'
    from aeos.verification.collector import collect_evidence as impl

    return impl(**kwargs)


def read_json(ev: EvidenceArtifact | None) -> dict[str, Any] | None:
    """Full JSON content of an artifact, only when its stored bytes still match its ``content_hash``."""
    from aeos.verification.collector import read_json as impl

    return impl(ev)


def latest_acceptance(session: Session, task_id: str) -> AcceptanceRecord | None:
    from aeos.verification.acceptance import latest_acceptance as impl

    return impl(session, task_id)


def acceptance_is_valid(
    session: Session, task_id: str, *, base_commit: str | None = None, config_hash: str | None = None,
    plan_version_hash: str | None = None,
) -> bool:
    """True only for an ACCEPTED record still valid for the given base/config/plan identity."""
    from aeos.verification.acceptance import acceptance_is_valid as impl

    return impl(session, task_id, base_commit=base_commit, config_hash=config_hash, plan_version_hash=plan_version_hash)


def verifier_runner_status(session: Session, *, domain: str) -> dict[str, Any]:
    """Which runner the verifier executes candidate code in for *domain* now (§20 "isolated runner"):
    ``isolation`` CONTAINER on a host with a proven container boundary + the verifier image, else WEAK, with the
    reasons, remedies and residual risks (additive 2026-10-19, K2)."""
    from aeos.verification.runner import runner_status as impl

    return impl(session, domain=domain)
