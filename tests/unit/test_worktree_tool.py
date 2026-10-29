"""scripts/worktree.py: safe git worktrees (list / add / remove).

Incident class: a link (``.venv`` junction) inside a worktree made ``git worktree remove`` delete what the link
pointed at. Every bite test builds a throw-away repository in tmp_path (hermetic git environment, local origin),
a violating fixture that must be refused and a near-miss legitimate fixture that must be accepted.

Layout (owner request 2026-10-28): every worktree lives in ONE folder next to the main checkout,
``<main>-worktrees/<name>``; worktrees made earlier as siblings ``<main>-wt-<name>`` are still found by remove.
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


def wt_folder(repo):
    """The one folder that holds every worktree: <main>-worktrees next to the main checkout."""
    return repo.parent / f"{repo.name}-worktrees"


def wt_dir(repo, name):
    return wt_folder(repo) / name


def legacy_dir(repo, name):
    """Where worktrees made before 2026-10-28 live: the sibling <main>-wt-<name>."""
    return repo.parent / f"{repo.name}-wt-{name}"


def add_legacy(repo, name, branch, *, marker=True):
    """A worktree as the previous version of the tool made it: sibling directory, marker in its git dir."""
    dest = legacy_dir(repo, name)
    g(repo, "worktree", "add", "--no-track", "-b", branch, str(dest), "origin/main")
    if marker:
        (Path(g(dest, "rev-parse", "--absolute-git-dir")) / wt_tool.MARKER).write_text("old\n", encoding="utf-8")
    return dest


def branch_exists(repo, branch):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                          capture_output=True, text=True, check=False).returncode == 0


WINERROR_PRIVILEGE_NOT_HELD = 1314  # Windows without Developer Mode or SeCreateSymbolicLinkPrivilege


def host_cannot_symlink(exc, platform=sys.platform):
    """Only these mean the HOST cannot create symlinks; every other error is a test or layout defect."""
    if isinstance(exc, NotImplementedError):
        return True
    return (platform == "win32" and isinstance(exc, OSError)
            and getattr(exc, "winerror", None) == WINERROR_PRIVILEGE_NOT_HELD)


def make_symlink(target, link):
    """Skip only on a host that truly cannot create symlinks. A missing parent (a worktree that is not where the
    test expects it) or any other error fails loudly: a skip would hide a link-safety bite test as green."""
    parent = Path(link).parent
    assert parent.is_dir(), f"make_symlink: parent directory {parent} does not exist (layout changed?); not a skip"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        if host_cannot_symlink(exc):
            pytest.skip(f"this host cannot create symlinks ({type(exc).__name__}: needs privilege or support)")
        raise


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


def require_case_insensitive_fs(tmp_path):
    probe = tmp_path / "case-probe"
    probe.write_text("x\n", encoding="utf-8")
    if not (tmp_path / "CASE-PROBE").exists():
        pytest.skip("the temporary filesystem is case-sensitive: Old and old are different directories here")


def registered_paths(repo):
    """Registered worktree paths in this OS's spelling (Git for Windows prints C:/..., str(WindowsPath) is C:\\...)."""
    return [str(Path(line[len("worktree "):])) for line in g(repo, "worktree", "list", "--porcelain").splitlines()
            if line.startswith("worktree ")]


LINK_REFUSAL = "is a link, not a worktree: delete only the link entry by hand (rm, never rm -r"


# ------------------------------------------------------------------------------------------------ test helpers
def without_skip(fn, *args):
    """Call *fn*; a pytest skip raised by it fails the caller (a helper bite test must never become a skip)."""
    try:
        return fn(*args)
    except pytest.skip.Exception as exc:
        pytest.fail(f"skipped instead of failing: {exc}")


def test_make_symlink_fails_loudly_when_the_parent_directory_is_missing(tmp_path):
    """Bite for the helper: a layout regression (the worktree is not where the test expects it) must turn the
    link-safety bite tests red, never into skips that a suite run counts as green."""
    link = tmp_path / "no-such-worktree" / ".venv"
    with pytest.raises(AssertionError) as caught:
        without_skip(make_symlink, tmp_path, link)
    assert f"make_symlink: parent directory {link.parent} does not exist (layout changed?); not a skip" in str(
        caught.value)
    assert not os.path.lexists(link)


def test_make_symlink_reraises_errors_that_do_not_mean_the_host_lacks_symlinks(tmp_path, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "symlink", refuse)
    with pytest.raises(PermissionError):
        without_skip(make_symlink, tmp_path, tmp_path / "link")
    # near misses: only a missing OS feature, or the Windows privilege error, is a real "cannot" and skips
    assert host_cannot_symlink(NotImplementedError())

    class PrivilegeNotHeld(OSError):
        winerror = WINERROR_PRIVILEGE_NOT_HELD

    assert host_cannot_symlink(PrivilegeNotHeld(1, "privilege"), platform="win32")
    assert not host_cannot_symlink(PrivilegeNotHeld(1, "privilege"), platform="darwin")
    assert not host_cannot_symlink(FileNotFoundError(2, "missing"), platform="win32")
    assert not host_cannot_symlink(PermissionError(13, "denied"), platform="win32")

    def unsupported(*_args, **_kwargs):
        raise NotImplementedError("no symlinks here")

    monkeypatch.setattr(os, "symlink", unsupported)
    with pytest.raises(pytest.skip.Exception, match="this host cannot create symlinks"):
        make_symlink(tmp_path, tmp_path / "link")


def test_make_symlink_near_miss_into_an_existing_directory_creates_the_link(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    make_symlink(target, tmp_path / "link")  # skips only on a host that truly cannot create symlinks
    assert os.path.islink(tmp_path / "link")
    with pytest.raises(FileExistsError):  # a second link at the same path is an error, not a skip
        without_skip(make_symlink, target, tmp_path / "link")


# ------------------------------------------------------------------------------------------------ add
def test_add_creates_the_worktree_in_the_one_worktrees_folder_with_marker_and_no_links(repo, capsys):
    assert not os.path.lexists(wt_folder(repo))
    code, out = run(repo, capsys, "add", "alpha", "feat/alpha")
    dest = wt_dir(repo, "alpha")
    assert code == 0, out
    assert dest == repo.parent / "main-worktrees" / "alpha"
    assert wt_folder(repo).is_dir() and not os.path.islink(wt_folder(repo))
    assert f"created folder: {wt_folder(repo)}" in out
    assert not legacy_dir(repo, "alpha").exists()  # no sibling directory any more
    assert sorted(p.name for p in repo.parent.iterdir()) == ["main", "main-worktrees"]
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
    # near miss: a second worktree goes into the existing real folder, next to the first
    code, out = run(repo, capsys, "add", "beta", "feat/beta")
    assert code == 0, out
    assert "created folder" not in out
    assert sorted(p.name for p in wt_folder(repo).iterdir()) == ["alpha", "beta"]


def test_add_refuses_a_worktrees_folder_that_is_a_symlink_and_creates_nothing(repo, capsys, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    make_symlink(elsewhere, wt_folder(repo))
    code, out = run(repo, capsys, "add", "alpha", "feat/alpha")
    assert code == 1, out
    assert (f"refused: {wt_folder(repo)} is a symlink, junction or reparse point; the worktrees folder must be "
            "a real directory") in out
    assert out.splitlines()[-1] == "WORKTREE ADD: REFUSED alpha"
    assert os.path.islink(wt_folder(repo)) and list(elsewhere.iterdir()) == []
    assert not branch_exists(repo, "feat/alpha")
    assert len(g(repo, "worktree", "list", "--porcelain").split("\n\n")) == 1


def test_add_refuses_a_worktrees_folder_that_is_a_file_and_creates_nothing(repo, capsys):
    wt_folder(repo).write_text("not a folder\n", encoding="utf-8")
    code, out = run(repo, capsys, "add", "alpha", "feat/alpha")
    assert code == 1, out
    assert f"refused: {wt_folder(repo)} exists and is not a directory" in out
    assert wt_folder(repo).read_text(encoding="utf-8") == "not a folder\n"
    assert not branch_exists(repo, "feat/alpha")


def test_add_refuses_a_name_that_a_legacy_worktree_already_uses(repo, capsys):
    add_legacy(repo, "old", "feat/old")
    code, out = run(repo, capsys, "add", "old", "feat/new-old")
    assert code == 1, out
    assert f"a worktree named old already exists in the legacy location {legacy_dir(repo, 'old')}" in out
    assert not wt_dir(repo, "old").exists() and not os.path.lexists(wt_folder(repo))
    assert not branch_exists(repo, "feat/new-old")
    # near miss: another name is free
    assert run(repo, capsys, "add", "new", "feat/new")[0] == 0


def test_add_refuses_a_case_variant_of_a_legacy_name_on_a_case_insensitive_filesystem(repo, capsys, tmp_path):
    """On the default macOS APFS (and NTFS) main-wt-Old IS main-wt-old: a guard that compares path text with the
    case kept would accept 'add Old' and make 'remove old' silently pick one of the two."""
    require_case_insensitive_fs(tmp_path)
    add_legacy(repo, "old", "feat/old")
    code, out = run(repo, capsys, "add", "Old", "feat/Old-new")
    assert code == 1, out
    assert f"refused: a worktree named Old already exists in the legacy location {legacy_dir(repo, 'old')}" in out
    assert out.splitlines()[-1] == "WORKTREE ADD: REFUSED Old"
    assert not os.path.lexists(wt_folder(repo)) and not branch_exists(repo, "feat/Old-new")
    # near miss: a name that differs in more than case is free
    code, out = run(repo, capsys, "add", "other", "feat/other")
    assert code == 0, out


def test_add_refuses_a_link_at_the_folder_path_or_the_legacy_path_of_the_name(repo, capsys, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    make_symlink(elsewhere, legacy_dir(repo, "lnk"))
    code, out = run(repo, capsys, "add", "lnk", "feat/lnk")
    assert code == 1, out
    assert f"refused: {legacy_dir(repo, 'lnk')} {LINK_REFUSAL}" in out
    assert out.splitlines()[-1] == "WORKTREE ADD: REFUSED lnk"
    assert not os.path.lexists(wt_folder(repo)) and not branch_exists(repo, "feat/lnk")
    wt_folder(repo).mkdir()
    make_symlink(elsewhere, wt_dir(repo, "inner"))
    code, out = run(repo, capsys, "add", "inner", "feat/inner")
    assert code == 1, out
    assert f"refused: {wt_dir(repo, 'inner')} {LINK_REFUSAL}" in out
    assert os.path.islink(wt_dir(repo, "inner")) and list(elsewhere.iterdir()) == []
    assert not branch_exists(repo, "feat/inner")
    # near miss: a name with no link at either path is accepted next to them
    code, out = run(repo, capsys, "add", "plain", "feat/plain")
    assert code == 0, out


def test_add_removes_the_folder_it_created_when_git_refuses(repo, capsys):
    code, out = run(repo, capsys, "add", "alpha", "feat/alpha", "no-such-start-point")
    assert code == 2 and out.splitlines()[-1] == "WORKTREE ADD: ERROR"
    assert not os.path.lexists(wt_folder(repo))


def test_add_refuses_existing_destination_or_branch_and_bad_names(repo, capsys):
    assert run(repo, capsys, "add", "alpha", "feat/alpha")[0] == 0
    code, out = run(repo, capsys, "add", "alpha", "feat/other")
    assert code == 1 and "already exists" in out
    code, out = run(repo, capsys, "add", "beta", "feat/alpha")
    assert code == 1 and "branch feat/alpha already exists" in out and not wt_dir(repo, "beta").exists()
    (wt_dir(repo, "stray")).write_text("a file where the worktree would go\n", encoding="utf-8")
    code, out = run(repo, capsys, "add", "stray", "feat/stray")
    assert code == 1 and f"refused: {wt_dir(repo, 'stray')} already exists" in out
    assert not branch_exists(repo, "feat/stray")
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
    dest = wt_dir(repo, "lnk")
    make_symlink(sentinel, dest / ".venv")
    code, out = run(repo, capsys, "remove", "lnk")
    assert code == 1, out
    assert "link: .venv" in out and "REFUSED" in out.splitlines()[-1]
    assert dest.is_dir() and os.path.islink(dest / ".venv")
    assert sentinel_intact(sentinel)


def test_remove_unlink_links_removes_only_the_link_then_the_worktree(repo, capsys, sentinel):
    assert run(repo, capsys, "add", "lnk", "feat/lnk")[0] == 0
    dest = wt_dir(repo, "lnk")
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
    dest = wt_dir(repo, "clean")
    (dest / "node_modules" / "dep").mkdir(parents=True)
    (dest / "node_modules" / "dep" / "index.js").write_text("x", encoding="utf-8")
    (dest / "mod.pyc").write_bytes(b"\0")
    code, out = run(repo, capsys, "remove", "clean")
    assert code == 0, out
    assert not dest.exists()


def test_remove_refuses_a_tracked_link_even_with_unlink_links(repo, capsys, sentinel):
    assert run(repo, capsys, "add", "trk", "feat/trk")[0] == 0
    dest = wt_dir(repo, "trk")
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
    dest = wt_dir(repo, "jn")
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
    dest = wt_dir(repo, "wip")
    commit_file(dest, "new.txt", "work\n", "wip")
    code, out = run(repo, capsys, "remove", "wip")
    assert code == 1 and "neither merged into origin/main nor pushed" in out
    assert dest.is_dir()
    code, out = run(repo, capsys, "remove", "wip", "--force")
    assert code == 0, out
    assert not dest.exists()


def test_remove_accepts_pushed_but_unmerged_and_merged_but_unpushed(repo, capsys):
    assert run(repo, capsys, "add", "pushed", "feat/pushed")[0] == 0
    dest = wt_dir(repo, "pushed")
    commit_file(dest, "p.txt", "p\n", "p")
    g(dest, "push", "-q", "origin", "feat/pushed")
    code, out = run(repo, capsys, "remove", "pushed")
    assert code == 0, out  # pushed, not merged

    assert run(repo, capsys, "add", "merged", "feat/merged")[0] == 0  # same sha as origin/main: merged, not pushed
    code, out = run(repo, capsys, "remove", "merged")
    assert code == 0, out


def test_remove_refuses_when_pushed_sha_differs_from_head(repo, capsys):
    assert run(repo, capsys, "add", "ahead", "feat/ahead")[0] == 0
    dest = wt_dir(repo, "ahead")
    commit_file(dest, "a.txt", "a\n", "a")
    g(dest, "push", "-q", "origin", "feat/ahead")
    commit_file(dest, "b.txt", "b\n", "b")  # one local commit after the push
    code, out = run(repo, capsys, "remove", "ahead")
    assert code == 1 and "neither merged" in out


def test_remove_refuses_uncommitted_changes_but_not_ignored_files(repo, capsys):
    assert run(repo, capsys, "add", "dirty", "feat/dirty")[0] == 0
    dest = wt_dir(repo, "dirty")
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
    plain = wt_dir(repo, "plain")
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
    shutil.rmtree(wt_dir(repo, "gone"))
    code, out = run(repo, capsys, "remove", "gone")
    assert code == 0 and "already missing" in out
    assert "main-worktrees/gone" not in g(repo, "worktree", "list", "--porcelain")
    assert f"removed empty folder: {wt_folder(repo)}" in out and not os.path.lexists(wt_folder(repo))


# ------------------------------------------------------------------------------------------------ remove: one folder
def test_remove_deletes_the_worktrees_folder_only_once_it_is_empty(repo, capsys):
    assert run(repo, capsys, "add", "one", "feat/one")[0] == 0
    assert run(repo, capsys, "add", "two", "feat/two")[0] == 0
    code, out = run(repo, capsys, "remove", "one")
    assert code == 0, out
    assert not wt_dir(repo, "one").exists() and wt_dir(repo, "two").is_dir()
    assert f"kept folder: {wt_folder(repo)} (1 entry left)" in out  # near miss: not empty, kept
    code, out = run(repo, capsys, "remove", "two")
    assert code == 0, out
    assert f"removed empty folder: {wt_folder(repo)}" in out
    assert not os.path.lexists(wt_folder(repo))
    assert repo.is_dir() and (repo / "README.md").is_file()  # the main checkout next to it is untouched


def test_remove_keeps_a_worktrees_folder_that_holds_a_stray_file(repo, capsys):
    assert run(repo, capsys, "add", "one", "feat/one")[0] == 0
    (wt_folder(repo) / "notes.txt").write_text("keep me\n", encoding="utf-8")
    code, out = run(repo, capsys, "remove", "one")
    assert code == 0, out
    assert f"kept folder: {wt_folder(repo)} (1 entry left)" in out
    assert (wt_folder(repo) / "notes.txt").read_text(encoding="utf-8") == "keep me\n"


def test_remove_folder_if_empty_never_removes_a_link_or_its_target(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    folder = tmp_path / "main-worktrees"
    make_symlink(target, folder)
    msg = wt_tool.remove_folder_if_empty(folder)
    assert msg.startswith(f"kept folder: {folder} is a symlink, junction or reparse point")
    assert os.path.islink(folder) and target.is_dir()
    # near miss: a real empty directory is removed with rmdir
    real = tmp_path / "real-worktrees"
    real.mkdir()
    assert wt_tool.remove_folder_if_empty(real) == f"removed empty folder: {real}"
    assert not real.exists()


def test_remove_finds_a_worktree_in_the_legacy_sibling_location(repo, capsys):
    old = add_legacy(repo, "old", "feat/old")
    code, out = run(repo, capsys, "remove", "old")
    assert code == 0, out
    assert f"removed: {old} (legacy location)" in out and out.splitlines()[-1] == "WORKTREE REMOVE: OK old"
    assert not old.exists() and repo.is_dir()
    assert "main-wt-old" not in g(repo, "worktree", "list", "--porcelain")
    # the legacy parent is the directory of the main checkout: never a candidate for the folder clean-up
    assert "removed empty folder" not in out and "kept folder" not in out


def test_remove_legacy_keeps_the_marker_check(repo, capsys):
    old = add_legacy(repo, "old", "feat/old", marker=False)
    code, out = run(repo, capsys, "remove", "old")
    assert code == 1 and "no 'aeos-worktree-created' marker" in out and old.is_dir()


def test_remove_a_plain_sibling_directory_that_is_not_a_worktree_is_not_found(repo, capsys):
    stray = legacy_dir(repo, "stray")
    stray.mkdir()
    (stray / "f.txt").write_text("x\n", encoding="utf-8")
    code, out = run(repo, capsys, "remove", "stray")
    assert code == 1 and f"refused: no worktree {wt_dir(repo, 'stray')}" in out
    assert (stray / "f.txt").is_file()


def test_remove_refuses_a_name_that_exists_in_the_folder_and_the_legacy_location(repo, capsys):
    assert run(repo, capsys, "add", "dup", "feat/dup")[0] == 0
    old = add_legacy(repo, "dup", "feat/dup-old")
    code, out = run(repo, capsys, "remove", "dup")
    assert code == 1, out
    assert (f"refused: ambiguous name dup: both {wt_dir(repo, 'dup')} and the legacy {old} are worktrees; "
            f"move one of them under another name, for example git worktree move {old} "
            f"{wt_folder(repo) / '<other name>'} (never git worktree remove: it skips these checks and can follow "
            "a link), then run remove again") in out
    assert "or remove" not in out  # never advise a manual removal: it skips the link scan (LESSONS L4)
    assert out.splitlines()[-1] == "WORKTREE REMOVE: REFUSED dup"
    assert wt_dir(repo, "dup").is_dir() and old.is_dir()
    # near miss: once the legacy one is moved under another name, both names resolve
    g(repo, "worktree", "move", str(old), str(wt_dir(repo, "dup-old")))
    assert run(repo, capsys, "remove", "dup")[0] == 0
    assert run(repo, capsys, "remove", "dup-old")[0] == 0
    assert not os.path.lexists(wt_folder(repo))


def test_remove_refuses_a_case_variant_ambiguity_on_a_case_insensitive_filesystem(repo, capsys, tmp_path):
    require_case_insensitive_fs(tmp_path)
    old = add_legacy(repo, "old", "feat/old")
    upper = wt_dir(repo, "Old")  # made by hand: the tool itself refuses this name now
    g(repo, "worktree", "add", "--no-track", "-b", "feat/upper-old", str(upper), "origin/main")
    (Path(g(upper, "rev-parse", "--absolute-git-dir")) / wt_tool.MARKER).write_text("x\n", encoding="utf-8")
    code, out = run(repo, capsys, "remove", "old")
    assert code == 1, out
    assert f"refused: ambiguous name old: both {upper} and the legacy {old} are worktrees" in out
    assert out.splitlines()[-1] == "WORKTREE REMOVE: REFUSED old"
    assert upper.is_dir() and old.is_dir() and len(registered_paths(repo)) == 3
    # near miss: once the legacy one is moved under another name, 'old' names the folder worktree Old
    g(repo, "worktree", "move", str(old), str(wt_dir(repo, "old-legacy")))
    code, out = run(repo, capsys, "remove", "old")
    assert code == 0, out
    assert f"removed: {upper}" in out and "(legacy location)" not in out
    assert wt_dir(repo, "old-legacy").is_dir()


def test_remove_refuses_a_link_at_the_legacy_path_to_a_folder_worktree_and_leaves_both(repo, capsys):
    """A compatibility link main-wt-keep -> main-worktrees/keep left after a move is not a second worktree: it is
    refused with its own message (not as ambiguous) and neither the link nor the worktree is touched."""
    assert run(repo, capsys, "add", "keep", "feat/keep")[0] == 0
    link = legacy_dir(repo, "keep")
    make_symlink(wt_dir(repo, "keep"), link)
    code, out = run(repo, capsys, "remove", "keep")
    assert code == 1, out
    assert f"refused: {link} {LINK_REFUSAL}" in out and "ambiguous" not in out
    assert out.splitlines()[-1] == "WORKTREE REMOVE: REFUSED keep"
    assert os.path.islink(link) and (wt_dir(repo, "keep") / "README.md").is_file()
    assert str(wt_dir(repo, "keep")) in registered_paths(repo)
    # near miss: with the link entry deleted by hand, the one worktree is removed
    os.unlink(link)
    code, out = run(repo, capsys, "remove", "keep")
    assert code == 0, out
    assert f"removed: {wt_dir(repo, 'keep')}" in out and "(legacy location)" not in out


def test_remove_never_resolves_a_name_through_a_link_to_another_worktree(repo, capsys):
    assert run(repo, capsys, "add", "other", "feat/other")[0] == 0
    other = wt_dir(repo, "other")
    make_symlink(other, legacy_dir(repo, "ghost"))
    code, out = run(repo, capsys, "remove", "ghost")
    assert code == 1, out
    assert f"refused: {legacy_dir(repo, 'ghost')} {LINK_REFUSAL}" in out
    make_symlink(other, wt_dir(repo, "ghost2"))
    code, out = run(repo, capsys, "remove", "ghost2")
    assert code == 1, out
    assert f"refused: {wt_dir(repo, 'ghost2')} {LINK_REFUSAL}" in out
    assert (other / "README.md").is_file() and str(other) in registered_paths(repo)
    assert branch_exists(repo, "feat/other")
    # near miss: the worktree's own name still removes it (the stray link entry keeps the folder)
    code, out = run(repo, capsys, "remove", "other")
    assert code == 0, out
    assert f"kept folder: {wt_folder(repo)} (1 entry left)" in out and os.path.islink(wt_dir(repo, "ghost2"))


def test_remove_refuses_while_the_worktrees_folder_is_a_link(repo, capsys):
    """A folder link would alias every name below it: main-worktrees -> <parent> makes the folder path of the
    name main-wt-x the legacy worktree x."""
    legacy = add_legacy(repo, "x", "feat/x")
    make_symlink(repo.parent, wt_folder(repo))
    code, out = run(repo, capsys, "remove", "main-wt-x")
    assert code == 1, out
    assert (f"refused: {wt_folder(repo)} is a symlink, junction or reparse point; the worktrees folder must be "
            "a real directory") in out
    assert out.splitlines()[-1] == "WORKTREE REMOVE: REFUSED main-wt-x"
    assert legacy.is_dir() and str(legacy) in registered_paths(repo)
    # near miss: with the folder link deleted by hand, the legacy worktree is removed by its own name
    os.unlink(wt_folder(repo))
    code, out = run(repo, capsys, "remove", "x")
    assert code == 0, out
    assert f"removed: {legacy} (legacy location)" in out


def test_same_place_matches_a_registered_path_by_itself_never_through_a_link(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    assert wt_tool.same_place(real, real) and wt_tool.same_place(str(real), real)
    assert not wt_tool.same_place(real, other)
    assert not wt_tool.same_place(real, tmp_path / "missing")  # one exists, the other does not
    # both missing: the path text decides, case folded only where the default filesystem ignores case
    assert wt_tool.same_place(tmp_path / "gone", tmp_path / "gone")
    assert wt_tool.same_place(tmp_path / "gone", str(tmp_path / "y" / ".." / "gone"))  # text is normalised
    assert wt_tool.same_place(tmp_path / "gone", tmp_path / "Gone") == (sys.platform in ("darwin", "win32"))
    assert not wt_tool.same_place(tmp_path / "gone", tmp_path / "gone2")
    make_symlink(real, tmp_path / "alias")
    assert not wt_tool.same_place(real, tmp_path / "alias") and not wt_tool.same_place(tmp_path / "alias", real)


# ------------------------------------------------------------------------------------------------ list
def test_list_reports_merged_pushed_marker_and_link_counts(repo, capsys, sentinel):
    assert run(repo, capsys, "add", "one", "feat/one")[0] == 0
    assert run(repo, capsys, "add", "two", "feat/two")[0] == 0
    two = wt_dir(repo, "two")
    commit_file(two, "t.txt", "t\n", "t")
    make_symlink(sentinel, two / ".venv")
    g(repo, "worktree", "add", "--no-track", "-b", "feat/plain", str(wt_dir(repo, "plain")), "origin/main")
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
    assert f"worktrees folder: {wt_folder(repo)}" in out
    assert "legacy-location" not in out
    assert lines[-1] == "WORKTREE LIST: OK 4 worktrees, 1 with links"


def test_list_flags_worktrees_in_the_legacy_location(repo, capsys):
    assert run(repo, capsys, "add", "new", "feat/new")[0] == 0
    add_legacy(repo, "old", "feat/old")
    code, out = run(repo, capsys, "list")
    assert code == 0, out
    lines = out.splitlines()

    def row(branch):
        return next(line for line in lines if f"branch={branch} " in line)

    assert row("feat/old").endswith("[legacy-location]") and "created-by-tool=yes" in row("feat/old")
    assert "legacy-location" not in row("feat/new") and "legacy-location" not in row("main")
    assert "1 worktree(s) in the legacy location <main>-wt-<name>: move each with 'git worktree move" in out
    assert lines[-1] == "WORKTREE LIST: OK 3 worktrees, 0 with links"


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
