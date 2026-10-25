"""Cross-module contract of ``aeos.control_center`` (§35 dashboard projections). Keep signatures stable.

The Control Center owns no records and offers no write contract: it projects other modules' records for the
dashboard (one control plane, U-12). Other modules may read the same projections - always scoped to the
principal's explicitly chosen domain - instead of re-deriving them.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from aeos.core.enums import Domain
from aeos.identity.facade import Principal


def scoped_project_ids(session: Session, principal: Principal, domain: Domain) -> list[str]:
    """Project ids the principal may see in *domain* (the scope every projection applies)."""
    from aeos.control_center.projections import scoped_project_ids as impl

    return impl(session, principal, domain)


def audit_timeline(session: Session, principal: Principal, domain: Domain, *, action: str | None = None,
                   target_type: str | None = None, target_id: str | None = None, result: str | None = None,
                   before: int | None = None, limit: int = 50) -> dict[str, Any]:
    """Domain-scoped audit timeline (redacted, with each record's retention marker)."""
    from aeos.control_center.projections import audit_timeline as impl

    return impl(session, principal, domain, action=action, target_type=target_type, target_id=target_id,
                result=result, before=before, limit=limit)


def decision_timeline(session: Session, principal: Principal, domain: Domain, *, limit: int = 200) -> dict[str, Any]:
    from aeos.control_center.projections import decision_timeline as impl

    return impl(session, principal, domain, limit=limit)
