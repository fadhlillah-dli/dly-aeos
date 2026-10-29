"""scripts/owner_dryrun.py: an owner script is dry-run against a copy of the live database (LESSONS L13).

Incident 2026-10-28: a dry run against a copy with no secrets passed, and on the real system the script left
CAN_RUN_WORKER BLOCKED. The harness mirrors which secrets exist and compares the Doctor capabilities before and
after. Hermetic: the 'live' database is a synthetic one in tmp, the presence set is a fake (the real
``aeos secrets status`` never runs here), the SecretGuard of tests/conftest.py stays on, and the owner scripts are
tiny fakes written into tmp. Bite tests: a script that disables the only keyed WORKER route is red with
"CAN_RUN_WORKER: READY -> BLOCKED" (exit 1), and also when the gate was already BLOCKED (a critical check that newly
fails); the same with --accept-degrade exits 0 and prints the reason; a script that changes nothing relevant is
green (OWNER_DRYRUN: OK); a script without run(), a prompt without an answer and an unavailable real secret store
fail (exit 2) with their message. The live file is byte-identical afterwards and the temporary dir is gone.
Host endpoints: the 'live' fixture pins AEOS_PORT and AEOS_OPENBAO_ADDR to closed loopback ports and installs a fake
probe runner, so no test here reaches the owner's running Control Center (default port 8787) or OpenBao; the
SecretGuard refuses those ports as well, and a guard test records every connect of a dry run to prove it.
Host binaries: 'live' also depends on no_docker and opencode_installed, so no test here asks the owner's Docker daemon
(over its Unix socket, which no TCP guard sees) or runs the real opencode; an autouse guard refuses and records every
subprocess.Popen in this file, and a test fails when one was attempted (bite: the real probes restored, red).
"""

from __future__ import annotations

import hashlib
import importlib.util
import socket
import sys
import tempfile
from pathlib import Path

import pytest

from aeos.core.paths import repo_root
from aeos.worker_runtime import opencode as opencode_mod
from aeos.worker_runtime import sandbox as sandbox_mod
from tests.unit.test_doctor_seams import (  # noqa: F401 - fixtures used through usefixtures, autouse speed-ups
    ROUTE,
    _activate,
    _admin,
    _cheap_argon2,
    _enable_agent,
    _memo_http_guard,
    _process_exception,
    fake_host,
    no_docker,
    opencode_installed,
)

ROOT = repo_root()
KEY_REF = "workers/dryrun-test"


def _closed_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# One closed port per module for the 'live' Control Center (stable, so the memoised HTTP guard probe is reused) and
# one for its OpenBao: never the owner's real ones.
PINNED_CC_PORT = _closed_loopback_port()
PINNED_BAO_PORT = _closed_loopback_port()


def _load():
    spec = importlib.util.spec_from_file_location("aeos_script_owner_dryrun", ROOT / "scripts" / "owner_dryrun.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


od = _load()

# The real host-binary probes, kept before any fixture replaces them: only the bite test below puts them back.
REAL_DOCKER_PROBE = sandbox_mod._docker_probe
REAL_OPENCODE_STATUS = opencode_mod.opencode_status


@pytest.fixture(autouse=True)
def no_host_process(monkeypatch):
    """Every subprocess.Popen in this file is refused (never started) and recorded; a test that attempted one fails.

    The real Docker CLI talks to the owner's daemon over a Unix socket and the real opencode is a host binary: neither
    may run from these tests (review finding on 729940b). The Doctor's freshness checks (runtime.frontend_fresh,
    runtime.code_drift) ask git about the checkout; here they get a fixed answer instead of a git process (main went
    red on 3ba0dec when both arrived together, LESSONS L24)."""
    import subprocess

    from aeos.operations import freshness

    started: list[list[str]] = []

    def refuse(args, *a, **kw):
        started.append([str(x) for x in args] if isinstance(args, (list, tuple)) else [str(args)])
        raise OSError("the owner dry-run tests never start a host process")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(freshness, "_git", lambda repo, *args, timeout=30: None)  # "not a git checkout": no process
    yield started
    assert started == [], f"a test started a host process: {started}"

LOGIN = '''
def _login(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": r.json()["csrf_token"]}
'''
DISABLE = LOGIN + f'''

def run(client, username, password, ask):
    h = _login(client, username, password)
    if ask("Disable the route? Type JA: ").strip() == "JA":
        r = client.post("/api/routes/{ROUTE}/disable", json={{"reason": "owner dry-run test"}}, headers=h)
        print("disable", r.status_code)
    return {{"done": True}}
'''
NOOP = LOGIN + '''

def run(client, username, password, ask):
    _login(client, username, password)
    print("routes", len(client.get("/api/routes").json()["items"]))
'''
NO_RUN = "def main():\n    print('not a dry-runnable owner script')\n"
PROBE = LOGIN + f'''

def run(client, username, password, ask):
    import socket

    import keyring

    from aeos.core.errors import Blocked
    from aeos.secrets import facade

    print("keyring", type(keyring.get_keyring()).__module__)
    print("present", facade.secret_status("{KEY_REF}").exists, "absent", facade.secret_status("workers/x").exists)
    print("dummy", facade.get_secret("{KEY_REF}", purpose="workers:test", actor="t").startswith("dry-run-dummy-"))
    try:
        facade.get_secret("workers/x", purpose="workers:test", actor="t")
    except Blocked as exc:
        print("missing", exc.code)
    for host, port in ((ask("host"), int(ask("port"))), ("203.0.113.9", 443), ("example.com", 443)):
        try:
            socket.create_connection((host, port), timeout=2).close()
            print("connected", host)
        except OSError as exc:
            print("refused", host, type(exc).__name__)
'''


def _route(db) -> None:
    """A free loopback WORKER route that names a credential: eligible only while that credential is present."""
    from aeos.core.timeutil import utcnow
    from aeos.models.routing import ModelRoute
    from aeos.routing.service import compute_config_hash

    now = utcnow()
    r = ModelRoute(route_key=ROUTE, pool="WORKER", provider="local-test", endpoint="http://127.0.0.1:11434/v1",
                   model_id="coder-free", display_name="Local keyed coder", harness="opencode", is_free="YES",
                   pricing={"input_per_mtok": 0}, pricing_checked_at=now, terms_checked_at=now,
                   data_terms={"may_use_inputs_for_training": False}, eligible_domains=["PERSONAL"],
                   allowed_data_classes=["PUBLIC", "INTERNAL"], context_window=65536, supports_tools="YES",
                   status="CANDIDATE", mode="LIVE", origin="OWNER", availability="AVAILABLE",
                   mode_params={"credential_ref": KEY_REF})
    r.config_hash = compute_config_hash(r)
    db.add(r)
    db.flush()


def _disable_agent(path: Path) -> None:
    import sqlite3

    con = sqlite3.connect(path)
    try:
        with con:
            con.execute("UPDATE agent_definitions SET enabled = 0 WHERE agent_key = 'opencode-implementer'")
    finally:
        con.close()  # the last connection: the WAL is checkpointed and the file is stable again


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def live(settings, db, tmp_path, monkeypatch, request):
    """A synthetic 'live' database in tmp where CAN_RUN_WORKER is READY while KEY_REF is present.

    Hermetic host endpoints for every test: the 'live' Control Center and OpenBao are closed loopback ports (the
    harness opens the Control Center port to the Doctor, so the default 8787 would reach the owner's running server),
    the Doctor's probe runner is a fake (no HTTP beyond its canned answers), and no_docker and opencode_installed are
    requested before anything else here (the setup itself asks for the opencode status), so neither the Docker CLI (the
    owner's daemon) nor the real opencode binary runs for any test that requests 'live'."""
    from aeos.core import config as config_mod
    from aeos.core.db import get_engine
    from aeos.inventory.probes import set_runner_override
    from tests.unit.test_inventory_fakes import base_runner

    for fake in ("no_docker", "opencode_installed"):  # first: the setup below already asks for the opencode status
        request.getfixturevalue(fake)
    admin = _admin(db)
    _route(db)
    _activate(db, admin)
    _process_exception(db, admin)
    _enable_agent(db)
    db.commit()
    db.close()
    get_engine().dispose()  # no open connection: the WAL is checkpointed and the file is stable
    monkeypatch.setenv("AEOS_PORT", str(PINNED_CC_PORT))
    monkeypatch.setenv("AEOS_OPENBAO_ADDR", f"http://127.0.0.1:{PINNED_BAO_PORT}")
    config_mod.reset_settings_cache()
    set_runner_override(base_runner())
    request.addfinalizer(lambda: set_runner_override(None))
    path = settings.layout.db_path
    temp_root = tmp_path / "tmpdirs"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    state = {"present": {KEY_REF: None}}
    monkeypatch.setattr(od, "probe_presence",
                        lambda: od.Presence(backend="openbao", available=True, present=dict(state["present"])))
    return {"db": path, "digest": _digest(path), "listing": sorted(p.name for p in path.parent.iterdir()),
            "temp_root": temp_root, "state": state}


def _script(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / "owner-scripts" / f"{name}.py"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _untouched(live) -> None:
    assert _digest(live["db"]) == live["digest"], "the live database file changed"
    assert sorted(p.name for p in live["db"].parent.iterdir()) == live["listing"], "files appeared beside it"
    assert list(live["temp_root"].iterdir()) == [], "the temporary data dir was not removed"


def _cap_row(out: str, cap: str) -> list[str]:
    """The cells of one row of the printed capability table."""
    for line in out.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and cells[0] == cap:
            return cells
    raise AssertionError(f"{cap} not in the capability table:\n{out}")


USES = pytest.mark.usefixtures("fake_host", "no_docker", "opencode_installed")


@USES
def test_disabling_the_only_keyed_worker_route_is_red(live, tmp_path, capsys):
    code = od.main([_script(tmp_path, "disable", DISABLE), "--answer", "JA"])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "CAN_RUN_WORKER: READY -> BLOCKED" in out
    assert "route.registry: CONFIGURED -> MISSING" in out
    assert "disable 200" in out and "[--answer 1: JA]" in out
    assert out.rstrip().splitlines()[-1] == "OWNER_DRYRUN: DEGRADED"
    _untouched(live)
    out.encode("ascii")


@USES
def test_an_accepted_degradation_exits_zero_with_its_reason_and_never_says_ok(live, tmp_path, capsys):
    reason = "owner retires this route on purpose"
    code = od.main([_script(tmp_path, "disable", DISABLE), "--answer", "JA", "--accept-degrade", reason])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "CAN_RUN_WORKER: READY -> BLOCKED" in out and f"degradation accepted by the owner: {reason}" in out
    assert out.rstrip().splitlines()[-1] == "OWNER_DRYRUN: DEGRADED-ACCEPTED"
    _untouched(live)


@USES
def test_near_miss_a_script_that_changes_nothing_relevant_is_ok(live, tmp_path, capsys):
    code = od.main([_script(tmp_path, "noop", NOOP)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "-> BLOCKED" not in out and "WORSE" not in out
    row = _cap_row(out, "CAN_RUN_WORKER")
    assert row[1:3] == ["READY", "READY"], out  # the presence mirror makes the keyed route READY
    assert out.rstrip().splitlines()[-1] == "OWNER_DRYRUN: OK"
    _untouched(live)


@USES
def test_without_the_credential_in_the_real_store_the_gate_is_not_ready_before(live, tmp_path, capsys):
    """The mirror is the point: the same database with the key absent in the real store is not READY."""
    live["state"]["present"] = {}
    code = od.main([_script(tmp_path, "noop", NOOP)])
    out = capsys.readouterr().out
    row = _cap_row(out, "CAN_RUN_WORKER")
    assert row[1] == "BLOCKED", out
    assert code == 0 and "route.worker_key" not in out.split("Checks whose status changed")[1].split("OWNER_DRYRUN")[0]


@USES
def test_a_gating_check_that_newly_fails_under_an_already_blocked_capability_is_red(live, tmp_path, capsys):
    """Review finding: BLOCKED -> BLOCKED hid route.registry CONFIGURED -> MISSING. The copy may be BLOCKED where the
    real system is READY, so a critical check that newly fails is a regression even when the status stays.
    The key stays present (a keyless active route already fails route.registry since route key eligibility); the
    gate is BLOCKED before by a disabled worker agent instead."""
    _disable_agent(live["db"])
    live["digest"] = _digest(live["db"])
    code = od.main([_script(tmp_path, "disable", DISABLE), "--answer", "JA"])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "CAN_RUN_WORKER: BLOCKED -> BLOCKED (gating check newly failing: route.registry CONFIGURED -> MISSING" in out
    assert _cap_row(out, "CAN_RUN_WORKER")[1:4] == ["BLOCKED", "BLOCKED", "WORSE"], out
    assert out.rstrip().splitlines()[-1] == "OWNER_DRYRUN: DEGRADED"
    _untouched(live)


@USES
def test_an_unavailable_real_store_fails_before_anything_is_copied(live, tmp_path, capsys, monkeypatch):
    """Review finding: with OpenBao sealed or stopped the dry run had no store and ended OK. It is FAIL now."""
    monkeypatch.setattr(od, "probe_presence", lambda: od.Presence(backend="none", available=False))
    code = od.main([_script(tmp_path, "disable", DISABLE), "--answer", "JA"])
    out = capsys.readouterr().out
    assert code == 2, out
    assert "the real secret store is unavailable (backend none): the dry run cannot see which credentials" in out
    assert "Doctor before" not in out and "disable 200" not in out
    assert out.rstrip().splitlines()[-1] == "OWNER_DRYRUN: FAIL"
    _untouched(live)


@USES
def test_the_script_sees_the_mirror_null_keyring_and_a_refused_network(live, tmp_path, capsys, monkeypatch):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        # the loopback ports of the real Control Center and OpenBao are denied: let this listener stand for them
        monkeypatch.setattr(od, "_port", lambda addr, default: port)
        code = od.main([_script(tmp_path, "probe", PROBE), "--answer", "127.0.0.1", "--answer", str(port)])
        out = capsys.readouterr().out
        # near miss: the same listener accepts this test once the dry run is over (the guard is gone)
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    assert code == 0, out
    assert "keyring keyring.backends.null" in out
    assert "present True absent False" in out and "dummy True" in out and "missing SECRET_MISSING" in out
    assert "refused 127.0.0.1 ConnectionRefusedError" in out and "refused 203.0.113.9 ConnectionRefusedError" in out
    assert "refused example.com gaierror" in out and "connected" not in out
    _untouched(live)


def test_a_script_without_run_fails_before_touching_anything(live, tmp_path, capsys):
    code = od.main([_script(tmp_path, "norun", NO_RUN)])
    out = capsys.readouterr().out
    assert code == 2
    assert "norun.py has no top-level run(client, username, password, ask)" in out
    assert out.rstrip().splitlines()[-1] == "OWNER_DRYRUN: FAIL"
    _untouched(live)


@USES
def test_a_prompt_without_an_answer_fails_with_its_text(live, tmp_path, capsys):
    code = od.main([_script(tmp_path, "disable", DISABLE)])
    out = capsys.readouterr().out
    assert code == 2, out
    assert "no --answer left for prompt: Disable the route? Type JA:" in out
    assert out.rstrip().splitlines()[-1] == "OWNER_DRYRUN: FAIL"
    _untouched(live)


def test_accept_degrade_needs_a_reason(capsys):
    assert od.main(["x.py", "--accept-degrade", "  "]) == 2
    assert capsys.readouterr().out.rstrip().splitlines()[-1] == "OWNER_DRYRUN: FAIL"


STATUS_OUT = """store: CONNECTED backend=openbao
  remedy: aeos vault status --verify
office/atlassian             NO_ACCESS backend=openbao              updated=- expires=-
personal/github              PRESENT  backend=openbao              updated=2026-10-20T10:00:00+00:00 expires=-
workers/openrouter           MISSING  backend=openbao              updated=- expires=-
workers/opencode-go          EXPIRED  backend=openbao              updated=2026-01-01T00:00:00+00:00 expires=2026-02-01T00:00:00+00:00
"""


def test_the_status_output_is_parsed_into_names_without_values():
    p = od.parse_presence(STATUS_OUT)
    assert p.available and p.backend == "openbao"
    assert set(p.present) == {"personal/github", "workers/opencode-go"}
    assert p.present["workers/opencode-go"] is not None and p.present["personal/github"] is None
    assert p.missing == {"workers/openrouter"} and p.no_access == set()  # office/* is not mirrored


def test_an_unavailable_store_is_mirrored_as_unavailable_and_garbage_is_refused():
    down = "store: BLOCKED backend=none\n  reason: no store\npersonal/github  UNKNOWN  backend=none  updated=- expires=-\n"
    p = od.parse_presence(down)
    assert not p.available and p.present == {}
    with pytest.raises(od.HarnessError, match="no 'store:' line"):
        od.parse_presence("Traceback (most recent call last):\n")


def test_has_run_reads_the_script_without_executing_it(tmp_path):
    bomb = tmp_path / "bomb.py"
    bomb.write_text("raise SystemExit('executed')\ndef run(c, u, p, a):\n    pass\n", encoding="utf-8")
    assert od.has_run(bomb)
    short = tmp_path / "short.py"
    short.write_text("def run(client):\n    pass\n", encoding="utf-8")
    assert not od.has_run(short)


# ------------------------------------------------------------------ round 3 review findings (bite tests)


def _snap(caps: dict, checks: dict, gating: dict):
    return od.Snapshot({k: (v, "") for k, v in caps.items()}, {k: (v, "") for k, v in checks.items()}, gating)


BEFORE_X = _snap({"CAP_X": "BLOCKED"}, {"a": "MISSING", "b": "VERIFIED"}, {"CAP_X": {"a": "MISSING"}})


def test_a_newly_failing_gating_check_is_red_even_when_the_capability_got_better(live, tmp_path, capsys, monkeypatch):
    """Review finding: the regression was flagged only under an unchanged status. Critical a recovers and required b
    newly fails: BLOCKED -> DEGRADED looks better and hid b VERIFIED -> MISSING (verdict OK). It is DEGRADED now."""
    after = _snap({"CAP_X": "DEGRADED"}, {"a": "VERIFIED", "b": "MISSING"}, {"CAP_X": {"b": "MISSING"}})
    snaps = iter([BEFORE_X, after])
    monkeypatch.setattr(od, "doctor_snapshot", lambda settings: next(snaps))
    code = od.main([_script(tmp_path, "noop", NOOP)])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "CAP_X: BLOCKED -> DEGRADED (gating check newly failing: b VERIFIED -> MISSING)" in out
    assert _cap_row(out, "CAP_X")[1:4] == ["BLOCKED", "DEGRADED", "WORSE"], out
    assert out.rstrip().splitlines()[-1] == "OWNER_DRYRUN: DEGRADED"
    _untouched(live)


def test_near_miss_a_recovery_without_a_new_failure_is_better_not_worse(capsys):
    after = _snap({"CAP_X": "READY"}, {"a": "VERIFIED", "b": "VERIFIED"}, {"CAP_X": {}})
    assert od.print_diff(BEFORE_X, after) == []
    assert _cap_row(capsys.readouterr().out, "CAP_X")[1:4] == ["BLOCKED", "READY", "better"]


CC_PROBE = LOGIN + '''

def run(client, username, password, ask):
    import socket

    _login(client, username, password)
    try:
        socket.create_connection(("127.0.0.1", int(ask("port"))), timeout=2).close()
        print("connected to the real Control Center")
    except OSError as exc:
        print("refused control center", type(exc).__name__)
'''


@pytest.fixture
def built_frontend(tmp_path, monkeypatch):
    """The Control Center build the dashboard needs: runtime.frontend_dist reads ``ctx.repo/frontend/dist``. Only that
    check sees a temporary repo holding ``frontend/dist/index.html`` (every other check keeps the real checkout)."""
    import dataclasses

    from aeos.doctor.checks import REGISTRY, load_all

    load_all()
    repo = tmp_path / "built-repo"
    (repo / "frontend" / "dist").mkdir(parents=True)
    (repo / "frontend" / "dist" / "index.html").write_text("<!doctype html><title>t</title>\n", encoding="utf-8")
    spec = REGISTRY["runtime.frontend_dist"]

    def with_repo(fn):
        return lambda ctx: fn(dataclasses.replace(ctx, repo=repo))

    monkeypatch.setitem(REGISTRY, "runtime.frontend_dist", dataclasses.replace(
        spec, fn=with_repo(spec.fn), fingerprint=with_repo(spec.fingerprint) if spec.fingerprint else None))
    return repo


@pytest.mark.parametrize("window", [True, False], ids=["doctor-sees-the-real-port", "bite-without-the-window"])
@pytest.mark.usefixtures("no_docker", "opencode_installed", "built_frontend")
def test_a_running_control_center_on_aeos_port_is_no_false_blocked(live, tmp_path, capsys, monkeypatch, window):
    """Review finding: with the real Control Center running, the guard refused its port, host.port could not bind it
    and reported FAILED PORT_BIND_DENIED, so CAN_OPEN_DASHBOARD was BLOCKED before and after. The Doctor now sees the
    port as `aeos doctor` does (CONNECTED through /api/health); the script stays refused. A test-owned listener on
    the pinned AEOS_PORT stands in for the running server, and the frontend build is present, so every other critical
    check of the dashboard passes: CAN_OPEN_DASHBOARD is READY before and after. Without the window (the old harness)
    the same setup is red with the bind failure and CAN_OPEN_DASHBOARD BLOCKED: the test bites."""
    import contextlib

    from aeos.core import config as config_mod
    from aeos.inventory.probes import HttpResult, set_runner_override
    from tests.unit.test_inventory_fakes import base_runner

    snaps: list = []
    real_snapshot = od.doctor_snapshot
    monkeypatch.setattr(od, "doctor_snapshot", lambda settings: snaps.append(real_snapshot(settings)) or snaps[-1])
    if not window:
        monkeypatch.setattr(od, "doctor_probe_window", lambda denied, port: contextlib.nullcontext())
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(16)
        port = srv.getsockname()[1]
        monkeypatch.setenv("AEOS_PORT", str(port))
        config_mod.reset_settings_cache()
        health = HttpResult(200, {"status": "ok", "version": "0.0.0-dryrun-test"})
        set_runner_override(base_runner(http={f"http://127.0.0.1:{port}/api/health": health}))
        try:
            code = od.main([_script(tmp_path, "ccprobe", CC_PROBE), "--answer", str(port)])
        finally:
            set_runner_override(None)
    out = capsys.readouterr().out
    assert len(snaps) == 2, out
    assert "refused control center ConnectionRefusedError" in out and "connected to" not in out
    for snap in snaps:
        status, summary = snap.checks["host.port"]
        assert snap.checks["runtime.frontend_dist"][0] == "VERIFIED", snap.checks["runtime.frontend_dist"]
        if window:
            assert status == "CONNECTED", summary
            assert snap.gating["CAN_OPEN_DASHBOARD"] == {}, snap.gating["CAN_OPEN_DASHBOARD"]
            assert snap.capabilities["CAN_OPEN_DASHBOARD"][0] == "READY", snap.capabilities["CAN_OPEN_DASHBOARD"]
        else:
            assert status == "FAILED" and "tidak dapat di-bind" in summary, summary
            assert snap.gating["CAN_OPEN_DASHBOARD"] == {"host.port": "FAILED"}, snap.gating["CAN_OPEN_DASHBOARD"]
            assert snap.capabilities["CAN_OPEN_DASHBOARD"][0] == "BLOCKED"
    assert _cap_row(out, "CAN_OPEN_DASHBOARD")[1:3] == (["READY", "READY"] if window else ["BLOCKED", "BLOCKED"]), out
    assert code == 0, out
    _untouched(live)


GETPASS = LOGIN + '''
import getpass
from getpass import getpass as imported_getpass


def run(client, username, password, ask):
    _login(client, username, password)
    token = getpass.getpass("Vault token: ")
    again = imported_getpass("Again: ")
    print("got", len(token), token == again)
'''


@pytest.mark.parametrize("answers", [["s3cr3t-v4lue", "s3cr3t-v4lue"], ["s3cr3t-v4lue"]], ids=["answered", "short"])
def test_getpass_inside_run_takes_the_answer_queue_never_the_terminal(live, tmp_path, capsys, monkeypatch, answers):
    """Review finding: getpass reads /dev/tty or the console, not sys.stdin, so it blocked the dry run on a real
    terminal. The real getpass is replaced by a tripwire here: reaching it fails the test, as it would hang a terminal."""
    import getpass

    def tripwire(prompt="Password: ", stream=None):
        raise AssertionError("the real getpass was reached: it would wait on the terminal")

    monkeypatch.setattr(getpass, "getpass", tripwire)
    argv = [_script(tmp_path, "getpass", GETPASS)]
    for a in answers:
        argv += ["--answer", a]
    code = od.main(argv)
    out = capsys.readouterr().out
    assert getpass.getpass is tripwire, "getpass was not restored"
    assert "the real getpass was reached" not in out
    assert "s3cr3t-v4lue" not in out, "a getpass answer was echoed"
    assert "Vault token: [--answer 1: (hidden, getpass)]" in out
    if len(answers) == 2:
        assert code == 0 and "got 12 True" in out, out
        assert out.rstrip().splitlines()[-1] == "OWNER_DRYRUN: OK"
    else:
        assert code == 2, out
        assert "no --answer left for prompt: Again:" in out
        assert out.rstrip().splitlines()[-1] == "OWNER_DRYRUN: FAIL"
    _untouched(live)


STATUS_OFFICE_UNKNOWN = """store: CONNECTED backend=openbao
office/atlassian             UNKNOWN  backend=none                 updated=- expires=-
personal/github              PRESENT  backend=openbao              updated=2026-10-20T10:00:00+00:00 expires=-
workers/opencode-go          PRESENT  backend=openbao              updated=2026-10-20T10:00:00+00:00 expires=-
"""


def test_an_unknown_office_row_does_not_fail_a_personal_dry_run(monkeypatch):
    """Review finding: the harness read the office view too, and an UNKNOWN office row made the whole store
    unavailable. Owner scripts run in PERSONAL: the status runs without --include-office and office rows are ignored."""
    import subprocess

    from aeos.core.errors import PolicyDenied

    seen: list = []

    def fake_run(argv, **kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, STATUS_OFFICE_UNKNOWN, "")

    monkeypatch.setattr(od.subprocess, "run", fake_run)
    p = od.probe_presence()
    assert seen and seen[0][-2:] == ["secrets", "status"] and "--include-office" not in seen[0]
    assert p.available and set(p.present) == {"personal/github", "workers/opencode-go"}
    assert not any(r.startswith("office/") for r in (*p.present, *p.missing, *p.no_access))
    mirror = od.PresenceMirror(od.Presence("openbao", True, present={"office/atlassian": None, "personal/github": None}))
    with pytest.raises(PolicyDenied):
        mirror.status("office/atlassian")
    assert mirror.status("personal/github").exists
    # near miss on the other side: an UNKNOWN PERSONAL row still makes the store unavailable
    personal_unknown = STATUS_OFFICE_UNKNOWN.replace("personal/github              PRESENT  backend=openbao",
                                                     "personal/github              UNKNOWN  backend=none   ")
    assert not od.parse_presence(personal_unknown).available


# ------------------------------------------------------------------ round 4: hermetic host endpoints (LESSONS L20)


@pytest.mark.parametrize("pinned", [True, False], ids=["pinned-port", "bite-default-port"])
@pytest.mark.usefixtures("no_docker", "opencode_installed")
def test_a_dry_run_test_never_contacts_the_default_control_center_port(live, tmp_path, capsys, monkeypatch, pinned):
    """Review finding: the 'live' fixture did not pin AEOS_PORT, so every dry run here probed and sent GET /api/health
    to whatever listened on 8787, the owner's running Control Center included. Every connect of a full dry run (real
    host.port probe) is recorded: with the fixture's pinned port none goes to the default port or the real OpenBao,
    and the Doctor probes the pinned port. Bite: without the pin the harness opens 8787 to the Doctor, the recorder
    sees the connect, and the SecretGuard refuses it (so even the bite never reaches the owner's server)."""
    from aeos.core import config as config_mod

    default = int(config_mod.Settings.model_fields["port"].default)
    default_bao = od._port(str(config_mod.Settings.model_fields["openbao_addr"].default), 8200)
    if not pinned:
        monkeypatch.delenv("AEOS_PORT")
        config_mod.reset_settings_cache()
    seen: list[tuple[int, str]] = []
    connect = socket.socket.connect

    def recorder(sock, address):
        if isinstance(address, tuple) and len(address) >= 2:
            try:
                connect(sock, address)
            except OSError as exc:
                seen.append((int(address[1]), str(exc)))
                raise
            seen.append((int(address[1]), "connected"))
            return
        connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", recorder)
    ports: list[int] = []
    real_snapshot = od.doctor_snapshot
    monkeypatch.setattr(od, "doctor_snapshot", lambda settings: ports.append(settings.port) or real_snapshot(settings))
    code = od.main([_script(tmp_path, "noop", NOOP)])
    out = capsys.readouterr().out
    assert code == 0, out
    at_default = [why for port, why in seen if port == default]
    assert not [p for p, _ in seen if p == default_bao], seen
    if pinned:
        assert ports == [PINNED_CC_PORT, PINNED_CC_PORT], ports
        assert at_default == [], seen
        assert [why for port, why in seen if port == PINNED_CC_PORT], "the recorder did not see the host.port probe"
    else:
        assert ports == [default, default], ports
        assert at_default and all("tests never reach the owner's real Control Center" in why for why in at_default), seen
    _untouched(live)


# ------------------------------------------------------------------ round 5: hermetic host binaries (review of 729940b)


@pytest.mark.parametrize("real_probes", [False, True], ids=["live-alone", "bite-real-probes"])
def test_a_dry_run_test_with_live_alone_starts_no_host_binary(live, tmp_path, capsys, monkeypatch, no_host_process,
                                                               real_probes):
    """Review finding: 'live' pinned the ports and the probe runner only, so a test that did not also request
    no_docker and opencode_installed ran the real ``docker version`` and ``docker image inspect`` (the owner's daemon,
    over its Unix socket) and the real ``opencode --version``. This test requests 'live' alone: no host process is
    attempted. Bite: the real probes are put back (with a fake docker and opencode on the path); the autouse guard
    refuses and records ``docker version`` and ``opencode --version``, so nothing runs even in the bite."""
    import shutil

    if real_probes:
        docker = tmp_path / "fake-bin" / "docker"
        docker.parent.mkdir()
        docker.write_text("", encoding="utf-8")
        real_which = shutil.which
        monkeypatch.setattr(shutil, "which",
                            lambda name, *a, **kw: str(docker) if name == "docker" else real_which(name, *a, **kw))
        monkeypatch.setattr(opencode_mod, "resolve_binary", lambda configured=None: tmp_path / "fake-bin" / "opencode")
        monkeypatch.setattr(sandbox_mod, "_docker_probe", REAL_DOCKER_PROBE)
        monkeypatch.setattr(opencode_mod, "opencode_status", REAL_OPENCODE_STATUS)
        monkeypatch.setitem(sandbox_mod._cache, "value", None)
        monkeypatch.setitem(opencode_mod._status_cache, "value", None)
    od.main([_script(tmp_path, "noop", NOOP)])
    out = capsys.readouterr().out
    started = list(no_host_process)
    no_host_process.clear()  # the bite's refused attempts are its evidence, not a failure of the guard
    if real_probes:
        assert [str(docker), "version", "--format", "{{.Server.Version}}"] in started, started
        assert [str(tmp_path / "fake-bin" / "opencode"), "--version"] in started, started
    else:
        assert started == [], started
        assert out.rstrip().splitlines()[-1] == "OWNER_DRYRUN: OK", out
    _untouched(live)


# --- background work a script starts is held, never run (L13, 2026-10-29) ------------------------------------------


def test_background_planning_work_is_held_and_restored():
    """A plan re-run inside a dry run must not start the four Opus planning stages for real: on 2026-10-29 one dry
    run of revise_plan.py left two `claude -p` planning processes running against the throwaway copy."""
    from aeos.planning import runner

    original = runner.start
    with od.background_work_held() as held:
        assert runner.start is not original
        assert runner.start("plan-1", actor="owner", action="run") is True
    assert held == ["planning run for plan plan-1"]
    assert runner.start is original  # restored, also for the rest of the process


def test_run_script_holds_background_work_a_script_starts(monkeypatch, capsys):
    """Red without the hold: the script's call reaches the real starter (here a sentinel that fails the test)."""
    import fastapi

    from aeos.api import app as app_module
    from aeos.planning import runner

    def real_start(*_a, **_k):
        raise AssertionError("the dry run started real background planning work")

    monkeypatch.setattr(runner, "start", real_start)
    monkeypatch.setattr(app_module, "create_app", lambda *a, **k: fastapi.FastAPI())

    def script_run(client, username, password, ask):
        from aeos.planning import runner as r
        return r.start("plan-9", actor=username, action="run")

    od.run_script(script_run, settings=None, username="owner", password="pw", answers=od.Answers([]))
    out = capsys.readouterr().out
    assert "[dry run] background work NOT started (it would start for real): planning run for plan plan-9" in out
    assert "run() returned: True" in out
    assert runner.start is real_start  # restored after the run
