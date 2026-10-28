"""The verifier's diff of a candidate is never shaped by ``.gitattributes`` (owner rule R44, ADR-0132).

A worker could write ``* -diff`` into a ``.gitattributes`` file - even a nested one inside its allowed paths - and
turn every changed file into "Binary files differ": the reviewer's excerpt would stay small, the package would not be
truncated, and the change would be published unseen. The verifier's diff, numstat and changed-file reads take
attributes from the empty tree instead (``GitReader.diff_run``), so only git's own content check marks a file binary;
the checkpoint package lists those files and the publication gate refuses them (``BINARY``). A ``.gitattributes``
change is a protected path as well (quarantined for special review)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from aeos.core import protected_paths as pp
from aeos.verification import scope as vscope
from aeos.verification.procs import GitReader
from aeos.worker_runtime import scope as wscope


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c",
                           "commit.gpgsign=false", *args], cwd=cwd, capture_output=True, text=True, check=True,
                          timeout=60).stdout.strip()


def _candidate(tmp_path: Path) -> tuple[Path, str, str]:
    """A candidate whose worker commit hides its change behind a nested ``* -diff`` and adds a real binary file;
    the work tree is checked out at the head, so the attribute file is there too."""
    repo = tmp_path / "candidate"
    (repo / "src").mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(repo))
    (repo / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "src" / ".gitattributes").write_text("* -diff\n", encoding="utf-8")
    (repo / "src" / "app.py").write_text("value = 2  # changed where the reviewer must see it\n", encoding="utf-8")
    (repo / "assets").mkdir()
    (repo / "assets" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00binary")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "aeos: candidate")
    return repo, base, _git(repo, "rev-parse", "HEAD")


def test_a_nested_minus_diff_attribute_no_longer_hides_the_change(tmp_path):
    repo, base, head = _candidate(tmp_path)
    near_miss = subprocess.run(["git", "diff", base, head, "--", "src/app.py"], cwd=repo, capture_output=True,
                               text=True, check=True, timeout=60).stdout
    assert "Binary files" in near_miss, "plain git honours the worker's attribute (the bite is real)"
    git = GitReader(repo)
    diff = git.diff_bytes(base, head).decode("utf-8", "replace")
    assert "+value = 2  # changed where the reviewer must see it" in diff
    assert "Binary files a/src/app.py" not in diff and "Binary files /dev/null and b/src/.gitattributes" not in diff
    stats = git.numstat(base, head)
    assert stats["src/app.py"] == {"added": 1, "deleted": 1}
    assert stats["src/.gitattributes"] == {"added": 1, "deleted": 0}
    # a real binary file stays binary by git's own content check - the package lists it, the gate refuses it
    assert stats["assets/logo.png"] == {"added": None, "deleted": None}
    changed = git.changed_files(base, head)
    assert vscope.binary_changed_paths(changed, stats) == ["assets/logo.png"]
    assert sorted(p for _, p in changed) == ["assets/logo.png", "src/.gitattributes", "src/app.py"]


def test_a_deleted_binary_file_is_not_content_the_reviewer_missed(tmp_path):
    repo, _base, head = _candidate(tmp_path)
    (repo / "assets" / "logo.png").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "drop the logo")
    git = GitReader(repo)
    after = _git(repo, "rev-parse", "HEAD")
    assert vscope.binary_changed_paths(git.changed_files(head, after), git.numstat(head, after)) == []


def test_a_gitattributes_change_is_a_protected_path_anywhere():
    assert "**/.gitattributes" in pp.GENERIC_RESTRICTED_AREAS
    for path in (".gitattributes", "src/.gitattributes", "a/b/c/.gitattributes"):
        res = wscope.check_scope([wscope.ChangedPath(path=path, status="A")], allowed=["**"], forbidden=[],
                                 protected=pp.GENERIC_PROTECTED_PATHS)
        assert res.quarantine, path
        assert vscope.check_scope([path], allowed=["**"], forbidden=[],
                                  protected=list(pp.GENERIC_PROTECTED_PATHS)).status == "QUARANTINED", path
    near_miss = vscope.check_scope(["src/attributes.py"], allowed=["**"], forbidden=[],
                                   protected=list(pp.GENERIC_PROTECTED_PATHS))
    assert near_miss.status != "QUARANTINED"
