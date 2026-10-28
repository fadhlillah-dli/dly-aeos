"""Workforce limits shared by the scheduler, the CLI and the API (one source, ADR-0126).

``HARD_MAX_ACTIVE`` is the highest number of implementation workers that may run at once (§19/§21 pilot: 1 by
default, 2 only with a recorded owner POLICY decision). The scheduler's ``set_concurrency``, the CLI argument of
``aeos work concurrency`` and the API body ``ConcurrencyIn`` all validate against this constant, so the three can
never disagree (owner answer 4 of 2026-10-28: it stays 2 in this phase). Raising it is an ADR amendment.

The number itself lives in ``aeos.core.config`` (core cannot import workforce), where the per-route caps of
ADR-0124 (``MAX_ROUTE_ACTIVE``) are derived from it; this module re-exports it.
"""

from __future__ import annotations

from aeos.core.config import HARD_MAX_ACTIVE

__all__ = ["HARD_MAX_ACTIVE"]
