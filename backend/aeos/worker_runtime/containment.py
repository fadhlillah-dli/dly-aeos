"""Negative containment tests, executed for real in a child process inside a sandbox (§25, §50, §51).

Each result is PASS / FAIL / NOT_ENFORCED / NOT_RUN with evidence. For the ``process`` sandbox the
filesystem/egress/keyring checks are expected to come back NOT_ENFORCED — that is the honest
answer on Windows/macOS/Linux without an OS boundary, and it is reported, never hidden.

The probe never reads secret content: it only checks reachability (open/stat/connect/API call
returning "not found"), and the "outside" files it touches are created by this test itself.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from aeos.core.timeutil import utcnow
from aeos.worker_runtime import processes

PROBE = r'''
import json, os, re, socket, sys
args = json.loads(sys.argv[1])
res = {}
def can_read(p):
    try:
        with open(p, "rb") as fh:
            fh.read(1)
        return True
    except OSError:
        return False
res["read_outside"] = {"target": "probe file outside worktree", "readable": can_read(args["outside_file"])}
res["read_data_db"] = {"target": "AEOS data-dir database", "exists": os.path.exists(args["db_path"]),
                       "readable": os.path.exists(args["db_path"]) and can_read(args["db_path"])}
real_ssh = args["real_ssh"]
res["real_ssh_dir"] = {"exists": os.path.isdir(real_ssh), "accessible": os.path.isdir(real_ssh) and os.access(real_ssh, os.R_OK)}
home_ssh = os.path.expanduser("~/.ssh")
res["home_ssh"] = {"inside_sandbox": os.path.abspath(home_ssh).lower().startswith(os.path.abspath(args["sandbox_home"]).lower()),
                   "exists": os.path.exists(home_ssh)}
pat = re.compile(r"(?i)(token|secret|passw|api[_-]?key|credential|anthropic|openrouter|github|aws_|vault|bao_)")
vpat = re.compile(r"(sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_|AKIA[0-9A-Z]{16}|aeoscap_|hvs\.)")
res["env"] = {"secret_names": sorted(k for k in os.environ if pat.search(k)),
              "secret_values": sorted(k for k, v in os.environ.items() if vpat.search(v))}
res["home"] = {k: os.environ.get(k, "") for k in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "TEMP", "TMPDIR")}
try:
    with open(args["write_target"], "w") as fh:
        fh.write("containment probe")
    res["write_outside"] = {"written": True}
except OSError as e:
    res["write_outside"] = {"written": False, "error": type(e).__name__}
try:
    s = socket.create_connection(("127.0.0.1", args["egress_port"]), timeout=3)
    s.close()
    res["egress"] = {"connected": True}
except OSError as e:
    res["egress"] = {"connected": False, "error": type(e).__name__}
try:
    import keyring
    keyring.get_password(args["keyring_service"], "__aeos_containment_probe__")
    res["keyring"] = {"reachable": True}
except Exception as e:
    res["keyring"] = {"reachable": False, "error": type(e).__name__}
print(json.dumps(res))
'''

TREE = (
    "import subprocess, sys, time\n"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
    "print(g.pid, flush=True)\n"
    "time.sleep(300)\n"
)


def _result(check: str, status: str, evidence: str, **extra: Any) -> dict[str, Any]:
    return {"check": check, "status": status, "evidence": evidence, **extra}


def _listener() -> tuple[socket.socket, int]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]

    def accept() -> None:
        srv.settimeout(10)
        try:
            while True:
                conn, _ = srv.accept()
                conn.close()
        except OSError:
            return

    threading.Thread(target=accept, daemon=True).start()
    return srv, port


def _kill_tree_check(sandbox: processes.SandboxDirs, env: dict[str, str]) -> dict[str, Any]:
    run_id = f"containment-{utcnow().strftime('%H%M%S%f')}"
    mp = processes.spawn(run_id, [sys.executable, "-c", TREE], cwd=sandbox.root, env=env)
    try:
        assert mp.stdout is not None
        line = mp.stdout.readline().decode().strip()
        gpid = int(line) if line.isdigit() else None
        res = processes.kill_run_processes(run_id)
        child_alive = processes.is_alive(mp.pid, mp.create_time)
        # is_alive handles a process vanishing between the checks (no false FAIL from a race).
        grand_alive = bool(gpid and processes.is_alive(gpid, None))
    except Exception as exc:
        processes.kill_run_processes(run_id)
        return _result("cancel_kills_tree", "FAIL", f"probe error {type(exc).__name__}")
    ok = not child_alive and not grand_alive and gpid is not None
    return _result("cancel_kills_tree", "PASS" if ok else "FAIL",
                   f"child alive={child_alive}, grandchild alive={grand_alive}; methods={res.get('methods')}")


def containment_report(sandbox_kind: str = "process") -> dict[str, Any]:
    from aeos.core.config import get_settings

    settings = get_settings()
    checked = utcnow().isoformat()
    if sandbox_kind != "process":
        from aeos.worker_runtime.sandbox import sandbox_capabilities

        caps = sandbox_capabilities(refresh=True)["levels"].get(sandbox_kind, {})
        reason = caps.get("evidence", "unknown sandbox kind")
        checks = ["read_outside_worktree", "read_data_db", "real_ssh_dir", "home_ssh_redirected", "env_secrets",
                  "home_redirected", "write_outside_worktree", "egress_non_broker", "keyring_unreachable",
                  "cancel_kills_tree"]
        status_note = "container containment suite is NOT implemented/proven in this build" if caps.get("available") \
            else f"{sandbox_kind} not available: {reason}"
        return {"kind": sandbox_kind, "checked_at": checked, "platform": sys.platform, "overall": "NOT_PROVEN",
                "evidence_level": "E0", "results": [_result(c, "NOT_RUN", status_note) for c in checks],
                "summary": {"NOT_RUN": len(checks)}}
    root = Path(tempfile.mkdtemp(prefix="aeos-containment-"))
    sandbox = processes.SandboxDirs.create(root / "sandbox")
    worktree = root / "sandbox" / "repo"
    worktree.mkdir(parents=True, exist_ok=True)
    outside = root / "outside-secret-probe.txt"
    outside.write_text("probe", encoding="utf-8")
    write_target = root / "written-outside.txt"
    env = processes.build_sandbox_env(sandbox, run_id="containment-probe")
    srv, port = _listener()
    args = {"outside_file": str(outside), "db_path": str(settings.layout.db_path),
            "real_ssh": str(Path.home() / ".ssh"), "sandbox_home": str(sandbox.home), "write_target": str(write_target),
            "egress_port": port, "keyring_service": settings.keyring_service}
    results: list[dict[str, Any]] = []
    try:
        proc = subprocess.run([sys.executable, "-I", "-c", PROBE, json.dumps(args)], cwd=str(worktree), env=env,
                              capture_output=True, text=True, timeout=60, check=False)
        data = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, IndexError) as exc:
        data = {}
        results.append(_result("probe", "FAIL", f"probe did not complete: {type(exc).__name__}"))
    finally:
        srv.close()
    if data:
        weak = "process sandbox has no OS filesystem boundary"
        results.append(_result("read_outside_worktree", "NOT_ENFORCED" if data["read_outside"]["readable"] else "PASS",
                               f"file outside worktree readable={data['read_outside']['readable']} ({weak})"))
        db = data["read_data_db"]
        results.append(_result("read_data_db", "NOT_ENFORCED" if db["readable"] else "PASS",
                               f"AEOS DB exists={db['exists']} readable={db['readable']} (content not read)"))
        ssh = data["real_ssh_dir"]
        results.append(_result("real_ssh_dir", "NOT_ENFORCED" if ssh["accessible"] else "PASS",
                               f"real ~/.ssh exists={ssh['exists']} accessible={ssh['accessible']} (not listed)"))
        hs = data["home_ssh"]
        results.append(_result("home_ssh_redirected", "PASS" if hs["inside_sandbox"] and not hs["exists"] else "FAIL",
                               f"~/.ssh resolves inside sandbox={hs['inside_sandbox']}, exists={hs['exists']}"))
        envr = data["env"]
        bad = envr["secret_names"] + envr["secret_values"]
        results.append(_result("env_secrets", "FAIL" if bad else "PASS",
                               "no secret-looking variables" if not bad else f"secret-looking variables: {sorted(set(bad))}"))
        home_ok = all(str(v).lower().startswith(str(sandbox.root).lower()) for v in data["home"].values() if v)
        results.append(_result("home_redirected", "PASS" if home_ok else "FAIL",
                               "HOME/USERPROFILE/APPDATA/XDG/TEMP point into the run sandbox" if home_ok
                               else f"not redirected: {data['home']}"))
        w = data["write_outside"]
        results.append(_result("write_outside_worktree", "NOT_ENFORCED" if w["written"] else "PASS",
                               f"write outside worktree succeeded={w['written']} (detected only inside the repo diff)"))
        eg = data["egress"]
        results.append(_result("egress_non_broker", "NOT_ENFORCED" if eg["connected"] else "PASS",
                               f"TCP connect to non-broker 127.0.0.1:{port} succeeded={eg['connected']} "
                               "(no egress filter in process sandbox)"))
        kr = data["keyring"]
        results.append(_result("keyring_unreachable", "NOT_ENFORCED" if kr["reachable"] else "PASS",
                               f"OS keyring API reachable={kr['reachable']} (no secret read)"))
    results.append(_kill_tree_check(sandbox, env))
    summary: dict[str, int] = {}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    overall = "FAIL" if summary.get("FAIL") else "WEAK" if summary.get("NOT_ENFORCED") else "PASS"
    import shutil

    shutil.rmtree(root, ignore_errors=True)
    return {"kind": "process", "checked_at": checked, "platform": sys.platform, "overall": overall,
            "evidence_level": "E2", "results": results, "summary": summary,
            "note": "E2 controlled test on this host; NOT_ENFORCED items are residual risks (ADR-0051)"}


def record_containment_report(session: Any, report: dict[str, Any], *, actor: str) -> None:
    """Persist the latest report (SystemSetting) + an evidence artifact when the collector exists."""
    from aeos.audit import service as audit
    from aeos.models.identity import SystemSetting
    from aeos.worker_runtime.sandbox import CONTAINMENT_SETTING_PREFIX

    key = CONTAINMENT_SETTING_PREFIX + str(report.get("kind"))
    row = session.get(SystemSetting, key)
    if row is None:
        session.add(SystemSetting(key=key, value=report))
    else:
        row.value = report
        row.version = (row.version or 1) + 1
    from aeos.verification import facade as verification

    try:
        ev = verification.collect_evidence(
            session, kind="PROBE", origin="collector", collector_identity="aeos.worker_runtime.containment",
            claim=f"sandbox containment ({report.get('kind')}): {report.get('overall')}", content=report,
            evidence_level=str(report.get("evidence_level", "E2")), subject_type="sandbox",
            subject_id=str(report.get("kind")), environment={"platform": report.get("platform")})
        report["evidence_id"] = ev.id
    except ImportError:
        report["evidence_id"] = None
    audit.record(session, actor_type="user" if actor.startswith("user:") else "cli", actor_id=actor,
                 action="worker_runtime.containment.run", target_type="sandbox", target_id=str(report.get("kind")),
                 result="SUCCESS", details={"overall": report.get("overall"), "summary": report.get("summary")})
