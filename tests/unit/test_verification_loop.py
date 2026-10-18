"""Deterministic verifier trigger: runs in REVIEW are verified once per head commit (§16, §19)."""

from __future__ import annotations

from pathlib import Path

import pytest

from aeos.verification import loop, suite
from tests.unit.test_verification_support import (
    FakeWorkforce,
    bind_candidate,
    make_candidate,
    make_controller,
    make_world,
    requires_git,
)

pytestmark = requires_git


def test_run_once_verifies_each_head_once(db, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    FakeWorkforce().install(monkeypatch)
    root = make_controller(tmp_path / "controller")
    monkeypatch.setattr(suite, "trusted_controller_root", lambda: root)
    w = make_world(db, verifier_policy={"ruff": False, "candidate_tests": False})
    other = make_world(db, project_key="not-platform", is_platform=False)
    cand, base, head = make_candidate(settings, w, head_files={"backend/extra.py": "X = 1\n"})
    bind_candidate(db, w, cand, base, head)
    other.run.workspace_ref = str(cand)
    db.commit()
    assert [r.id for r in loop.pending_runs(db)] == [w.run.id], "only platform runs with a head are picked"
    results = loop.run_once(limit=5)
    assert results == [{"run_id": w.run.id, "status": "PASS", "report": results[0]["report"]}]
    db.expire_all()
    assert loop.pending_runs(db) == []
    assert loop.run_once() == []


def test_start_stop_is_idempotent(settings):
    loop.start(settings)
    loop.start(settings)
    loop.stop()
    loop.stop()
