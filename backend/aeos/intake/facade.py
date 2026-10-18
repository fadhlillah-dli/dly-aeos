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
