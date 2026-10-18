"""ClaudeNativeBridge hygiene (§12, U-13; §50 'Worker membaca vault/SSH/native Claude credential',
'Trace berisi secret/PII'): env allow-list, forbidden flags, isolated supervisor workspace."""

from __future__ import annotations

from pathlib import Path

import pytest

from aeos.claude_bridge import bridge
from aeos.claude_bridge.env import FORBIDDEN_NAMES, assert_clean, child_env, is_forbidden, parent_hygiene
from aeos.claude_bridge.workspace import (
    WorkspaceError,
    assert_workspace_safe,
    copy_context,
    high_confidence_secret_hits,
    prepare_workspace,
    sanitized_argv,
)
from aeos.core.enums import Domain
from aeos.core.paths import DataLayout, repo_root
from aeos.core.redaction import register_secret

FORBIDDEN_SAMPLE = {
    "ANTHROPIC_API_KEY": "x" * 30, "ANTHROPIC_AUTH_TOKEN": "y" * 30, "ANTHROPIC_BASE_URL": "https://proxy.example",
    "ANTHROPIC_MODEL": "claude-x", "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1",
    "CLAUDE_CODE_USE_FOUNDRY": "1", "CLAUDE_CODE_OAUTH_TOKEN": "z" * 30, "CLAUDE_CODE_SESSION_ID": "abc",
    "CLAUDE_CODE_ENTRYPOINT": "sdk", "CLAUDE_AGENT_SDK_VERSION": "1", "CLAUDECODE": "1",
    "AWS_ACCESS_KEY_ID": "AK", "AWS_SECRET_ACCESS_KEY": "s", "AWS_PROFILE": "p",
    "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/sa.json", "OPENROUTER_API_KEY": "k" * 30, "OPENAI_API_KEY": "o" * 30,
    "AEOS_DATA_DIR": "/data", "AEOS_SECRET": "v" * 30, "BAO_TOKEN": "t" * 30, "VAULT_TOKEN": "t" * 30,
    "GH_TOKEN": "g" * 30, "GITHUB_TOKEN": "g" * 30, "CLAUDE_PID": "123", "CLAUDE_EFFORT": "max",
}
ALLOWED_SAMPLE = {"PATH": "/usr/bin", "HOME": "/home/u", "TEMP": "/tmp", "LANG": "C.UTF-8", "SYSTEMROOT": "C:\\Windows",
                  "USERPROFILE": "C:\\Users\\u", "APPDATA": "C:\\Users\\u\\AppData\\Roaming"}


def test_child_env_drops_every_forbidden_variable():
    env = child_env({**FORBIDDEN_SAMPLE, **ALLOWED_SAMPLE})
    for name in FORBIDDEN_SAMPLE:
        assert name not in env, name
    for name in ("PATH", "HOME", "TEMP", "LANG"):
        assert name in env
    assert_clean(env)


def test_forbidden_names_cover_spec_list():
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
                 "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDECODE"):
        assert name in FORBIDDEN_NAMES or is_forbidden(name)
    for prefix_var in ("ANTHROPIC_ANYTHING", "CLAUDE_CODE_X", "CLAUDE_AGENT_SDK_X", "AWS_REGION", "OPENROUTER_X",
                       "OPENAI_X", "AEOS_ANY"):
        assert is_forbidden(prefix_var)


def test_assert_clean_rejects_leak():
    with pytest.raises(RuntimeError):
        assert_clean({"PATH": "/bin", "ANTHROPIC_API_KEY": "x"})


def test_proxy_with_embedded_credentials_is_dropped():
    env = child_env({"PATH": "/bin", "HTTPS_PROXY": "http://user:pw@proxy:8080", "NO_PROXY": "localhost"})
    assert "HTTPS_PROXY" not in env and env.get("NO_PROXY") == "localhost"


def test_parent_hygiene_reports_names_never_values():
    h = parent_hygiene({"ANTHROPIC_API_KEY": "secret-value-123456", "PATH": "/bin"})
    assert h["status"] == "WARN" and h["forbidden_present"] == ["ANTHROPIC_API_KEY"]
    assert "secret-value-123456" not in str(h)
    assert parent_hygiene({"PATH": "/bin"})["status"] == "OK"


def test_argv_contract_required_and_forbidden_flags():
    argv = bridge.build_argv("claude", schema_json='{"type":"object"}', model="opus", tools=["Read", "Grep", "Glob"],
                             effort="high")
    bridge.assert_safe_argv(argv)
    for flag in ("-p", "--restricted", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "--bare" not in argv and "--dangerously-skip-permissions" not in argv
    for bad in (["--bare"], ["--dangerously-skip-permissions"], ["--allow-dangerously-skip-permissions"],
                ["--fallback-model", "sonnet"], ["--permission-mode", "bypassPermissions"], ["--settings", "{}"],
                ["--mcp-config", "x.json"]):
        with pytest.raises(ValueError):
            bridge.assert_safe_argv([*argv, *bad])
    stripped = [a for a in argv if a != "--restricted"]
    with pytest.raises(ValueError):
        bridge.assert_safe_argv(stripped)
    prompts_host = list(argv)
    prompts_host[prompts_host.index("--permission-prompts") + 1] = "host"
    with pytest.raises(ValueError):
        bridge.assert_safe_argv(prompts_host)


def test_workspace_is_fresh_under_supervisor_and_not_repo_or_task_data(settings):
    layout = settings.layout
    ws = prepare_workspace(settings, "req-1")
    assert ws.parent.resolve() == layout.supervisor_workspace.resolve()
    assert repo_root().resolve() not in ws.resolve().parents and ws.resolve() != repo_root().resolve()
    task_data = layout.task_data(Domain.PERSONAL, "dly-aeos")
    assert task_data.resolve() not in [ws.resolve(), *ws.resolve().parents]
    with pytest.raises(WorkspaceError):
        prepare_workspace(settings, "req-1")  # never reused
    with pytest.raises(WorkspaceError):
        prepare_workspace(settings, "../escape")


def test_workspace_rejects_task_data_repo_and_git_dirs(settings, tmp_path: Path):
    layout = settings.layout
    with pytest.raises(WorkspaceError):
        assert_workspace_safe(layout, layout.task_data(Domain.PERSONAL, "p") / "x")
    with pytest.raises(WorkspaceError):
        assert_workspace_safe(layout, repo_root() / "supervisor" / "x")
    # a data dir placed inside a git repository is refused (project settings/hooks would load)
    repo = tmp_path / "somerepo"
    (repo / ".git").mkdir(parents=True)
    inner = DataLayout(repo / "data")
    with pytest.raises(WorkspaceError):
        assert_workspace_safe(inner, inner.supervisor_workspace / "r1")


def test_context_copied_read_only_and_secrets_refused(settings, tmp_path: Path):
    ws = prepare_workspace(settings, "req-ctx")
    src = tmp_path / "plan.md"
    src.write_text("# plan\ntoken: string field in the API schema\n", encoding="utf-8")
    manifest = copy_context(ws, [src])
    dest = ws / "context" / "plan.md"
    assert manifest[0]["name"] == "plan.md" and dest.read_text(encoding="utf-8").startswith("# plan")
    with pytest.raises(PermissionError):
        dest.write_text("tamper", encoding="utf-8")
    leaked = tmp_path / "leak.md"
    fake_key = "sk-or-v1-" + "a" * 40
    leaked.write_text(f"key {fake_key}", encoding="utf-8")
    ws2 = prepare_workspace(settings, "req-ctx2")
    with pytest.raises(WorkspaceError) as ei:
        copy_context(ws2, [src, leaked])
    assert ei.value.code == "SECRET_IN_PROMPT"
    assert not (ws2 / "context").exists()  # the partial copy (plan.md) is removed on refusal


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "latin-1"])
def test_credentials_in_non_utf8_context_are_found(settings, tmp_path: Path, encoding):
    """U-13: a token cannot slip into a model prompt by saving the file as UTF-16/latin-1."""
    leaked = tmp_path / "notes.txt"
    leaked.write_text("deploy with ghp_" + "e" * 36 + " please", encoding=encoding)
    ws = prepare_workspace(settings, f"req-enc-{encoding}")
    with pytest.raises(WorkspaceError) as ei:
        copy_context(ws, [leaked])
    assert ei.value.code == "SECRET_IN_PROMPT"


def test_context_domain_partition_boundary(settings, tmp_path: Path):
    """§12/U-05: each request only gets context from its own domain partition (symlinks resolved)."""
    layout = settings.layout
    office = layout.domain_root(Domain.OFFICE) / "projects" / "acme" / "evidence" / "a.md"
    personal = layout.domain_root(Domain.PERSONAL) / "projects" / "mine" / "evidence" / "b.md"
    for f in (office, personal):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("isi", encoding="utf-8")
    outside = tmp_path / "c.md"
    outside.write_text("isi", encoding="utf-8")
    cases = [("PERSONAL", office, False), ("PERSONAL", personal, True), ("OFFICE", personal, False),
             ("OFFICE", office, True), (None, office, False), (None, personal, False), ("PERSONAL", outside, True)]
    for i, (domain, f, allowed) in enumerate(cases):
        ws = prepare_workspace(settings, f"req-dom-{i}")
        if allowed:
            assert copy_context(ws, [f], layout=layout, domain=domain)
        else:
            with pytest.raises(WorkspaceError) as ei:
                copy_context(ws, [f], layout=layout, domain=domain)
            assert ei.value.code == "CONTEXT_DOMAIN_DENIED", (domain, f)
    link = tmp_path / "innocent.md"
    try:
        link.symlink_to(office)
    except (OSError, NotImplementedError):  # Windows without developer mode: symlinks unavailable
        return
    ws = prepare_workspace(settings, "req-dom-link")
    with pytest.raises(WorkspaceError):
        copy_context(ws, [link], layout=layout, domain="PERSONAL")


def test_secret_detector_registered_values_and_shapes():
    value = "registered-value-" + "9" * 12
    register_secret(value)
    assert high_confidence_secret_hits(f"use {value} now") >= 1
    assert high_confidence_secret_hits("ghp_" + "b" * 36) >= 1
    assert high_confidence_secret_hits("the field token: string is documented") == 0


def test_binary_probes_run_from_a_neutral_directory_with_scrubbed_env(monkeypatch):
    """`claude --version` / `claude auth status` never run in the controller cwd (a repository whose
    project settings the client would read, S05) and never see provider/credential variables."""
    import subprocess
    import tempfile

    from aeos.claude_bridge import binary as binmod

    seen: list[dict] = []

    def fake_run(argv, **kw):
        seen.append({"argv": argv, **kw})
        out = ('{"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"}'
               if "auth" in argv else "2.1.302 (Claude Code)")
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    for k, v in FORBIDDEN_SAMPLE.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(binmod.subprocess, "run", fake_run)
    monkeypatch.setattr(binmod, "fingerprint", lambda p: "sha256:" + "f" * 64)
    info = binmod.probe_version(binmod.BinaryInfo(found=True, path=Path("claude.exe"), source="PATH"))
    auth = binmod.probe_auth(info)
    assert info.version == "2.1.302" and auth.status == "VERIFIED"
    assert len(seen) == 2
    for call in seen:
        assert Path(call["cwd"]).resolve() == Path(tempfile.gettempdir()).resolve()
        assert call["stdin"] == subprocess.DEVNULL and call["timeout"] and "shell" not in call
        assert not any(is_forbidden(k) for k in call["env"])


def test_trace_argv_never_contains_schema_body():
    schema = '{"type":"object","properties":{"ok":{"type":"boolean"}}}'
    argv = ["-p", "--json-schema", schema, "--model", "opus"]
    out = sanitized_argv(argv, schema)
    assert schema not in out and any(a.startswith("<schema ") for a in out)
