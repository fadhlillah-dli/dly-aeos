"""UserPromptSubmit secret guard (scripts/claude_hooks/secret_prompt_guard.py) and the hook registration contract.

Every sample secret is assembled from parts at run time: no literal credential is in the repository (LESSONS L9, and
scripts/secret_scan.py stays green). Violating inputs are red for the right reason (the reason text names the kind);
near misses are green (nothing printed). Nothing here touches a keyring, vault or claude binary (LESSONS L20)."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "scripts" / "claude_hooks" / "secret_prompt_guard.py"
SETTINGS = ROOT / ".claude" / "settings.json"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("secret_prompt_guard_under_test", GUARD)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load()


def _alnum(n: int, seed: str = "Qm7x") -> str:
    return (seed * n)[:n]


def _payload(prompt: str) -> str:
    return json.dumps({"prompt": prompt, "hook_event_name": "UserPromptSubmit"})


def _samples() -> dict[str, tuple[str, str]]:
    """kind -> (sample secret, kind named by the guard)."""
    key = "-----" + "BEGIN " + "RSA PRIVATE" + " KEY-----"
    end = "-----" + "END " + "RSA PRIVATE" + " KEY-----"
    return {
        "openrouter": ("sk-or-" + "v1-" + _alnum(40, "a1b2"), "OpenRouter API key"),
        "anthropic": ("sk-" + "ant-" + "api03-" + _alnum(40, "Zk9_"), "Anthropic API key"),
        "openai-proj": ("sk-" + "proj-" + _alnum(40, "Pq3-"), "OpenAI-style API key"),
        "openai": ("sk-" + _alnum(48, "Lm5w"), "OpenAI-style API key"),
        "github-ghp": ("gh" + "p_" + _alnum(36), "GitHub token"),
        "github-gho": ("gh" + "o_" + _alnum(36), "GitHub token"),
        "github-ghs": ("gh" + "s_" + _alnum(36), "GitHub token"),
        "github-ghu": ("gh" + "u_" + _alnum(36), "GitHub token"),
        "github-pat": ("github" + "_pat_" + _alnum(50, "Ab1_"), "GitHub token"),
        "aws-akia": ("AK" + "IA" + "ABCDEFGH23456723"[:16], "AWS access key id"),
        "aws-asia": ("AS" + "IA" + "ABCDEFGH23456723"[:16], "AWS access key id"),
        "google-api": ("AI" + "za" + _alnum(35, "Wx_9"), "Google API key"),
        "slack": ("xo" + "xb-" + "123456789012-" + _alnum(24), "Slack token"),
        "vault-hvs": ("hv" + "s." + _alnum(30), "OpenBao/Vault token"),
        "vault-hvb": ("hv" + "b." + _alnum(30), "OpenBao/Vault token"),
        "pem-block": (key + "\n" + _alnum(64) + "\n" + end, "private key"),
        "pem-header-only": (key + "\n" + _alnum(32), "private key"),
        "unseal-key": ("Unseal " + "Key 1: " + _alnum(44, "Kd8/"), "OpenBao/Vault unseal key"),
    }


SAMPLES = _samples()


def _decide(prompt: str) -> dict[str, object] | None:
    return guard.decide(_payload(prompt))


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_each_kind_is_blocked_and_the_reason_never_holds_the_value(name: str) -> None:
    secret, kind = SAMPLES[name]
    decision = _decide(f"please use this for the deploy: {secret} and continue")
    assert decision is not None, f"{name} was not blocked"
    assert decision["decision"] == "block"
    reason = str(decision["reason"])
    assert kind in reason, f"{name}: reason does not name the kind {kind!r}: {reason!r}"
    # not the value, and not a recognisable part of it
    assert secret not in reason
    assert secret[8:] not in reason and secret[-12:] not in reason
    # bilingual and actionable
    assert "DIBLOKIR" in reason and "BLOCKED" in reason
    assert "./aeos secrets set" in reason
    assert "rotasi" in reason and "rotate" in reason
    # the block message must not echo the prompt (docs: hookSpecificOutput.suppressOriginalPrompt)
    assert decision["hookSpecificOutput"] == {"hookEventName": "UserPromptSubmit", "suppressOriginalPrompt": True}
    assert secret not in json.dumps(decision)


def test_several_kinds_are_listed_once_each_in_order() -> None:
    first, second = SAMPLES["github-ghp"][0], SAMPLES["aws-akia"][0]
    decision = _decide(f"{first} then {second} then {SAMPLES['github-gho'][0]}")
    assert decision is not None
    reason = str(decision["reason"])
    assert reason.count("GitHub token") == 2  # once in Indonesian text, once in English text
    assert reason.index("GitHub token") < reason.index("AWS access key id")


NEAR_MISSES = {
    "git sha": "fixed in 3b0b325 and 3b0b3253b0b3253b0b3253b0b3253b0b3253b0b3",
    "uuid": "run 00893aaf-19fa-41d2-8238-13269b9b3ca0 finished",
    "sha256 digest": "sha256:" + "ab12" * 16,
    "the word token": "what is a token, and how long can a token be?",
    "placeholder": "set GITHUB_TOKEN to <your-token> and retry; api_key=<your-key>",
    "openrouter prefix too short": "sk-or-v1-" + "a1b2" * 3,
    "openai prefix too short": "sk-" + "a1b2" * 3,
    "github prefix too short": "gh" + "p_" + "a1b2" * 3,
    "aws prefix too short": "AK" + "IA" + "ABCD2345",
    "example address": "mail you@example.com about it",
    "dotted code identifier": "look at model" + "s" + ".CustomerSubscriptionHistoryRecord and token" + "s" + ".AnotherVeryLongIdentifierName",
    "hyphenated word with sk": "the disk-" + "backed-storage-layer-for-the-queue-service is slow",
    "longer uppercase word": "region ASIA" + "PACIFICREGIONXYZW1 is not a key id",
    "pem header word without dashes": "BEGIN RSA PRIVATE KEY blocks are explained in the manual",
    "unseal key prose": "the unseal key is kept offline and the recovery key too",
    "empty prompt": "",
}


@pytest.mark.parametrize("name", sorted(NEAR_MISSES))
def test_near_misses_are_not_blocked(name: str) -> None:
    assert _decide(NEAR_MISSES[name]) is None, f"{name} was blocked"


def test_noisy_short_forms_are_skipped_on_purpose_and_still_exist_canonically() -> None:
    """The guard skips three canonical patterns. If the canonical list changes so a skipped prefix no longer matches
    one of its patterns, the skip list is stale: fix it here, do not copy patterns."""
    sys.path.insert(0, str(ROOT / "backend"))
    from aeos.core.redaction import _PATTERNS, contains_high_confidence_secret

    for prefix in guard.SKIPPED_PATTERN_PREFIXES:
        assert any(p.pattern.startswith(prefix) for p in _PATTERNS), f"stale skip entry {prefix!r}"
    noisy = "see model" + "s" + ".CustomerSubscriptionHistoryRecord"  # built from parts: secret_scan flags this shape
    assert contains_high_confidence_secret(noisy)  # why the guard skips the short Vault form
    assert _decide(noisy) is None


def test_gap_patterns_are_still_gaps_in_the_canonical_module() -> None:
    """GAPS exists only for what aeos.core.redaction does not catch. When it starts to catch a kind, delete the
    entry from GAPS (one pattern list, LESSONS L10/L17)."""
    sys.path.insert(0, str(ROOT / "backend"))
    from aeos.core.redaction import _PATTERNS

    for name in ("aws-asia", "pem-header-only"):
        sample = SAMPLES[name][0]
        covered = [p.pattern for p in _PATTERNS if not p.groups and p.search(sample)]
        assert not covered, f"{name} is now canonical ({covered[0][:30]}...): remove it from GAPS"


def test_every_canonical_token_pattern_is_either_used_or_deliberately_skipped() -> None:
    used = guard.canonical_patterns()
    sys.path.insert(0, str(ROOT / "backend"))
    from aeos.core.redaction import _PATTERNS

    token_shaped = [p for p in _PATTERNS if not p.groups]
    skipped = [p for p in token_shaped if p.pattern.startswith(guard.SKIPPED_PATTERN_PREFIXES)]
    assert len(used) + len(skipped) == len(token_shaped)
    assert len(used) >= 10


@pytest.mark.parametrize(
    "raw",
    ["", "not json", "{", "[]", "null", '"text"', "{}", '{"prompt": null}', '{"prompt": 12}', '{"prompt": ""}',
     '{"prompt": ["a"]}', '{"other": "sk-nothing"}'],
)
def test_malformed_input_is_silent_exit_zero(raw: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert guard.main(lambda: raw.encode("utf-8")) == 0
    assert capsys.readouterr().out == ""


def test_invalid_utf8_is_silent_exit_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert guard.main(lambda: b"\xff\xfe\x00{") == 0
    assert capsys.readouterr().out == ""


def test_an_internal_error_fails_open(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def boom() -> list[re.Pattern[str]]:
        raise ImportError("canonical module unavailable")

    monkeypatch.setattr(guard, "canonical_patterns", boom)
    secret = SAMPLES["github-ghp"][0]
    assert guard.main(lambda: _payload(secret).encode()) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_main_prints_exactly_one_json_object_on_a_block(capsys: pytest.CaptureFixture[str]) -> None:
    secret = SAMPLES["slack"][0]
    assert guard.main(lambda: _payload(f"x {secret} y").encode()) == 0
    out = capsys.readouterr().out
    assert secret not in out
    assert json.loads(out)["decision"] == "block"


def _run(stdin: bytes) -> tuple[subprocess.CompletedProcess[bytes], float]:
    started = time.monotonic()
    proc = subprocess.run([sys.executable, str(GUARD)], input=stdin, capture_output=True, timeout=60, check=False,
                          cwd=str(ROOT))
    return proc, time.monotonic() - started


def test_subprocess_blocks_a_secret_and_stays_inside_a_loose_time_budget() -> None:
    secret = SAMPLES["anthropic"][0]
    proc, elapsed = _run(_payload(f"here: {secret}").encode())
    assert proc.returncode == 0
    assert proc.stderr == b""
    decision = json.loads(proc.stdout)
    assert decision["decision"] == "block" and "Anthropic API key" in decision["reason"]
    assert secret.encode() not in proc.stdout
    # the design target is < 200 ms; the bound is loose because a loaded CI host starts Python slowly
    assert elapsed < 10, f"hook took {elapsed:.2f}s"


def test_subprocess_is_silent_for_a_clean_prompt_and_for_garbage() -> None:
    for stdin in (_payload("refactor the config loader please").encode(), b"{", b"", b"\xff\xff"):
        proc, elapsed = _run(stdin)
        assert proc.returncode == 0 and proc.stdout == b"" and proc.stderr == b""
        assert elapsed < 10


def test_a_large_prompt_with_many_pem_headers_is_scanned_in_linear_time() -> None:
    header = SAMPLES["pem-header-only"][0].splitlines()[0]
    prompt = (header + "\n") * 4000 + "x" * 200_000
    started = time.monotonic()
    decision = _decide(prompt)
    assert decision is not None and "private key" in str(decision["reason"])
    assert time.monotonic() - started < 10


# --- the hook registration (.claude/settings.json) -------------------------------------------------------------


def settings_problems(settings: object, root: Path) -> list[str]:
    """What is wrong with a Claude Code settings document as far as the two DLY AEOS hooks go."""
    problems: list[str] = []
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return ["no 'hooks' object"]
    commands: dict[str, list[str]] = {}
    for event, groups in hooks.items():
        for group in groups if isinstance(groups, list) else []:
            for hook in group.get("hooks", []) if isinstance(group, dict) else []:
                if not isinstance(hook, dict) or hook.get("type") != "command":
                    continue
                command = str(hook.get("command", ""))
                commands.setdefault(event, []).append(command)
                timeout = hook.get("timeout")
                if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 30:
                    problems.append(f"{event}: timeout must be an integer of 1..30 seconds, got {timeout!r}")
                for rel in re.findall(r"scripts/[\w./-]+\.py", command):
                    if not (root / rel).is_file():
                        problems.append(f"{event}: script {rel} does not exist")
    if not any("scripts/claude_hooks/secret_prompt_guard.py" in c for c in commands.get("UserPromptSubmit", [])):
        problems.append("UserPromptSubmit does not run scripts/claude_hooks/secret_prompt_guard.py")
    if not any("scripts/preflight.py" in c and "--facts" in c for c in commands.get("SessionStart", [])):
        problems.append("SessionStart does not run scripts/preflight.py --facts")
    return problems


def _good_settings() -> dict[str, object]:
    return {"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "timeout": 20,
                                     "command": 'python3 "$CLAUDE_PROJECT_DIR/scripts/preflight.py" --facts || true'}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "timeout": 5, "command":
                                         'python3 "$CLAUDE_PROJECT_DIR/scripts/claude_hooks/secret_prompt_guard.py"'}]}],
    }}


def test_settings_checker_accepts_a_good_document_in_the_real_tree() -> None:
    assert settings_problems(_good_settings(), ROOT) == []


def test_settings_checker_is_red_for_the_right_reason() -> None:
    bad = _good_settings()
    prompt_hook = bad["hooks"]["UserPromptSubmit"][0]["hooks"][0]  # type: ignore[index]
    prompt_hook["command"] = 'python3 "$CLAUDE_PROJECT_DIR/scripts/claude_hooks/nope.py"'
    prompt_hook.pop("timeout")
    problems = settings_problems(bad, ROOT)
    assert any("scripts/claude_hooks/nope.py does not exist" in p for p in problems)
    assert any("timeout must be an integer" in p for p in problems)
    assert any("does not run scripts/claude_hooks/secret_prompt_guard.py" in p for p in problems)
    assert settings_problems({}, ROOT) == ["no 'hooks' object"]


@pytest.mark.skipif(not SETTINGS.is_file(), reason=".claude/settings.json is not in this tree (added by the delegating session)")
def test_committed_settings_reference_existing_scripts() -> None:
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    assert settings_problems(settings, ROOT) == []
