"""`aeos seed` hook: protected, held-out EvalSuites for the §44 task classes (definitions only — no
results; every candidate stays BELUM DIUJI until recorded EvalRuns exist)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from aeos.evaluation.service import seed_suites


def run(session: Session) -> dict[str, Any]:
    return seed_suites(session)
