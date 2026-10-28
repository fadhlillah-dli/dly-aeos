"""The verifier's diff of a candidate is never shaped by ``.gitattributes`` (owner rule R44, ADR-0132).

A worker could write ``* -diff`` into a ``.gitattributes`` file - even a nested one inside its allowed paths - and
turn every changed file into "Binary files differ": the reviewer's excerpt would stay small, the package would not be
truncated, and the change would be published unseen. The verifier's diff, numstat and changed-file reads take
attributes from the empty tree instead (``GitReader.diff_run``), so only git's own content check marks a file binary;
the checkpoint package lists those files and the publication gate refuses them (``BINARY``). A ``.gitattributes``
change is a protected path as well (quarantined for special review)."""

from __future__ import annotations

import os
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


# ----------------------------------------------------------------------------- the git dir that is read (H1)
def _separate(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """A candidate laid out like an isolated clone: the work tree's ``.git`` is a pointer file to a separate
    (trusted) git dir. The head changes text and adds a real binary file."""
    work, trusted = tmp_path / "work", tmp_path / "trusted.git"
    _git(tmp_path, "init", "-q", f"--separate-git-dir={trusted.as_posix()}", str(work))
    (work / "app.py").write_text("value = 1\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "base")
    base = _git(work, "rev-parse", "HEAD")
    (work / "app.py").write_text("value = 2\n", encoding="utf-8")
    (work / "blob.bin").write_bytes(b"BIN\x00\x00payload\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "aeos: candidate")
    return work, trusted, base, _git(work, "rev-parse", "HEAD")


def _assert_real_change(git: GitReader, base: str, head: str) -> None:
    changed = git.changed_files(base, head)
    stats = git.numstat(base, head)
    assert sorted(p for _, p in changed) == ["app.py", "blob.bin"], changed
    assert vscope.binary_changed_paths(changed, stats) == ["blob.bin"]
    assert "+value = 2" in git.diff_bytes(base, head).decode("utf-8", "replace")


def test_a_rewritten_pointer_to_a_planted_git_dir_is_never_followed(tmp_path):
    """The worker rewrites ``.git`` to a planted git dir that borrows the trusted objects (alternates) but carries
    its own replace ref (head tree -> base tree) and ``*.bin diff``: through the pointer the change vanishes; through
    the trusted git dir the reviewer sees the real diff and the binary file."""
    work, trusted, base, head = _separate(tmp_path)
    planted = tmp_path / "planted.git"
    _git(tmp_path, "init", "-q", "--bare", str(planted))
    (planted / "objects" / "info" / "alternates").write_text((trusted / "objects").as_posix() + "\n",
                                                              encoding="utf-8")
    (planted / "info").mkdir(exist_ok=True)
    (planted / "info" / "attributes").write_text("*.bin diff\n", encoding="utf-8")
    head_tree, base_tree = _git(work, "rev-parse", f"{head}^{{tree}}"), _git(work, "rev-parse", f"{base}^{{tree}}")
    _git(tmp_path, "--git-dir", str(planted), "replace", "-f", head_tree, base_tree)
    (work / ".git").write_text(f"gitdir: {planted.as_posix()}\n", encoding="utf-8")
    hidden = subprocess.run(["git", "diff", "--numstat", base, head], cwd=work, capture_output=True, text=True,
                            check=True, timeout=60).stdout
    assert hidden.strip() == "", "through the pointer the change is gone (the bite is real)"
    git = GitReader(work, git_dir=trusted)
    _assert_real_change(git, base, head)
    assert git.integrity_problem() is None


def test_a_replace_ref_in_the_trusted_git_dir_is_ignored_and_reported(tmp_path):
    work, trusted, base, head = _separate(tmp_path)
    head_tree, base_tree = _git(work, "rev-parse", f"{head}^{{tree}}"), _git(work, "rev-parse", f"{base}^{{tree}}")
    _git(work, "replace", "-f", head_tree, base_tree)
    assert subprocess.run(["git", "diff", "--numstat", base, head], cwd=work, capture_output=True, text=True,
                          check=True, timeout=60).stdout.strip() == "", "plain git honours the replace ref"
    git = GitReader(work, git_dir=trusted)
    _assert_real_change(git, base, head)  # every read ignores replace refs
    problem = git.integrity_problem()
    assert problem is not None and problem.startswith("replace refs are present (refs/replace/")
    git.export_tree(head, tmp_path / "snap", max_bytes=10_000_000)
    assert (tmp_path / "snap" / "blob.bin").read_bytes() == b"BIN\x00\x00payload\n"  # the snapshot is the real head


# ----------------------------------------------------------------------------- the other attributes sources (M1)
def _attr_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".config" / "git").mkdir(parents=True)
    return home


def test_global_and_xdg_attribute_files_do_not_shape_the_diff(tmp_path, monkeypatch):
    work, trusted, base, head = _separate(tmp_path)
    home = _attr_home(tmp_path)
    attrs = home / "global-attributes"
    attrs.write_text("*.bin diff\n", encoding="utf-8")
    for channel in ("global", "xdg"):
        if channel == "global":
            (home / ".gitconfig").write_text(f"[core]\n\tattributesFile = {attrs.as_posix()}\n", encoding="utf-8")
        else:
            (home / ".gitconfig").unlink()
            (home / ".config" / "git" / "attributes").write_text("*.bin diff\n", encoding="utf-8")
        env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "GIT_CONFIG_NOSYSTEM": "1"}
        plain = subprocess.run(["git", "diff", "--numstat", base, head, "--", "blob.bin"], cwd=work, env=env,
                               capture_output=True, text=True, check=True, timeout=60).stdout
        assert plain.startswith("1\t0\t"), (channel, plain)  # the channel does shape a plain diff (the bite)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
        _assert_real_change(GitReader(work, git_dir=trusted), base, head)


def test_the_system_attributes_file_and_replace_refs_are_off_for_every_read(tmp_path):
    work, trusted, _base, _head = _separate(tmp_path)
    git = GitReader(work, git_dir=trusted)
    assert git.env["GIT_ATTR_NOSYSTEM"] == "1" and git.env["GIT_NO_REPLACE_OBJECTS"] == "1"
    args = git._args("status")
    assert args[1] == "--no-replace-objects" and f"core.attributesFile={os.devnull}" in args
    assert f"--git-dir={trusted}" in args and f"--work-tree={work}" in args


def test_info_attributes_in_the_trusted_git_dir_is_reported(tmp_path):
    work, trusted, base, head = _separate(tmp_path)
    git = GitReader(work, git_dir=trusted)
    (trusted / "info").mkdir(exist_ok=True)
    (trusted / "info" / "attributes").write_text("# comments only\n\n", encoding="utf-8")
    assert git.integrity_problem() is None  # near miss
    (trusted / "info" / "attributes").write_text("*.bin diff\n", encoding="utf-8")
    problem = git.integrity_problem()
    assert problem is not None and "info" in problem and "repository attributes shape every diff" in problem


# ----------------------------------------------------------------------------- paths with tabs (M2)
def _tree_repo(tmp_path: Path, base_files: dict[str, bytes], head_files: dict[str, bytes]) -> tuple[Path, str, str]:
    """Commits built through the index only (``--cacheinfo``): paths no file system has to hold (a tab is not a
    valid Windows file name character)."""
    repo = tmp_path / "tabs"
    tmp_path.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "init", "-q", str(repo))
    shas = []
    for files in (base_files, head_files):
        _git(repo, "read-tree", "--empty")
        for path, data in files.items():
            blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo, input=data, capture_output=True,
                                  check=True, timeout=60).stdout.decode().strip()
            _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
        _git(repo, "commit", "-q", "--allow-empty", "-m", f"commit {len(shas)}")
        shas.append(_git(repo, "rev-parse", "HEAD"))
    return repo, shas[0], shas[1]


def test_a_tab_in_a_path_never_hides_a_binary_file(tmp_path):
    binary, text = b"BIN\x00\x00payload\n", b"plain text\n"
    repo, base, head = _tree_repo(tmp_path, {"keep.txt": text}, {"keep.txt": text, "x\tA": binary, "x\tB": text})
    git = GitReader(repo)
    stats = git.numstat(base, head)
    assert stats["x\tA"] == {"added": None, "deleted": None} and stats["x\tB"] == {"added": 1, "deleted": 0}
    assert vscope.binary_changed_paths(git.changed_files(base, head), stats) == ["x\tA"]
    repo2, base2, head2 = _tree_repo(tmp_path / "second", {"x": text}, {"x\tA": binary})
    git2 = GitReader(repo2)
    changed = git2.changed_files(base2, head2)
    assert sorted(changed) == [("A", "x\tA"), ("D", "x")]
    assert vscope.binary_changed_paths(changed, git2.numstat(base2, head2)) == ["x\tA"]


def test_a_changed_path_without_a_numstat_entry_counts_as_binary():
    assert vscope.binary_changed_paths([("A", "a.txt"), ("M", "b.dat"), ("D", "c.bin")],
                                       {"a.txt": {"added": 1, "deleted": 0}}) == ["b.dat"]
