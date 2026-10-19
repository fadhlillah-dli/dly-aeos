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
