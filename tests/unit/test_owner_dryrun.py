"""scripts/owner_dryrun.py: an owner script is dry-run against a copy of the live database (LESSONS L13).

Incident 2026-10-28: a dry run against a copy with no secrets passed, and on the real system the script left
CAN_RUN_WORKER BLOCKED. The harness mirrors which secrets exist and compares the Doctor capabilities before and
after. Hermetic: the 'live' database is a synthetic one in tmp, the presence set is a fake (the real
``aeos secrets status`` never runs here), the SecretGuard of tests/conftest.py stays on, and the owner scripts are
tiny fakes written into tmp. Bite tests: a script that disables the only keyed WORKER route is red with
"CAN_RUN_WORKER: READY -> BLOCKED" (exit 1); the same with --accept-degrade exits 0 and prints the reason; a script
that changes nothing relevant is green (OWNER_DRYRUN: OK); a script without run() and a prompt without an answer
fail (exit 2) with their message. The live file is byte-identical afterwards and the temporary dir is gone.
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


def _load():
    spec = importlib.util.spec_from_file_location("aeos_script_owner_dryrun", ROOT / "scripts" / "owner_dryrun.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


od = _load()

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


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def live(settings, db, tmp_path, monkeypatch):
    """A synthetic 'live' database in tmp where CAN_RUN_WORKER is READY while KEY_REF is present."""
    from aeos.core.db import get_engine

    admin = _admin(db)
    _route(db)
    _activate(db, admin)
    _process_exception(db, admin)
    _enable_agent(db)
    db.commit()
    db.close()
    get_engine().dispose()  # no open connection: the WAL is checkpointed and the file is stable
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
    assert "the script asked a question with no --answer left: 'Disable the route? Type JA:'" in out
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
    assert p.missing == {"workers/openrouter"} and p.no_access == {"office/atlassian"}


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
