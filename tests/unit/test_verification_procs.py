"""Verifier child-process hygiene (§26, §31; §50 "PR code mencoba membaca production secret",
"Worker membaca vault/SSH/native Claude credential"): allow-list environment, no provider keys,
home/temp redirected, keyring disabled, timeouts kill the whole process tree."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from aeos.verification import procs
from tests.unit.test_verification_support import GIT, fake_secret, git, requires_git


def test_scrubbed_env_is_allow_list(monkeypatch, tmp_path: Path):
    token = fake_secret()
    monkeypatch.setenv("ANTHROPIC_API_KEY", token)
    monkeypatch.setenv("GITHUB_TOKEN", token)
    monkeypatch.setenv("OPENROUTER_API_KEY", token)
    monkeypatch.setenv("AEOS_OPENBAO_ADDR", "http://127.0.0.1:8200")
    monkeypatch.setenv("VAULT_TOKEN", token)
    env = procs.scrubbed_env(pythonpath=[tmp_path / "s" / "backend"], home=tmp_path / "h", tmp=tmp_path / "t",
                             data_dir=tmp_path / "d", disable_plugin_autoload=True)
    assert token not in "".join(env.values())
    for key in ("ANTHROPIC_API_KEY", "GITHUB_TOKEN", "OPENROUTER_API_KEY", "VAULT_TOKEN", "AEOS_OPENBAO_ADDR"):
        assert key not in env
    assert env["HOME"] == str(tmp_path / "h") and env["USERPROFILE"] == str(tmp_path / "h")
    assert env["TEMP"] == env["TMP"] == env["TMPDIR"] == str(tmp_path / "t")
    assert env["PYTHON_KEYRING_BACKEND"] == "keyring.backends.fail.Keyring"
    assert env["HTTPS_PROXY"] == procs.DEAD_PROXY and "testserver" in env["NO_PROXY"]
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert env["AEOS_DATA_DIR"] == str(tmp_path / "d")
    if sys.platform == "win32":
        assert "SYSTEMROOT" in env


def test_child_cannot_see_parent_secrets(monkeypatch, tmp_path: Path):
    token = fake_secret()
    monkeypatch.setenv("ANTHROPIC_API_KEY", token)
    env = procs.scrubbed_env(pythonpath=[tmp_path], home=tmp_path / "h", tmp=tmp_path / "t", data_dir=tmp_path / "d",
                             disable_plugin_autoload=True)
    res = procs.run_process([sys.executable, "-c", "import os; print(os.environ.get('ANTHROPIC_API_KEY', 'ABSENT'))"],
                            cwd=tmp_path, env=env, timeout=60)
    assert res.returncode == 0 and res.stdout.strip() == "ABSENT"


def test_timeout_kills_process_tree(tmp_path: Path):
    code = ("import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "time.sleep(60)\n")
    env = procs.scrubbed_env(pythonpath=[tmp_path], home=tmp_path / "h", tmp=tmp_path / "t", data_dir=tmp_path / "d",
                             disable_plugin_autoload=True)
    res = procs.run_process([sys.executable, "-c", code], cwd=tmp_path, env=env, timeout=3)
    assert res.timed_out and res.returncode is None
    assert res.duration_s < 30


@requires_git
def test_git_reader_export_is_exact_tree(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "a").mkdir()
    (repo / "a" / "x.py").write_text("X = 1\n", encoding="utf-8")
    (repo / ".gitattributes").write_text("a/x.py export-ignore\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c")
    (repo / "a" / "x.py").write_text("X = 2  # uncommitted\n", encoding="utf-8")
    reader = procs.GitReader(repo)
    head = reader.resolve_commit("HEAD")
    dest = tmp_path / "snap"
    dest.mkdir()
    info = reader.export_tree(head, dest, max_bytes=10_000)
    # export-ignore attributes and uncommitted edits do not change what is verified
    assert (dest / "a" / "x.py").read_text(encoding="utf-8") == "X = 1\n"
    assert info["files"] == 2
    with pytest.raises(procs.GitError):
        reader.resolve_commit("--upload-pack=evil")


@pytest.mark.skipif(GIT is None, reason="git not available")
def test_git_reader_rejects_non_repo(tmp_path: Path):
    assert procs.GitReader(tmp_path).is_repository() is False


def test_git_env_is_an_allow_list_without_credentials(monkeypatch):
    token = fake_secret()
    monkeypatch.setenv("GITHUB_TOKEN", token)
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.hooksPath'='/evil'")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i key")
    env = procs.git_env()
    assert token not in "".join(env.values())
    for key in ("GITHUB_TOKEN", "GIT_DIR", "GIT_CONFIG_PARAMETERS", "GIT_SSH_COMMAND"):
        assert key not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_NO_LAZY_FETCH"] == "1"


@pytest.mark.parametrize("name,ok", [
    ("aeos/t1-attempt-1", True), ("feature/x.y", True), ("main", True),
    ("-delete", False), ("a..b", False), ("a//b", False), ("x.lock", False), ("x/", False), ("HEAD@{1}", False),
    ("refs/heads/../x", False), ("", False), (None, False), ("a b", False), ("a:b", False), ("~1", False),
])
def test_valid_branch(name, ok):
    assert procs.valid_branch(name) is ok


@requires_git
def test_branch_head_reads_only_local_branches(tmp_path: Path):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "a")
    git(repo, "branch", "aeos/t1")
    reader = procs.GitReader(repo)
    assert reader.branch_head("aeos/t1") == git(repo, "rev-parse", "HEAD")
    with pytest.raises(procs.GitError):
        reader.branch_head("aeos/missing")
    with pytest.raises(procs.GitError):
        reader.branch_head("--output=/tmp/x")
