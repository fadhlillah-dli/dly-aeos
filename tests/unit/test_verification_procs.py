"""Verifier child-process hygiene (§26, §31; §50 "PR code mencoba membaca production secret",
"Worker membaca vault/SSH/native Claude credential"): allow-list environment, no provider keys,
home/temp redirected, keyring disabled, timeouts kill the whole process tree."""

from __future__ import annotations

import os
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
    assert env["PYTHON_KEYRING_BACKEND"] == "keyring.backends.null.Keyring"
    assert env["HTTPS_PROXY"] == procs.DEAD_PROXY and "testserver" in env["NO_PROXY"]
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert env["AEOS_DATA_DIR"] == str(tmp_path / "d")
    # Windows platformdirs ignores LOCALAPPDATA (Known Folder API); its explicit overrides point into the run home
    for name, rel in (("LOCAL_APPDATA", ("AppData", "Local")), ("APPDATA", ("AppData", "Roaming")),
                      ("PERSONAL", ("Documents",))):
        assert env[f"WIN_PD_OVERRIDE_{name}"] == str(tmp_path.joinpath("h", *rel)), name
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


# A probe run as candidate code under the verifier: it looks for the owner's credentials the way tools find
# them through the environment (gh, git, the Python keyring, AEOS' own data dir) and reports what it found.
# It never invokes a credential helper and never reads a real credential store (config listing only).
ENV_PROBE = r'''
import json, os, subprocess, sys
from pathlib import Path
res = {}
gh_dirs = [os.environ.get("GH_CONFIG_DIR"),
           os.path.join(os.environ.get("XDG_CONFIG_HOME", ""), "gh") if os.environ.get("XDG_CONFIG_HOME") else None,
           os.path.join(os.environ.get("APPDATA", ""), "GitHub CLI") if os.environ.get("APPDATA") else None,
           os.path.join(os.path.expanduser("~"), ".config", "gh")]
res["gh_hosts"] = [d for d in gh_dirs if d and os.path.isfile(os.path.join(d, "hosts.yml"))]
res["gh_config_dir"] = os.environ.get("GH_CONFIG_DIR")
res["git_credentials_file"] = os.path.isfile(os.path.join(os.path.expanduser("~"), ".git-credentials"))
cp = subprocess.run(["git", "config", "--show-scope", "--get-all", "credential.helper"], capture_output=True,
                    text=True, stdin=subprocess.DEVNULL)
res["git_helpers"] = [ln.split("\t", 1) for ln in cp.stdout.splitlines() if ln.strip()]
res["git_nosystem"] = os.environ.get("GIT_CONFIG_NOSYSTEM")
import keyring
res["keyring_backend"] = type(keyring.get_keyring()).__module__
res["keyring_value"] = keyring.get_password("dly-aeos", "aeos-probe")
import platformdirs
data_candidates = [os.environ.get("AEOS_DATA_DIR"), platformdirs.user_data_dir("DLY-AEOS", appauthor=False, roaming=False)]
res["data_dbs"] = [d for d in data_candidates if d and os.path.isfile(os.path.join(d, "aeos.sqlite3"))]
res["data_candidates"] = [d for d in data_candidates if d]
res["pd_data_dir"] = platformdirs.user_data_dir("DLY-AEOS", appauthor=False, roaming=False)
res["pd_documents_dir"] = platformdirs.user_documents_dir()
res["ssh_agent"] = bool(os.environ.get("SSH_AUTH_SOCK"))
res["dbus"] = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
print(json.dumps(res))
'''


def _plant_owner_state(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    """A fake owner profile with credential-bearing files (fake values only) wired into the parent env."""
    owner = tmp_path / "owner"
    token = fake_secret()
    files = {
        owner / ".config" / "gh" / "hosts.yml": f"github.com:\n    oauth_token: {token}\n",
        owner / "AppData" / "Roaming" / "GitHub CLI" / "hosts.yml": f"github.com:\n    oauth_token: {token}\n",
        owner / "ghdir" / "hosts.yml": f"github.com:\n    oauth_token: {token}\n",
        owner / ".git-credentials": f"https://aeos:{token}@aeos-probe.invalid\n",
        owner / ".gitconfig": "[credential]\n\thelper = store\n",
        owner / "data" / "aeos.sqlite3": "not a real database\n",
    }
    for p, text in files.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    for k, v in {"HOME": owner, "USERPROFILE": owner, "APPDATA": owner / "AppData" / "Roaming",
                 "LOCALAPPDATA": owner / "AppData" / "Local", "XDG_CONFIG_HOME": owner / ".config",
                 "XDG_DATA_HOME": owner / ".local" / "share", "GH_CONFIG_DIR": owner / "ghdir",
                 "AEOS_DATA_DIR": owner / "data", "GIT_CONFIG_GLOBAL": owner / ".gitconfig"}.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.setenv("SSH_AUTH_SOCK", str(owner / "agent.sock"))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    return {"owner": owner, "token": Path(token)}


@pytest.mark.skipif(GIT is None, reason="git not available")
def test_candidate_child_finds_no_owner_credentials_through_the_environment(monkeypatch, tmp_path: Path):
    """§20/§25/§26 negative test: candidate code executed by the verifier looks for the keyring, the gh hosts.yml,
    git credentials (credential helper / ~/.git-credentials), the SSH agent and the AEOS data DB through the
    environment and gets nothing (fake owner state planted in the parent env)."""
    planted = _plant_owner_state(tmp_path, monkeypatch)
    run = tmp_path / "run"
    env = procs.scrubbed_env(pythonpath=[run / "s"], home=run / "h", tmp=run / "t", data_dir=run / "d",
                             disable_plugin_autoload=True)
    res = procs.run_process([sys.executable, "-c", ENV_PROBE], cwd=tmp_path, env=env, timeout=120)
    assert res.returncode == 0, res.tail()
    found = __import__("json").loads(res.stdout.strip().splitlines()[-1])
    assert found["gh_hosts"] == [] and found["gh_config_dir"].startswith(str(run / "h"))
    assert found["git_credentials_file"] is False
    # no system/global config; the only credential.helper entry is the explicit reset (empty, command scope)
    assert found["git_nosystem"] == "1"
    assert all(scope == "command" and value == "" for scope, value in found["git_helpers"]), found["git_helpers"]
    assert found["git_helpers"], "credential.helper is explicitly reset for the child"
    assert found["keyring_backend"] == "keyring.backends.null" and found["keyring_value"] is None
    assert found["data_dbs"] == [] and all(str(planted["owner"]) not in d for d in found["data_candidates"])
    # platformdirs (aeos.core.paths.default_data_dir) resolves inside the run home on every OS - on Windows
    # through the WIN_PD_OVERRIDE_* variables, not the Known Folder API of the owner profile
    home = os.path.normcase(str(run / "h"))
    assert os.path.normcase(found["pd_data_dir"]).startswith(home), found["pd_data_dir"]
    assert os.path.normcase(found["pd_documents_dir"]).startswith(home), found["pd_documents_dir"]
    assert found["ssh_agent"] is False and found["dbus"] == "disabled:"
    assert str(planted["token"]) not in res.stdout


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
