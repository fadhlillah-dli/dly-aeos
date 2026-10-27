"""Command drift: what the docs say about commands and flags must exist in the code.

Scanned: CONTRIBUTING.md, README.md, docs/OPERATIONS.md, docs/OWNER_ACTIONS.md and docs/runbooks/*.md, fenced
code blocks (every line is a command, a trailing backslash joins lines) and inline code spans.

* ``scripts/<name>.py`` must exist, and every ``--flag`` written after it in the same command must be defined by
  that script. The flags are read with ``ast`` (``add_argument("--x")`` calls and ``"--x" in argv`` comparisons);
  a script is never imported or run.
* ``aeos <command> [<subcommand>]`` must be a registered CLI command, resolved by the shared resolver in
  ``tests/unit/docs_contract_support.py`` (the one the VS Code task test uses).
* ``EXEMPT`` lists a documented token that is deliberately not real, with the reason. An entry that matches
  nothing fails as stale, so the table cannot rot.

A failure names ``file:line`` and the bad token. Fix the doc; exempt only what is meant to be shown as wrong.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from aeos.core.paths import repo_root
from tests.unit.docs_contract_support import WORD, command_problem, registered_commands, script_flags

ROOT = repo_root()

DOC_FILES = ("CONTRIBUTING.md", "README.md", "docs/OPERATIONS.md", "docs/OWNER_ACTIONS.md")
DOC_GLOBS = ("docs/runbooks/*.md",)

# (doc path, bad token as printed in the failure) -> reason. Empty: every real drift was fixed in the docs.
EXEMPT: dict[tuple[str, str], str] = {}

FENCE = re.compile(r"^\s*(```+|~~~+)")
INLINE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
PROMPT = re.compile(r"^\s*(?:\$|PS>|PS [^>]*>|>|%)\s+")
COMMENT = re.compile(r"\s#(?:\s.*)?$")
CONTINUATION = ("\\", "`", "^")
SPLIT = re.compile(r"&&|\|\||;|\|")
SCRIPT = re.compile(r"^(?:\.[\\/])?scripts[\\/]([A-Za-z0-9_]+)\.py$")
AEOS = re.compile(r"^(?:\.[\\/])?aeos(?:\.ps1|\.cmd)?$")
FLAG = re.compile(r"(?<![\w-])--[A-Za-z][\w-]*")
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(frozen=True)
class Finding:
    doc: str
    line: int
    token: str
    message: str

    def render(self) -> str:
        return f"{self.doc}:{self.line}: {self.message}"


@dataclass(frozen=True)
class Command:
    """One logical command: the text (continuations joined) and the doc line of every character."""

    text: str
    lines: tuple[int, ...]


def _join(parts: list[tuple[int, str]]) -> Command:
    text, lines = "", []
    for no, part in parts:
        if text:
            text += " "
            lines.append(no)
        text += part
        lines.extend([no] * len(part))
    return Command(text, tuple(lines))


def code_commands(markdown: str) -> list[Command]:
    """Fenced block lines (continuations joined) and inline code spans, as commands with line numbers."""
    out: list[Command] = []
    fence: str | None = None
    pending: list[tuple[int, str]] = []
    for no, raw in enumerate(markdown.splitlines(), start=1):
        m = FENCE.match(raw)
        if m and (fence is None or m.group(1)[0] == fence[0]):
            if pending:
                out.append(_join(pending))
                pending = []
            fence = None if fence else m.group(1)
            continue
        if fence is not None:
            line = PROMPT.sub("", raw).strip()
            if not pending and (not line or line.startswith("#")):
                continue
            line = COMMENT.sub("", line).rstrip()
            cont = line.endswith(CONTINUATION)
            pending.append((no, line[:-1].rstrip() if cont else line))
            if not cont:
                out.append(_join(pending))
                pending = []
            continue
        for span in INLINE.finditer(raw):
            out.append(Command(span.group(2).strip(), (no,) * len(span.group(2).strip())))
    if pending:
        out.append(_join(pending))
    return out


def _pieces(text: str) -> list[tuple[int, str]]:
    """Split on shell operators (not on an escaped table pipe ``\\|``); keep each piece's offset."""
    masked = text.replace("\\|", "\x00\x01")
    pieces, start = [], 0
    for m in SPLIT.finditer(masked):
        pieces.append((start, masked[start:m.start()]))
        start = m.end()
    pieces.append((start, masked[start:]))
    return pieces


def _unquote(token: str) -> str:
    return token.strip("\"'")


def scan(docs: Mapping[str, str], scripts_dir: Path, commands: set[str]) -> list[Finding]:
    """Every drift in ``docs`` (relative path -> markdown text) against the scripts and the CLI registry."""
    findings: list[Finding] = []
    for doc, markdown in docs.items():
        for cmd in code_commands(markdown):
            for offset, piece in _pieces(cmd.text):
                tokens = [(m.start() + offset, m.group()) for m in re.finditer(r"\S+", piece)]
                while tokens and ENV_ASSIGN.match(tokens[0][1]):
                    tokens.pop(0)
                if tokens and AEOS.match(_unquote(tokens[0][1])):
                    words = []
                    for _, tok in tokens[1:]:
                        if not WORD.match(tok):
                            break
                        words.append(tok)
                    bad = command_problem(words, commands)
                    if bad:
                        line = cmd.lines[min(tokens[0][0], len(cmd.lines) - 1)]
                        findings.append(Finding(doc, line, bad, f"{bad!r} is not a registered aeos command"))
                script: str | None = None
                for pos, tok in tokens:
                    line = cmd.lines[min(pos, len(cmd.lines) - 1)]
                    m = SCRIPT.match(_unquote(tok))
                    if m:
                        script = f"scripts/{m.group(1)}.py"
                        if not (scripts_dir / f"{m.group(1)}.py").is_file():
                            findings.append(Finding(doc, line, script, f"{script} does not exist"))
                            script = None
                        continue
                    if script is None:
                        continue
                    for flag in FLAG.findall(tok):
                        if flag not in script_flags(scripts_dir / Path(script).name):
                            findings.append(Finding(doc, line, f"{script} {flag}",
                                                    f"{flag} is not a flag of {script}"))
    return findings


def check(docs: Mapping[str, str], scripts_dir: Path, commands: set[str],
          exempt: Mapping[tuple[str, str], str]) -> list[str]:
    """Problems as printable lines: drift not exempted, plus every exemption that matched nothing."""
    findings = scan(docs, scripts_dir, commands)
    used = {(f.doc, f.token) for f in findings}
    problems = [f.render() for f in findings if (f.doc, f.token) not in exempt]
    for (doc, token), reason in exempt.items():
        if not reason.strip():
            problems.append(f"exemption {doc!r} {token!r} has no reason")
        if (doc, token) not in used:
            problems.append(f"stale exemption {doc!r} {token!r}: it matches nothing, delete it")
    return problems


def _real_docs() -> dict[str, str]:
    paths = [ROOT / p for p in DOC_FILES]
    for pattern in DOC_GLOBS:
        paths.extend(sorted(ROOT.glob(pattern)))
    return {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8") for p in paths}


def test_docs_commands_flags_and_cli_exist():
    docs = _real_docs()
    assert "CONTRIBUTING.md" in docs and any(d.startswith("docs/runbooks/") for d in docs)
    problems = check(docs, ROOT / "scripts", registered_commands(), EXEMPT)
    assert not problems, "documented commands that do not exist in the code:\n" + "\n".join(problems)


def test_docs_actually_contain_commands_to_check():
    """Guards the scanner itself: it must keep finding script flags and aeos commands in the real docs."""
    docs = _real_docs()
    scripts = aeos_cmds = flags = 0
    for doc, text in docs.items():
        for cmd in code_commands(text):
            scripts += len(re.findall(r"scripts/[A-Za-z0-9_]+\.py", cmd.text))
            flags += bool(re.search(r"scripts/[A-Za-z0-9_]+\.py[^|;&]*--[a-z]", cmd.text))
            aeos_cmds += bool(re.match(r"^(\./|\.\\)?aeos(\.ps1|\.cmd)? [a-z]", cmd.text))
    assert scripts >= 5 and flags >= 3 and aeos_cmds >= 5, (scripts, flags, aeos_cmds)


# --- bite tests: a violating fixture is red, a near miss is green ---------------------------------------------------

COMMANDS = {"doctor", "admin", "admin create", "serve"}


def _bad(md: str, exempt: Mapping[tuple[str, str], str] | None = None) -> list[str]:
    return check({"docs/x.md": md}, ROOT / "scripts", COMMANDS, exempt or {})


def test_bite_misspelled_flag_is_red_with_file_line_and_token():
    md = "Intro\n\n```bash\npython scripts/changelog.py --assmble\n```\n"
    problems = _bad(md)
    assert len(problems) == 1
    assert "docs/x.md:4:" in problems[0] and "--assmble" in problems[0] and "scripts/changelog.py" in problems[0]


def test_bite_inline_code_and_continuation_lines_are_scanned():
    inline = _bad("Run `python scripts/changelog.py --assmble` first.\n")
    assert len(inline) == 1 and "docs/x.md:1:" in inline[0] and "--assmble" in inline[0]
    joined = _bad("```bash\npython scripts/changelog.py \\\n    --dry-run --nope\n```\n")
    assert len(joined) == 1 and "docs/x.md:3:" in joined[0] and "--nope" in joined[0]


def test_near_miss_real_flags_are_green():
    assert (ROOT / "scripts" / "changelog.py").is_file()
    flags = script_flags(ROOT / "scripts" / "changelog.py")
    assert {"--assemble", "--dry-run", "--check"} <= flags
    md = ("```bash\npython scripts/changelog.py --assemble --dry-run\npython scripts/changelog.py --check\n```\n"
          "Also `python scripts/changelog.py --assemble --dry-run` inline, and a flag of another tool: "
          "`git push --no-verify`, and a flag before the script: `python -X dev scripts/changelog.py --check`.\n")
    assert _bad(md) == []


def test_bite_missing_script_is_red():
    problems = _bad("`python scripts/no_such_tool.py --check`\n")
    assert len(problems) == 1 and "scripts/no_such_tool.py" in problems[0] and "docs/x.md:1:" in problems[0]


def test_bite_unknown_aeos_command_is_red_and_near_misses_are_green():
    problems = _bad("```bash\n./aeos no-such-command\n```\n")
    assert len(problems) == 1 and "docs/x.md:2:" in problems[0] and "no-such-command" in problems[0]
    sub = _bad("`aeos admin nonsense`\n")
    assert len(sub) == 1 and "aeos admin nonsense" in sub[0]
    green = ("`aeos doctor`, `aeos admin create`, `aeos serve --dev`, `aeos.yaml`, `aeos <cmd>`, `./aeos`, "
             "`aeos admin <sub>`, `aeos serve [--release auto\\|active]`\n```\n.\\aeos.ps1 doctor\n```\n")
    assert _bad(green) == []


def test_the_real_registry_resolves_like_the_bite_registry():
    commands = registered_commands()
    assert command_problem(["admin", "create"], commands) is None
    assert command_problem(["admin", "no-such"], commands) == "aeos admin no-such"
    assert command_problem(["no-such-command"], commands) == "aeos no-such-command"
    assert command_problem([], commands) is None


def test_exemption_silences_only_its_own_token_and_a_stale_one_is_red():
    md = "`python scripts/changelog.py --assmble`\n"
    exempt = {("docs/x.md", "scripts/changelog.py --assmble"): "shown as the typo this tool must reject"}
    assert _bad(md, exempt) == []
    stale = _bad("`python scripts/changelog.py --check`\n", exempt)
    assert len(stale) == 1 and "stale exemption" in stale[0] and "--assmble" in stale[0]
    other = _bad("`python scripts/changelog.py --other`\n", exempt)
    assert any("--other" in p for p in other) and any("stale exemption" in p for p in other)
    assert any("no reason" in p for p in _bad(md, {("docs/x.md", "scripts/changelog.py --assmble"): " "}))


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_fences_of_both_kinds_are_scanned(fence):
    assert len(_bad(f"{fence}\npython scripts/changelog.py --assmble\n{fence}\n")) == 1
