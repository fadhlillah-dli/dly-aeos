"""A scripted stand-in for the docker CLI (real child processes, a state directory instead of a daemon).

It implements only what ``aeos.worker_runtime.container`` calls: image/network/container inspect, network
create/connect, ``run -d`` (gateway), attached ``run`` (worker / probe / cancel probe), ``ps`` by label and
``rm -f``. Every call's argv is appended to ``calls.jsonl`` so tests can assert the exact hardening flags.
The probe answer is configured per test (compliant or leaky). This proves the AEOS side of the container
sandbox (argv, lifecycle, teardown, verdict logic) - NOT the isolation of a real Docker host, which only
``aeos worker containment --kind container`` on such a host can prove.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

FAKE_DOCKER = r'''
import json, os, pathlib, sys, time, uuid
st = pathlib.Path(os.environ["FAKE_DOCKER_STATE"])
cfg = json.loads((st / "config.json").read_text(encoding="utf-8"))
args = sys.argv[1:]
with open(st / "calls.jsonl", "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\n")
for d in ("networks", "gateways", "containers"):
    (st / d).mkdir(exist_ok=True)

def labels():
    return [args[i + 1] for i, a in enumerate(args) if a == "--label" and i + 1 < len(args)]

def containers():
    out = []
    for f in (st / "containers").glob("*.json"):
        try:
            out.append((f.stem, json.loads(f.read_text(encoding="utf-8"))["run_id"]))
        except (OSError, ValueError, KeyError):
            continue
    return out

if args[:1] == ["version"]:
    print("27.0.0"); sys.exit(0)
if args[:2] == ["image", "inspect"]:
    if args[-1] == cfg.get("verifier_image"):
        if cfg.get("verifier_image_present", True):
            print(cfg.get("verifier_image_id", "sha256:fakeverifier")); sys.exit(0)
        sys.stderr.write("No such image\n"); sys.exit(1)
    if cfg.get("image_present", True):
        print(cfg.get("image_id", "sha256:fakeimage")); sys.exit(0)
    sys.stderr.write("No such image\n"); sys.exit(1)
vimg = cfg.get("verifier_image")
if args[:1] == ["run"] and vimg and vimg in args:
    # verifier step: run the inner command on this host with the container paths mapped back to their bind
    # sources and the two tmpfs mounts to fresh host dirs (proves argv/paths/lifecycle, not isolation)
    import subprocess, tempfile
    with open(st / "verifier_runs.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(args) + "\n")
    inner = args[args.index(vimg) + 1:]
    vid = next(x.split("=", 1)[1] for x in labels() if x.startswith("aeos.verify_id="))
    cid = uuid.uuid4().hex[:12]
    marker = st / "containers" / f"{cid}.json"
    marker.write_text(json.dumps({"run_id": vid}), encoding="utf-8")
    maps = []
    for i, a in enumerate(args):
        if a == "--mount":
            spec = args[i + 1]
            src = spec.split("source=", 1)[1].split(",target=", 1)[0]
            tgt = spec.split(",target=", 1)[1].split(",", 1)[0]
            maps.append((tgt, src))
    for t in ("/tmp", "/home/verifier"):
        maps.append((t, tempfile.mkdtemp(prefix="fakefs-")))
    maps.sort(key=lambda m: len(m[0]), reverse=True)
    def host(v):
        if v == "/dev/null":
            return os.devnull
        for tgt, src in maps:
            if v == tgt:
                return src
            if v.startswith(tgt + "/"):
                return os.path.join(src, *v[len(tgt) + 1:].split("/"))
        return v
    envs = dict(args[j + 1].split("=", 1) for j, x in enumerate(args) if x == "-e")
    env = {k: host(v) for k, v in envs.items()}
    env["PYTHONPATH"] = os.pathsep.join(host(p) for p in envs.get("PYTHONPATH", "").split(":") if p)
    for k in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PATH"):
        if k in os.environ:
            env[k] = os.environ[k]
    exe = sys.executable if inner[0] == "python" else inner[0]
    cwd = host(args[args.index("--workdir") + 1])
    rc = subprocess.run([exe, *[host(a) for a in inner[1:]]], env=env, cwd=cwd, stdin=subprocess.DEVNULL).returncode
    import shutil
    for tgt, src in maps:
        if tgt in ("/tmp", "/home/verifier"):
            shutil.rmtree(src, ignore_errors=True)
    try:
        marker.unlink()
    except OSError:
        pass
    sys.exit(rc)
if args[:2] == ["network", "inspect"]:
    f = st / "networks" / args[-1]
    if not f.exists():
        sys.exit(1)
    print(f.read_text(encoding="utf-8")); sys.exit(0)
if args[:2] == ["network", "create"]:
    (st / "networks" / args[-1]).write_text("true" if "--internal" in args else "false", encoding="utf-8"); sys.exit(0)
if args[:2] == ["network", "connect"]:
    sys.exit(0)
if args[:1] == ["inspect"]:
    f = st / "gateways" / args[-1]
    if not f.exists():
        sys.exit(1)
    print(f.read_text(encoding="utf-8")); sys.exit(0)
if args[:1] == ["rm"]:
    for cid in [a for a in args[1:] if not a.startswith("-")]:
        for p in (st / "containers" / f"{cid}.json", st / "gateways" / cid):
            if p.exists():
                p.unlink()
    sys.exit(0)
if args[:1] == ["ps"]:
    flt = args[args.index("--filter") + 1] if "--filter" in args else ""
    want = flt.split("=", 2)[2] if flt.count("=") >= 2 else None
    for cid, rid in containers():
        if want is None or rid == want:
            print(f"{cid} {rid}" if "--format" in args else cid)
    sys.exit(0)
if args[:1] == ["run"]:
    lab = labels()
    image = cfg["image"]
    if "-d" in args:
        name = args[args.index("--name") + 1]
        port = next((x.split("=", 1)[1] for x in lab if x.startswith("aeos.broker_port=")), "")
        (st / "gateways" / name).write_text(f"true {port} {image}", encoding="utf-8")
        print(uuid.uuid4().hex); sys.exit(0)
    run_id = next(x.split("=", 1)[1] for x in lab if x.startswith("aeos.run_id="))
    cid = uuid.uuid4().hex[:12]
    marker = st / "containers" / f"{cid}.json"
    marker.write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    mounts = {}
    for i, a in enumerate(args):
        if a == "--mount":
            spec = args[i + 1]
            src = spec.split("source=", 1)[1].split(",target=", 1)[0]
            tgt = spec.split(",target=", 1)[1].split(",", 1)[0]
            mounts[tgt] = src
    inner = args[args.index(image) + 1:]
    entry = args[args.index("--entrypoint") + 1] if "--entrypoint" in args else None
    if entry == "node" and inner and inner[0].endswith("probe.js"):
        print(json.dumps(cfg["probe"]), flush=True)
    elif entry == "node":
        while marker.exists():
            time.sleep(0.2)
        sys.exit(137)
    elif inner[:1] == ["run"]:
        wt = pathlib.Path(mounts["/workspace"])
        (wt / "src").mkdir(exist_ok=True)
        (wt / "src" / "feature.py").write_text("VALUE = 7\n", encoding="utf-8")
        print(json.dumps({"type": "tool_use", "part": {"tool": "write", "state": {"status": "completed",
              "input": {"filePath": "/workspace/src/feature.py"}}}}), flush=True)
        print(json.dumps({"type": "step_finish", "part": {"reason": "stop", "tokens": {"input": 5, "output": 1}}}),
              flush=True)
    try:
        marker.unlink()
    except OSError:
        pass
    sys.exit(0)
sys.stderr.write("fake docker: unsupported " + " ".join(args) + "\n")
sys.exit(2)
'''


def compliant_probe() -> dict[str, Any]:
    return {
        "identity": {"uid": 10001, "gid": 10001},
        "mounts": [{"point": p} for p in ("/", "/workspace", "/aeos", "/tmp", "/home/worker", "/proc", "/dev",
                                          "/dev/shm", "/sys", "/etc/hosts", "/etc/hostname", "/etc/resolv.conf")],
        "host_paths": {"outside_file": False, "db": False, "data_dir": False, "real_ssh": False, "controller": False},
        "home": {"HOME": "/home/worker", "XDG_CONFIG_HOME": "/home/worker/.config", "TMPDIR": "/tmp"},
        "home_ssh": False,
        "env": {"secret_names": [], "secret_values": []},
        "writes": {"root": False, "etc": False, "usr": False, "config": False, "worktree": True},
        "sockets": {"docker": False, "ssh_agent": False, "dbus": False, "run_user": False},
        "credentials": {"git_credentials": False, "gh_hosts": False, "helpers": []},
        "cgroup": {"memory": "2147483648", "pids": "256"},
        "egress": [{"host": "1.1.1.1", "port": 443, "connected": False, "error": "ENETUNREACH"}],
        "gateway": {"connected": True},
        "gateway_filter": {"status": 403, "gateway": "deny"},
        "broker_path": {"status": 502, "gateway": "upstream-error"},
    }


class FakeDocker:
    def __init__(self, state: Path) -> None:
        self.state = state

    def configure(self, **kw: Any) -> None:
        cfg = json.loads((self.state / "config.json").read_text(encoding="utf-8"))
        cfg.update(kw)
        (self.state / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    def calls(self) -> list[list[str]]:
        f = self.state / "calls.jsonl"
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []

    def containers(self) -> list[str]:
        return [f.stem for f in (self.state / "containers").glob("*.json")] if (self.state / "containers").exists() else []

    def verifier_runs(self) -> list[list[str]]:
        f = self.state / "verifier_runs.jsonl"
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


def install_fake_docker(monkeypatch: Any, tmp_path: Path, **cfg: Any) -> FakeDocker:
    from aeos.worker_runtime import container, sandbox

    state = tmp_path / "fake-docker"
    state.mkdir()
    script = tmp_path / "fake_docker.py"
    script.write_text(FAKE_DOCKER, encoding="utf-8")
    (state / "config.json").write_text(json.dumps({"image": container.image_tag(), "probe": compliant_probe(),
                                                   **cfg}), encoding="utf-8")
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    monkeypatch.setattr(container, "docker_cmd", lambda: [sys.executable, str(script)])
    monkeypatch.setattr(sandbox, "_docker_probe", lambda timeout=10.0: {
        "available": True, "evidence": "docker server 27.0.0 responded (fake)", "cli": "docker",
        "server_version": "27.0.0"})
    sandbox._cache.update(at=0.0, value=None)
    return FakeDocker(state)
