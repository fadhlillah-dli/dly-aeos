"""PreToolUse hook for the Bash tool: deny the few shell traps that break silently in zsh (LESSONS L14).

    echo '{"tool_name": "Bash", "tool_input": {"command": "..."}}' | python3 scripts/claude_hooks/zsh_wordsplit_guard.py

zsh does not word-split an unquoted parameter expansion (unless ``SH_WORD_SPLIT`` is set), and a glob that matches
nothing is an error, not the glob itself. Incidents (LESSONS L14, 2026-10-26 to 2026-10-28): a ``set -- $pair`` loop
gave one argument instead of two (three times one day; once the shell exited 129), and a pytest command whose
``$(ls <glob>)`` matched nothing came back empty, so pytest ran the whole suite serially. Denied, each with its own
reason and the fix:

* ``set -- $NAME`` / ``set -- ${NAME}`` with one unquoted variable as the only argument;
* ``for x in $NAME`` / ``for x in ${NAME}`` with one unquoted variable as the only word list;
* a command substitution (``$(...)`` or backticks) holding a bare glob, passed to pytest or ``scripts/run_tests.sh``;
* an unquoted glob word that matches no file in the session's working directory (or after a plain ``cd DIR``): zsh's
  NOMATCH error stops the whole command before anything runs (four maintainer commands on 2026-10-28/29, for example
  ``grep -rn x backend --include=*.py``). Checked only when the hook input names an existing ``cwd``. Skipped: quoted,
  escaped, expanded or braced words, assignments, ``[[ ]]``, ``(( ))`` and ``case`` patterns, and a command that turns
  NOMATCH off (``setopt nonomatch``/``null_glob``, ``noglob``). Known false deny: a file created earlier in the same
  command; the reason names the fix (``setopt nonomatch;`` first, or split the command).

High precision over recall; everything else passes. Not denied: quoted expansions, explicit word lists, the zsh
split form ``${=NAME}``, subscripted or flagged expansions, a NAME assigned as an array in the same command (zsh
expands an unquoted array to its elements), a command that turns on ``SH_WORD_SPLIT`` or ``emulate sh/ksh/bash``,
and text that zsh does not run itself: single- or double-quoted strings (so ``bash -c '...'``), heredoc bodies and
comments. A ``$(...)`` or backtick substitution inside double quotes is still checked: zsh runs it. The traps are
zsh traps, so the hook only denies when the session shell is zsh: the basename of
``CLAUDE_CODE_SHELL``, else ``SHELL``, else (neither set) the platform default (zsh on macOS).

Protocol (Claude Code hooks reference, https://code.claude.com/docs/en/hooks, section "PreToolUse"):

* Input: JSON on stdin with ``tool_name`` and ``tool_input.command``.
* Allow: print nothing and exit 0.
* Deny: exit 0 and print ``{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
  "permissionDecisionReason": "..."}}``; the reason goes to Claude, which can retry with the fix.
* Fail-open: every internal error (bad JSON, a missing field, anything) exits 0 with no output, as
  ``secret_prompt_guard.py`` does. Do not add ``argparse`` (it exits 2 on a bad argument, and exit 2 blocks).

Stdlib only; runs under the system ``python3`` (3.9 on macOS) as well as the project interpreter."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable, Mapping

sys.dont_write_bytecode = True  # a hook must not drop __pycache__ into the checkout

TRAP_SET = "set-unquoted-var"
TRAP_FOR = "for-unquoted-var"
TRAP_GLOB = "test-runner-glob-substitution"
TRAP_NOMATCH = "unquoted-glob-matches-nothing"

# str.format templates: '{name}' is the variable, '{{' and '}}' are literal braces.
_FIX_SPLIT = (
    "Fix: list the items explicitly, split explicitly with a zsh array (arr=(${{={name}}}), then \"${{arr[@]}}\"), "
    "or run the loop under bash -c '...'."
)

REASONS: dict[str, str] = {
    TRAP_SET: (
        "zsh word-split trap (LESSONS L14): 'set -- ${name}' sets ONE positional parameter holding the whole "
        "value, because zsh does not word-split an unquoted variable, so $1 is everything and $2 is empty. "
        + _FIX_SPLIT
    ),
    TRAP_FOR: (
        "zsh word-split trap (LESSONS L14): 'for ... in ${name}' runs the loop ONCE with the whole value, because "
        "zsh does not word-split an unquoted variable. " + _FIX_SPLIT
    ),
    TRAP_GLOB: (
        "zsh glob trap (LESSONS L14): a command substitution with a bare glob feeds the test runner; in zsh a glob "
        "that matches nothing fails inside the substitution ('no matches found'), the substitution comes back empty "
        "and pytest runs the WHOLE suite serially. Fix: scripts/run_tests.sh -- <explicit test file paths> (or "
        "pytest with explicit file paths)."
    ),
    TRAP_NOMATCH: (
        "zsh NOMATCH trap (LESSONS L14): the unquoted glob '{name}' matches no file, so zsh stops with 'no matches "
        "found' and runs NOTHING of this command (an option such as --include=*.py is globbed too). Fix: quote it "
        "('{name}'), or use find; if an earlier step of this same command creates the files, put 'setopt nonomatch;' "
        "first or split the command."
    ),
}

# zsh special parameters that are arrays: an unquoted one expands to its elements, so a loop over it is fine.
ZSH_ARRAYS = frozenset({
    "argv", "path", "fpath", "cdpath", "manpath", "mailpath", "module_path", "psvar", "pipestatus", "signals",
    "fignore", "watch", "match", "mbegin", "mend", "reply", "dirstack", "funcstack", "functrace", "funcfiletrace",
    "funcsourcetrace", "historywords", "precmd_functions", "preexec_functions", "chpwd_functions",
})

_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_VAR = rf"\$(?:(?P<a>{_NAME})|\{{(?P<b>{_NAME})\}})"
_CMD_START = r"(?:^|[;&|(){}\n]|\b(?:then|do|else)\b)[ \t]*"
_SET_RE = re.compile(rf"{_CMD_START}set[ \t]+--[ \t]+{_VAR}(?=[ \t]*(?:$|[;&|)}}\n]))")
_FOR_RE = re.compile(rf"{_CMD_START}for[ \t]+{_NAME}[ \t]+in[ \t]+{_VAR}(?=[ \t]*(?:;|\n))")
_WORD_SPLIT_ON = re.compile(
    r"\bsetopt\b[^;\n]*\b(?:sh_?word_?split)\b|\bset[ \t]+-o[ \t]+sh_?word_?split\b"
    r"|\bemulate\b(?:[ \t]+-[A-Za-z]+)*[ \t]+(?:sh|ksh|bash)\b",
    re.I,
)
_RUNNER_RE = re.compile(r"(?:^|[\s/=])(?:pytest|py\.test|run_tests\.sh)(?=\s|$)")
_GLOB_RE = re.compile(r"(?<![$\\])[*?]|[A-Za-z0-9_./-]\[[^\]\s]+\]")
_SEPARATOR = re.compile(r"[;&|\n]")
# Words zsh does not glob or that this hook cannot resolve: anything quoted, expanded, braced or escaped.
_UNRESOLVED = re.compile(r"['\"`$\\{}]|__")
_GLOB_OFF = re.compile(r"\b(?:setopt|set\s+-o)\b[^;\n]*\b(?:no_?nomatch|null_?glob|csh_?null_?glob|no_?glob)\b"
                       r"|\bunsetopt\b[^;\n]*\b(?:nomatch|glob)\b|(?:^|[;&|(\n]\s*)noglob\b", re.I)
_NO_GLOB_SPANS = re.compile(r"\[\[.*?\]\]|\(\(.*?\)\)|\bcase\b.*?\besac\b", re.S)
_CD_RE = re.compile(r"(?:^|[;&|(\n])[ \t]*cd[ \t]+([^\s;&|)]+)")
_WORD_RE = re.compile(r"[^\s;&|()<>]+")
_ASSIGNMENT = re.compile(rf"^{_NAME}(?:\[[^\]]*\])?\+?=")


def _array_assigned(masked: str, name: str) -> bool:
    """True when ``name`` is assigned as an array in this command (zsh then splits its unquoted expansion)."""
    n = re.escape(name)
    patterns = (
        rf"(?:^|[\s;&|(){{}}]){n}\+?=\(",
        rf"\b(?:typeset|declare|local|readonly|export)\b(?:[ \t]+-[A-Za-z]*)*[ \t]+-[A-Za-z]*a[A-Za-z]*\b[^;\n]*\b{n}\b",
        rf"\bread\b(?:[ \t]+-[A-Za-z]+)*[ \t]+-[A-Za-z]*A[A-Za-z]*(?:[ \t]+-[A-Za-z]+)*[ \t]+{n}\b",
        rf"\bset[ \t]+-A[ \t]+{n}\b",
    )
    return any(re.search(p, masked) for p in patterns)


def _read_heredoc_word(text: str, i: int) -> tuple[str, bool, int] | None:
    """At ``text[i:]`` just after ``<<``: (delimiter, strip_tabs, index after the delimiter word), or None."""
    strip_tabs = False
    if i < len(text) and text[i] == "-":
        strip_tabs, i = True, i + 1
    while i < len(text) and text[i] in " \t":
        i += 1
    m = re.match(r"'([^'\n]*)'|\"([^\"\n]*)\"|\\?([A-Za-z0-9_.-]+)", text[i:])
    if not m:
        return None
    word = next(g for g in m.groups() if g is not None)
    return (word, strip_tabs, i + m.end()) if word else None


def _substitution_end(text: str, i: int) -> tuple[int, bool]:
    """At ``text[i:]`` just after ``$(``: (index after the matching ``)``, True), or (``len(text)``, False) when it
    is not closed. Quoted strings inside are skipped."""
    depth, j, n = 1, i, len(text)
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
        elif c == "'":
            k = text.find("'", j + 1)
            j = n if k < 0 else k + 1
        elif c == '"':
            j = _double_quote_end(text, j + 1) + 1
        elif c == "`":
            k = text.find("`", j + 1)
            j = n if k < 0 else k + 1
        else:
            depth += {"(": 1, ")": -1}.get(c, 0)
            j += 1
            if depth == 0:
                return j, True
    return n, False


def _double_quote_end(text: str, i: int) -> int:
    """At ``text[i:]`` just after an opening ``"``: the index of the closing ``"`` (or ``len(text)``)."""
    j, n = i, len(text)
    while j < n and text[j] != '"':
        if text[j] == "\\":
            j += 2
        elif text.startswith("$(", j) and not text.startswith("$((", j):
            j = _substitution_end(text, j + 2)[0]
        elif text[j] == "`":
            k = text.find("`", j + 1)
            j = n if k < 0 else k + 1
        else:
            j += 1
    return min(j, n)


def _mask_double_quoted(body: str) -> str:
    """The body of a double-quoted string: plain text becomes ``_``; a ``$(...)`` or backtick substitution stays,
    masked itself, because zsh runs it (so ``"$(ls <glob>)"`` is still seen)."""
    out: list[str] = []
    j, n = 0, len(body)
    while j < n:
        if body[j] == "\\":
            out.append("__"[: n - j])
            j += 2
        elif body.startswith("$(", j) and not body.startswith("$((", j):
            end, closed = _substitution_end(body, j + 2)
            out.append("$(" + mask(body[j + 2:end - 1 if closed else end]) + (")" if closed else ""))
            j = end
        elif body[j] == "`":
            k = body.find("`", j + 1)
            end = n if k < 0 else k
            out.append("`" + mask(body[j + 1:end]) + ("`" if k >= 0 else ""))
            j = end + 1
        else:
            out.append("_")
            j += 1
    return "".join(out)


def mask(text: str) -> str:
    """The command with the text zsh does not run itself blanked out.

    Quoted string contents become ``_`` (the quote characters stay), heredoc bodies and comments are dropped, an
    escaped character becomes ``_``. A ``$(...)`` or backtick substitution inside double quotes is kept (masked
    itself), because zsh runs it. What remains is what zsh parses as words and operators. Approximate on purpose."""
    out: list[str] = []
    heredocs: list[tuple[str, bool]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            out.append("__" if i + 1 < n and text[i + 1] != "\n" else "")
            i += 2
            continue
        if c == "'" or (c == "$" and i + 1 < n and text[i + 1] == "'"):
            start = i + (2 if c == "$" else 1)
            j = start
            while j < n and text[j] != "'":
                j += 2 if (c == "$" and text[j] == "\\") else 1
            out.append("'" + "_" * (min(j, n) - start) + "'")
            i = j + 1
            continue
        if c == '"':
            j = _double_quote_end(text, i + 1)
            out.append('"' + _mask_double_quoted(text[i + 1:j]) + '"')
            i = j + 1
            continue
        if c == "#" and (i == 0 or text[i - 1] in " \t\n;&|()"):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("<<", i) and not text.startswith("<<<", i):
            parsed = _read_heredoc_word(text, i + 2)
            if parsed is not None:
                word, strip_tabs, after = parsed
                heredocs.append((word, strip_tabs))
                out.append("<<_")
                i = after
                continue
        if c == "\n" and heredocs:
            i += 1
            for word, strip_tabs in heredocs:
                while i < n:
                    j = text.find("\n", i)
                    line = text[i:] if j < 0 else text[i:j]
                    i = n if j < 0 else j + 1
                    if (line.lstrip("\t") if strip_tabs else line) == word:
                        break
            heredocs = []
            out.append("\n")
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _substitutions(masked: str) -> list[tuple[int, str]]:
    """(start, content) of every ``$(...)`` (not ``$((``) and backtick substitution in the masked command."""
    found: list[tuple[int, str]] = []
    i, n = 0, len(masked)
    while i < n:
        if masked.startswith("$(", i) and not masked.startswith("$((", i):
            depth, j = 1, i + 2
            while j < n and depth:
                depth += {"(": 1, ")": -1}.get(masked[j], 0)
                j += 1
            found.append((i, masked[i + 2:j - 1] if depth == 0 else masked[i + 2:]))
            i = i + 2
            continue
        if masked[i] == "`":
            j = masked.find("`", i + 1)
            end = n if j < 0 else j
            found.append((i, masked[i + 1:end]))
            i = end + 1
            continue
        i += 1
    return found


def shell_is_zsh(env: Mapping[str, str], platform: str) -> bool:
    shell = env.get("CLAUDE_CODE_SHELL") or env.get("SHELL")
    if shell:
        return re.split(r"[\\/]", shell.strip())[-1].lower().startswith("zsh")
    return platform == "darwin"


def traps(command: str, cwd: str | None = None) -> list[tuple[str, str]]:
    """(trap id, variable name or glob word or '') for every trap in ``command``, first occurrence order, without
    repeats. The NOMATCH check needs the session's working directory and runs only when ``cwd`` is a directory."""
    masked = mask(command)
    found: list[tuple[int, str, str]] = []
    if not _WORD_SPLIT_ON.search(masked):
        for trap, pattern in ((TRAP_SET, _SET_RE), (TRAP_FOR, _FOR_RE)):
            for m in pattern.finditer(masked):
                name = m.group("a") or m.group("b")
                if name in ZSH_ARRAYS or _array_assigned(masked, name):
                    continue
                found.append((m.start(), trap, name))
    for start, content in _substitutions(masked):
        if not _GLOB_RE.search(content):
            continue
        seps = list(_SEPARATOR.finditer(masked, 0, start))
        segment = masked[seps[-1].end() if seps else 0:start]
        if _RUNNER_RE.search(segment):
            found.append((start, TRAP_GLOB, ""))
    if cwd and os.path.isdir(cwd):
        for start, word in nomatch_globs(command, cwd):
            found.append((start, TRAP_NOMATCH, word))
    result: list[tuple[str, str]] = []
    for _, trap, name in sorted(found):
        if (trap, name) not in result:
            result.append((trap, name))
    return result


def _base_dir(masked: str, start: int, cwd: str) -> str | None:
    """The directory a relative glob at ``start`` is matched in: the last plain ``cd DIR`` before it, else ``cwd``;
    None when that ``cd`` cannot be resolved here (an expansion or a quoted path)."""
    target = None
    for m in _CD_RE.finditer(masked, 0, start):
        target = m.group(1)
    if target is None:
        return cwd
    if _UNRESOLVED.search(target):
        return None
    return os.path.join(cwd, os.path.expanduser(target))


def nomatch_globs(command: str, cwd: str) -> list[tuple[int, str]]:
    """(offset, word) for every unquoted glob word that matches no file: zsh's NOMATCH error stops the whole
    command. Words zsh does not glob ([[ ]], (( )), case patterns, assignments) or that cannot be resolved here
    (quoted, expanded, braced, escaped, after an unresolvable cd) are skipped."""
    import glob

    masked = mask(command)
    if _GLOB_OFF.search(masked):
        return []
    blocked = [(m.start(), m.end()) for m in _NO_GLOB_SPANS.finditer(masked)]
    found: list[tuple[int, str]] = []
    for m in _WORD_RE.finditer(masked):
        word, start = m.group(0), m.start()
        if not _GLOB_RE.search(word) or _UNRESOLVED.search(word) or _ASSIGNMENT.match(word):
            continue
        if any(a <= start < b for a, b in blocked):
            continue
        pattern = os.path.expanduser(word)
        if not os.path.isabs(pattern):
            base = _base_dir(masked, start, cwd)
            if base is None:
                continue
            pattern = os.path.join(base, pattern)
        try:
            if not glob.glob(pattern):
                found.append((start, word))
        except (OSError, ValueError, re.error):
            continue
    return found


def deny_decision(found: list[tuple[str, str]]) -> dict[str, object]:
    reason = " | ".join(REASONS[trap].format(name=name or "NAME") for trap, name in found)
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


def decide(raw: str, env: Mapping[str, str] | None = None, platform: str | None = None) -> dict[str, object] | None:
    """The deny decision for the hook input ``raw``, or None to allow."""
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("tool_name") != "Bash":
        return None
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        return None
    if not shell_is_zsh(os.environ if env is None else env, sys.platform if platform is None else platform):
        return None
    cwd = data.get("cwd")
    found = traps(command, cwd if isinstance(cwd, str) else None)
    return deny_decision(found) if found else None


def main(read: Callable[[], bytes] | None = None) -> int:
    """Always 0: this hook never obstructs the session by failing."""
    try:
        raw = (read() if read else sys.stdin.buffer.read()).decode("utf-8", "replace")
        decision = decide(raw)
        if decision is not None:
            sys.stdout.write(json.dumps(decision))
            sys.stdout.flush()
    except BaseException:  # noqa: S110 - fail-open on anything, including a broken pipe
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
