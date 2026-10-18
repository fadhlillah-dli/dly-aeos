"""End-to-end: the independent verifier runs the REAL trusted acceptance suite of this controller
checkout against a candidate copy of the platform code (§09 self-build, §19, §50 "Controller membangun
versi AEOS baru" → the candidate cannot fake its own pass). Needs git; slower, so marked integration."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from aeos.core.paths import repo_root
from aeos.verification import suite, verifier
from tests.unit.test_verification_support import (
    FakeWorkforce,
    git,
    make_world,
    requires_git,
    write_files,
)

pytestmark = [pytest.mark.integration, requires_git]


def _candidate_from_controller(settings, world, name):
    cand = settings.layout.task_data(world.project.domain and __import__("aeos.core.enums", fromlist=["Domain"])
                                     .Domain(world.project.domain), world.project.project_key) / name
    # A real self-build candidate is a full worktree of the platform repository (§09): copy every tracked
    # file (the trusted suite reads install-manifest/, policy/, scripts/, docs/decisions/ ...).
    root = repo_root()
    tracked = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True,
                             timeout=60).stdout.decode("utf-8").split(chr(0))
    for rel in filter(None, tracked):
        src = root / rel
        if not src.is_file():
            continue  # deleted in the working tree
        dst = cand / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    git(cand, "init", "-q")
    git(cand, "add", "-A")
    git(cand, "commit", "-q", "-m", "base")
    return cand, git(cand, "rev-parse", "HEAD")


def _verify(db, world, cand, base, head):
    world.run.workspace_ref, world.run.base_commit, world.run.head_commit = str(cand), base, head
    db.commit()
    out = verifier.verify_run(db, world.run.id, actor="it", request_repair_on_fail=False)
    db.commit()
    return out


def test_real_suite_passes_for_clean_candidate_and_catches_regression(db, settings, monkeypatch):
    FakeWorkforce().install(monkeypatch)
    w = make_world(db, verifier_policy={"candidate_tests": False, "ruff": True},
                   allowed_paths=["backend/aeos/api/**", "docs/**"])
    cand, base = _candidate_from_controller(settings, w, "real-1")
    write_files(cand, {"docs/note.md": "# note\n"})
    git(cand, "add", "-A")
    git(cand, "commit", "-q", "-m", "docs")
    head = git(cand, "rev-parse", "HEAD")
    out = _verify(db, w, cand, base, head)
    if out.status != "PASS":  # surface the trusted report for diagnosis
        from aeos.models.evidence import EvidenceArtifact
        from aeos.verification import collector

        rep = collector.read_json(db.get(EvidenceArtifact, out.checks["trusted_suite"].evidence_id)) or {}
        failing = [c for c in (rep.get("report") or {}).get("cases", []) if c["outcome"] != "passed"]
        pytest.fail(f"{out.reasons} missing={rep.get('missing_tests')} failing={failing} tail={rep.get('output_tail')}")
    assert out.suite_hash == suite.trusted_suite_hash()
    assert out.checks["trusted_suite"].details["passed"] >= 30
    assert out.checks["ruff"].status == "PASS"

    # Regression: the candidate silently lets VIEWER write. The trusted suite (not the candidate's) fails.
    deps = cand / "backend" / "aeos" / "api" / "deps.py"
    src = deps.read_text(encoding="utf-8")
    weakened = src.replace('raise Forbidden("VIEWER role is read-only", code="READ_ONLY_ROLE")', "pass")
    assert weakened != src
    deps.write_text(weakened, encoding="utf-8", newline="\n")
    git(cand, "add", "-A")
    git(cand, "commit", "-q", "-m", "weaken rbac")
    head2 = git(cand, "rev-parse", "HEAD")
    w.run.head_commit = head2
    out2 = _verify(db, w, cand, base, head2)
    assert out2.status == "FAIL"
    assert any("test_viewer_is_denied_on_every_write_route" in t for t in out2.failing_tests)
