"""scripts/worktree.py: safe git worktrees (list / add / remove).

Incident class: a link (``.venv`` junction) inside a worktree made ``git worktree remove`` delete what the link
pointed at. Every bite test builds a throw-away repository in tmp_path (hermetic git environment, local origin),
a violating fixture that must be refused and a near-miss legitimate fixture that must be accepted.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aeos.core.paths import repo_root

ROOT = repo_root()
HAS_GIT = subprocess.run(["git", "--version"], capture_output=True, check=False).returncode == 0
pytestmark = pytest.mark.skipif(not HAS_GIT, reason="git is not installed")


def _load():
    spec = importlib.util.spec_from_file_location("aeos_script_worktree", ROOT / "scripts" / "worktree.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


wt_tool = _load()


@pytest.fixture(autouse=True)
def hermetic_git(tmp_path, monkeypatch):
    # Git also reads a user-level ignore file ($XDG_CONFIG_HOME/git/ignore, else ~/.config/git/ignore), which often lists
    # .venv/: point XDG_CONFIG_HOME into tmp_path and name an excludes file that does not exist, so no ignore rule of
    # the machine changes what `git status` reports.
    empty = tmp_path / "gitconfig-empty"
    empty.write_text(f"[core]\n\texcludesFile = {(tmp_path / 'no-excludes').as_posix()}\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)


def g(cwd, *args):
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout.strip()


def commit_file(repo, name, text="x\n", msg="c"):
    (Path(repo) / name).write_text(text, encoding="utf-8")
    g(repo, "add", name)
    g(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def repo(tmp_path):
    """<tmp>/work/main (checkout on main, origin = <tmp>/origin.git with main pushed)."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    g(origin, "init", "-q", "--bare")
    g(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    work = tmp_path / "work"
    work.mkdir()
    main = work / "main"
    main.mkdir()
    g(main, "init", "-q")
    g(main, "symbolic-ref", "HEAD", "refs/heads/main")
    g(main, "config", "user.name", "Test User")
    g(main, "config", "user.email", "test@example.com")
    g(main, "config", "commit.gpgsign", "false")
    commit_file(main, "README.md", "hello\n", "init")
    g(main, "remote", "add", "origin", str(origin))
    g(main, "push", "-q", "-u", "origin", "main")
    return main


def run(repo, capsys, *args):
    code = wt_tool.main(["--repo", str(repo), *args])
    out = capsys.readouterr().out
    out.encode("ascii")  # CLI output is ASCII only
    return code, out


def sibling(repo, name):
    return repo.parent / f"{repo.name}-wt-{name}"


def make_symlink(target, link):
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create symlinks (needs privilege)")


@pytest.fixture
def sentinel(tmp_path):
    """A directory OUTSIDE every worktree that a removal must never touch."""
    d = tmp_path / "sentinel-venv"
    (d / "lib").mkdir(parents=True)
    (d / "lib" / "keep.txt").write_text("precious\n", encoding="utf-8")
    (d / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    return d


def sentinel_intact(d):
    return (d / "lib" / "keep.txt").read_text(encoding="utf-8") == "precious\n" and (d / "pyvenv.cfg").is_file()


# ------------------------------------------------------------------------------------------------ add
def test_add_creates_sibling_worktree_with_marker_and_no_links(repo, capsys):
    code, out = run(repo, capsys, "add", "alpha", "feat/alpha")
    dest = sibling(repo, "alpha")
    assert code == 0, out
    assert dest.is_dir() and (dest / "README.md").is_file()
    assert g(dest, "branch", "--show-current") == "feat/alpha"
    marker = Path(g(dest, "rev-parse", "--absolute-git-dir")) / wt_tool.MARKER
    assert marker.is_file()
    assert wt_tool.scan_links(dest) == ([], [])
    assert not (dest / ".venv").exists()
    assert "PYTHONPATH=backend" in out and str(repo / ".venv") in out
    assert out.strip().splitlines()[-1] == "WORKTREE ADD: OK alpha"
    # --no-track: the new branch has no upstream
    assert subprocess.run(["git", "-C", str(dest), "config", "--get", "branch.feat/alpha.merge"],
                          capture_output=True, text=True, check=False).stdout.strip() == ""


def test_add_refuses_existing_destination_or_branch_and_bad_names(repo, capsys):
    assert run(repo, capsys, "add", "alpha", "feat/alpha")[0] == 0
    code, out = run(repo, capsys, "add", "alpha", "feat/other")
    assert code == 1 and "already exists" in out
    code, out = run(repo, capsys, "add", "beta", "feat/alpha")
    assert code == 1 and "branch feat/alpha already exists" in out and not sibling(repo, "beta").exists()
    for bad in ("../x", "a/b", "", ".hidden", "x y"):
        code, out = run(repo, capsys, "add", bad, "feat/zzz")
        assert code == 2 and "ERROR" in out.splitlines()[-1]


# ------------------------------------------------------------------------------------------------ links scan
def test_scan_finds_links_without_following_and_skips_node_modules_contents(tmp_path, sentinel):
    root = tmp_path / "tree"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "real.txt").write_text("r", encoding="utf-8")
    make_symlink(sentinel, root / "pkg" / "venv-link")
    (root / "node_modules" / "dep").mkdir(parents=True)
    make_symlink(sentinel, root / "node_modules" / "dep" / "inner-link")  # inside node_modules: not entered
    (root / ".git").mkdir()
    make_symlink(sentinel, root / ".git" / "inner-link")  # inside .git: not entered
    links, errors = wt_tool.scan_links(root)
    assert links == ["pkg/venv-link"] and errors == []


def test_scan_reports_a_node_modules_that_is_itself_a_link(tmp_path, sentinel):
    root = tmp_path / "tree"
    root.mkdir()
    make_symlink(sentinel, root / "node_modules")
    assert wt_tool.scan_links(root)[0] == ["node_modules"]


def test_scan_near_miss_plain_tree_has_no_links(tmp_path):
    root = tmp_path / "tree"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "f.txt").write_text("x", encoding="utf-8")
    (root / "node_modules").mkdir()
    assert wt_tool.scan_links(root) == ([], [])


# ------------------------------------------------------------------------------------------------ remove: links
def test_remove_refuses_a_worktree_holding_a_symlink_and_leaves_everything_intact(repo, capsys, sentinel):
    assert run(repo, capsys, "add", "lnk", "feat/lnk")[0] == 0
    dest = sibling(repo, "lnk")
    make_symlink(sentinel, dest / ".venv")
    code, out = run(repo, capsys, "remove", "lnk")
    assert code == 1, out
    assert "link: .venv" in out and "REFUSED" in out.splitlines()[-1]
    assert dest.is_dir() and os.path.islink(dest / ".venv")
    assert sentinel_intact(sentinel)


def test_remove_unlink_links_removes_only_the_link_then_the_worktree(repo, capsys, sentinel):
    assert run(repo, capsys, "add", "lnk", "feat/lnk")[0] == 0
    dest = sibling(repo, "lnk")
    make_symlink(sentinel, dest / ".venv")
    code, out = run(repo, capsys, "remove", "lnk", "--unlink-links")
    assert code == 0, out
    assert "unlinked: .venv" in out and out.splitlines()[-1] == "WORKTREE REMOVE: OK lnk"
    assert not dest.exists()
    assert sentinel_intact(sentinel)
    assert str(dest) not in g(repo, "worktree", "list", "--porcelain")


def test_remove_near_miss_real_node_modules_and_ignored_files_are_not_links(repo, capsys):
    (repo / ".gitignore").write_text("node_modules/\n*.pyc\n", encoding="utf-8")
    g(repo, "add", ".gitignore")
    g(repo, "commit", "-q", "-m", "ignore")
    g(repo, "push", "-q", "origin", "main")
    assert run(repo, capsys, "add", "clean", "feat/clean")[0] == 0
    dest = sibling(repo, "clean")
    (dest / "node_modules" / "dep").mkdir(parents=True)
    (dest / "node_modules" / "dep" / "index.js").write_text("x", encoding="utf-8")
    (dest / "mod.pyc").write_bytes(b"\0")
    code, out = run(repo, capsys, "remove", "clean")
    assert code == 0, out
    assert not dest.exists()


def test_remove_refuses_a_tracked_link_even_with_unlink_links(repo, capsys, sentinel):
    assert run(repo, capsys, "add", "trk", "feat/trk")[0] == 0
    dest = sibling(repo, "trk")
    make_symlink(sentinel, dest / "tracked-link")
    g(dest, "add", "tracked-link")
    g(dest, "commit", "-q", "-m", "tracked link")
    g(dest, "push", "-q", "origin", "feat/trk")
    code, out = run(repo, capsys, "remove", "trk", "--unlink-links")
    assert code == 1 and "tracked link" in out
    assert os.path.islink(dest / "tracked-link") and sentinel_intact(sentinel)


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions exist only on Windows")
def test_remove_junction_is_refused_then_removed_with_rmdir_and_target_survives(repo, capsys, sentinel):
    import _winapi  # type: ignore[import-not-found,unused-ignore]  # exists on Windows only

    assert run(repo, capsys, "add", "jn", "feat/jn")[0] == 0
    dest = sibling(repo, "jn")
    _winapi.CreateJunction(str(sentinel), str(dest / ".venv"))  # type: ignore[attr-defined,unused-ignore]
    assert wt_tool.scan_links(dest)[0] == [".venv"]
    code, out = run(repo, capsys, "remove", "jn")
    assert code == 1 and "link: .venv" in out and sentinel_intact(sentinel)
    code, out = run(repo, capsys, "remove", "jn", "--unlink-links")
    assert code == 0, out
    assert not dest.exists() and sentinel_intact(sentinel)


def test_a_user_level_ignore_file_cannot_change_what_git_status_reports(tmp_path):
    """Bite for the hermetic fixture: a machine whose ~/.config/git/ignore lists .venv/ must not make
    test_dirty_paths_... red (git reads the XDG ignore file by default)."""
    r = tmp_path / "plain"
    r.mkdir()
    g(r, "init", "-q")
    (r / ".venv").mkdir()
    (r / ".venv" / "pyvenv.cfg").write_text("x\n", encoding="utf-8")
    ignore = tmp_path / "xdg" / "git" / "ignore"
    ignore.parent.mkdir(parents=True)
    ignore.write_text(".venv/\n", encoding="utf-8")
    assert wt_tool.dirty_paths(r, set()) == [".venv/pyvenv.cfg"]  # the ignore file is not read
    (r / ".gitignore").write_text(".venv/\n", encoding="utf-8")  # near miss: the repository's own rule still applies
    assert wt_tool.dirty_paths(r, set()) == [".gitignore"]


def test_dirty_paths_ignores_the_files_git_lists_below_a_link_it_sees_as_a_directory(tmp_path):
    """Git for Windows reports a junction as a directory and lists the target's files below it: a real directory
    in a throw-away repository reproduces exactly that view on every host."""
    r = tmp_path / "plain"
    r.mkdir()
    g(r, "init", "-q")
    g(r, "config", "user.email", "t@example.invalid")
    g(r, "config", "user.name", "t")
    (r / ".venv" / "lib").mkdir(parents=True)
    (r / ".venv" / "lib" / "keep.txt").write_text("x\n", encoding="utf-8")
    (r / ".venv" / "pyvenv.cfg").write_text("x\n", encoding="utf-8")
    assert wt_tool.dirty_paths(r, set()) == [".venv/lib/keep.txt", ".venv/pyvenv.cfg"]  # the violating view
    assert wt_tool.dirty_paths(r, {".venv"}) == []
    # near misses: a sibling that merely shares the prefix, and a file of the same name, are still changes
    (r / ".venv-other").mkdir()
    (r / ".venv-other" / "a.txt").write_text("x\n", encoding="utf-8")
    (r / ".venvx").write_text("x\n", encoding="utf-8")
    assert wt_tool.dirty_paths(r, {".venv"}) == [".venv-other/a.txt", ".venvx"]
    # a tracked file that is modified is a change even below an ignored name
    commit_file(r, "tracked.txt", "a\n", "t")
    (r / "tracked.txt").write_text("b\n", encoding="utf-8")
    assert "tracked.txt" in wt_tool.dirty_paths(r, {".venv"})


# ------------------------------------------------------------------------------------------------ remove: branch safety
def test_remove_refuses_unmerged_unpushed_branch_then_force_accepts(repo, capsys):
    assert run(repo, capsys, "add", "wip", "feat/wip")[0] == 0
    dest = sibling(repo, "wip")
    commit_file(dest, "new.txt", "work\n", "wip")
    code, out = run(repo, capsys, "remove", "wip")
    assert code == 1 and "neither merged into origin/main nor pushed" in out
    assert dest.is_dir()
    code, out = run(repo, capsys, "remove", "wip", "--force")
    assert code == 0, out
    assert not dest.exists()


def test_remove_accepts_pushed_but_unmerged_and_merged_but_unpushed(repo, capsys):
    assert run(repo, capsys, "add", "pushed", "feat/pushed")[0] == 0
    dest = sibling(repo, "pushed")
    commit_file(dest, "p.txt", "p\n", "p")
    g(dest, "push", "-q", "origin", "feat/pushed")
    code, out = run(repo, capsys, "remove", "pushed")
    assert code == 0, out  # pushed, not merged

    assert run(repo, capsys, "add", "merged", "feat/merged")[0] == 0  # same sha as origin/main: merged, not pushed
    code, out = run(repo, capsys, "remove", "merged")
    assert code == 0, out


def test_remove_refuses_when_pushed_sha_differs_from_head(repo, capsys):
    assert run(repo, capsys, "add", "ahead", "feat/ahead")[0] == 0
    dest = sibling(repo, "ahead")
    commit_file(dest, "a.txt", "a\n", "a")
    g(dest, "push", "-q", "origin", "feat/ahead")
    commit_file(dest, "b.txt", "b\n", "b")  # one local commit after the push
    code, out = run(repo, capsys, "remove", "ahead")
    assert code == 1 and "neither merged" in out


def test_remove_refuses_uncommitted_changes_but_not_ignored_files(repo, capsys):
    assert run(repo, capsys, "add", "dirty", "feat/dirty")[0] == 0
    dest = sibling(repo, "dirty")
    (dest / "scratch.txt").write_text("untracked\n", encoding="utf-8")
    code, out = run(repo, capsys, "remove", "dirty")
    assert code == 1 and "uncommitted change" in out and "scratch.txt" in out
    (dest / "scratch.txt").unlink()
    (dest / "README.md").write_text("edited\n", encoding="utf-8")
    code, out = run(repo, capsys, "remove", "dirty")
    assert code == 1 and "README.md" in out
    g(dest, "checkout", "--", "README.md")
    assert run(repo, capsys, "remove", "dirty")[0] == 0


# ------------------------------------------------------------------------------------------------ remove: marker
def test_remove_refuses_foreign_worktree_unless_foreign(repo, capsys):
    plain = sibling(repo, "plain")
    g(repo, "worktree", "add", "--no-track", "-b", "feat/plain", str(plain), "origin/main")
    code, out = run(repo, capsys, "remove", "plain")
    assert code == 1 and "marker" in out and plain.is_dir()
    code, out = run(repo, capsys, "remove", "plain", "--foreign")
    assert code == 0, out
    assert not plain.exists()


def test_remove_unknown_name_is_refused(repo, capsys):
    code, out = run(repo, capsys, "remove", "nope")
    assert code == 1 and "no worktree" in out


def test_remove_prunes_a_worktree_whose_directory_is_gone(repo, capsys):
    import shutil

    assert run(repo, capsys, "add", "gone", "feat/gone")[0] == 0
    shutil.rmtree(sibling(repo, "gone"))
    code, out = run(repo, capsys, "remove", "gone")
    assert code == 0 and "already missing" in out
    assert "-wt-gone" not in g(repo, "worktree", "list", "--porcelain")


# ------------------------------------------------------------------------------------------------ list
def test_list_reports_merged_pushed_marker_and_link_counts(repo, capsys, sentinel):
    assert run(repo, capsys, "add", "one", "feat/one")[0] == 0
    assert run(repo, capsys, "add", "two", "feat/two")[0] == 0
    two = sibling(repo, "two")
    commit_file(two, "t.txt", "t\n", "t")
    make_symlink(sentinel, two / ".venv")
    g(repo, "worktree", "add", "--no-track", "-b", "feat/plain", str(sibling(repo, "plain")), "origin/main")
    code, out = run(repo, capsys, "list")
    assert code == 0, out
    lines = out.splitlines()

    def row(branch):
        return next(line for line in lines if f"branch={branch} " in line)

    main_row = row("main")
    assert "merged=yes" in main_row and "pushed=yes" in main_row and "created-by-tool=n/a" in main_row
    assert "merged=yes" in row("feat/one") and "pushed=no" in row("feat/one")
    assert "created-by-tool=yes" in row("feat/one") and "LINKS=0" in row("feat/one")
    assert "merged=no" in row("feat/two") and "LINKS=1" in row("feat/two")
    assert "created-by-tool=no" in row("feat/plain")
    assert "link: .venv" in out
    assert lines[-1] == "WORKTREE LIST: OK 4 worktrees, 1 with links"


def test_list_without_origin_main_reports_not_merged_not_pushed(tmp_path, capsys):
    solo = tmp_path / "solo"
    solo.mkdir()
    g(solo, "init", "-q")
    g(solo, "symbolic-ref", "HEAD", "refs/heads/main")
    g(solo, "config", "user.name", "Test User")
    g(solo, "config", "user.email", "test@example.com")
    commit_file(solo, "a.txt")
    code, out = run(solo, capsys, "list")
    assert code == 0 and "merged=no pushed=no" in out


def test_output_is_ascii_even_for_non_ascii_paths(tmp_path, capsys):
    main = tmp_path / "wérk"
    main.mkdir()
    g(main, "init", "-q")
    g(main, "symbolic-ref", "HEAD", "refs/heads/main")
    g(main, "config", "user.name", "Test User")
    g(main, "config", "user.email", "test@example.com")
    commit_file(main, "a.txt")
    code, out = run(main, capsys, "list")  # run() asserts ASCII
    assert code == 0 and "\\xe9" in out


def test_outside_a_repository_is_an_error_not_a_crash(tmp_path, capsys):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    code, out = run(plain, capsys, "list")
    assert code == 2 and out.splitlines()[-1] == "WORKTREE LIST: ERROR"
