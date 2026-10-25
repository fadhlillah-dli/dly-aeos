"""U-23 (§01): the master keeps its name and later updates use the same file "with internal change history and
traceable approval" (review W6-006). The Readiness Doctor compares the master file's sha256 with the newest
human-approved ArchitectureSnapshot (or a human Decision on ``master_architecture`` bound to that hash) and reports an
unapproved master change as NOT-PROVEN (UNKNOWN, never a pass) with the owner's remedy."""

from __future__ import annotations

from pathlib import Path

from aeos.core.hashing import content_hash, file_sha256
from aeos.doctor.checks import REGISTRY, load_all
from aeos.doctor.checks import policy as policy_checks
from aeos.doctor.runner import make_context, run_check
from aeos.inventory.manifest import MASTER_REL
from aeos.models.decisions import ArchitectureSnapshot, Decision
from tests.unit.test_inventory_fakes import base_runner


def _check(settings, db, repo: Path):
    load_all()
    return run_check(REGISTRY["policy.master_approval"], make_context(settings, db, runner=base_runner(), repo=repo))


def _repo(tmp_path: Path, text: str = "# DLY AEOS Master Architecture Final\n\nv1\n") -> Path:
    repo = tmp_path / "repo"
    master = repo / MASTER_REL
    master.parent.mkdir(parents=True, exist_ok=True)
    master.write_text(text, encoding="utf-8")
    return repo


def _approved_snapshot(db, sha: str | None, *, domain: str | None = None, label: str = "baseline",
                       approve: bool = True) -> ArchitectureSnapshot:
    refs = {"schema": "architecture-snapshot/1", "scope": {"domain": domain, "lenses": ["DESIGN", "CONFIG"]},
            "master": {"path": MASTER_REL, "sha256": sha} if sha else None}
    snap = ArchitectureSnapshot(label=label, refs=refs, snapshot_hash=content_hash(refs), created_by="owner")
    db.add(snap)
    db.flush()
    if approve:
        db.add(Decision(kind="APPROVAL", actor_user_id="owner", actor_kind="HUMAN", target_type="architecture_snapshot",
                        target_id=snap.id, target_hash=snap.snapshot_hash, domain=domain,
                        allowed_operations=["architecture.snapshot.approve"], rationale="owner approves"))
    db.commit()
    return snap


def test_the_check_reads_the_canonical_master_path():
    load_all()
    spec = REGISTRY["policy.master_approval"]
    assert spec.ok_statuses == frozenset({"VERIFIED"}) and spec.needs_db
    assert policy_checks._master_rel() == MASTER_REL == "docs/architecture/DLY_AEOS_Master_Architecture_Final.md"


def test_no_approved_snapshot_is_not_proven(settings, db, tmp_path):
    res = _check(settings, db, _repo(tmp_path))
    assert res.status == "UNKNOWN" and res.error_code == "MASTER_APPROVAL_MISSING"
    assert "NOT-PROVEN" in res.summary and res.remediation and "system-map" in str(res.remediation)


def test_an_approved_snapshot_of_the_current_master_verifies(settings, db, tmp_path):
    repo = _repo(tmp_path)
    snap = _approved_snapshot(db, file_sha256(repo / MASTER_REL))
    res = _check(settings, db, repo)
    assert res.status == "VERIFIED", res.summary
    assert res.observed_evidence["binding"]["snapshot_id"] == snap.id


def test_an_edited_master_is_flagged_until_a_new_approval(settings, db, tmp_path):
    repo = _repo(tmp_path)
    _approved_snapshot(db, file_sha256(repo / MASTER_REL))
    (repo / MASTER_REL).write_text("# DLY AEOS Master Architecture Final\n\nv2 - edited outside the pipeline\n",
                                   encoding="utf-8")
    res = _check(settings, db, repo)
    assert res.status == "UNKNOWN" and res.error_code == "MASTER_CHANGED_SINCE_APPROVAL", res.summary
    assert res.observed_evidence["sha256"] == file_sha256(repo / MASTER_REL)
    # an unapproved snapshot of the edited master proves nothing
    _approved_snapshot(db, file_sha256(repo / MASTER_REL), label="draft", approve=False)
    assert _check(settings, db, repo).error_code == "MASTER_CHANGED_SINCE_APPROVAL"
    # neither does an approved snapshot in the OFFICE scope (its label never reaches this platform row)
    _approved_snapshot(db, file_sha256(repo / MASTER_REL), domain="OFFICE", label="office")
    assert _check(settings, db, repo).status == "UNKNOWN"
    # the owner approves the new state: traceable again
    _approved_snapshot(db, file_sha256(repo / MASTER_REL), label="v2")
    assert _check(settings, db, repo).status == "VERIFIED"


def test_a_human_decision_bound_to_the_master_hash_verifies(settings, db, tmp_path):
    repo = _repo(tmp_path)
    sha = file_sha256(repo / MASTER_REL)
    db.add(Decision(kind="APPROVAL", actor_user_id="owner", actor_kind="HUMAN", target_type="master_architecture",
                    target_id=MASTER_REL, target_hash="sha256:" + "0" * 64, rationale="older version"))
    db.commit()
    assert _check(settings, db, repo).status == "UNKNOWN"  # a decision on another hash never matches
    db.add(Decision(kind="APPROVAL", actor_user_id="owner", actor_kind="HUMAN", target_type="master_architecture",
                    target_id=MASTER_REL, target_hash=sha, rationale="owner approves this master"))
    db.commit()
    res = _check(settings, db, repo)
    assert res.status == "VERIFIED" and res.observed_evidence["binding"]["via"] == "decision"


def test_approving_a_snapshot_marks_the_stored_result_stale(settings, db, tmp_path):
    """The stored NOT-PROVEN row does not linger for its TTL once the owner approves: a snapshot approval (or a
    master_architecture decision) invalidates ``config:master``, so readiness asks for a recheck at once."""
    from aeos.doctor.runner import latest_results, run_doctor
    from aeos.inventory import snapshots
    from aeos.policy import facade as policy
    from tests.conftest import create_admin

    repo = _repo(tmp_path)
    run_doctor(settings, db, runner=base_runner(), repo=repo, checks=["policy.master_approval"])
    db.commit()
    assert latest_results(db, ["policy.master_approval"])["policy.master_approval"].status == "UNKNOWN"
    owner = create_admin(db)
    snap = _approved_snapshot(db, file_sha256(repo / MASTER_REL), approve=False)
    snapshots.approve_snapshot(db, snap.id, domain=None, actor_user_id=owner.id, rationale="owner reviewed the master")
    db.commit()
    db.expire_all()
    row = latest_results(db, ["policy.master_approval"])["policy.master_approval"]
    assert row.status == "STALE" and "config:master" in row.observed_evidence["stale_reason"], row.observed_evidence
    report = run_doctor(settings, db, runner=base_runner(), repo=repo, checks=["policy.master_approval"])
    assert report.results[0].status == "VERIFIED"
    # a direct decision on the master hash invalidates the same way
    run_doctor(settings, db, runner=base_runner(), repo=repo, checks=["policy.master_approval"])
    db.commit()
    policy.record_decision(db, kind="APPROVAL", actor_user_id=owner.id, target_type="master_architecture",
                           target_id=MASTER_REL, target_hash=file_sha256(repo / MASTER_REL),
                           rationale="owner approves this master")
    db.commit()
    db.expire_all()
    assert latest_results(db, ["policy.master_approval"])["policy.master_approval"].status == "STALE"


def test_a_missing_master_is_missing(settings, db, tmp_path):
    (tmp_path / "empty").mkdir()
    res = _check(settings, db, tmp_path / "empty")
    assert res.status == "MISSING" and res.error_code == "MASTER_MISSING"
