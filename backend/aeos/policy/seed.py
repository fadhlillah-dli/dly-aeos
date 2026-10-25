"""`aeos seed` step for the Policy & Decision Registry (idempotent).

1. Load ``docs/decisions/ADR-*.md`` into the ``adrs`` table (upsert by key, content-hashed).
2. Record the ADR-0003 owner exception (commit/push/open_pr/merge_pr on the platform repo while
   building the seed) as a Decision of kind EXCEPTION with actor ``owner:ADR-0003`` /
   HUMAN_RECORDED, bound to the ADR's content hash. A changed ADR supersedes an ACTIVE binding; an
   exception revoked by the owner is never re-created by the seed, not even after the ADR file changes
   (the delegate can edit files, so a file edit must not undo a human revocation).
3. Activate the initial PolicySnapshot.

The exception records the owner's delegation to the trusted seed builder (official Claude Code in
the owner's clone). It is *not* usable by AEOS workers or the external-write interlock, which deny
merge as a human-only operation (U-09, ADR-0003 §4).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.core.config import get_settings
from aeos.core.enums import Domain
from aeos.models.decisions import ArchitectureDecisionRecord, Decision
from aeos.policy import adrs as adr_loader
from aeos.policy import decisions as registry
from aeos.policy import snapshot

ADR_0003 = "ADR-0003"
OWNER_ACTOR = f"owner:{ADR_0003}"
EXCEPTION_OPERATIONS = ["commit", "push", "open_pr", "merge_pr"]


def _record_seed_exception(session: Session) -> str:
    adr = session.scalar(select(ArchitectureDecisionRecord).where(ArchitectureDecisionRecord.adr_key == ADR_0003))
    if adr is None or adr.status != "ACCEPTED" or not adr.content_hash:
        return "skipped (ADR-0003 not loaded/accepted)"
    owner_status = adr_loader.effective_register_status(session, adr)
    if owner_status != "ACCEPTED":  # §02: the owner's own ADR decision wins over the file's Status line
        return f"skipped (owner recorded {owner_status} for ADR-0003)"
    repo = get_settings().platform_repo_slug
    existing = list(session.scalars(
        select(Decision).where(Decision.kind == "EXCEPTION", Decision.actor_user_id == OWNER_ACTOR,
                               Decision.target_type == "repository", Decision.target_id == repo)
        .order_by(Decision.created_at.desc())
    ))
    if any(d.target_hash == adr.content_hash for d in existing):
        # Present for this exact ADR content (ACTIVE, or revoked by the owner: revocation sticks).
        current = next(d for d in existing if d.target_hash == adr.content_hash)
        if adr.decision_id != current.id:
            adr.decision_id = current.id
        return "present" if current.status == "ACTIVE" else f"present ({current.status})"
    revoked = [d for d in existing if d.status == "INVALIDATED" and (d.invalidation_reason or "").startswith("revoked:")]
    if revoked:
        # A human revocation is never undone by editing the ADR file (the delegate itself can edit files):
        # re-delegation needs a fresh, explicit owner decision outside this seed (fail closed).
        return "revoked by owner (not re-recorded from a changed ADR file)"

    platform = None
    try:
        from aeos.portfolio.facade import platform_project

        platform = platform_project(session)
    except Exception:  # portfolio may not be seeded yet; the exception targets the repository
        platform = None
    decision = registry.record_decision(
        session,
        kind="EXCEPTION",
        actor_user_id=OWNER_ACTOR,
        target_type="repository",
        target_id=repo,
        target_version=ADR_0003,
        target_hash=adr.content_hash,
        allowed_operations=list(EXCEPTION_OPERATIONS),
        auth_scope={
            "repository": repo,
            "branch": "main",
            "delegate": "official Claude Code (owner's Max login) in the owner's trusted clone",
            "scope": "seed build of DLY AEOS (M0-M4 code)",
            "not_for": ["AEOS workers", "broker", "publisher", "any non-platform repository"],
        },
        project_id=platform.id if platform is not None and platform.domain == Domain.PERSONAL.value else None,
        domain=Domain.PERSONAL.value,
        rationale=(
            "Owner instruction 2026-10-18 recorded in ADR-0003: Claude may commit, push, open and merge PRs for "
            "fadhlillah-dli/dly-aeos while building the seed. Plan approval, publication approval, risk "
            "acceptance, budgets, production, OFFICE data and credentials are NOT delegated."
        ),
        details={
            "adr_key": ADR_0003, "adr_hash": adr.content_hash, "adr_path": adr.source_path,
            "expires_when": "seed reaches M4 delivery enforcement, or the owner revokes it",
            "not_delegated": ["plan_version_approval", "publication_approval", "risk_acceptance", "budget",
                              "production_deploy", "office_data_authorisation", "credentials", "oauth_consent",
                              "vault_init"],
        },
    )
    for old in existing:
        if old.status == "ACTIVE":
            registry.supersede_decision(session, old, new_decision_id=decision.id,
                                        reason="ADR-0003 content changed; binding re-recorded")
    adr.decision_id = decision.id
    session.flush()
    return "created" if not existing else "superseded"


def run(session: Session) -> dict[str, Any]:
    loaded = adr_loader.load_adrs(session)
    exception = _record_seed_exception(session)
    registry.expire_due_decisions(session)
    snap = snapshot.ensure_active_snapshot(session, source="aeos seed")
    return {
        "adrs": {k: loaded[k] for k in ("created", "updated", "unchanged")} | {"skipped": loaded["skipped"]},
        "adr_0003_exception": exception,
        "policy_snapshot": {"version": snap.version, "content_hash": snap.content_hash},
        "mci": snapshot.mci_reference()["status"],
    }
