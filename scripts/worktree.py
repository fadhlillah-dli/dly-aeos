"""Safe git worktrees for this repository: list, add, remove.

Incident this prevents: a ``.venv`` junction placed inside a worktree made ``git worktree remove`` follow the
junction and delete half of the real virtual environment it pointed at. Worktrees also pile up unnoticed. This
tool never creates a link, refuses to remove a worktree that holds one, and shows what each worktree is.

  python scripts/worktree.py list
  python scripts/worktree.py add <name> <branch> [<base>]
  python scripts/worktree.py remove <name> [--unlink-links] [--foreign] [--force]

Layout: one folder next to the main checkout holds every worktree (owner request 2026-10-28: many sibling
folders cluttered the directory that holds the checkout):

  <parent>/<main checkout name>            the main checkout, stays on main
  <parent>/<main checkout name>-worktrees/  one folder, one worktree per parallel branch inside
      <name>/

``add`` creates ``<main checkout>-worktrees/<name>`` with ``git worktree add -b`` and writes the marker file
``aeos-worktree-created`` into that worktree's git directory (it disappears with the worktree). It creates the
folder ``<main checkout>-worktrees`` when it is missing, as a real directory, and refuses (exit 1, nothing
created) when that folder is a symlink, junction or other reparse point, or exists as a file; it also refuses an
existing destination, an existing branch, a name that a legacy worktree (below) already uses, and a link at the
new or the legacy path of the name.
Use the main checkout's interpreter that ``add`` prints; do not create a ``.venv`` link in a worktree.

Names and paths: a name is matched to the registered worktree AT its path (``same_place``), never through a link.
A link at ``<main checkout>-worktrees/<name>`` or ``<main checkout>-wt-<name>`` is refused with its own message
(delete only the link entry by hand); it is never resolved to the worktree it points at. Both paths exist: the same
directory (``os.path.samefile``), so ``Old`` and ``old`` collide on a case-insensitive filesystem (the default on
macOS and Windows) and stay distinct on a case-sensitive one. Both missing: the same path text, case folded on
macOS and Windows.

Legacy location: worktrees made before 2026-10-28 are siblings named ``<main checkout>-wt-<name>``. ``remove``
still finds such a worktree when that path is a registered worktree, and refuses as ambiguous when a worktree of
the same name exists in both places. ``list`` flags it ``legacy-location``. To move one into the folder, create
the folder first and give the FULL target path (on an existing directory git would nest the worktree under its
old name):

  mkdir <main checkout>-worktrees
  git worktree move <main checkout>-wt-<name> <main checkout>-worktrees/<name>

``git worktree move`` renames the directory in one step and updates git's records; it does not follow links (a
link inside moves as a link, its target is untouched) and the marker stays in the worktree's git directory.

``remove`` refuses (exit 1, nothing changed) when
  * ``<main checkout>-worktrees`` is a link, or the folder path or the legacy path of the name is a link,
  * the name is ambiguous (a worktree of that name in the folder and in the legacy location): move one of them
    under another name with ``git worktree move`` and run ``remove`` again (never ``git worktree remove`` by
    hand: it skips every check here and can follow a link),
  * the worktree has no marker (``--foreign`` to accept a worktree this tool did not create),
  * it holds a symlink, junction or other reparse point (``--unlink-links`` removes only those link entries,
    never what they point at, then scans again),
  * it has uncommitted changes (untracked link entries do not count), or
  * its branch is neither merged into origin/main nor pushed to origin (``--force`` to accept the loss).
Merged / pushed are read from the remote-tracking refs of the last fetch (no network access).
After a removal from the folder, ``remove`` deletes ``<main checkout>-worktrees`` itself only when it is now
empty and a real directory (``os.rmdir``, never a recursive delete; a link is never removed).

Link scan: the tree is walked without following links. ``.git`` and ``node_modules`` are not entered, but a
``.git`` or ``node_modules`` that is itself a link is reported. Windows junctions and every other reparse point
count as links (``os.walk`` on Python 3.11 would walk through a junction, so the walk is done by hand).

Exit 0 ok, 1 refused, 2 usage or git error. Output is ASCII only; the last line is the verdict. Stdlib only.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

MARKER = "aeos-worktree-created"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SKIP_DIRS = frozenset({".git", "node_modules"})
REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT
MAX_LINES_SHOWN = 20
FOLDS_CASE = sys.platform in ("darwin", "win32")  # default APFS and NTFS ignore case; normcase folds only on win32


class ToolError(Exception):
    """Environment or git failure (exit 2)."""


# ------------------------------------------------------------------------------------------------ output
def say(text: str = "") -> None:
    """Print ASCII only: anything else (paths, git messages) is escaped, legacy Windows consoles stay safe."""
    print(text.encode("ascii", "backslashreplace").decode("ascii"))


# ------------------------------------------------------------------------------------------------ git
def git(cwd: Path | str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
    except FileNotFoundError as exc:
        raise ToolError("git is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"git {' '.join(args[:2])} timed out") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}")[:300]
        raise ToolError(f"git {' '.join(args[:2])} failed: {detail}")
    return proc


def same_path(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


@dataclass(frozen=True)
class Entry:
    path: str
    head: str
    branch: str | None  # None = detached or bare
    prunable: bool
    locked: bool


def list_entries(repo: Path | str) -> list[Entry]:
    """Every worktree of the repository, main checkout first (``git worktree list --porcelain``)."""
    out = git(repo, "worktree", "list", "--porcelain").stdout
    entries: list[Entry] = []
    block: dict[str, str] = {}

    def flush() -> None:
        if "worktree" in block:
            ref = block.get("branch", "")
            entries.append(Entry(
                path=block["worktree"], head=block.get("HEAD", ""),
                branch=ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else None,
                prunable="prunable" in block, locked="locked" in block))

    for line in out.splitlines():
        if not line.strip():
            flush()
            block = {}
            continue
        key, _, value = line.partition(" ")
        block[key] = value
    flush()
    return entries


def ref_sha(repo: Path | str, ref: str) -> str | None:
    proc = git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False)
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def is_merged(repo: Path | str, head: str) -> bool:
    """HEAD is an ancestor of origin/main (last fetched state)."""
    if not head or ref_sha(repo, "refs/remotes/origin/main") is None:
        return False
    return git(repo, "merge-base", "--is-ancestor", head, "refs/remotes/origin/main", check=False).returncode == 0


def is_pushed(repo: Path | str, branch: str | None, head: str) -> bool:
    """The branch exists on origin (last fetched state) with exactly this sha."""
    if not branch or not head:
        return False
    return ref_sha(repo, f"refs/remotes/origin/{branch}") == head


def git_dir(wt: Path | str) -> Path:
    return Path(git(wt, "rev-parse", "--absolute-git-dir").stdout.strip())


def has_marker(wt: Path | str) -> bool:
    try:
        return (git_dir(wt) / MARKER).is_file()
    except ToolError:
        return False


# ------------------------------------------------------------------------------------------------ links
def is_link(entry: os.DirEntry[str]) -> bool:
    """Symlink, Windows junction or any other reparse point; never follows it."""
    if entry.is_symlink():
        return True
    if sys.platform == "win32":
        try:
            return bool(entry.stat(follow_symlinks=False).st_file_attributes & REPARSE_POINT)
        except OSError:
            return False
    return False


def path_is_link(path: str | Path) -> bool:
    """Same test for a path (used right before an unlink, never trusting the earlier scan)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return sys.platform == "win32" and bool(getattr(st, "st_file_attributes", 0) & REPARSE_POINT)


def path_key(path: str | Path) -> str:
    """Path text made comparable without touching the disk: absolute, normcase, case folded on macOS and Windows
    (``posixpath.normcase`` keeps the case, and their default filesystems ignore it)."""
    key = os.path.normcase(os.path.abspath(path))
    return key.casefold() if FOLDS_CASE else key


def same_place(registered: str | Path, candidate: str | Path) -> bool:
    """Whether the registered worktree path *registered* is the path *candidate*, never through a link.

    A link at either path never matches (callers refuse a link at the candidate with its own message). Both
    exist: the same directory (``os.path.samefile``), so a case variant matches on a case-insensitive filesystem
    and a different directory never does. Both missing: the same path text (``path_key``). Only one exists:
    different places.
    """
    if path_is_link(registered) or path_is_link(candidate):
        return False
    here, there = os.path.lexists(registered), os.path.lexists(candidate)
    if here != there:
        return False
    if not here:
        return path_key(registered) == path_key(candidate)
    try:
        return os.path.samefile(registered, candidate)
    except OSError:
        return path_key(registered) == path_key(candidate)


def link_refusal(path: Path) -> str | None:
    """A link where a worktree of the name would be: refused, never resolved to what it points at, never removed."""
    if not path_is_link(path):
        return None
    return (f"{path} is a link, not a worktree: delete only the link entry by hand (rm, never rm -r; a Windows "
            "junction with rmdir, never rmdir /s), then run this command again")


def scan_links(root: Path | str) -> tuple[list[str], list[str]]:
    """(relative posix paths of links, directories that could not be read). Links are never entered."""
    base = str(root)
    links: list[str] = []
    errors: list[str] = []
    stack = [""]
    while stack:
        rel = stack.pop()
        here = os.path.join(base, rel) if rel else base
        try:
            with os.scandir(here) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            errors.append(f"{rel or '.'}: {exc.strerror or exc}")
            continue
        for entry in entries:
            child = f"{rel}/{entry.name}" if rel else entry.name
            if is_link(entry):
                links.append(child)
                continue
            if entry.name in SKIP_DIRS:
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if is_dir:
                stack.append(child)
    return sorted(links), sorted(errors)


def unlink_link(path: Path) -> None:
    """Remove ONE link entry and nothing it points at. Never rmtree; a junction is removed with rmdir."""
    if not path_is_link(path):
        raise ToolError(f"not a link any more, left alone: {path}")
    if os.path.islink(path):
        try:
            os.unlink(path)
        except OSError:
            if sys.platform != "win32":
                raise
            os.rmdir(path)  # a directory symlink on Windows
        return
    try:
        os.rmdir(path)  # junction (a directory reparse point that is not a symlink)
    except OSError:
        os.unlink(path)  # a file-type reparse point


# ------------------------------------------------------------------------------------------------ helpers
def check_name(name: str) -> str:
    if not NAME_RE.match(name) or name.endswith(".lock") or ".." in name:
        raise ToolError(f"bad worktree name {name!r}: use letters, digits, '.', '_', '-' (max 64)")
    return name


def worktrees_folder(main: Path) -> Path:
    """The one folder that holds every worktree: ``<main checkout>-worktrees`` next to the main checkout."""
    return main.parent / f"{main.name}-worktrees"


def worktree_path(main: Path, name: str) -> Path:
    return worktrees_folder(main) / name


def legacy_path(main: Path, name: str) -> Path:
    """Where worktrees made before 2026-10-28 live: the sibling ``<main checkout>-wt-<name>``."""
    return main.parent / f"{main.name}-wt-{name}"


def is_legacy_location(main: Path, path: str | Path) -> bool:
    """A sibling of the main checkout named ``<main checkout>-wt-<something>`` (compared as written by git,
    case folded where ``path_key`` folds it)."""
    p = Path(path)
    prefix = path_key(main.parent / f"{main.name}-wt-")
    return path_key(p.parent) == path_key(main.parent) and path_key(p).startswith(prefix)


def folder_problem(folder: Path) -> str | None:
    """Why *folder* cannot hold worktrees (a link or a non-directory), or None (a real directory, or missing)."""
    if path_is_link(folder):
        return (f"{folder} is a symlink, junction or reparse point; the worktrees folder must be a real "
                "directory (a link can be followed by a removal)")
    if os.path.lexists(folder) and not folder.is_dir():
        return f"{folder} exists and is not a directory"
    return None


def remove_folder_if_empty(folder: Path) -> str:
    """Delete the worktrees folder when it is empty and a real directory: ``os.rmdir`` only, never a link."""
    if not os.path.lexists(folder):
        return f"folder already gone: {folder}"
    problem = folder_problem(folder)
    if problem:
        return f"kept folder: {problem}"
    try:
        with os.scandir(folder) as it:
            left = sum(1 for _ in it)
    except OSError as exc:
        return f"kept folder: {folder} ({exc.strerror or exc})"
    if left:
        return f"kept folder: {folder} ({left} entr{'y' if left == 1 else 'ies'} left)"
    try:
        os.rmdir(folder)
    except OSError as exc:
        return f"kept folder: {folder} ({exc.strerror or exc})"
    return f"removed empty folder: {folder}"


def dirty_paths(wt: Path | str, ignore: set[str]) -> list[str]:
    """Paths with uncommitted changes (untracked included).

    Untracked entries that are, or lie below, a path in *ignore* do not count. Git for Windows reports a
    junction as a plain directory and lists the target's files below it, so the prefix match is needed.
    """
    out = git(wt, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    tokens = out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if len(tok) < 4:
            continue
        code, path = tok[:2], tok[3:]
        if code[0] in "RC":
            i += 1  # the second path of a rename or copy
        if code == "??" and any(path.rstrip("/") == link or path.startswith(link.rstrip("/") + "/") for link in ignore):
            continue
        paths.append(path)
    return paths


def venv_python(main: Path) -> Path:
    sub = ("Scripts", "python.exe") if sys.platform == "win32" else ("bin", "python")
    return main / ".venv" / sub[0] / sub[1]


def show_links(links: Sequence[str]) -> None:
    for item in links[:MAX_LINES_SHOWN]:
        say(f"    link: {item}")
    if len(links) > MAX_LINES_SHOWN:
        say(f"    ... and {len(links) - MAX_LINES_SHOWN} more")


# ------------------------------------------------------------------------------------------------ commands
def cmd_list(repo: Path) -> int:
    entries = list_entries(repo)
    main = entries[0].path
    with_links = 0
    legacy = 0
    say(f"main checkout: {main}")
    say(f"worktrees folder: {worktrees_folder(Path(main))}")
    for entry in entries:
        is_main = same_path(entry.path, main)
        present = Path(entry.path).is_dir()
        links, errors = scan_links(entry.path) if present else ([], [])
        with_links += bool(links)
        merged = "yes" if is_merged(repo, entry.head) else "no"
        pushed = "yes" if is_pushed(repo, entry.branch, entry.head) else "no"
        if is_main:
            created = "n/a"
        else:
            created = "yes" if present and has_marker(entry.path) else "no"
        flags = []
        if entry.prunable or not present:
            flags.append("MISSING-on-disk")
        if entry.locked:
            flags.append("locked")
        if errors:
            flags.append(f"unreadable-dirs={len(errors)}")
        if not is_main and is_legacy_location(Path(main), entry.path):
            flags.append("legacy-location")
            legacy += 1
        say(f"worktree: {entry.path}" + ("  (main)" if is_main else ""))
        say(f"  branch={entry.branch or '(detached)'} head={entry.head[:10] or '-'} merged={merged} "
            f"pushed={pushed} created-by-tool={created} LINKS={len(links)}" + (f" [{' '.join(flags)}]" if flags else ""))
        show_links(links)
    if legacy:
        say(f"{legacy} worktree(s) in the legacy location <main>-wt-<name>: move each with "
            "'git worktree move <old path> <worktrees folder>/<name>' (worktree.py --help)")
    say(f"WORKTREE LIST: OK {len(entries)} worktrees, {with_links} with links")
    return 0


def cmd_add(repo: Path, name: str, branch: str, base: str | None) -> int:
    check_name(name)
    if git(repo, "check-ref-format", "--branch", branch, check=False).returncode != 0:
        raise ToolError(f"bad branch name {branch!r}")
    entries = list_entries(repo)
    if not entries:
        raise ToolError("no worktree found: not a git repository?")
    main = Path(entries[0].path)
    folder = worktrees_folder(main)
    dest = worktree_path(main, name)
    legacy = legacy_path(main, name)
    refusal = folder_problem(folder) or link_refusal(dest)
    if refusal is None and (dest.exists() or os.path.lexists(dest)):
        refusal = f"{dest} already exists"
    refusal = refusal or link_refusal(legacy)
    old = next((e for e in entries[1:] if same_place(e.path, legacy)), None)
    if refusal is None and old is not None:
        refusal = (f"a worktree named {name} already exists in the legacy location {Path(old.path)} (remove would be "
                   "ambiguous): move it into the worktrees folder or pick another name")
    if refusal is None and ref_sha(main, f"refs/heads/{branch}") is not None:
        refusal = f"branch {branch} already exists (add makes a NEW branch)"
    if refusal is not None:
        say(f"refused: {refusal}")
        say(f"WORKTREE ADD: REFUSED {name}")
        return 1
    start = base or ("origin/main" if ref_sha(main, "refs/remotes/origin/main") else "HEAD")
    made_folder = not os.path.lexists(folder)
    if made_folder:
        folder.mkdir()  # a real directory, never a link; its parent holds the main checkout, so it exists
    try:
        git(main, "worktree", "add", "--no-track", "-b", branch, str(dest), start)
    except ToolError:
        if made_folder:
            remove_folder_if_empty(folder)  # leave nothing behind when git refused
        raise
    if made_folder:
        say(f"created folder: {folder}")
    marker = git_dir(dest) / MARKER
    marker.write_text("created by scripts/worktree.py\n", encoding="utf-8")
    say(f"created: {dest}")
    say(f"  branch {branch} from {start}; marker written to the worktree git dir (removed with the worktree)")
    py = venv_python(main)
    say(f"  interpreter (main checkout, no link, no .venv in the worktree): {py}" +
        ("" if py.is_file() else "   [not found: create it with the bootstrap script in the main checkout]"))
    if sys.platform == "win32":
        say(f'  run: cd /d "{dest}" && set PYTHONPATH=backend && "{py}" -m pytest -q <tests>')
    else:
        say(f"  run: cd '{dest}' && PYTHONPATH=backend '{py}' -m pytest -q <tests>")
    say("  never create a symlink or junction inside the worktree (git worktree remove may follow it)")
    say(f"WORKTREE ADD: OK {name}")
    return 0


def cmd_remove(repo: Path, name: str, *, unlink_links: bool, foreign: bool, force: bool) -> int:
    check_name(name)
    entries = list_entries(repo)
    main = Path(entries[0].path)
    folder = worktrees_folder(main)
    dest = worktree_path(main, name)
    legacy = legacy_path(main, name)

    # A link is never resolved to a worktree: not the folder (it would alias every name below it), not either path.
    refusal = (folder_problem(folder) if path_is_link(folder) else None) or link_refusal(dest) or link_refusal(legacy)
    if refusal is not None:
        say(f"refused: {refusal}")
        say(f"WORKTREE REMOVE: REFUSED {name}")
        return 1

    def registered(path: Path) -> Entry | None:
        """The worktree registered AT *path* (``same_place``: its own path, never through a link)."""
        return next((e for e in entries[1:] if same_place(e.path, path) and not same_place(e.path, main)), None)

    in_folder, in_legacy = registered(dest), registered(legacy)
    if in_folder is not None and in_legacy is not None:
        say(f"refused: ambiguous name {name}: both {Path(in_folder.path)} and the legacy {Path(in_legacy.path)} are worktrees; "
            f"move one of them under another name, for example git worktree move {Path(in_legacy.path)} "
            f"{folder / '<other name>'} (never git worktree remove: it skips these checks and can follow a link), "
            "then run remove again")
        say(f"WORKTREE REMOVE: REFUSED {name}")
        return 1
    entry = in_folder or in_legacy
    if entry is None:
        say(f"refused: no worktree {dest} (nor in the legacy location {legacy})")
        say(f"WORKTREE REMOVE: REFUSED {name}")
        return 1
    wt = Path(entry.path)
    where = "" if in_folder is not None else " (legacy location)"
    reasons: list[str] = []
    links: list[str] = []
    if not wt.is_dir():
        say(f"worktree directory is gone: {wt}; pruning the stale entry")
        git(repo, "worktree", "prune")
        if in_folder is not None:
            say(f"  {remove_folder_if_empty(folder)}")
        say(f"WORKTREE REMOVE: OK {name} (was already missing){where}")
        return 0

    if not foreign and not has_marker(wt):
        reasons.append("no 'aeos-worktree-created' marker: this tool did not create it (--foreign to accept)")
    links, scan_errors = scan_links(wt)
    if scan_errors:
        reasons.append(f"cannot scan {len(scan_errors)} directories for links (first: {scan_errors[0]})")
    if links and not unlink_links:
        reasons.append(f"{len(links)} link(s) inside (a link can be followed by the removal; "
                       "--unlink-links removes only the link entries)")
    elif links:
        tracked = {p for p in git(wt, "ls-files", "-z").stdout.split("\0") if p}
        tracked_links = [p for p in links if p in tracked]
        if tracked_links:
            reasons.append(f"tracked link(s) {tracked_links[:3]}: unlinking would modify the checkout; "
                           "remove them by hand after review")
    dirty = dirty_paths(wt, set(links))
    if dirty:
        reasons.append(f"{len(dirty)} uncommitted change(s), first: {dirty[0]}")
    merged = is_merged(repo, entry.head)
    pushed = is_pushed(repo, entry.branch, entry.head)
    if not merged and not pushed and not force:
        what = f"branch {entry.branch}" if entry.branch else f"detached HEAD {entry.head[:10]}"
        reasons.append(f"{what} is neither merged into origin/main nor pushed to origin with this sha "
                       "(--force to accept losing it)")
    if reasons:
        say(f"worktree: {wt}")
        for reason in reasons:
            say(f"  refused: {reason}")
        show_links(links)
        say(f"WORKTREE REMOVE: REFUSED {name} ({len(reasons)} reason(s), nothing changed)")
        return 1

    if links:
        for rel in links:
            unlink_link(wt / rel)
            say(f"  unlinked: {rel}")
        links, scan_errors = scan_links(wt)
        if links or scan_errors:
            show_links(links)
            say(f"WORKTREE REMOVE: REFUSED {name} (links remain after unlinking)")
            return 1
    git(repo, "worktree", "remove", str(wt))
    git(repo, "worktree", "prune")
    say(f"removed: {wt}{where}")
    if in_folder is not None:
        say(f"  {remove_folder_if_empty(folder)}")
    say(f"WORKTREE REMOVE: OK {name}")
    return 0


# ------------------------------------------------------------------------------------------------ cli
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="worktree.py", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="List, add and safely remove git worktrees. Every worktree lives in ONE folder next to the "
                    "main checkout: <main>-worktrees/<name>.",
        epilog="Move a worktree from the legacy location <main>-wt-<name> into the folder (create the folder "
               "first,\ngive the full target path):\n"
               "  mkdir <main>-worktrees\n"
               "  git worktree move <main>-wt-<name> <main>-worktrees/<name>")
    p.add_argument("--repo", default=".", help="any directory inside the repository (default: current)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show every worktree with merged, pushed, marker and link counts and a "
                                "legacy-location flag")
    a = sub.add_parser("add", help="create <main>-worktrees/<name> on a new branch (the folder is created "
                                   "when missing, never as a link)")
    a.add_argument("name")
    a.add_argument("branch")
    a.add_argument("base", nargs="?", default=None, help="start point (default origin/main, else HEAD)")
    r = sub.add_parser("remove", help="remove <main>-worktrees/<name> (or the legacy <main>-wt-<name>) after "
                                      "safety checks; an empty <main>-worktrees is removed too")
    r.add_argument("name")
    r.add_argument("--unlink-links", action="store_true", help="remove link entries inside (never their targets)")
    r.add_argument("--foreign", action="store_true", help="accept a worktree this tool did not create")
    r.add_argument("--force", action="store_true", help="accept a branch that is neither merged nor pushed")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = Path(args.repo)
    try:
        if args.cmd == "list":
            return cmd_list(repo)
        if args.cmd == "add":
            return cmd_add(repo, args.name, args.branch, args.base)
        return cmd_remove(repo, args.name, unlink_links=args.unlink_links, foreign=args.foreign, force=args.force)
    except ToolError as exc:
        say(f"error: {exc}")
        say(f"WORKTREE {args.cmd.upper()}: ERROR")
        return 2
    except OSError as exc:
        say(f"error: {exc}")
        say(f"WORKTREE {args.cmd.upper()}: ERROR")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
