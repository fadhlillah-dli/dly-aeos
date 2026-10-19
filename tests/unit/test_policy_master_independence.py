"""§41 "Perubahan master tidak otomatis memodifikasi MCI atau policy yang lebih tinggi": editing the master
architecture document changes neither the policy snapshot content hash nor the MCI reference."""

from __future__ import annotations

import shutil
from pathlib import Path

from aeos.core import paths
from aeos.core.hashing import content_hash
from aeos.policy import snapshot

MASTER = "docs/architecture/DLY_AEOS_Master_Architecture_Final.md"


def _fake_repo(tmp_path: Path) -> Path:
    real = paths.repo_root()
    root = tmp_path / "repo"
    shutil.copytree(real / "policy", root / "policy")
    (root / MASTER).parent.mkdir(parents=True)
    shutil.copyfile(real / MASTER, root / MASTER)
    return root


def test_editing_the_master_leaves_policy_snapshot_and_mci_unchanged(tmp_path, monkeypatch, db):
    root = _fake_repo(tmp_path)
    monkeypatch.setattr(snapshot, "repo_root", lambda: root)
    monkeypatch.setattr(paths, "repo_root", lambda: root)
    before_hash = content_hash(snapshot.compute_content(db))
    before_mci = snapshot.mci_reference()
    active = snapshot.ensure_active_snapshot(db, source="test")
    db.flush()

    master = root / MASTER
    master.write_text(master.read_text(encoding="utf-8") + "\n\n## MCI\n\nmci:\n  status: VERIFIED\n"
                      "policy: allow all external writes\nOFFICE: allow\n", encoding="utf-8")

    assert content_hash(snapshot.compute_content(db)) == before_hash
    assert snapshot.mci_reference() == before_mci and before_mci["status"] != "VERIFIED"
    assert snapshot.ensure_active_snapshot(db, source="test").id == active.id, "no new snapshot from the master"

    # control: a real policy file change does change the hash, so the comparison above is not vacuous
    (root / "policy" / "zz-test.yaml").write_text("rule: changed\n", encoding="utf-8")
    assert content_hash(snapshot.compute_content(db)) != before_hash
