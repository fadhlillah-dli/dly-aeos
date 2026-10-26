"""Verifier child-process hygiene (§26, §31; §50 "PR code mencoba membaca production secret",
"Worker membaca vault/SSH/native Claude credential"): allow-list environment, no provider keys,
home/temp redirected, keyring disabled, timeouts kill the whole process tree."""

from __future__ import annotations

import contextlib
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


# Candidate code that starts a detached grandchild (own process group / session, no pipes) which acts two seconds
# later - after the step returned (review W6-054/W6-057: it could write the DB after the tamper window closed).
DETACHED_WRITER = r'''
import os, subprocess, sys
code = "import pathlib, sys, time; time.sleep(2); pathlib.Path(sys.argv[1]).write_text('late')"
kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
if os.name == "nt":
    kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
else:
    kw["start_new_session"] = True  # leaves the step's session: only the inherited marker still names it
subprocess.Popen([sys.executable, "-c", code, sys.argv[1]], **kw)
'''


def _wait_for(path: Path, seconds: float) -> bool:
    import time

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists():
            return True
        time.sleep(0.2)
    return path.exists()


def test_a_detached_grandchild_outlives_an_uncontained_step_but_never_a_contained_one(tmp_path: Path):
    env = procs.scrubbed_env(pythonpath=[tmp_path], home=tmp_path / "h", tmp=tmp_path / "t", data_dir=tmp_path / "d",
                             disable_plugin_autoload=True)
    late = tmp_path / "late-uncontained.txt"
    res = procs.run_process([sys.executable, "-c", DETACHED_WRITER, str(late)], cwd=tmp_path, env=env, timeout=60)
    assert res.returncode == 0 and res.survivors == []
    assert _wait_for(late, 15), "the defect the kill group closes: the grandchild acted after its step had ended"
    caught = tmp_path / "late-contained.txt"
    res = procs.run_process([sys.executable, "-c", DETACHED_WRITER, str(caught)], cwd=tmp_path, env=env, timeout=60,
                            contain=True)
    assert res.returncode == 0 and not res.timed_out
    assert res.containment in ("job-object", "process-group"), (res.containment, res.containment_errors)
    assert len(res.survivors) >= 1 and all("pid" in s for s in res.survivors), res.survivors
    assert not _wait_for(caught, 5), "the survivor was killed before it could act"


def test_a_contained_step_is_not_stalled_by_a_descendant_holding_its_output(tmp_path: Path):
    """A background child that keeps the step's stdout open used to keep the verifier waiting until the step
    timeout (and was not killed when it had left the tree); the contained step returns when its main process does."""
    holder = ("import subprocess, sys\n"
              "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
              "print('main done')\n")
    env = procs.scrubbed_env(pythonpath=[tmp_path], home=tmp_path / "h", tmp=tmp_path / "t", data_dir=tmp_path / "d",
                             disable_plugin_autoload=True)
    res = procs.run_process([sys.executable, "-c", holder], cwd=tmp_path, env=env, timeout=90, contain=True)
    assert not res.timed_out and res.returncode == 0 and "main done" in res.stdout
    # Windows lists the venv launcher + interpreter (+ its console host) of the sleeper: at least one survivor
    assert res.duration_s < 45 and len(res.survivors) >= 1, (res.duration_s, res.survivors)


# A step whose main process returns while a helper it started is still finishing (esbuild's service process ends when
# its parent's pipe closes): the helper here lingers HELPER_SECONDS, longer than the default survivor grace.
HELPER_SECONDS = 3.5
LINGERING_HELPER = r'''
import os, subprocess, sys
kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
if os.name == "nt":
    kw["creationflags"] = 0x00000008  # DETACHED_PROCESS: no console host of its own (still in the step's job)
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(float(__import__('sys').argv[1]))", sys.argv[1]], **kw)
'''


def _helper_names() -> frozenset[str]:
    """The allow-list naming this interpreter (the lingering helper of the tests is a child of the same executable)."""
    import psutil

    from aeos.verification.procgroup import helper_key

    return frozenset({helper_key(psutil.Process().name()), helper_key(Path(sys.executable).name)})


def _contained(tmp_path: Path, seconds: float, **kw):
    env = procs.scrubbed_env(pythonpath=[tmp_path], home=tmp_path / "h", tmp=tmp_path / "t", data_dir=tmp_path / "d",
                             disable_plugin_autoload=True)
    return procs.run_process([sys.executable, "-c", LINGERING_HELPER, str(seconds)], cwd=tmp_path, env=env,
                             timeout=60, contain=True, **kw)


def test_an_allow_listed_build_tool_helper_that_exits_within_its_grace_is_no_survivor(tmp_path: Path):
    """An honest build step is not quarantined: an allow-listed helper process still finishing after its parent got
    the helper grace, ended inside it (while the tamper window was open) and is listed, not hidden."""
    res = _contained(tmp_path, HELPER_SECONDS, helpers=_helper_names(), helper_grace=15.0)
    assert res.returncode == 0 and not res.timed_out
    assert res.survivors == [], res.survivors
    assert res.helper_exits and all(s.get("pid") for s in res.helper_exits), res.helper_exits
    assert res.duration_s < 15


def test_the_same_lingering_process_is_a_survivor_when_it_is_not_allow_listed(tmp_path: Path):
    res = _contained(tmp_path, HELPER_SECONDS)
    assert res.returncode == 0 and len(res.survivors) >= 1 and res.helper_exits == [], res.survivors


def test_an_allow_listed_helper_still_alive_after_its_grace_is_a_survivor(tmp_path: Path):
    res = _contained(tmp_path, 60, helpers=_helper_names(), helper_grace=1.0)
    assert res.returncode == 0 and len(res.survivors) >= 1, res.survivors
    assert res.duration_s < 30  # listed and killed, never waited for


def test_a_contained_step_without_descendants_has_no_survivors(tmp_path: Path):
    env = procs.scrubbed_env(pythonpath=[tmp_path], home=tmp_path / "h", tmp=tmp_path / "t", data_dir=tmp_path / "d",
                             disable_plugin_autoload=True)
    code = ("import subprocess, sys\n"
            "subprocess.run([sys.executable, '-c', 'print(1)'], check=True)\n"  # a child that ends inside the step
            "print('ok')\n")
    res = procs.run_process([sys.executable, "-c", code], cwd=tmp_path, env=env, timeout=60, contain=True)
    assert res.returncode == 0 and res.survivors == [] and "ok" in res.stdout


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


# ---------------------------------------------------------------- survivors that detach or appear late (POSIX sweep)
def _scripted_group(monkeypatch, scans, detached_pids=()):
    """A StepGroup whose sweeps return *scans* in turn (the last one repeats) - no real processes involved."""
    from aeos.verification import procgroup

    g = procgroup.StepGroup(method="process-group", pid=4242, started=0.0)
    calls = {"n": 0, "killed": 0}

    def members():
        i = min(calls["n"], len(scans) - 1)
        calls["n"] += 1
        return list(scans[i])

    monkeypatch.setattr(g, "_members", members)
    monkeypatch.setattr(g, "_left_group", lambda pid: pid in detached_pids)
    monkeypatch.setattr(g, "kill_all", lambda: calls.__setitem__("killed", calls["killed"] + 1))
    monkeypatch.setattr(procgroup, "_describe", lambda pid: {"pid": pid, "name": "python"})
    monkeypatch.setattr(procgroup.psutil, "wait_procs", lambda procs, timeout=None: ([], []))
    monkeypatch.setattr(procgroup.psutil, "Process", lambda pid: object())
    return g, calls


def test_a_member_that_left_the_group_is_a_survivor_at_once_without_grace(monkeypatch):
    """macOS CI: a grandchild that left the step's session was seen, got the 2 s grace, did its work and exited inside
    it - and was then not reported. A detached member is an escape, not an exiting child: no grace, killed now."""
    import time

    g, calls = _scripted_group(monkeypatch, scans=[[777], [], []], detached_pids={777})
    start = time.monotonic()
    survivors = g.reap(grace=2.0)
    assert [s["pid"] for s in survivors] == [777] and survivors[0]["detached"] is True
    assert calls["killed"] == 1 and time.monotonic() - start < 1.0  # no grace was waited


def test_an_ordinary_member_still_gets_the_grace_to_finish_exiting(monkeypatch):
    g, calls = _scripted_group(monkeypatch, scans=[[555], [555], []])
    assert g.reap(grace=2.0) == [] and calls["killed"] == 0  # exited inside the grace: not a survivor


def test_an_empty_first_sweep_is_repeated_before_the_group_counts_as_empty(monkeypatch):
    """A descendant spawned in the step's last moments may not be readable on the first sweep (macOS)."""
    g, calls = _scripted_group(monkeypatch, scans=[[], [], [888], [888]], detached_pids={888})
    survivors = g.reap(grace=2.0)
    assert [s["pid"] for s in survivors] == [888] and calls["n"] >= 3 and calls["killed"] == 1


def test_the_job_object_needs_no_settle_window(monkeypatch):
    g, calls = _scripted_group(monkeypatch, scans=[[], [999]])
    g.method = "job-object"
    assert g.reap(grace=0.0) == [] and calls["n"] == 1  # the job's own accounting is exact


# ---------------------------------------------------------------- a process that cannot be read during a sweep (POSIX)
# What psutil raises on macOS when it reads the environment of a process the kernel is tearing down
# (sysctl(KERN_PROCARGS2) fails with errno 0): seen in about 1 of 300 000 reads under heavy process churn.
TORN_DOWN = "<built-in function proc_environ> returned a result with an exception set"
posix_sweep = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sweep; Windows reads its Job Object")

# A step that starts a detached grandchild (own session: found through the step's marker only) named by argv[1].
DETACHED_SLEEPER = r'''
import subprocess, sys
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", sys.argv[1]], stdin=subprocess.DEVNULL,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
'''


def _outside_sleeper():
    """A live process that is not in any step's group (a child of the test process, no step marker)."""
    import subprocess

    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _step(tmp_path: Path, *args: str):
    env = procs.scrubbed_env(pythonpath=[tmp_path], home=tmp_path / "h", tmp=tmp_path / "t", data_dir=tmp_path / "d",
                             disable_plugin_autoload=True)
    return procs.run_process([sys.executable, *args], cwd=tmp_path, env=env, timeout=60, contain=True)


@posix_sweep
def test_an_unrelated_process_torn_down_while_it_is_read_is_no_survivor(tmp_path: Path, monkeypatch):
    """Lesson L15 regression (macOS CI on PR #22 and #24, loaded Macs): reading an exiting process made psutil raise
    SystemError, which escaped the sweep, so the whole group read as unreadable and a step without a single
    descendant (the import preflight) was QUARANTINED. Reproduced without timing: the read of an unrelated process
    fails exactly while the kernel tears that process down."""
    import psutil

    victim = _outside_sleeper()
    real = psutil.Process.environ
    torn: list[int] = []

    def environ(self):
        if self.pid == victim.pid and not torn:
            victim.kill()
            victim.wait()  # torn down while it is being read
            torn.append(self.pid)
            raise SystemError(TORN_DOWN)
        return real(self)

    monkeypatch.setattr(psutil.Process, "environ", environ)
    try:
        res = _step(tmp_path, "-c", "print('ok')")
    finally:
        victim.kill()
        victim.wait()
    assert torn == [victim.pid], "the sweep never read the process that was torn down"
    assert res.returncode == 0 and res.survivors == [], res.survivors


@posix_sweep
def test_an_unrelated_live_process_with_one_failed_read_is_read_again_not_counted(tmp_path: Path, monkeypatch):
    import psutil

    other = _outside_sleeper()
    real = psutil.Process.environ
    failed: list[int] = []

    def environ(self):
        if self.pid == other.pid and not failed:
            failed.append(self.pid)
            raise SystemError(TORN_DOWN)
        return real(self)

    monkeypatch.setattr(psutil.Process, "environ", environ)
    try:
        res = _step(tmp_path, "-c", "print('ok')")
        assert other.poll() is None, "the process outside the group was killed"
    finally:
        other.kill()
        other.wait()
    assert failed == [other.pid] and res.survivors == [], res.survivors


@posix_sweep
def test_a_live_descendant_that_stays_unreadable_is_still_a_survivor(tmp_path: Path, monkeypatch):
    """The other direction: the re-check never hides a process of the step. A detached grandchild (found through the
    marker only) whose every read fails stays alive through the re-check: a survivor (fail closed), and killed."""
    import psutil

    from aeos.verification import procgroup

    token = f"aeos-unreadable-{os.getpid()}-{tmp_path.name}"
    real = psutil.Process.environ
    reads: set[int] = set()

    def environ(self):
        try:
            unreadable = token in self.cmdline()
        except psutil.Error:
            unreadable = False
        if unreadable:
            reads.add(self.pid)
            raise SystemError(TORN_DOWN)
        return real(self)

    monkeypatch.setattr(psutil.Process, "environ", environ)
    # the grandchild lives 60 s: any re-check bound reaches the same decision, a short one keeps the test fast
    monkeypatch.setattr(procgroup, "UNREADABLE_RECHECK_SECONDS", 0.3)
    try:
        res = _step(tmp_path, "-c", DETACHED_SLEEPER, token)
        assert res.returncode == 0 and reads, "the grandchild was never read"
        listed = {s.get("pid"): s for s in res.survivors}
        assert reads <= set(listed), res.survivors
        assert all(listed[pid].get("detached") for pid in reads), res.survivors  # no grace for a detached member
        gone, alive = psutil.wait_procs([psutil.Process(pid) for pid in reads if psutil.pid_exists(pid)], timeout=10)
        assert not alive, "the unreadable survivor was not killed"
    finally:
        for p in psutil.process_iter(["cmdline"]):
            if token in (p.info.get("cmdline") or []) and p.pid != os.getpid():
                with contextlib.suppress(psutil.Error):
                    p.kill()
