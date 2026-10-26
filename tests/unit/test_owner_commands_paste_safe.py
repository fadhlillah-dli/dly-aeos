"""Owner-facing shell commands must be paste-safe in zsh (incident 2026-10-26).

zsh does not treat ``#`` as a comment at an interactive prompt, so a pasted ``cmd  # note`` passes
``#`` and ``note`` to the command, and an unquoted ``.[dev]`` stops with ``no matches found``
(``nomatch``). Every fenced bash/sh/zsh/shell/console block in the owner-facing Markdown must
therefore carry no ``#`` comment (put the explanation in the prose before the block) and no
unquoted glob character (``[ ] ? *``) in any word. A block that shows file contents instead of
commands opts out with ``<!-- paste-safe: file-content -->`` directly above it. powershell, cmd,
text, yaml and json blocks are not scanned.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aeos.core.paths import repo_root

ROOT = repo_root()
SHELL_LANGS = frozenset({"bash", "sh", "zsh", "shell", "console"})
OPT_OUT = "<!-- paste-safe: file-content -->"
# CommonMark fence: any number of blockquote markers and list markers (nested lists, a list inside a quote and a
# quote inside a list), then 3+ backticks or tildes.
FENCE = re.compile(r"^[ \t]*(?:(?:>[ \t]?|(?:[-*+]|\d{1,9}[.)])[ \t]+)[ \t]*)*(`{3,}|~{3,})[ \t]*(.*)$")
CLOSE_PREFIX = re.compile(r"^[ \t]*(?:>[ \t]?)*[ \t]*")
PROMPT = re.compile(r"^\s*[$%] ")
HEREDOC = re.compile(r"<<(-?)[ \t]*([\"']?)([A-Za-z_][A-Za-z0-9_]*)\2")
WORD_START_PREV = " \t;&|("
GLOB_CHARS = "[]?*"
# Untagged fences are scanned too when their first line starts like a shell command (incident class:
# an untagged "Reproduce" block that carried the exact pasted command).
COMMAND_START = re.compile(r"^\s*(?:[$%] )?(?:\./|\.\./|~/)?(?:aeos|pip3?|python3?|npm|npx|git|gh|cd|brew|curl|make|"
                           r"source|export|sh|bash|zsh|uv|node|pytest|ruff|mypy)(?:\s|$)")


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


def paste_problems(markdown: str) -> list[tuple[int, str, str]]:
    """Return (line number, line, problem) for every non paste-safe line in shell fences."""
    out: list[tuple[int, str, str]] = []
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
                out += [(idx + 2 + off, raw, p) for p in probs]
        idx = end + 1
    return out


NOT_REPOSITORY_FILES = frozenset({".git", ".venv", "node_modules", "dist", "build", "__pycache__", ".mypy_cache",
                                  ".ruff_cache", ".pytest_cache"})


def owner_markdown_files() -> list[Path]:
    """Every Markdown file of the repository (an owner reads a README, a runbook or a fragment alike); folders that
    hold installed or generated files are skipped."""
    return sorted(p for p in ROOT.rglob("*.md")
                  if p.is_file() and not NOT_REPOSITORY_FILES & set(p.relative_to(ROOT).parts))


def test_owner_markdown_shell_blocks_are_paste_safe():
    findings = []
    for path in owner_markdown_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line, problem in paste_problems(text):
            findings.append(f"{path.relative_to(ROOT).as_posix()}:{lineno}: {problem}: {line.strip()}")
    assert not findings, "not paste-safe in zsh:\n" + "\n".join(findings)


def test_scanner_covers_the_owner_documents():
    names = {p.relative_to(ROOT).as_posix() for p in owner_markdown_files()}
    assert {"README.md", "CONTRIBUTING.md", "SECURITY.md", "docs/OWNER_ACTIONS.md", "docs/SETUP_MACOS.md",
            "changelog.d/README.md", "install-manifest/README.md", "tests/acceptance/MATRIX.md"} <= names
    assert not {n for n in names if NOT_REPOSITORY_FILES & set(n.split("/"))}


def _block(body: str, lang: str = "bash", before: str = "") -> str:
    return f"{before}\n```{lang}\n{body}\n```\n"


# ----------------------------------------------------------------------------- bite tests: red
@pytest.mark.parametrize(
    "body, fragment",
    [
        ("./aeos doctor  # x", "'#' comment"),
        ("# comment", "'#' comment"),
        ("$ ./aeos doctor  # x", "'#' comment"),
        ("% ./aeos doctor # x", "'#' comment"),
        ("pip install -e .[dev]", "glob character '['"),
        ("ls *.md", "glob character '*'"),
        ("ls file?.txt", "glob character '?'"),
        ("echo 'a' # note 'b'", "'#' comment"),
        ("echo \"multi\nline\" # tail", "'#' comment"),
    ],
)
@pytest.mark.parametrize("lang", ["bash", "sh", "zsh", "shell", "console"])
def test_violating_commands_are_red(body, fragment, lang):
    found = paste_problems(_block(body, lang))
    assert found, body
    assert any(fragment in problem for _, _, problem in found)


def test_violation_after_a_comment_marker_for_another_block_is_still_red():
    text = _block("echo ok", before=OPT_OUT) + _block("./aeos doctor  # x")
    assert len(paste_problems(text)) == 1


# --------------------------------------------------------------------------- bite tests: green
@pytest.mark.parametrize(
    "body",
    [
        'echo "a#b"',
        "echo 'a # b'",
        'pip install -e ".[dev]"',
        "pip install -e '.[dev]'",
        'open "https://example.invalid/page#anchor"',
        'echo "${#PATH}" ${#HOME}',
        'ls "*.md" \\*.md',
        "echo $? $# ok",
        "$ ./aeos doctor",
        "./aeos vault install --version <X.Y.Z>",
        'git commit -m "fix: a # b"',
        'echo "multi\nline # inside" done',
        "cat <<'EOF'\n# file content [x] *\nEOF",
        "echo a#b",
    ],
)
def test_near_miss_legitimate_commands_are_green(body):
    assert paste_problems(_block(body)) == []


@pytest.mark.parametrize("lang", ["powershell", "cmd", "text", "yaml", "json", "python", "", "pwsh"])
def test_other_block_kinds_are_not_scanned(lang):
    assert paste_problems(_block("Get-Item .\\*.md  # list\npip install -e .[dev]", lang)) == []


def test_file_content_marker_exempts_only_the_next_block():
    text = _block("# a config file\nkey: [1, 2]", before=OPT_OUT)
    assert paste_problems(text) == []
    assert paste_problems(_block("# a config file")) != []


def test_console_output_lines_are_not_commands():
    text = _block("$ ./aeos doctor\nchecks [ok] * done # fine", "console")
    assert paste_problems(text) == []


def test_tilde_fences_and_longer_fences_are_handled():
    text = "~~~bash\nls *.md\n~~~\n\n````bash\n./aeos x  # y\n````\n"
    assert len(paste_problems(text)) == 2


# ------------------------------------------- bite tests: fence recognition (verifier defects)
def test_fence_inside_a_list_item_does_not_desynchronise_later_fences():
    text = "1. ```bash\n   echo ok\n   ```\n\n```bash\n./aeos serve  # x\n```\n"
    found = paste_problems(text)
    assert [ln for ln, _, _ in found] == [6]


def test_violation_inside_a_list_item_fence_is_red():
    for marker in ("- ", "* ", "+ ", "5. ", "5) "):
        text = f"{marker}```bash\n   ./aeos serve  # x\n   ```\n"
        assert len(paste_problems(text)) == 1, marker


@pytest.mark.parametrize("opener, indent", [("1. - ", "     "), ("- > ", "  > "), ("> - ", "> "), ("- - ", "    "),
                                            ("1) * ", "     ")])
def test_nested_container_fences_are_scanned_and_do_not_desynchronise(opener, indent):
    violating = f"{opener}```bash\n{indent}./aeos serve  # x\n{indent}```\n"
    assert len(paste_problems(violating)) == 1, opener
    # the closer of a nested fence must not be read as an opener that pairs with the next top-level block
    desync = f"{opener}```bash\n{indent}echo ok\n{indent}```\n\n```bash\n./aeos serve  # x\n```\n"
    assert [ln for ln, _, _ in paste_problems(desync)] == [6], opener
    assert paste_problems(f"{opener}```bash\n{indent}./aeos serve\n{indent}```\n") == []  # near miss


def test_blockquote_fence_is_scanned():
    assert len(paste_problems("> ```bash\n> ./aeos serve  # x\n> ```\n")) == 1


def test_list_item_fence_near_miss_is_green():
    assert paste_problems("1. Run it:\n   ```bash\n   ./aeos serve\n   ```\n") == []


def test_closing_fence_must_be_bare_and_at_least_as_long():
    # a ```bash line inside a block is content, not a closer; a shorter fence does not close either
    text = "````bash\n```bash\n./aeos x  # y\n```\n````\n"
    assert len(paste_problems(text)) == 1
    # an unclosed fence runs to the end of the document and is still scanned
    assert len(paste_problems("```bash\n./aeos x  # y\n")) == 1


def test_untagged_fence_with_a_shell_command_is_scanned():
    assert len(paste_problems("```\npip install -e .[dev]  # x\n```\n")) == 3
    assert len(paste_problems("```\n$ ./aeos doctor  # x\n```\n")) == 1


def test_untagged_fence_that_is_not_a_command_is_green():
    assert paste_problems("```\nflowchart [a] --> [b] # note\n```\n") == []
    assert paste_problems("```\n# Title\nkey: [1]\n```\n") == []
    assert paste_problems('```\npip install -e ".[dev]"\n```\n') == []


# ------------------------------------------- bite tests: heredoc and here-string detection
@pytest.mark.parametrize(
    "body",
    [
        'cat <<< "x"\n./aeos doctor  # x',
        'echo "use <<EOF"\n./aeos doctor  # x',
        "echo 'use <<EOF'\n./aeos doctor  # x",
        "cat <<< x <<<y\n./aeos doctor  # x",
        "cat <<EOF\nbody\nEOF\n./aeos doctor  # x",
        "cat <<-'EOF' | tee f\nbody\n\tEOF\n./aeos doctor  # x",
    ],
)
def test_here_string_and_quoted_heredoc_markers_do_not_hide_later_violations(body):
    assert len(paste_problems(_block(body))) == 1, body


def test_heredoc_body_is_still_exempt_and_comment_after_the_marker_is_red():
    assert paste_problems(_block("cat <<'EOF'\n# keep [x] *\nEOF")) == []
    assert paste_problems(_block('cat <<EOF # c\nbody\nEOF')) != []


# ------------------------------------------- bite tests: console continuation lines
def test_console_continuation_lines_are_commands():
    text = _block("$ ./aeos doctor \\\n  --x # y", "console")
    assert len(paste_problems(text)) == 1


def test_console_output_after_a_finished_command_stays_exempt():
    text = _block("$ ./aeos doctor \\\n  --x\nchecks [ok] * done # fine", "console")
    assert paste_problems(text) == []
