"""Cross-module contract for intake attachments (§05 step 5, U-11). Owned by ``aeos.intake``.

Attachment content is untrusted DATA: it can inform planning but never changes goal, policy,
permission, memory, domain or routing (§25, §50).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from aeos.models.portfolio import Attachment


def get_attachment(session: Session, attachment_id: str) -> Attachment | None:
    return session.get(Attachment, attachment_id)


def attachment_text(session: Session, attachment_id: str, *, max_chars: int = 200_000) -> str:
    """Redacted extracted text; raises ``NotFound``/``Blocked`` when unavailable or rejected."""
    from aeos.intake.service import attachment_text as impl

    return impl(session, attachment_id, max_chars=max_chars)


# ---- additive (non-breaking) contract ---------------------------------------------------------------


def link_attachment(session: Session, attachment_id: str, *, proposal_id: str) -> Attachment:
    """Link an intake attachment (e.g. a ``.md`` brief, U-11) to a change proposal as provenance.

    The attachment must be in the proposal's domain (and project, when project-scoped), not REJECTED and
    not linked to another proposal (``NotFound`` / ``Blocked`` / ``Conflict``). Idempotent; does not
    commit. Linking never makes the content trusted or authoritative (§25, §50)."""
    from aeos.intake.service import link_attachment as impl

    return impl(session, attachment_id, proposal_id=proposal_id)
