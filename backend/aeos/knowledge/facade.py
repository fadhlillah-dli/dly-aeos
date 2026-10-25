"""Cross-module contract for context packs (§24). Owned by ``aeos.knowledge``.

Model-facing consumers (worker prompt builder, planner, reviewers) take context only through
``use_pack``: it refuses STALE/EXPIRED/SUPERSEDED/UNKNOWN packs (``CONTEXT_STALE``) and audits the use
with the pack hash, so what a model received is traceable to one minimal, budgeted pack.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session


def use_pack(session: Session, pack_id: str, *, project_id: str, consumer: str, task_id: str | None = None,
             actor: str = "system") -> dict[str, Any]:
    from aeos.knowledge.packs import use_pack as impl

    return impl(session, pack_id, project_id=project_id, consumer=consumer, task_id=task_id, actor=actor)


def mark_stale_for_repository(session: Session, repository_id: str, new_head: str, *, reason: str) -> int:
    from aeos.knowledge.packs import mark_stale_for_repository as impl

    return impl(session, repository_id, new_head, reason=reason)


def build_task_pack(session: Session, task_id: str, *, base_commit: str | None,
                    artifacts: list[dict[str, Any]] | None = None, actor: str = "worker_runtime.executor",
                    token_budget: int | None = None) -> dict[str, Any]:
    """Build the minimum task-scoped ContextPack for one TaskSpec at *base_commit* (additive, wave 7, W6-071):
    role profile + allowed-path files at that commit + policy + accepted project docs + the TaskSpec's delivered
    input artifacts as DATA, under the pack's token budget, by a system principal bound to the TaskSpec. Raises
    ``AeosError`` when it cannot be built (fail closed: the caller states the reason). Does not commit."""
    from aeos.knowledge.packs import build_task_pack as impl

    pack, meta = impl(session, task_id, base_commit=base_commit, artifacts=artifacts, actor=actor,
                      token_budget=token_budget)
    return {"context_pack_id": pack.id, "pack_hash": pack.pack_hash, "base_commit": pack.base_commit,
            "items": len(pack.items or []), "token_budget": pack.token_budget, "token_estimate": meta.token_estimate,
            "omitted": list(meta.omitted or []), "warnings": list(meta.warnings or [])}
