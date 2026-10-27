"""Owner-facing shell commands must be paste-safe in zsh (LESSONS L2, L21).

The rules live in one place, ``scripts/paste_safe.py`` (single owner, L10/L17); this test imports that checker and
runs it over every Markdown file of the repository, and holds its bite tests: a violating fixture is red for the
right reason (its own message) and a near-miss legitimate fixture is green. Rules: no ``#`` comment, no unquoted
glob character, no ``rm`` after a ``cp`` outside ``cp && cmp && rm``, no line longer than 80 characters, a command
that reads an answer from the terminal (a hidden password or a typed confirmation) is the only one of its block and
its last line, and no ``<placeholder>`` outside quotes. The lists of CLI commands with a hidden prompt and with a
typed confirmation are derived from ``backend/aeos`` here and compared with the checker's lists.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import io
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from aeos.core.paths import repo_root

ROOT = repo_root()
CHECKER = ROOT / "scripts" / "paste_safe.py"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("aeos_script_paste_safe", CHECKER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ps = _load_checker()
FENCE = ps.FENCE
OPT_OUT = ps.OPT_OUT
MAX_LINE = ps.MAX_LINE
check_markdown = ps.check_markdown
paste_problems = ps.paste_problems
prompt_problems = ps.prompt_problems
shell_fences = ps.shell_fences
unguarded_deletes = ps.unguarded_deletes

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
        for lineno, line, problem in check_markdown(path.read_text(encoding="utf-8")):
            findings.append(f"{path.relative_to(ROOT).as_posix()}:{lineno}: {problem}: {line.strip()}")
    assert not findings, "not paste-safe in zsh:\n" + "\n".join(findings)


def test_owner_markdown_never_deletes_after_an_unproven_copy():
    """Data-loss class: the offline move of the OpenBao recovery copy (and any other cp-then-rm) deletes the original
    only in 'cp SRC DST && cmp SRC DST && rm SRC'."""
    findings = []
    for path in owner_markdown_files():
        for lineno, command in unguarded_deletes(path.read_text(encoding="utf-8")):
            findings.append(f"{path.relative_to(ROOT).as_posix()}:{lineno}: rm without a proven copy: {command}")
    assert not findings, "a delete runs even when the copy failed:\n" + "\n".join(findings)


RECOVERY_RUNBOOK = "docs/runbooks/openbao-unseal-recovery.md"


def test_recovery_runbook_moves_the_copy_with_a_guarded_chain():
    text = (ROOT / RECOVERY_RUNBOOK).read_text(encoding="utf-8")
    blocks = ["\n".join(body) for _, _, body in shell_fences(text)]
    move = [b for b in blocks if "cp " in b and "rm " in b]
    assert len(move) == 1 and "&& cmp " in move[0] and "&& rm " in move[0], move
    assert "/Volumes/AEOS-RECOVERY/" in move[0] and "your own encrypted volume" in text  # the name is the owner's


OWNER_ACTIONS = "docs/OWNER_ACTIONS.md"
WINDOWS_NOTES = "## 5. Windows notes"


def windows_continuation_problems(text: str) -> list[str]:
    """OWNER_ACTIONS says every command applies on Windows with the differences of its section 5: when a shell block
    continues a line with a trailing backslash, section 5 must map it to '^' (cmd.exe) and a backtick (PowerShell),
    which is how those shells continue a line."""
    continued = [line for _idx, _lang, body in shell_fences(text) for line in body if line.rstrip().endswith("\\")]
    notes = text.split(WINDOWS_NOTES, 1)[1] if WINDOWS_NOTES in text else ""
    if continued and not ("`^`" in notes and "backtick" in notes):
        return [f"{len(continued)} shell line(s) end with a backslash continuation but '{WINDOWS_NOTES}' does not map "
                "it to '^' (cmd.exe) and a backtick (PowerShell)"]
    return []


def test_windows_notes_map_the_line_continuation():
    assert windows_continuation_problems((ROOT / OWNER_ACTIONS).read_text(encoding="utf-8")) == []


def test_bite_windows_notes_without_the_continuation_mapping_are_red():
    block = _block("docker build --build-arg V=1 \\\n  -t img sandbox/")
    red = windows_continuation_problems(block + f"\n{WINDOWS_NOTES}\n\nThe docker build lines are the same.\n")
    assert red == [f"1 shell line(s) end with a backslash continuation but '{WINDOWS_NOTES}' does not map it to "
                   "'^' (cmd.exe) and a backtick (PowerShell)"]
    mapped = f"\n{WINDOWS_NOTES}\n\n| `\\` | `^` | `` ` `` (backtick) |\n"
    assert windows_continuation_problems(block + mapped) == []
    assert windows_continuation_problems(_block("docker build -t img sandbox/") + f"\n{WINDOWS_NOTES}\n") == []


def test_scanner_covers_the_owner_documents():
    names = {p.relative_to(ROOT).as_posix() for p in owner_markdown_files()}
    assert {"README.md", "CONTRIBUTING.md", "SECURITY.md", "docs/OWNER_ACTIONS.md", "docs/SETUP_MACOS.md",
            "changelog.d/README.md", "install-manifest/README.md", "tests/acceptance/MATRIX.md"} <= names
    assert not {n for n in names if NOT_REPOSITORY_FILES & set(n.split("/"))}


def test_the_rules_have_one_owner():
    """L10/L17: the test holds no second copy of the rules; it uses the checker's functions."""
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert not defined & {"_scan_line", "_is_closing", "_command_head", "paste_problems", "shell_fences",
                          "unguarded_deletes", "prompt_problems"}
    assert paste_problems.__module__ == "aeos_script_paste_safe"


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
        './aeos vault install --version "<X.Y.Z>"',
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


@pytest.mark.parametrize("hostile", [">" + "\t>" * 40 + "x", "*" + "\t\t*" * 40 + "x"])
def test_fence_regex_is_linear_on_hostile_prefixes(hostile: str) -> None:
    """CodeQL py/redos on the earlier FENCE regex: these inputs backtracked exponentially."""
    import time

    start = time.perf_counter()
    assert FENCE.match(hostile) is None
    assert time.perf_counter() - start < 0.5


# ------------------------------------------- bite tests: delete after an unproven copy (data-loss class)
SRC, DST = '"$HOME/aeos-openbao-recovery.txt"', '"/Volumes/AEOS-RECOVERY/aeos-openbao-recovery.txt"'


@pytest.mark.parametrize(
    "body",
    [
        f"cp {SRC} {DST}\nrm {SRC}",  # the block this runbook carried before: rm runs when cp failed
        f"cp {SRC} {DST}; rm {SRC}",
        f"cp {SRC} {DST} && rm {SRC}",  # the copy may be short or on the wrong volume: nothing compared it
        f"cp {SRC} {DST} || rm {SRC}",
        f"cp {SRC} {DST} && cmp {SRC} {DST}\nrm {SRC}",
        f"cp {SRC} {DST} && cmp {SRC} {DST}; rm {SRC}",
        f"$ cp {SRC} {DST}\n$ rm {SRC}",
        f"cp {SRC} {DST} && sudo rm {SRC}",
        f"cp {SRC} {DST} && \\rm {SRC}",
        f"cp {SRC} {DST} && /bin/rm {SRC}",
        f"cp {SRC} {DST} && command rm {SRC}",
    ],
)
def test_unguarded_delete_after_copy_is_red(body):
    found = unguarded_deletes(_block(body, "console" if body.startswith("$") else "bash"))
    assert found and all("rm " in cmd for _, cmd in found), body
    assert any("rm without a proven copy" in p for _, _, p in check_markdown(_block(body)))


def test_old_runbook_block_is_red_in_the_runbook_scan():
    old = _block(f"cp {SRC} {DST}\nrm {SRC}")
    assert [ln for ln, _ in unguarded_deletes("# Runbook\n" + old)] == [5]  # the rm line


@pytest.mark.parametrize(
    "body",
    [
        f"cp {SRC} {DST} && cmp {SRC} {DST} && rm {SRC}",
        f"cp {SRC} {DST} \\\n  && cmp {SRC} {DST} \\\n  && rm {SRC}",
        f"cp {SRC} {DST} && diff {SRC} {DST} && rm {SRC}",
        f"rm {SRC}",  # no copy in the block: not a move
        f"rm {SRC}\ncp {SRC} {DST}",
        f'SRC={SRC}\nDST={DST}\ncp "$SRC" "$DST" && cmp "$SRC" "$DST" && rm "$SRC"',
    ],
)
def test_guarded_or_unrelated_delete_is_green(body):
    assert unguarded_deletes(_block(body)) == [], body


def test_delete_in_another_fence_kind_is_not_scanned():
    assert unguarded_deletes(_block(f"cp {SRC} {DST}\nrm {SRC}", "text")) == []


# ------------------------------------------- bite tests: long lines (wrap risk, incident 2026-10-27)
def _line(width: int) -> str:
    head = "./aeos doctor --reason "
    return head + '"' + "x" * (width - len(head) - 2) + '"'


def test_an_81_character_line_is_red_with_its_fix():
    line = _line(MAX_LINE + 1)
    assert len(line) == 81
    found = paste_problems(_block(line))
    assert [ln for ln, _, _ in found] == [3]
    assert "line is 81 characters (more than 80)" in found[0][2] and "trailing backslash" in found[0][2]


def test_an_80_character_line_is_green():
    line = _line(MAX_LINE)
    assert len(line) == 80
    assert check_markdown(_block(line)) == []


def test_line_length_ignores_the_container_indent_but_counts_the_command():
    assert check_markdown("1. Run:\n\n   ```bash\n   " + _line(80) + "\n   ```\n") == []
    assert check_markdown("> ```bash\n> " + _line(80) + "\n> ```\n") == []
    assert len(paste_problems("1. Run:\n\n   ```bash\n   " + _line(81) + "\n   ```\n")) == 1


def test_a_long_line_split_with_backslashes_or_after_a_pipe_is_green():
    assert check_markdown(_block("./aeos policy decide --kind BUDGET \\\n  --rationale \"" + "x" * 60 + "\"")) == []
    assert check_markdown(_block("grep -q 'a' \"$CFG\" ||\n  printf 'b' >> \"$CFG\"")) == []


def test_a_long_heredoc_body_line_and_console_output_are_green():
    assert check_markdown(_block("cat <<'EOF' > f.txt\n" + "y" * 120 + "\nEOF")) == []
    assert check_markdown(_block("$ ./aeos doctor\n" + "z" * 120, "console")) == []


# ------------------------------------------- bite tests: hidden prompts (incident 2026-10-27)
ADMIN = '"<admin>"'


def test_two_as_commands_in_one_block_are_red():
    body = f"./aeos policy adrs --load\n./aeos policy decide --as {ADMIN}\n./aeos routes set-pool r --as {ADMIN}"
    found = prompt_problems(_block(body))
    assert any("2 commands in this block prompt for input (lines 4, 5)" in p for _, _, p in found), found


def test_an_as_command_followed_by_another_line_is_red():
    found = prompt_problems(_block(f"./aeos policy decide --as {ADMIN}\necho x"))
    assert len(found) == 1
    lineno, _line_text, problem = found[0]
    assert lineno == 3
    assert "'--as' asks for the admin password and line 4 follows it in the same block" in problem
    assert "end the block with this command" in problem


@pytest.mark.parametrize(
    "body, fragment",
    [
        ("./aeos admin create\n./aeos serve", "'aeos admin create' asks for hidden input"),
        ("./aeos vault import-unseal-key --shares 1 --with-approle\n./aeos vault status",
         "'aeos vault import-unseal-key' asks for hidden input"),
        ("./aeos vault unseal\n./aeos vault status", "'aeos vault unseal' asks for hidden input"),
        ("./aeos secrets set workers/x\n./aeos secrets status", "'aeos secrets set' asks for hidden input"),
        ("./aeos secrets set workers/x --stdin\n./aeos secrets status", "'aeos secrets set' asks"),  # reads the paste
        ("./aeos policy decide --as=admin\n./aeos doctor", "'--as' asks for the admin password"),
        ("FOO=1 aeos routes activate r --as admin \\\n  --mode TRIAL\n./aeos doctor", "'--as' asks"),
        ("$ ./aeos policy decide --as admin\nPassword for admin:\n$ ./aeos doctor", "'--as' asks"),
        # typed confirmations read a pasted line like a password (review finding on docs/OWNER_ACTIONS.md 1.14)
        ('./aeos ops release accept --commit "$C" --operator "$A" \\\n  --reason "ci green"\n'
         "./aeos ops release build --ref HEAD", "'aeos ops release accept' asks for a typed confirmation"),
        ('./aeos ops release promote "$R" --operator "$A"\n./aeos ops release list',
         "'aeos ops release promote' asks for a typed confirmation"),
        ('./aeos ops release rollback --operator "$A"\n./aeos doctor', "'aeos ops release rollback' asks"),
        ('./aeos ops backup restore "$B"\n./aeos doctor -g RECOVERY', "'aeos ops backup restore' asks"),
        ('./aeos ops emergency-stop --reason "leak"\n./aeos doctor', "'aeos ops emergency-stop' asks"),
        ("./aeos secrets delete workers/x\n./aeos secrets status", "'aeos secrets delete' asks"),
        ('./aeos vault install --version "$V"\n./aeos vault start', "'aeos vault install' asks"),
        ("./aeos vault init --yes --show-unseal-once\n./aeos vault status", "'aeos vault init' asks"),
    ],
)
def test_a_hidden_prompt_command_that_is_not_last_is_red(body, fragment):
    found = prompt_problems(_block(body, "console" if body.startswith("$") else "bash"))
    assert any(fragment in p and "follows it in the same block" in p for _, _, p in found), found


@pytest.mark.parametrize(
    "body",
    [
        f'ADMIN={ADMIN}\nWHY="first route"\n./aeos policy decide --as "$ADMIN" \\\n  --rationale "$WHY"',
        "./aeos vault start\n./aeos vault unseal",
        "./aeos vault unseal --from-keyring\n./aeos vault status",
        "printf '%s' \"$V\" | ./aeos secrets set workers/x --stdin\n./aeos secrets status",
        "./aeos admin recover owner\n./aeos doctor",
        "./aeos doctor --assume x\n./aeos serve",
        "git log --as\n./aeos doctor",
        f"$ ./aeos policy decide --as {ADMIN}\nPassword for admin:\nRecorded.",
        './aeos ops release build --ref HEAD\n./aeos ops release accept --commit "$C" --operator "$A" \\\n'
        '  --reason "ci green"',
        './aeos ops release accept --commit "$C" --operator "$A" --reason "ci" --yes\n./aeos ops release build',
        './aeos ops backup restore-test "$B"\n./aeos doctor -g RECOVERY',
        "./aeos vault start\n./aeos vault init",
        "./aeos secrets delete workers/x --yes\n./aeos secrets status",
    ],
)
def test_one_hidden_prompt_command_as_the_last_line_is_green(body):
    assert prompt_problems(_block(body, "console" if body.startswith("$") else "bash")) == [], body


def test_the_release_block_of_the_review_finding_is_red_and_its_split_is_green():
    accept = ('./aeos ops release accept --commit "$(git rev-parse HEAD)" \\\n  --operator "<admin>" \\\n'
              '  --reason "GitHub Actions CI green on main for this commit"')
    old = _block(f"./aeos ops release build --ref HEAD\n{accept}\n./aeos ops release build --ref HEAD")
    found = check_markdown(old)
    assert [ln for ln, _, _ in found] == [4], found
    assert "line 7 follows it in the same block" in found[0][2] and "as the answer" in found[0][2]
    assert check_markdown(_block(f"./aeos ops release build --ref HEAD\n{accept}")) == []


def test_a_hidden_prompt_inside_a_heredoc_body_is_not_a_command():
    assert prompt_problems(_block("cat <<'EOF' > steps.txt\n./aeos admin create\n./aeos serve\nEOF\necho done")) == []


# ------------------------------------------- bite tests: placeholders outside quotes
@pytest.mark.parametrize("body, shown", [
    ("./aeos secrets set personal/github --token <your-token>", "<your-token>"),
    ("./aeos ops backup restore-test <backup_id>", "<backup_id>"),
    ("rm ~/Downloads/client_secret_<...>.json", "<...>"),
    ("./aeos ops sli target x --max <new hash>", "<new hash>"),
])
def test_a_placeholder_outside_quotes_is_red(body, shown):
    found = paste_problems(_block(body))
    assert any(f"placeholder '{shown}' outside quotes" in p and "redirection" in p for _, _, p in found), found


@pytest.mark.parametrize("body", [
    './aeos ops backup restore-test "<x>"',
    "./aeos ops backup restore-test '<x>'",
    "./aeos inventory show \"<id>\" --json",
    "cat <<'EOF' > f.txt\nkey: <x>\nEOF",
    "sort < in.txt > out.txt",
    "sort <in.txt >out.txt",
    "cat <<< \"<x>\"",
    "echo 1 2>&1",
])
def test_a_quoted_placeholder_a_heredoc_body_and_a_redirection_are_green(body):
    assert paste_problems(_block(body)) == [], body


# ------------------------------------------- the CLI: --check, stdin, --text, exit codes, verdict line
def _run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, list[str]]:
    code = ps.main(argv)
    out = capsys.readouterr().out.splitlines()
    return code, out


def test_check_cli_exit_codes_and_verdict_line(tmp_path, capsys, monkeypatch):
    good = tmp_path / "good.md"
    good.write_text(_block("./aeos doctor"), encoding="utf-8")
    bad = tmp_path / "bad.md"
    bad.write_text(_block("./aeos doctor  # x"), encoding="utf-8")
    code, out = _run(capsys, ["--check", str(good)])
    assert code == 0 and out[-1] == "PASTE_SAFE: OK"
    code, out = _run(capsys, ["--check", str(good), str(bad)])
    assert code == 1 and out[-1] == "PASTE_SAFE: FAIL"
    assert any(line.startswith(f"{bad}:3: '#' comment outside quotes") for line in out), out
    for argv in ([], ["--check"], ["--check", str(tmp_path / "missing.md")], ["--no-such-flag"], [str(good)]):
        code, out = _run(capsys, argv)
        assert code == 2 and out[-1] == "PASTE_SAFE: FAIL", argv
    shell = tmp_path / "steps.sh"
    shell.write_text(f"./aeos policy decide --as {ADMIN}\necho x\n", encoding="utf-8")
    code, out = _run(capsys, ["--check", str(shell)])
    assert code == 1 and any(line.startswith(f"{shell}:1: '--as' asks") and "line 2 follows" in line for line in out)
    code, out = _run(capsys, ["--check", "--text", "echo ok\n./aeos ops backup restore-test <id>"])
    assert code == 1 and any(line.startswith("<text 1>:2: placeholder '<id>'") for line in out), out
    code, out = _run(capsys, ["--check", "--text", './aeos ops backup restore-test "<id>"'])
    assert code == 0 and out[-1] == "PASTE_SAFE: OK"
    monkeypatch.setattr(sys, "stdin", io.StringIO(_block("ls *.md")))
    code, out = _run(capsys, ["--check", "-"])
    assert code == 1 and any(line.startswith("<stdin>:3: unquoted glob") for line in out), out
    monkeypatch.setattr(sys, "stdin", io.StringIO("./aeos admin create\n./aeos serve\n"))
    code, out = _run(capsys, ["--check", "--shell", "-"])
    assert code == 1 and any(line.startswith("<stdin>:1: 'aeos admin create'") and "line 2 follows" in line
                             for line in out), out
    two = "./aeos vault unseal\n./aeos policy decide --as x"
    assert any("(lines 1, 2)" in p for _, _, p in ps.check_markdown(two, shell=True))


def test_check_output_is_ascii(tmp_path, capsys):
    page = tmp_path / "page.md"
    page.write_text(_block("echo été  # x"), encoding="utf-8")
    code, out = _run(capsys, ["--check", str(page)])
    assert code == 1 and all(line.isascii() for line in out)


# ------------------------------------------- the prompt lists follow the CLI
VISIBLE_PROMPT_CALLS = frozenset({("typer", "prompt"), ("typer", "confirm"), ("click", "prompt"), ("click", "confirm")})


def _prompt_functions(source_root: Path, package: str) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """(hidden, visible): (module, function) of every function under source_root/package that reaches a hidden prompt
    (getpass.getpass or a hide_input=True prompt) or a visible one (typer/click prompt or confirm without
    hide_input, input()), directly or through a function it calls by name (same module or imported by name)."""
    defs: dict[tuple[str, str], ast.AST] = {}
    imports: dict[str, dict[str, tuple[str, str]]] = {}
    for path in sorted((source_root / package).rglob("*.py")):
        module = ".".join(path.relative_to(source_root).with_suffix("").parts)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports[module] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs[(module, node.name)] = node
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    imports[module][alias.asname or alias.name] = (node.module, alias.name)

    def direct(fn: ast.AST) -> set[str]:
        kinds: set[str] = set()
        for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call)):
            f = call.func
            getpass = (isinstance(f, ast.Attribute) and f.attr == "getpass") or (
                isinstance(f, ast.Name) and f.id == "getpass")
            if getpass or any(k.arg == "hide_input" and isinstance(k.value, ast.Constant) and k.value.value is True
                              for k in call.keywords):
                kinds.add("hidden")
            elif (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                  and (f.value.id, f.attr) in VISIBLE_PROMPT_CALLS) or (isinstance(f, ast.Name) and f.id == "input"):
                kinds.add("visible")
        return kinds

    def callees(module: str, fn: ast.AST) -> set[tuple[str, str]]:
        return {imports[module].get(n.func.id, (module, n.func.id)) for n in ast.walk(fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}

    first = {key: direct(fn) for key, fn in defs.items()}
    reached: dict[str, set[tuple[str, str]]] = {}
    for kind in ("hidden", "visible"):
        found = {key for key, kinds in first.items() if kind in kinds}
        changed = True
        while changed:
            changed = False
            for key, fn in defs.items():
                if key not in found and callees(key[0], fn) & found:
                    found.add(key)
                    changed = True
        reached[kind] = found
    return reached["hidden"], reached["visible"]


def _commands(command: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[str, Any]]:
    if hasattr(command, "commands"):
        for name, sub in command.commands.items():
            yield from _commands(sub, (*path, name))
    else:
        yield " ".join(path), command


def prompt_list_problems(app: Any, source_root: Path, package: str, hidden_listed: Mapping[str, tuple[str, ...]],
                         confirm_listed: Mapping[str, tuple[str, ...]]) -> list[str]:
    """Commands without an '--as' option that prompt for hidden input must be in ``hidden_listed``; those that only
    ask for a typed confirmation must be in ``confirm_listed``. Every listed entry must be such a command, and its
    skip flags must be options of it."""
    import typer

    hidden_fns, visible_fns = _prompt_functions(source_root, package)
    found: dict[str, tuple[str, set[str]]] = {}  # path -> (kind, option names); kind 'as', 'hidden' or 'visible'
    for path, cmd in _commands(typer.main.get_command(app)):
        options = {opt for p in cmd.params for opt in getattr(p, "opts", [])}
        cb = cmd.callback
        key = (cb.__module__, cb.__name__) if cb is not None else None
        hidden = key in hidden_fns or any(getattr(p, "hide_input", False) for p in cmd.params)
        visible = key in visible_fns or any(getattr(p, "prompt", None) for p in cmd.params)
        if ps.AS_OPTION in options and (hidden or visible):
            found[path] = ("as", options)
        elif hidden or visible:
            found[path] = ("hidden" if hidden else "visible", options)
    problems = [f"CLI command '{path}' prompts for hidden input but HIDDEN_PROMPT_COMMANDS in scripts/paste_safe.py "
                f"lacks it" for path, (kind, _o) in sorted(found.items())
                if kind == "hidden" and path not in hidden_listed]
    problems += [f"CLI command '{path}' asks for a typed confirmation but CONFIRM_PROMPT_COMMANDS in "
                 f"scripts/paste_safe.py lacks it" for path, (kind, _o) in sorted(found.items())
                 if kind == "visible" and path not in confirm_listed]
    for name, kind, listed in (("HIDDEN_PROMPT_COMMANDS", "hidden", hidden_listed),
                               ("CONFIRM_PROMPT_COMMANDS", "visible", confirm_listed)):
        what = "prompts for hidden input" if kind == "hidden" else "asks only for a typed confirmation"
        for path, flags in sorted(listed.items()):
            if found.get(path, ("", set()))[0] != kind:
                problems.append(f"{name} entry '{path}' is not a CLI command that {what}")
                continue
            problems += [f"{name} entry '{path}': {flag} is not an option of the command"
                         for flag in flags if flag not in found[path][1]]
    return problems


def test_the_prompt_lists_match_the_cli():
    from aeos.cli import app

    assert prompt_list_problems(app, ROOT / "backend", "aeos", ps.HIDDEN_PROMPT_COMMANDS,
                                ps.CONFIRM_PROMPT_COMMANDS) == []


FAKE_CLI = '''
import getpass

import typer

app = typer.Typer()


def _ask() -> str:
    return getpass.getpass("Secret: ")


def _really(what: str) -> None:
    if typer.prompt(f"Type '{what}' to confirm") != what:
        raise typer.Exit(1)


@app.command("store")
def store() -> None:
    _ask()


@app.command("decide")
def decide(as_user: str = typer.Option(..., "--as")) -> None:
    typer.prompt("Password", hide_input=True)


@app.command("wipe")
def wipe(yes: bool = typer.Option(False, "--yes")) -> None:
    if not yes:
        _really("wipe")


@app.command("show")
def show(flag: bool = typer.Option(False, "--quiet")) -> None:
    pass
'''


@pytest.fixture()
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    package = tmp_path / "fake_paste_cli"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(FAKE_CLI, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in [m for m in sys.modules if m.startswith("fake_paste_cli")]:
        monkeypatch.delitem(sys.modules, name)
    module = importlib.import_module("fake_paste_cli.cli")
    return module.app


def test_bite_a_new_prompt_missing_from_the_lists_is_red(tmp_path, fake_cli):
    problems = prompt_list_problems(fake_cli, tmp_path, "fake_paste_cli", {}, {})
    assert problems == ["CLI command 'store' prompts for hidden input but HIDDEN_PROMPT_COMMANDS in "
                        "scripts/paste_safe.py lacks it",
                        "CLI command 'wipe' asks for a typed confirmation but CONFIRM_PROMPT_COMMANDS in "
                        "scripts/paste_safe.py lacks it"]


def test_bite_a_stale_entry_or_a_wrong_flag_is_red_and_the_right_lists_are_green(tmp_path, fake_cli):
    right = {"store": ()}, {"wipe": ("--yes",)}
    assert prompt_list_problems(fake_cli, tmp_path, "fake_paste_cli", *right) == []  # '--as' is generic
    stale = prompt_list_problems(fake_cli, tmp_path, "fake_paste_cli", {"store": (), "show": ()}, right[1])
    assert stale == ["HIDDEN_PROMPT_COMMANDS entry 'show' is not a CLI command that prompts for hidden input"]
    flag = prompt_list_problems(fake_cli, tmp_path, "fake_paste_cli", {"store": ("--quiet",)}, right[1])
    assert flag == ["HIDDEN_PROMPT_COMMANDS entry 'store': --quiet is not an option of the command"]
    swapped = prompt_list_problems(fake_cli, tmp_path, "fake_paste_cli", {"store": (), "wipe": ()}, {})
    assert swapped == ["CLI command 'wipe' asks for a typed confirmation but CONFIRM_PROMPT_COMMANDS in "
                       "scripts/paste_safe.py lacks it",
                       "HIDDEN_PROMPT_COMMANDS entry 'wipe' is not a CLI command that prompts for hidden input"]
