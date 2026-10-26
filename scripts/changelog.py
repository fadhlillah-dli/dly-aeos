"""Changelog fragments: stop CHANGELOG.md merge conflicts (stdlib only, ASCII output).

Every pull request that needs a changelog entry adds ONE new file ``changelog.d/<short-slug>.md`` instead of
editing ``CHANGELOG.md``. Two branches that add two different new files merge without a conflict; two branches
that edit the same spot of ``## [Unreleased]`` do not. A release runs ``--assemble``, which moves every fragment
into ``## [Unreleased]`` and deletes the fragments.

  python scripts/changelog.py --check                  validate every fragment and the CHANGELOG.md structure
  python scripts/changelog.py --assemble [--dry-run]   move the fragments into CHANGELOG.md [Unreleased]

Fragment format (``changelog.d/README.md`` is the only other file allowed there):

  line 1   ``### <Section>`` with Section one of Added, Changed, Deprecated, Removed, Fixed, Security
  line 2   empty
  line 3+  one or more bullets, the first starting ``- **`` (``- **Title.** what, why, evidence level``)

A fragment is UTF-8 text with no conflict markers, no trailing whitespace, no other heading and no link-reference
definition at column 0 (``[label]: url`` belongs in CHANGELOG.md). A ``#`` line inside a fenced code block is
text, except a ``## `` or ``### `` line at column 0, which --assemble could not tell from a real heading.
Hidden junk files such as ``.DS_Store`` or an editor swap file are ignored.

Exit codes: 0 ok, 1 violations (each printed), 2 usage or IO error.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Keep a Changelog 1.1.0 order.
SECTIONS = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")
FRAGMENT_DIR = "changelog.d"
README = "README.md"
CHANGELOG = "CHANGELOG.md"

_CONFLICT = re.compile(r"^(?:<{7}(?: |$)|>{7}(?: |$)|={7}$)")
_HEADING = re.compile(r"^#{1,6}(?:\s|$)")
_SECTION_LINE = re.compile(r"^### (\S.*)$")
_FENCED_SECTION_LIKE = re.compile(r"^#{2,3} ")  # what assemble/structure would mistake for a heading
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_UNRELEASED = "## [Unreleased]"
_RELEASE_HEADING = re.compile(r"^## \[")
_LINK_DEF = re.compile(r"^\[[^\]]+\]:\s")


class ChangelogIOError(Exception):
    """A file could not be read or written (exit code 2)."""


def _ascii(text: object) -> str:
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ChangelogIOError(f"cannot read {_ascii(path)}: {_ascii(exc)}") from exc


def _read_text(path: Path) -> str:
    """Strict UTF-8 read; CRLF is normalised to LF so a Windows checkout without .gitattributes still validates."""
    return _read_bytes(path).decode("utf-8").replace("\r\n", "\n")


def _conflict_marker_lines(lines: list[str]) -> list[int]:
    return [i + 1 for i, line in enumerate(lines) if _CONFLICT.match(line)]


# ---------------------------------------------------------------------------------------------- fragments
def fragment_problems(text: str) -> list[str]:
    """Violations of the fragment format in *text* (already decoded); an empty list means valid."""
    problems: list[str] = []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the final newline is not an empty line
    for number in _conflict_marker_lines(lines):
        problems.append(f"line {number}: merge conflict marker")
    for number, line in enumerate(lines, start=1):
        if line != line.rstrip():
            problems.append(f"line {number}: trailing whitespace")
    first = lines[0] if lines else ""
    match = _SECTION_LINE.match(first)
    if match is None:
        problems.append("line 1: must be '### <Section>' (a heading is missing or malformed)")
    elif match.group(1) not in SECTIONS:
        problems.append(f"line 1: unknown section '{match.group(1)}' (use one of {', '.join(SECTIONS)})")
    if len(lines) < 2 or lines[1] != "":
        problems.append("line 2: must be empty")
    if len(lines) < 3 or not lines[2].startswith("- **"):
        problems.append("line 3: must be the first bullet, starting with '- **'")
    in_fence = False
    for number, line in enumerate(lines, start=1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if number > 1 and _LINK_DEF.match(line):
            problems.append(f"line {number}: a link-reference definition at column 0 is not allowed "
                            f"(it would end [Unreleased]; put it in {CHANGELOG})")
        elif number > 1 and (_FENCED_SECTION_LIKE.match(line) if in_fence else _HEADING.match(line)):
            problems.append(f"line {number}: only one heading is allowed (line 1)")
    if not any(line.startswith("- **") for line in lines):
        problems.append("no bullet starting with '- **'")
    return problems


def fragment_section(text: str) -> str:
    """The section name of a VALID fragment."""
    first = text.split("\n", 1)[0]
    match = _SECTION_LINE.match(first)
    assert match is not None
    return match.group(1)


def fragment_body(text: str) -> list[str]:
    """The bullet lines of a VALID fragment (everything after the blank line 2, trailing blanks dropped)."""
    lines = text.split("\n")[2:]
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def list_fragments(root: Path) -> tuple[list[Path], list[str]]:
    """(fragment files in filename order, violations about files that should not be there)."""
    directory = root / FRAGMENT_DIR
    found: list[Path] = []
    problems: list[str] = []
    try:
        if not directory.is_dir():
            return [], []
        for entry in sorted(directory.iterdir(), key=lambda p: p.name):
            if entry.name == README:
                continue
            if entry.name.startswith(".") and entry.suffix != ".md":
                continue  # OS and editor junk (.DS_Store, .x.swp); a hidden *.md is still a fragment
            rel = f"{FRAGMENT_DIR}/{entry.name}"
            if entry.is_symlink():
                problems.append(f"{_ascii(rel)}: a symlink is not allowed")
            elif not entry.is_file():
                problems.append(f"{_ascii(rel)}: only files are allowed in {FRAGMENT_DIR}/")
            elif entry.suffix != ".md":
                problems.append(f"{_ascii(rel)}: a fragment must be a .md file")
            else:
                found.append(entry)
    except OSError as exc:
        raise ChangelogIOError(f"cannot list {_ascii(directory)}: {_ascii(exc)}") from exc
    return found, problems


def check_fragments(root: Path) -> tuple[list[Path], list[str]]:
    """Valid-or-not verdict for every fragment: (fragment files, one message per violation)."""
    files, problems = list_fragments(root)
    for path in files:
        rel = _ascii(f"{FRAGMENT_DIR}/{path.name}")
        try:
            text = _read_text(path)
        except UnicodeDecodeError:
            problems.append(f"{rel}: not valid UTF-8")
            continue
        problems.extend(f"{rel}: {p}" for p in fragment_problems(text))
    return files, problems


# ---------------------------------------------------------------------------------------------- CHANGELOG.md
def _unreleased_span(lines: list[str]) -> tuple[int, int] | None:
    """(index of the '## [Unreleased]' line, index where its region ends); None when there is no such line.

    The region ends at the next '## ' heading; a file with no release section yet ends it at the first
    link-reference definition (the compare links at the bottom). A link definition before a release heading
    is part of the region, so it can never hide the sections after it."""
    try:
        start = lines.index(_UNRELEASED)
    except ValueError:
        return None
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            return start, i
    for i in range(start + 1, len(lines)):
        if _LINK_DEF.match(lines[i]):
            return start, i
    return start, len(lines)


def structure_problems(text: str) -> list[str]:
    """Violations of the CHANGELOG.md structure that fragments and --assemble rely on."""
    problems: list[str] = []
    lines = text.replace("\r\n", "\n").split("\n")
    for number in _conflict_marker_lines(lines):
        problems.append(f"{CHANGELOG} line {number}: merge conflict marker")
    if not lines or lines[0] != "# Changelog":
        problems.append(f"{CHANGELOG} line 1: must be '# Changelog'")
    count = lines.count(_UNRELEASED)
    if count != 1:
        problems.append(f"{CHANGELOG}: needs exactly one '{_UNRELEASED}' heading (found {count})")
    span = _unreleased_span(lines)
    if span is not None:
        start, end = span
        for i, line in enumerate(lines[:start]):
            if _RELEASE_HEADING.match(line):
                problems.append(f"{CHANGELOG} line {i + 1}: a release heading comes before '{_UNRELEASED}'")
        names = [m.group(1) for m in (_SECTION_LINE.match(x) for x in lines[start + 1:end]) if m]
        for name in names:
            if name not in SECTIONS:
                problems.append(f"{CHANGELOG} [Unreleased]: unknown section '{_ascii(name)}'")
        known = [n for n in names if n in SECTIONS]
        for name in sorted(set(known)):
            if known.count(name) > 1:
                problems.append(f"{CHANGELOG} [Unreleased]: section '{name}' appears {known.count(name)} times")
        ranks = [SECTIONS.index(n) for n in known]
        if ranks != sorted(ranks):
            problems.append(f"{CHANGELOG} [Unreleased]: sections are out of Keep-a-Changelog order "
                            f"({', '.join(SECTIONS)})")
    if not any(re.match(r"^\[unreleased\]:\s\S+", line, re.I) for line in lines):
        problems.append(f"{CHANGELOG}: the '[unreleased]:' compare link is missing")
    return problems


def assemble_text(text: str, fragments: list[tuple[str, list[str]]], eol: str | None = None) -> str:
    """CHANGELOG.md text with the fragments (section, bullet lines) in filename order appended to [Unreleased].

    Existing bullets stay first; a missing section is created in Keep-a-Changelog order; a section that no
    fragment touches is kept byte for byte. *text* must pass structure_problems(). *eol* defaults to the line
    ending *text* uses."""
    eol = eol or ("\r\n" if "\r\n" in text else "\n")
    lines = text.replace("\r\n", "\n").split("\n")
    span = _unreleased_span(lines)
    assert span is not None
    start, end = span
    region = lines[start + 1:end]
    heads = [i for i, line in enumerate(region) if _SECTION_LINE.match(line)]
    preamble = region[:heads[0]] if heads else region
    sections: list[tuple[str, list[str]]] = []  # (name, raw lines including the heading)
    for n, h in enumerate(heads):
        stop = heads[n + 1] if n + 1 < len(heads) else len(region)
        match = _SECTION_LINE.match(region[h])
        assert match is not None
        sections.append((match.group(1), region[h:stop]))
    added: dict[str, list[list[str]]] = {}
    for section, body in fragments:
        added.setdefault(section, []).append(body)
    for section, bodies in added.items():
        extra: list[str] = []
        for body in bodies:
            extra += ([""] if extra else []) + body
        index = next((i for i, (name, _) in enumerate(sections) if name == section), None)
        if index is None:
            rank = SECTIONS.index(section)
            index = next((i for i, (name, _) in enumerate(sections)
                          if name in SECTIONS and SECTIONS.index(name) > rank), len(sections))
            sections.insert(index, (section, [f"### {section}", "", *extra, ""]))
        else:
            raw = list(sections[index][1])
            while raw and raw[-1] == "":
                raw.pop()
            sections[index] = (section, [*raw, "", *extra, ""])
    out_region = list(preamble)
    if not out_region or out_region[-1] != "":
        out_region.append("")
    for _, raw in sections:
        out_region += raw if raw[-1] == "" else [*raw, ""]
    return eol.join(lines[:start + 1] + out_region + lines[end:])


def _atomic_write(path: Path, text: str) -> None:
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".changelog-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(text.encode("utf-8"))
            if path.exists():
                shutil.copymode(path, tmp_name)  # mkstemp creates 0600; keep the file's own permissions
            os.replace(tmp_name, path)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
    except OSError as exc:
        raise ChangelogIOError(f"cannot write {_ascii(path)}: {_ascii(exc)}") from exc


# ---------------------------------------------------------------------------------------------- commands
def _print_problems(problems: list[str]) -> None:
    for problem in problems:
        print(f"FAIL  {_ascii(problem)}")


def cmd_check(root: Path) -> int:
    path = root / CHANGELOG
    if not path.is_file():
        raise ChangelogIOError(f"{CHANGELOG} not found in {_ascii(root)}")
    try:
        problems = structure_problems(_read_text(path))
    except UnicodeDecodeError:
        problems = [f"{CHANGELOG}: not valid UTF-8"]
    files, fragment_issues = check_fragments(root)
    problems += fragment_issues
    if problems:
        _print_problems(problems)
        print(f"{len(problems)} violation(s)")
        return 1
    print(f"OK  {CHANGELOG} structure, {len(files)} fragment(s)")
    return 0


def cmd_assemble(root: Path, dry_run: bool) -> int:
    path = root / CHANGELOG
    if not path.is_file():
        raise ChangelogIOError(f"{CHANGELOG} not found in {_ascii(root)}")
    raw = _read_bytes(path)
    try:
        text = raw.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError:
        _print_problems([f"{CHANGELOG}: not valid UTF-8"])
        return 1
    problems = structure_problems(text)
    files, fragment_issues = check_fragments(root)
    problems += fragment_issues
    if problems:
        _print_problems(problems)
        print(f"{len(problems)} violation(s); nothing was changed")
        return 1
    if not files:
        print("nothing to assemble: no fragments")
        return 0
    parsed: list[tuple[str, list[str]]] = []
    for fragment in files:
        body = _read_text(fragment)
        parsed.append((fragment_section(body), fragment_body(body)))
        print(f"{'WOULD MOVE' if dry_run else 'MOVE'}  {_ascii(FRAGMENT_DIR + '/' + fragment.name)} "
              f"-> [Unreleased] / {parsed[-1][0]}")
    if dry_run:
        print(f"dry run: {len(files)} fragment(s), nothing was changed")
        return 0
    eol = "\r\n" if b"\r\n" in raw else "\n"  # the text was normalised to LF; keep the file's own
    _atomic_write(path, assemble_text(text, parsed, eol))
    for fragment in files:
        try:
            fragment.unlink()
        except OSError as exc:
            raise ChangelogIOError(f"cannot delete {_ascii(fragment)}: {_ascii(exc)}") from exc
    print(f"assembled {len(files)} fragment(s) into {CHANGELOG}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="changelog.py", description="Changelog fragments (changelog.d/).")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="validate fragments and the CHANGELOG.md structure")
    mode.add_argument("--assemble", action="store_true", help="move all fragments into CHANGELOG.md [Unreleased]")
    parser.add_argument("--dry-run", action="store_true", help="with --assemble: show what would move, change nothing")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this repository)")
    args = parser.parse_args(argv)
    if args.dry_run and not args.assemble:
        parser.error("--dry-run only works with --assemble")
    try:
        return cmd_check(args.root) if args.check else cmd_assemble(args.root, args.dry_run)
    except ChangelogIOError as exc:
        print(f"ERROR  {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
