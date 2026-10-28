"""PreToolUse zsh word-split guard (scripts/claude_hooks/zsh_wordsplit_guard.py) and its hook registration.

Bite tests (LESSONS L14): every trap is denied with its own reason text (red for the right reason); near misses
print nothing; bad input and other tools fail open. Nothing here starts a shell: the guard only reads text."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "scripts" / "claude_hooks" / "zsh_wordsplit_guard.py"
SETTINGS = ROOT / ".claude" / "settings.json"
ZSH = {"SHELL": "/bin/zsh"}


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("zsh_wordsplit_guard_under_test", GUARD)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load()


def _payload(command: str, tool: str = "Bash") -> str:
    return json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"command": command}})


def _reason(command: str, env: dict[str, str] | None = None) -> str | None:
    decision = guard.decide(_payload(command), env=ZSH if env is None else env, platform="darwin")
    if decision is None:
        return None
    out = decision["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "deny"
    return str(out["permissionDecisionReason"])


# --- bite: each trap denied with its own reason ------------------------------------------------------------------

DENIED = [
    ("set-loop", "for pair in a:b c:d; do set -- $pair; echo $1; done", "'set -- $pair' sets ONE positional"),
    ("set-braced", "set -- ${p} && worktree add $1 $2", "'set -- $p' sets ONE positional"),
    ("set-end", "p='a b'\nset -- $p", "'set -- $p' sets ONE positional"),
    ("for-var", "for f in $FILES; do echo $f; done", "'for ... in $FILES' runs the loop ONCE"),
    ("for-braced-newline", "for f in ${FILES}\ndo echo $f\ndone", "'for ... in $FILES' runs the loop ONCE"),
    ("pytest-ls-glob", "PYTHONPATH=backend .venv/bin/python -m pytest -q $(ls tests/unit/test_wt*.py)",
     "pytest runs the WHOLE suite"),
    ("run-tests-backtick", "scripts/run_tests.sh -- `ls tests/unit/test_?.py`", "pytest runs the WHOLE suite"),
    ("pytest-bracket-glob", "cd x && pytest $(echo tests/unit/test_[ab].py)", "pytest runs the WHOLE suite"),
    ("pytest-double-quoted-subst", 'pytest -q "$(ls tests/unit/test_wt*.py)"', "pytest runs the WHOLE suite"),
    ("run-tests-double-quoted-backtick", 'scripts/run_tests.sh -- "`ls tests/unit/test_?.py`"',
     "pytest runs the WHOLE suite"),
    ("pytest-double-quoted-nested-quotes", 'pytest "$(ls "tests/unit"/test_wt*.py)"', "pytest runs the WHOLE suite"),
]


@pytest.mark.parametrize(("label", "command", "expected"), DENIED, ids=[d[0] for d in DENIED])
def test_each_trap_is_denied_with_its_own_reason(label: str, command: str, expected: str) -> None:
    reason = _reason(command)
    assert reason is not None, label
    assert expected in reason
    assert "LESSONS L14" in reason


def test_the_reasons_name_the_fix() -> None:
    split = _reason("set -- $pair")
    assert split is not None and "${=pair}" in split and "bash -c" in split and "explicitly" in split
    loop = _reason("for f in $FILES; do :; done")
    assert loop is not None and "${=FILES}" in loop and "bash -c" in loop
    glob = _reason("pytest -q $(ls tests/unit/test_x*.py)")
    assert glob is not None and "scripts/run_tests.sh -- <explicit test file paths>" in glob
    assert "word-split" not in glob and "glob" not in str(split)


def test_two_traps_in_one_command_give_both_reasons_once() -> None:
    reason = _reason("for p in $PAIRS; do set -- $p; done; for q in $PAIRS; do :; done")
    assert reason is not None
    assert reason.count("'for ... in $PAIRS'") == 1 and reason.count("'set -- $p'") == 1


def test_the_hook_process_prints_the_deny_and_exits_zero() -> None:
    result = subprocess.run([sys.executable, "-S", "-I", str(GUARD)], input=_payload("set -- $pair").encode(),
                            capture_output=True, timeout=30, env={"SHELL": "/bin/zsh"}, check=False)
    assert result.returncode == 0
    out = json.loads(result.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "'set -- $pair'" in out["permissionDecisionReason"]


# --- near misses: allowed, nothing printed -----------------------------------------------------------------------

ALLOWED = [
    ("set-quoted", 'set -- "$p"'),
    ("set-quoted-braced", 'set -- "${pair}" && echo $1'),
    ("set-literal", "set -- a b c"),
    ("set-two-vars", "set -- $a $b"),
    ("set-zsh-split", "set -- ${=pair}"),
    ("for-literal", "for x in a b c; do echo $x; done"),
    ("for-quoted-array", 'for x in "${arr[@]}"; do echo $x; done'),
    ("for-zsh-split", "for x in ${=FILES}; do echo $x; done"),
    ("for-cmd-subst", "for x in $(git ls-files); do echo $x; done"),
    ("zsh-split-array-assign", "parts=(${=pair}); set -- $parts; echo $1"),
    ("array-assign-then-for", "files=(a b c)\nfor f in $files; do echo $f; done"),
    ("zsh-special-array", "for d in $path; do echo $d; done"),
    ("bash-c-set", "bash -c 'for pair in a:b c:d; do IFS=: ; set -- $pair; echo $1; done'"),
    ("bash-c-double-quoted", 'bash -c "for f in \\$FILES; do echo \\$f; done"'),
    ("sh-word-split-on", "setopt shwordsplit; set -- $p"),
    ("heredoc-script", "cat > /tmp/x.sh <<'EOF'\nfor f in $FILES; do set -- $f; done\nEOF\nbash /tmp/x.sh"),
    ("comment", "# for f in $FILES; do set -- $f; done\necho ok"),
    ("echo-text", 'echo "set -- $p is a zsh trap"'),
    ("pytest-explicit", "PYTHONPATH=backend .venv/bin/python -m pytest -q tests/unit/test_a.py tests/unit/test_b.py"),
    ("run-tests-explicit", "scripts/run_tests.sh -- tests/unit/test_zsh_wordsplit_guard.py"),
    ("pytest-quoted-find", "pytest $(find tests -name 'test_x*.py')"),
    ("pytest-double-quoted-find", "pytest \"$(find tests -name 'test_x*.py')\""),
    ("pytest-double-quoted-k", 'pytest -q -k "a*b or $(echo c)" tests/unit/test_a.py'),
    ("double-quoted-glob-subst-not-a-test-runner", 'echo "$(ls docs/*.md)"'),
    ("double-quoted-arithmetic", 'echo "$((2*3))" && pytest -q tests/unit/test_a.py'),
    ("glob-subst-not-a-test-runner", "wc -l $(ls docs/*.md)"),
    ("arithmetic", "echo $((2*3)) && pytest -q tests/unit/test_a.py"),
]


@pytest.mark.parametrize(("label", "command"), ALLOWED, ids=[a[0] for a in ALLOWED])
def test_near_misses_are_allowed(label: str, command: str) -> None:
    assert _reason(command) is None, label


def test_a_non_zsh_shell_is_not_denied() -> None:
    assert _reason("set -- $pair", env={"SHELL": "/bin/bash"}) is None
    assert _reason("set -- $pair", env={"CLAUDE_CODE_SHELL": "/bin/bash", "SHELL": "/bin/zsh"}) is None
    assert _reason("set -- $pair", env={"CLAUDE_CODE_SHELL": "/opt/homebrew/bin/zsh"}) is not None


def test_an_unknown_shell_follows_the_platform_default() -> None:
    assert guard.shell_is_zsh({}, "darwin") is True
    assert guard.shell_is_zsh({}, "win32") is False


# --- fail-open ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["", "not json", "[]", "null", '{"tool_name": "Bash"}',
                                 '{"tool_name": "Bash", "tool_input": {"command": 3}}',
                                 '{"tool_name": "Bash", "tool_input": "set -- $p"}'])
def test_bad_input_prints_nothing(raw: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert guard.main(read=lambda: raw.encode()) == 0
    assert capsys.readouterr().out == ""


def test_a_non_bash_tool_is_allowed(capsys: pytest.CaptureFixture[str]) -> None:
    assert guard.decide(_payload("set -- $pair", tool="Write"), env=ZSH, platform="darwin") is None
    assert guard.main(read=lambda: _payload("set -- $pair", tool="Edit").encode()) == 0
    assert capsys.readouterr().out == ""


def test_an_internal_error_fails_open(capsys: pytest.CaptureFixture[str]) -> None:
    def boom() -> bytes:
        raise RuntimeError("read failed")

    assert guard.main(read=boom) == 0
    assert capsys.readouterr().out == ""


def test_the_hook_process_is_silent_on_bad_json() -> None:
    result = subprocess.run([sys.executable, "-S", "-I", str(GUARD)], input=b"{not json", capture_output=True,
                            timeout=30, env={"SHELL": "/bin/zsh"}, check=False)
    assert result.returncode == 0 and result.stdout == b""


# --- registration (.claude/settings.json) ------------------------------------------------------------------------

def bash_guard_problems(settings: object) -> list[str]:
    """What is wrong with the PreToolUse registration of this guard; the other hooks must still be there."""
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return ["no 'hooks' object"]
    problems = [f"{event} hook is missing" for event in ("SessionStart", "UserPromptSubmit") if not hooks.get(event)]
    entries = [
        (group.get("matcher"), hook)
        for group in hooks.get("PreToolUse", []) if isinstance(group, dict)
        for hook in group.get("hooks", []) if isinstance(hook, dict)
        if "scripts/claude_hooks/zsh_wordsplit_guard.py" in str(hook.get("command", ""))
    ]
    if not entries:
        return problems + ["PreToolUse does not run scripts/claude_hooks/zsh_wordsplit_guard.py"]
    for matcher, hook in entries:
        if matcher != "Bash":
            problems.append(f"PreToolUse guard matcher must be 'Bash', got {matcher!r}")
        if hook.get("type") != "command" or hook.get("timeout") != 5:
            problems.append("PreToolUse guard must be a command hook with timeout 5")
        if not str(hook.get("command", "")).rstrip().endswith("|| true"):
            problems.append("PreToolUse guard command must end with '|| true' (fail-open)")
    return problems


def _good() -> dict[str, object]:
    command = 'python3 "$CLAUDE_PROJECT_DIR/scripts/claude_hooks/zsh_wordsplit_guard.py" || true'
    return {"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "timeout": 20, "command": "x"}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "timeout": 5, "command": "y"}]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "timeout": 5, "command": command}]}],
    }}


def test_the_registration_checker_is_red_for_the_right_reason() -> None:
    assert bash_guard_problems(_good()) == []
    bad = _good()
    entry = bad["hooks"]["PreToolUse"][0]  # type: ignore[index]
    entry["matcher"] = "Write"
    entry["hooks"][0]["command"] = entry["hooks"][0]["command"].replace(" || true", "")
    problems = bash_guard_problems(bad)
    assert any("matcher must be 'Bash'" in p for p in problems)
    assert any("'|| true'" in p for p in problems)
    replaced = _good()
    del replaced["hooks"]["UserPromptSubmit"]  # type: ignore[attr-defined]
    assert bash_guard_problems(replaced) == ["UserPromptSubmit hook is missing"]


def test_committed_settings_register_the_guard_beside_the_other_hooks() -> None:
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    assert bash_guard_problems(settings) == []
