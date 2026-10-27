"""Paste-safety checker for shell commands meant for the owner (LESSONS L2, L21).

The owner pastes commands into zsh on macOS. A pasted block breaks when:

* a ``#`` follows a command: an interactive zsh has no comments, so ``cmd  # note`` hands ``#`` and ``note`` to the
  command;
* a word holds an unquoted glob character (``[ ] ? *``): ``.[dev]`` stops with ``no matches found``;
* a ``cp`` is followed by an ``rm`` that is not guarded in one chain ``cp SRC DST && cmp SRC DST && rm SRC``: the
  delete runs even when the copy failed (data-loss class, L21);
* a physical line is longer than 80 characters: a chat view or a narrow terminal wraps it, and the paste then
  carries a newline into the middle of the command (inside quotes zsh waits at ``dquote>``);
* a command that prompts for hidden input (a password or a key) is not the last command of its block: the lines
  after it are typed into the prompt as the password, and two such commands in one block always fail;
* a placeholder such as ``<your-token>`` stands outside quotes: zsh reads ``<`` as a redirection from a file.

Input is Markdown (every fenced bash, sh, zsh, shell or console block, and an untagged fence whose first line starts
like a shell command) or plain shell text (``--text``, ``--shell``, or a ``.sh``/``.zsh``/``.bash``/``.txt`` file).
A Markdown block that shows file contents instead of commands opts out with ``<!-- paste-safe: file-content -->``
directly above it. powershell, cmd, text, yaml and json blocks are not scanned.

Usage::

    python scripts/paste_safe.py --check FILE [FILE ...]
    python scripts/paste_safe.py --check -            (Markdown on standard input)
    python scripts/paste_safe.py --check --text 'CMD'

Exit codes: 0 every block is paste-safe, 1 findings (each printed as ``path:line: problem: line``), 2 usage or
read error. The last line is ``PASTE_SAFE: OK`` or ``PASTE_SAFE: FAIL``. Stdlib only, ASCII output.

``tests/unit/test_owner_commands_paste_safe.py`` runs this checker over every Markdown file of the repository and
holds its bite tests; it also fails when a CLI command gains a hidden prompt that ``HIDDEN_PROMPT_COMMANDS`` lacks.
"""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

SHELL_LANGS = frozenset({"bash", "sh", "zsh", "shell", "console"})
SHELL_SUFFIXES = frozenset({".sh", ".zsh", ".bash", ".txt"})
OPT_OUT = "<!-- paste-safe: file-content -->"
MAX_LINE = 80
# CommonMark fence: any number of blockquote markers and list markers (nested lists, a list inside a quote and a
# quote inside a list), then 3+ backticks or tildes.
# Each repetition starts with '>' or a list marker (never with whitespace), so the match is linear (no ReDoS).
FENCE = re.compile(r"^[ \t]*(?:(?:>|(?:[-*+]|\d{1,9}[.)])(?=[ \t]))[ \t]*)*(`{3,}|~{3,})[ \t]*(.*)$")
CLOSE_PREFIX = re.compile(r"^[ \t]*(?:>[ \t]?)*[ \t]*")
PROMPT = re.compile(r"^\s*[$%] ")
HEREDOC = re.compile(r"<<(-?)[ \t]*([\"']?)([A-Za-z_][A-Za-z0-9_]*)\2")
# '<word>' as a placeholder: a letter, digit, '_' or '.' right after '<' and no blank right before '>'
# ('cmd <in >out' is a redirection, not a placeholder).
PLACEHOLDER = re.compile(r"<[A-Za-z0-9_.](?:[^<>'\"\n]*[^<>'\"\s])?>")
WORD_START_PREV = " \t;&|("
GLOB_CHARS = "[]?*"
# Untagged fences are scanned too when their first line starts like a shell command (incident class:
# an untagged "Reproduce" block that carried the exact pasted command).
COMMAND_START = re.compile(r"^\s*(?:[$%] )?(?:\./|\.\./|~/)?(?:aeos|pip3?|python3?|npm|npx|git|gh|cd|brew|curl|make|"
                           r"source|export|sh|bash|zsh|uv|node|pytest|ruff|mypy)(?:\s|$)")

# Commands of the aeos CLI that prompt for hidden input (getpass or a hide_input prompt) without an '--as' option,
# with the flags that skip the prompt. Every command with an '--as <admin>' option prompts for the admin password
# and is recognised by that option. The test derives this list from backend/aeos and fails when it differs.
AS_OPTION = "--as"
HIDDEN_PROMPT_COMMANDS: dict[str, tuple[str, ...]] = {
    "admin create": (),
    "secrets set": ("--stdin",),
    "vault import-unseal-key": (),
    "vault unseal": ("--from-keyring",),
}
# A flag that skips the hidden prompt only when the command's standard input is a pipe: without one, the command
# reads the rest of the paste as the value.
PIPE_ONLY_FLAGS = frozenset({"--stdin"})
COMMAND_WRAPPERS = ("sudo", "command", "builtin", "nohup", "exec", "env")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

LONG_LINE_FIX = ("break it with a trailing backslash (cmd \\ then the rest on the next line) or after a pipe; a chat "
                 "view or a narrow terminal wraps a long line and the paste then breaks it (inside quotes zsh waits "
                 "at 'dquote>')")


# ----------------------------------------------------------------------------- fences
def _scan_line(line: str, quote: str | None) -> tuple[list[str], str | None, str | None]:
    """Return (problems, quote state at end of line, heredoc delimiter opened on this line)."""
    problems: list[str] = []
    heredoc: str | None = None
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if quote == "'":
            if c == "'":
                quote = None
        elif quote == '"':
            if c == "\\":
                i += 1
            elif c == '"':
                quote = None
        elif c == "\\":
            i += 1
        elif c in "'\"":
            quote = c
        elif c == "<" and line.startswith("<<<", i):
            i += 2  # here-string: not a heredoc
        elif c == "<" and line.startswith("<<", i):
            hd = HEREDOC.match(line, i)
            if hd:
                heredoc = hd.group(3)
                i = hd.end() - 1
            else:
                i += 1
        elif c == "<" and (ph := PLACEHOLDER.match(line, i)):
            problems.append(f"placeholder {ph.group(0)!r} outside quotes: zsh reads '<' as a redirection from a "
                            f"file; quote it (\"{ph.group(0)}\") or set a variable on an earlier line")
            i = ph.end() - 1
        elif c == "$" and line.startswith("${", i):
            end = line.find("}", i)
            i = n if end < 0 else end
        elif c == "#" and (i == 0 or line[i - 1] in WORD_START_PREV):
            problems.append("'#' comment outside quotes")
            break
        elif c in GLOB_CHARS and not (c == "?" and i > 0 and line[i - 1] == "$"):
            problems.append(f"unquoted glob character {c!r}")
        i += 1
    return problems, quote, heredoc


def _is_closing(line: str, char: str, length: int) -> bool:
    rest = CLOSE_PREFIX.sub("", line, count=1).rstrip()
    return len(rest) >= length and set(rest) == {char}


def _fences(markdown: str) -> Iterator[tuple[int, str, list[str], int]]:
    """(index of the opening fence line, language, body lines, width of the container prefix) of every scanned
    shell fence."""
    lines = markdown.splitlines()
    idx = 0
    while idx < len(lines):
        m = FENCE.match(lines[idx])
        if not m or (m.group(1)[0] == "`" and "`" in m.group(2)):
            idx += 1
            continue
        fence, info = m.group(1), m.group(2).split()
        lang = info[0].lower() if info else ""
        prev = idx - 1
        while prev >= 0 and not lines[prev].strip():
            prev -= 1
        opted_out = prev >= 0 and lines[prev].strip() == OPT_OUT
        end = idx + 1
        while end < len(lines) and not _is_closing(lines[end], fence[0], len(fence)):
            end += 1
        body = lines[idx + 1 : end]
        first = next((b for b in body if b.strip()), "")
        if not lang and COMMAND_START.match(first):
            lang = "bash"
        if lang in SHELL_LANGS and not opted_out:
            yield idx, lang, body, m.start(1)
        idx = end + 1


def _blocks(text: str, shell: bool) -> Iterator[tuple[int, str, list[str], int]]:
    """The scanned shell fences of Markdown, or plain shell text as one block whose lines keep their numbers (the
    block "opens" at index -1, so its first line is line 1)."""
    if shell:
        yield -1, "bash", text.splitlines(), 0
    else:
        yield from _fences(text)


def shell_fences(markdown: str, *, shell: bool = False) -> Iterator[tuple[int, str, list[str]]]:
    """(index of the opening fence line, language, body lines) of every shell fence that is scanned."""
    for idx, lang, body, _width in _blocks(markdown, shell):
        yield idx, lang, body


def _shown(raw: str, width: int) -> str:
    """The line as the rendered block shows it: the list indent and quote markers of its container removed."""
    k = 0
    while k < width and k < len(raw) and raw[k] in " \t>":
        k += 1
    return raw[k:].rstrip()


def paste_problems(markdown: str, *, shell: bool = False) -> list[tuple[int, str, str]]:
    """Return (line number, line, problem) for every non paste-safe line in shell fences: a '#' comment, an unquoted
    glob character, a placeholder outside quotes and a line longer than MAX_LINE characters."""
    out: list[tuple[int, str, str]] = []
    for idx, lang, body, width in _blocks(markdown, shell):
        prompted = lang == "console" and any(PROMPT.match(b) for b in body)
        quote: str | None = None
        heredoc: str | None = None
        continued = False
        for off, raw in enumerate(body):
            if heredoc is not None:
                if raw.strip() == heredoc:
                    heredoc = None
                continue
            if lang == "console" and prompted and not PROMPT.match(raw) and quote is None and not continued:
                continue  # command output in a console transcript
            text = PROMPT.sub("", raw, count=1)
            probs, quote, heredoc = _scan_line(text, quote)
            continued = quote is None and text.rstrip().endswith("\\") and not text.rstrip().endswith("\\\\")
            shown = _shown(raw, width)
            if len(shown) > MAX_LINE:
                probs.append(f"line is {len(shown)} characters (more than {MAX_LINE}): {LONG_LINE_FIX}")
            out += [(idx + 2 + off, raw, p) for p in probs]
    return out


# ----------------------------------------------------------------------------- hidden prompts
def _command_lines(lang: str, body: list[str], first_lineno: int) -> list[tuple[int, str]]:
    """(line number, logical command) of every command of a block: continuation lines (a trailing backslash, a
    trailing '|', '&&' or '||', an open quote) are joined; heredoc bodies, blank lines and console output are
    skipped."""
    prompted = lang == "console" and any(PROMPT.match(b) for b in body)
    commands: list[tuple[int, str]] = []
    quote: str | None = None
    heredoc: str | None = None
    pending: list[str] = []
    start = 0
    for off, raw in enumerate(body):
        if heredoc is not None:
            if raw.strip() == heredoc:
                heredoc = None
            continue
        if not pending:
            if not raw.strip():
                continue
            if lang == "console" and prompted and not PROMPT.match(raw):
                continue  # command output in a console transcript
            start = first_lineno + off
        text = PROMPT.sub("", raw, count=1) if not pending else raw
        _probs, quote, opened = _scan_line(text, quote)
        heredoc = opened or heredoc
        stripped = text.rstrip()
        if quote is None and stripped.endswith("\\") and not stripped.endswith("\\\\"):
            pending.append(stripped[:-1])
            continue
        if quote is not None or (stripped.endswith(("|", "&&")) and not stripped.endswith("\\|")):
            pending.append(stripped)
            continue
        commands.append((start, " ".join([*pending, text.strip()]).strip()))
        pending = []
    if pending:
        commands.append((start, " ".join(pending).strip()))
    return commands


def _segments(line: str) -> list[tuple[str, bool]]:
    """(simple command, receives a pipe) of a command line, split outside quotes at ';', '&', '&&', '|', '||'."""
    out: list[tuple[str, bool]] = []
    quote: str | None = None
    cur: list[str] = []
    piped = False
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if quote:
            if c == quote:
                quote = None
            elif c == "\\" and quote == '"':
                cur.append(c)
                i += 1
                c = line[i] if i < n else ""
            cur.append(c)
        elif c == "\\":
            cur.append(line[i:i + 2])
            i += 1
        elif c in "'\"":
            quote = c
            cur.append(c)
        elif c in ";&|":
            op = line[i:i + 2] if line[i:i + 2] in ("&&", "||") else c
            out.append(("".join(cur).strip(), piped))
            cur = []
            piped = op == "|"
            i += len(op) - 1
        else:
            cur.append(c)
        i += 1
    out.append(("".join(cur).strip(), piped))
    return [(s, p) for s, p in out if s]


def _words(segment: str) -> list[str]:
    try:
        return shlex.split(segment, comments=False, posix=True)
    except ValueError:
        return segment.split()


def hidden_prompt(segment: str, piped: bool = False) -> str | None:
    """What makes this simple command prompt for hidden input, or None when it does not."""
    words = _words(segment)
    while words and (words[0] in COMMAND_WRAPPERS or ASSIGNMENT.match(words[0])):
        words = words[1:]
    if not words or words[0].lstrip("\\").replace("\\", "/").rsplit("/", 1)[-1] != "aeos":
        return None
    args = words[1:]
    if any(a == AS_OPTION or a.startswith(AS_OPTION + "=") for a in args):
        return f"'{AS_OPTION}' asks for the admin password"
    path = [a for a in args if not a.startswith("-")]
    for name, skip in HIDDEN_PROMPT_COMMANDS.items():
        parts = name.split()
        if path[:len(parts)] != parts:
            continue
        if any(f in args and (piped or f not in PIPE_ONLY_FLAGS) for f in skip):
            return None
        return f"'aeos {name}' asks for hidden input"
    return None


def prompt_problems(markdown: str, *, shell: bool = False) -> list[tuple[int, str, str]]:
    """(line number, line, problem) for every block where a hidden prompt would read pasted lines: more than one
    prompting command in a block, or a prompting command followed by another command line."""
    out: list[tuple[int, str, str]] = []
    for idx, lang, body, _width in _blocks(markdown, shell):
        commands = _command_lines(lang, body, idx + 2)
        prompts: list[tuple[int, int, str]] = []  # (position in commands, line number, reason)
        for pos, (lineno, line) in enumerate(commands):
            for segment, piped in _segments(line):
                reason = hidden_prompt(segment, piped)
                if reason:
                    prompts.append((pos, lineno, reason))
        if len(prompts) > 1:
            lines = ", ".join(str(ln) for _p, ln, _r in prompts)
            lineno = prompts[1][1]
            out.append((lineno, body[lineno - idx - 2],
                        f"{len(prompts)} commands in this block prompt for hidden input (lines {lines}): the paste "
                        "types the lines after the first prompt into it as the password; give each one its own "
                        "block, as the last line"))
        for pos, lineno, reason in prompts:
            if pos + 1 < len(commands):
                nxt = commands[pos + 1][0]
                out.append((lineno, body[lineno - idx - 2],
                            f"{reason} and line {nxt} follows it in the same block: the paste types line {nxt} "
                            "into the prompt as the password; end the block with this command (one hidden-prompt "
                            "command per block, last)"))
    return out


# ----------------------------------------------------------------------------- delete after copy (L21)
# Data-loss class: a copy followed by a delete must delete only when the copy is proven, in one '&&' chain with a
# compare ('cp SRC DST && cmp SRC DST && rm SRC'). A separate 'rm' line (or one after ';') runs even when the copy
# failed and may delete the only copy of recovery material.
_CHAIN_SPLIT = re.compile(r"\s*(&&|\|\||;|\|)\s*")


def _command_head(word: str) -> str:
    """The program a simple command runs: 'sudo rm', 'command rm', '\\rm' and '/bin/rm' all run rm."""
    tokens = word.split()
    while tokens and tokens[0] in ("sudo", "command", "builtin", "nohup", "exec"):
        tokens = tokens[1:]
    return tokens[0].lstrip("\\").rsplit("/", 1)[-1] if tokens else ""


def unguarded_deletes(markdown: str, *, shell: bool = False) -> list[tuple[int, str]]:
    """(line number, command) of every 'rm' after a 'cp' in the same shell fence that is not guarded by
    'cp ... && cmp ... && rm' in one chain."""
    out: list[tuple[int, str]] = []
    for idx, _lang, body in shell_fences(markdown, shell=shell):
        seen_cp = False
        commands: list[tuple[int, str]] = []  # (line number, logical command line with continuations joined)
        pending = ""
        start = 0
        for off, raw in enumerate(body):
            text = PROMPT.sub("", raw, count=1)
            if not pending:
                start = idx + 2 + off
            if text.rstrip().endswith("\\"):
                pending += text.rstrip()[:-1] + " "
                continue
            commands.append((start, pending + text))
            pending = ""
        if pending:
            commands.append((start, pending))
        for lineno, line in commands:
            parts = _CHAIN_SPLIT.split(line.strip())
            words, ops = parts[0::2], ["&&", *parts[1::2]]
            guarded = False  # within the current '&&' chain: a cp followed by a cmp/diff
            chain_cp = False
            for word, op in zip(words, ops, strict=True):
                head = _command_head(word)
                if op != "&&":
                    guarded = chain_cp = False
                if head == "cp":
                    seen_cp = chain_cp = True
                    guarded = False
                elif head in ("cmp", "diff") and chain_cp:
                    guarded = True
                elif head == "rm" and seen_cp and not guarded:
                    out.append((lineno, word))
    return out


# ----------------------------------------------------------------------------- all rules
def check_markdown(markdown: str, *, shell: bool = False) -> list[tuple[int, str, str]]:
    """(line number, line, problem) of every finding of every rule, sorted by line. ``shell``: the text is plain
    shell commands, not Markdown."""
    found = paste_problems(markdown, shell=shell) + prompt_problems(markdown, shell=shell)
    lines = markdown.splitlines()
    for lineno, command in unguarded_deletes(markdown, shell=shell):
        found.append((lineno, lines[lineno - 1] if lineno <= len(lines) else command,
                      "rm without a proven copy: the delete runs even when the copy failed; use one chain "
                      "'cp SRC DST && cmp SRC DST && rm SRC'"))
    return sorted(found, key=lambda f: f[0])




def _ascii(text: str) -> str:
    return text.encode("ascii", "backslashreplace").decode("ascii")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check that shell commands for the owner are paste-safe in zsh.")
    ap.add_argument("--check", action="store_true", help="check the inputs (required)")
    ap.add_argument("--text", action="append", default=[], help="shell commands to check (repeatable)")
    ap.add_argument("--shell", action="store_true", help="read every FILE (and stdin) as shell text, not Markdown")
    ap.add_argument("files", nargs="*", help="Markdown files ('-' is standard input); .sh/.zsh/.bash/.txt are shell")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        if exc.code in (0, None):
            raise
        print("PASTE_SAFE: FAIL")
        return 2
    if not args.check or not (args.files or args.text):
        print("usage: paste_safe.py --check FILE [FILE ...] | --check - | --check --text 'CMD'", file=sys.stderr)
        print("PASTE_SAFE: FAIL")
        return 2
    inputs: list[tuple[str, str, bool]] = []  # (label, text, shell)
    for name in args.files:
        if name == "-":
            inputs.append(("<stdin>", sys.stdin.read(), args.shell))
            continue
        path = Path(name)
        try:
            inputs.append((name, path.read_text(encoding="utf-8"), args.shell or path.suffix.lower() in SHELL_SUFFIXES))
        except (OSError, UnicodeDecodeError) as exc:
            print(_ascii(f"{name}: cannot read: {exc}"), file=sys.stderr)
            print("PASTE_SAFE: FAIL")
            return 2
    inputs += [(f"<text {i + 1}>", t, True) for i, t in enumerate(args.text)]
    total = 0
    for label, text, shell in inputs:
        for lineno, line, problem in check_markdown(text, shell=shell):
            total += 1
            print(_ascii(f"{label}:{lineno}: {problem}: {line.strip()}"))
    if total:
        print(f"{total} finding(s) in {len(inputs)} input(s)")
        print("PASTE_SAFE: FAIL")
        return 1
    print(f"{len(inputs)} input(s) checked")
    print("PASTE_SAFE: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
