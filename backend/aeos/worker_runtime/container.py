"""Container sandbox for workers (§11 "lingkungan per tugas dalam container", §25 minimum controls, ADR-0051).

What this module does (software implemented; NOT_PROVEN on a host until the containment suite below
passes there - on the build host, Windows 11 + Docker 29.8.2, the acceptance containment test passed against the
real daemon on 2026-10-26; admission still reads only the owner's recorded report):

* ``image_status`` - the pinned worker image ``dly-aeos-worker:<opencode pin>`` built from
  ``sandbox/Dockerfile`` (never auto-built or pulled: the owner builds it, §07 no auto-install).
* ``ensure_network`` - an ``--internal`` docker network per domain profile: containers on it have no
  route to the host or the internet.
* ``ensure_gateway`` - the only endpoint reachable on that network: a tiny fixed HTTP forwarder that
  passes ``/broker/v1/*`` (and nothing else) to the AEOS broker on the host. Worker egress is therefore
  "default deny with the approved broker" (§25).
* ``worker_argv`` - the hardened ``docker run`` argv (``sandbox.container_command``): non-root user,
  read-only root fs, tmpfs /tmp and HOME, cap-drop ALL, no-new-privileges, memory/cpu/pids limits, only
  the run worktree (rw) and the per-run config dir (ro) mounted; never the docker socket, host home, SSH
  agent, data dir or credential stores. Containers carry the ``aeos.run_id`` label.
* ``teardown`` / ``reap_orphans`` - remove a run's containers by label (kill controller), and remove
  containers of runs that are no longer active (controller crash).
* ``containment_report`` - the negative containment suite executed INSIDE the worker image on this host
  (filesystem, mounts, egress, credentials, keyring, docker socket, non-root, resource limits, cancel).
  A check is PASS only on positive evidence of denial; anything unobserved is NOT_RUN, never PASS.

Profiles: PERSONAL and OFFICE use separate networks, gateways and container users, and their worktrees
live in separate per-domain data partitions (``DataLayout.task_data(domain, ...)``); each profile has
its own containment report (§11 "OFFICE execution with separate identity/process and separate
filesystem/egress").
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aeos.core.enums import Domain
from aeos.core.timeutil import utcnow

IMAGE_REPOSITORY = "dly-aeos-worker"
CONTAINER_WORKTREE = "/workspace"
CONTAINER_CONFIG = "/aeos"
CONTAINER_HOME = "/home/worker"
GATEWAY_PORT = 8787
LABEL_RUN = "aeos.run_id"
LABEL_MANAGED = "aeos.managed"
DOCKER_TIMEOUT = 60
DEFAULT_MEMORY_MB = 2048
DEFAULT_CPUS = "2"
DEFAULT_PIDS = 256


@dataclass(frozen=True)
class Profile:
    domain: str
    network: str
    gateway: str
    user: str  # uid:gid inside the container


PROFILES: dict[str, Profile] = {
    Domain.PERSONAL.value: Profile(Domain.PERSONAL.value, "aeos-worker", "aeos-broker-gw", "10001:10001"),
    Domain.OFFICE.value: Profile(Domain.OFFICE.value, "aeos-worker-office", "aeos-broker-gw-office", "10002:10002"),
}


class ContainerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def profile_for(domain: str) -> Profile:
    try:
        return PROFILES[str(domain)]
    except KeyError as exc:
        raise ContainerError("UNKNOWN_DOMAIN", f"no container profile for domain {domain!r}") from exc


def image_tag() -> str:
    from aeos.worker_runtime.opencode import PINNED_VERSION

    return f"{IMAGE_REPOSITORY}:{PINNED_VERSION}"


def docker_cmd() -> list[str] | None:
    """argv prefix of the docker CLI (a list so tests can substitute an interpreter + script)."""
    path = shutil.which("docker")
    return [path] if path else None


def docker_cli_env() -> dict[str, str]:
    """Environment for the docker CLI process itself: the controller's own environment (docker contexts and
    config live in the real HOME) minus anything secret-looking. The worker never sees it: the container gets
    only the explicit ``-e`` variables of ``worker_env``."""
    from aeos.worker_runtime.processes import is_secret_like

    return {k: v for k, v in os.environ.items() if not is_secret_like(k, v)}


def run_docker(args: list[str], *, timeout: float = DOCKER_TIMEOUT) -> subprocess.CompletedProcess[str]:
    cmd = docker_cmd()
    if not cmd:
        raise ContainerError("DOCKER_MISSING", "docker CLI not found on PATH")
    try:
        return subprocess.run([*cmd, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=docker_cli_env(), stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContainerError("DOCKER_FAILED", f"docker {args[0] if args else ''} failed: {type(exc).__name__}") from exc


def _err(proc: subprocess.CompletedProcess[str]) -> str:
    from aeos.core.redaction import redact_text

    return redact_text((proc.stderr or proc.stdout or "").strip())[:300]


# ----------------------------------------------------------------------------- image / network / gateway
def image_status() -> dict[str, Any]:
    tag = image_tag()
    try:
        proc = run_docker(["image", "inspect", "--format", "{{.Id}}", tag], timeout=30)
    except ContainerError as exc:
        return {"tag": tag, "present": False, "id": None, "evidence": exc.message}
    image_id = (proc.stdout or "").strip()
    if proc.returncode != 0 or not image_id:
        return {"tag": tag, "present": False, "id": None,
                "evidence": f"image {tag} not built on this host (docker build -t {tag} sandbox/)"}
    return {"tag": tag, "present": True, "id": image_id, "evidence": f"image {tag} present"}


def ensure_network(profile: Profile) -> dict[str, Any]:
    """The profile's ``--internal`` network (created when missing). An existing non-internal network with
    the same name is refused, never used."""
    proc = run_docker(["network", "inspect", "--format", "{{.Internal}}", profile.network], timeout=30)
    created = False
    if proc.returncode != 0:
        mk = run_docker(["network", "create", "--internal", "--label", f"{LABEL_MANAGED}=network",
                         "--label", f"aeos.domain={profile.domain}", profile.network], timeout=60)
        if mk.returncode != 0:
            raise ContainerError("NETWORK_CREATE_FAILED", f"docker network create {profile.network}: {_err(mk)}")
        created = True
        proc = run_docker(["network", "inspect", "--format", "{{.Internal}}", profile.network], timeout=30)
    if proc.returncode != 0 or (proc.stdout or "").strip().lower() != "true":
        raise ContainerError("NETWORK_NOT_INTERNAL", f"docker network {profile.network} is not --internal; remove it "
                             "(docker network rm) so AEOS can create the isolated worker network")
    return {"network": profile.network, "internal": True, "created": created}


# Fixed forwarder run in the worker image on the internal network: only /broker/v1/* is forwarded to the
# AEOS broker on the host (Host header rewritten to loopback, cookies dropped); every other path is 403.
GATEWAY_JS = r"""
const http = require('http');
const [listenPort, targetHost, targetPort] = process.argv.slice(1);
const PREFIX = '/broker/v1/';
http.createServer((req, res) => {
  const url = req.url || '';
  if (!url.startsWith(PREFIX) || url.includes('..') || url.includes('\\')) {
    res.writeHead(403, {'content-type': 'application/json', 'x-aeos-gateway': 'deny'});
    res.end('{"error":{"code":"GATEWAY_PATH_DENIED","message":"only /broker/v1/ is reachable from the worker sandbox"}}');
    return;
  }
  const headers = Object.assign({}, req.headers);
  delete headers['cookie'];
  headers['host'] = '127.0.0.1:' + targetPort;
  const up = http.request({host: targetHost, port: Number(targetPort), method: req.method, path: url, headers}, (ur) => {
    const h = Object.assign({}, ur.headers, {'x-aeos-gateway': 'forwarded'});
    res.writeHead(ur.statusCode || 502, h);
    ur.pipe(res);
  });
  up.on('error', () => {
    if (!res.headersSent) res.writeHead(502, {'content-type': 'application/json', 'x-aeos-gateway': 'upstream-error'});
    res.end('{"error":{"code":"BROKER_UNREACHABLE","message":"AEOS broker not reachable from the gateway"}}');
  });
  req.pipe(up);
}).listen(Number(listenPort), '0.0.0.0');
"""


def ensure_gateway(profile: Profile, *, broker_port: int, image: str) -> dict[str, Any]:
    """Start (or reuse) the profile's broker gateway: on the internal network AND the default bridge (so it,
    and only it, reaches the host broker port)."""
    fmt = '{{.State.Running}} {{index .Config.Labels "aeos.broker_port"}} {{.Config.Image}}'
    proc = run_docker(["inspect", "--format", fmt, profile.gateway], timeout=30)
    if proc.returncode == 0 and (proc.stdout or "").split() == ["true", str(broker_port), image]:
        return {"gateway": profile.gateway, "reused": True}
    if proc.returncode == 0:
        run_docker(["rm", "-f", profile.gateway], timeout=60)
    start = run_docker([
        "run", "-d", "--name", profile.gateway, "--label", f"{LABEL_MANAGED}=broker-gateway",
        "--label", f"aeos.broker_port={broker_port}", "--label", f"aeos.domain={profile.domain}",
        "--network", profile.network, "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--user", "10001:10001", "--memory", "128m", "--pids-limit", "64",
        "--add-host", "host.docker.internal:host-gateway", "--entrypoint", "node", image,
        "-e", GATEWAY_JS, str(GATEWAY_PORT), "host.docker.internal", str(broker_port)], timeout=120)
    if start.returncode != 0:
        raise ContainerError("GATEWAY_START_FAILED", f"broker gateway did not start: {_err(start)}")
    link = run_docker(["network", "connect", "bridge", profile.gateway], timeout=60)
    if link.returncode != 0:
        run_docker(["rm", "-f", profile.gateway], timeout=60)
        raise ContainerError("GATEWAY_START_FAILED", f"broker gateway could not reach the host network: {_err(link)}")
    return {"gateway": profile.gateway, "reused": False}


def broker_url(profile: Profile) -> str:
    return f"http://{profile.gateway}:{GATEWAY_PORT}/broker/v1"


# ----------------------------------------------------------------------------- granted shell: AEOS runner (§42)
# §42 "output command ringkas ... simpan log asli": when a TaskSpec grants a shell (container sandbox only), every
# allowed command runs through this fixed runner (OpenCode ``bash`` permission patterns allow ONLY
# ``node /aeos/aeos-run.js <command>``). It tees the complete stdout+stderr to a per-command log in the worktree
# (``.aeos/cmdlog/``; the runtime takes those logs out of the candidate and stores them as untrusted evidence) and
# returns a bounded head/tail to the engine context. Written into the per-run config dir (mounted read-only at
# /aeos), so it needs no image rebuild. It is a compaction/retention convention, not a security boundary (the
# container is): a compound command could keep part of its output out of the runner, never out of the sandbox.
SHELL_RUNNER_FILE = "aeos-run.js"
SHELL_RUNNER_COMMAND = f"node {CONTAINER_CONFIG}/{SHELL_RUNNER_FILE}"
COMMAND_LOG_DIR = ".aeos/cmdlog"  # relative to the worktree
RUNNER_HEAD_LINES = 40
RUNNER_TAIL_LINES = 80
RUNNER_MAX_LINE_CHARS = 400
SHELL_RUNNER_JS = r"""
'use strict';
const {spawn} = require('child_process');
const fs = require('fs');
const path = require('path');
const HEAD = __HEAD__, TAIL = __TAIL__, MAXLINE = __MAXLINE__;
const args = process.argv.slice(2);
if (!args.length) { process.stderr.write('aeos-run: usage: node /aeos/aeos-run.js <command>\n'); process.exit(2); }
const cmd = args.join(' ');
const dir = process.env.AEOSRUN_LOG_DIR || '/workspace/__LOGDIR__';
const shell = process.env.AEOSRUN_SHELL ? JSON.parse(process.env.AEOSRUN_SHELL) : ['/bin/sh', '-c'];
fs.mkdirSync(dir, {recursive: true});
const name = Date.now() + '-' + process.pid + '.log';
const logPath = path.join(dir, name);
const fd = fs.openSync(logPath, 'w');
fs.writeSync(fd, '$ ' + cmd + '\n');
const head = [], tail = [];
let total = 0;
const partial = {out: '', err: ''};
const line = (l) => {
  total += 1;
  const s = l.length > MAXLINE ? l.slice(0, MAXLINE) + ' [line truncated]' : l;
  if (head.length < HEAD) { head.push(s); } else { tail.push(s); if (tail.length > TAIL) tail.shift(); }
};
const feed = (key) => (buf) => {
  fs.writeSync(fd, buf);
  const parts = (partial[key] + buf.toString('utf8')).split('\n');
  partial[key] = parts.pop();
  parts.forEach(line);
};
const child = spawn(shell[0], [...shell.slice(1), cmd], {stdio: ['ignore', 'pipe', 'pipe'], env: process.env});
child.stdout.on('data', feed('out'));
child.stderr.on('data', feed('err'));
let failed = null;
child.on('error', (e) => { failed = e; });
child.on('close', (code, signal) => {
  for (const k of ['out', 'err']) { if (partial[k]) line(partial[k]); }
  if (failed) fs.writeSync(fd, '\n[aeos-run: could not start the command: ' + failed.code + ']\n');
  fs.writeSync(fd, '\n[aeos-run: exit ' + (signal ? 'signal ' + signal : code) + ']\n');
  fs.closeSync(fd);
  const omitted = total - head.length - tail.length;
  const out = [...head];
  if (omitted > 0) out.push('[aeos-run: ' + omitted + ' line(s) omitted; the full output is kept in __LOGDIR__/' + name +
                            ' and collected by the AEOS runtime as evidence]');
  out.push(...tail);
  out.push('[aeos-run: exit ' + (signal ? 'signal ' + signal : code) + ', ' + total + ' line(s) of output]');
  process.stdout.write(out.join('\n') + '\n', () => process.exit(failed ? 127 : (code === null ? 128 : code)));
});
""".replace("__HEAD__", str(RUNNER_HEAD_LINES)).replace("__TAIL__", str(RUNNER_TAIL_LINES)) \
    .replace("__MAXLINE__", str(RUNNER_MAX_LINE_CHARS)).replace("__LOGDIR__", COMMAND_LOG_DIR)


def write_shell_runner(config_dir: Path) -> Path:
    """Place the runner in the per-run config dir (mounted read-only at /aeos inside the worker container)."""
    path = config_dir / SHELL_RUNNER_FILE
    path.write_text(SHELL_RUNNER_JS, encoding="utf-8")
    return path


def prepare_launch(settings: Any, *, domain: str) -> dict[str, Any]:
    """Everything a worker container needs before ``docker run``; raises ContainerError (fail closed)."""
    profile = profile_for(domain)
    image = image_status()
    if not image["present"]:
        raise ContainerError("IMAGE_MISSING", image["evidence"])
    net = ensure_network(profile)
    gw = ensure_gateway(profile, broker_port=int(settings.port), image=image["tag"])
    return {"profile": profile, "image": image["tag"], "image_id": image["id"], "network": net["network"],
            "gateway": gw["gateway"], "broker_url": broker_url(profile)}


# ----------------------------------------------------------------------------- worker launch
def worker_env(run_id: str, extra: dict[str, str]) -> dict[str, str]:
    """The ONLY environment a worker container receives (paths/flags; no secret ever)."""
    from aeos.worker_runtime.processes import RUN_MARKER, is_secret_like

    env = {
        "HOME": CONTAINER_HOME, "XDG_CONFIG_HOME": f"{CONTAINER_HOME}/.config",
        "XDG_DATA_HOME": f"{CONTAINER_HOME}/.local/share", "XDG_CACHE_HOME": f"{CONTAINER_HOME}/.cache",
        "XDG_STATE_HOME": f"{CONTAINER_HOME}/.local/state", "TMPDIR": "/tmp",  # noqa: S108 - container tmpfs
        "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "NO_UPDATE_NOTIFIER": "1", RUN_MARKER: run_id,
        **extra,
    }
    return {k: v for k, v in env.items() if not is_secret_like(k, v)}


def limits_for(limits: dict[str, Any] | None) -> dict[str, Any]:
    lim = limits or {}
    mem = int(lim.get("max_memory_mb") or DEFAULT_MEMORY_MB)
    return {"memory": f"{max(256, min(mem, 65536))}m", "cpus": str(lim.get("max_cpus") or DEFAULT_CPUS),
            "pids": max(16, min(int(lim.get("max_processes") or DEFAULT_PIDS), 4096))}


def worker_argv(*, profile: Profile, image: str, run_id: str, worktree: Path, config_dir: Path,
                inner_argv: list[str], env: dict[str, str], limits: dict[str, Any] | None = None,
                entrypoint: str | None = None, labels: dict[str, str] | None = None) -> list[str]:
    from aeos.worker_runtime.sandbox import container_command

    lim = limits_for(limits)
    return container_command(image=image, run_id=run_id, worktree=str(worktree), config_dir=str(config_dir),
                             inner_argv=inner_argv, env=env, network=profile.network, memory=lim["memory"],
                             cpus=lim["cpus"], pids=lim["pids"], user=profile.user, docker=docker_cmd(),
                             entrypoint=entrypoint, labels={"aeos.domain": profile.domain, **(labels or {})})


# ----------------------------------------------------------------------------- teardown / orphans
def teardown(run_id: str) -> dict[str, Any]:
    """Remove every container of a run (by label). Idempotent; never raises."""
    try:
        proc = run_docker(["ps", "-aq", "--filter", f"label={LABEL_RUN}={run_id}"], timeout=30)
    except ContainerError as exc:
        return {"run_id": run_id, "removed": [], "error": exc.code}
    ids = (proc.stdout or "").split()
    if ids:
        try:
            run_docker(["rm", "-f", *ids], timeout=60)
        except ContainerError as exc:
            return {"run_id": run_id, "removed": [], "error": exc.code, "found": ids}
    return {"run_id": run_id, "removed": ids}


def remaining(run_id: str) -> list[str] | None:
    """Container ids still present for a run (None when docker cannot be asked)."""
    try:
        proc = run_docker(["ps", "-aq", "--filter", f"label={LABEL_RUN}={run_id}"], timeout=30)
    except ContainerError:
        return None
    return (proc.stdout or "").split() if proc.returncode == 0 else None


def reap_orphans(active_run_ids: set[str]) -> list[str]:
    """Remove worker containers whose run is not active any more (controller crash, lost lease)."""
    try:
        proc = run_docker(["ps", "-a", "--filter", f"label={LABEL_RUN}", "--format",
                           '{{.ID}} {{.Label "' + LABEL_RUN + '"}}'], timeout=30)
    except ContainerError:
        return []
    removed: list[str] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] not in active_run_ids and not parts[1].startswith("containment-"):
            try:
                run_docker(["rm", "-f", parts[0]], timeout=60)
                removed.append(parts[1])
            except ContainerError:
                continue
    return removed


# ----------------------------------------------------------------------------- verifier runner (§20 Verifier ENF)
# The independent verifier executes candidate code (trusted suite against the candidate, the candidate's own
# tests, the migration round-trip). On a host whose container boundary is PROVEN (a fresh PASS containment
# report for this domain profile, the same proof that admits container workers) it runs that code in the
# pinned verifier image built from sandbox/verifier.Dockerfile: no network at all, read-only root, non-root,
# no capabilities, and only the verifier's own run directory mounted (snapshot, trusted-suite copy and ini
# read-only; the report directory writable). Never the data dir/DB, the controller checkout, the host home,
# the docker socket, an SSH agent or a credential store. Not proven on a real host here (no Docker).
VERIFIER_IMAGE_REPOSITORY = "dly-aeos-verifier"
VERIFIER_LABEL = "aeos.verify_id"
VERIFIER_HOME = "/home/verifier"
VERIFIER_USERS = {Domain.PERSONAL.value: "10003:10003", Domain.OFFICE.value: "10004:10004"}
VERIFIER_MEMORY = "4g"
VERIFIER_CPUS = "2"
VERIFIER_PIDS = 512


def verifier_image_tag() -> str:
    from aeos import __version__

    return f"{VERIFIER_IMAGE_REPOSITORY}:{__version__}"


def verifier_image_status() -> dict[str, Any]:
    tag = verifier_image_tag()
    try:
        proc = run_docker(["image", "inspect", "--format", "{{.Id}}", tag], timeout=30)
    except ContainerError as exc:
        return {"tag": tag, "present": False, "id": None, "evidence": exc.message}
    image_id = (proc.stdout or "").strip()
    if proc.returncode != 0 or not image_id:
        return {"tag": tag, "present": False, "id": None,
                "evidence": f"verifier image {tag} not built on this host (docker build -f sandbox/verifier.Dockerfile "
                            f"-t {tag} .)"}
    return {"tag": tag, "present": True, "id": image_id, "evidence": f"verifier image {tag} present"}


def verifier_readiness(session: Any, *, domain: str) -> dict[str, Any]:
    """Whether the verifier may run candidate code in the container runner now (READY) or only in the WEAK
    process runner (UNAVAILABLE + reasons). READY needs a fresh PASS container containment report for this
    host, image and domain profile (``sandbox.container_report_problems``) AND the pinned verifier image."""
    from aeos.worker_runtime.sandbox import _last_containment, container_report_problems, sandbox_capabilities

    caps_all = sandbox_capabilities()
    caps = caps_all["levels"]
    container = caps["container"]
    tag = verifier_image_tag()
    base: dict[str, Any] = {"domain": domain, "image": tag, "image_id": None, "checked_at": caps_all["checked_at"],
                            "network": "none", "user": VERIFIER_USERS.get(domain)}
    remedy = (f"Install/start Docker, prove the container boundary (`aeos worker containment --kind container"
              f"{' --domain OFFICE' if domain == Domain.OFFICE.value else ''}` until PASS) and build the verifier "
              f"image (`docker build -f sandbox/verifier.Dockerfile -t {tag} .`)")
    if domain not in VERIFIER_USERS:
        return {**base, "status": "UNAVAILABLE", "reasons": [f"no verifier container profile for domain {domain!r}"],
                "remedies": [remedy]}
    if not container.get("available"):
        return {**base, "status": "UNAVAILABLE", "remedies": [remedy],
                "reasons": ["container sandbox not available: " + str(container.get("evidence"))]}
    if session is None:  # e.g. a Doctor run without the database: the containment proof cannot be read
        return {**base, "status": "UNAVAILABLE", "remedies": [remedy],
                "reasons": ["no database session: the container containment report cannot be read (NOT-PROVEN)"]}
    report = _last_containment(session, "container", domain)
    problems = container_report_problems(report, domain=domain, caps=caps)
    image = verifier_image_status()
    if not image["present"]:
        problems.append(str(image["evidence"]))
    base["image_id"] = image.get("id")
    if problems:
        return {**base, "status": "UNAVAILABLE", "reasons": problems, "remedies": [remedy]}
    return {**base, "status": "READY", "reasons": [], "remedies": [],
            "containment": {"overall": (report or {}).get("overall"), "checked_at": (report or {}).get("checked_at"),
                            "image": (report or {}).get("image"), "evidence_id": (report or {}).get("evidence_id")}}


def verifier_argv(*, domain: str, verify_id: str, mounts: list[tuple[str, str, bool]], env: dict[str, str],
                  inner_argv: list[str], workdir: str = "/tmp", image: str | None = None,  # noqa: S108 - tmpfs
                  user: str | None = None) -> list[str]:
    """Hardened ``docker run`` argv of one verifier step: ``--network none``, read-only root fs, non-root user,
    ``--cap-drop ALL``, no-new-privileges, pid/memory/cpu limits, tmpfs /tmp and HOME, and exactly the given
    bind mounts (host path, container path, read-only). Refuses a mount of the docker socket or a target
    outside /v (fail closed)."""
    if domain not in VERIFIER_USERS:
        raise ContainerError("UNKNOWN_DOMAIN", f"no verifier container profile for domain {domain!r}")
    uid = (user or VERIFIER_USERS[domain]).split(":")
    cli = docker_cmd() or ["docker"]
    argv = [
        *cli, "run", "--rm", "--pull", "never", "--name", f"aeos-verify-{verify_id[:24]}",
        "--user", ":".join(uid), "--read-only", "--network", "none",
        "--tmpfs", "/tmp:rw,exec,nosuid,size=2g",  # noqa: S108 - container-internal tmpfs
        "--tmpfs", f"{VERIFIER_HOME}:rw,nosuid,size=256m,uid={uid[0]},gid={uid[-1]}",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", str(VERIFIER_PIDS),
        "--memory", VERIFIER_MEMORY, "--memory-swap", VERIFIER_MEMORY, "--cpus", VERIFIER_CPUS,
        "--label", f"{LABEL_MANAGED}=verifier", "--label", f"{VERIFIER_LABEL}={verify_id}",
        "--label", f"aeos.domain={domain}",
    ]
    for host, target, readonly in mounts:
        low = str(host).replace("\\", "/").lower()
        if "docker.sock" in low or "docker_engine" in low or not target.startswith("/v/") or "," in str(host):
            raise ContainerError("MOUNT_REFUSED", f"verifier mount refused: {target}")
        argv += ["--mount", f"type=bind,source={host},target={target}" + (",readonly" if readonly else "")]
    argv += ["--workdir", workdir]
    for k, v in sorted(env.items()):
        argv += ["-e", f"{k}={v}"]
    return [*argv, image or verifier_image_tag(), *inner_argv]


def teardown_verifier(verify_id: str) -> dict[str, Any]:
    """Remove the verifier step's container(s) by label (the docker CLI being killed on a timeout does not
    stop a container). Idempotent; never raises."""
    try:
        proc = run_docker(["ps", "-aq", "--filter", f"label={VERIFIER_LABEL}={verify_id}"], timeout=30)
    except ContainerError as exc:
        return {"verify_id": verify_id, "removed": [], "error": exc.code}
    ids = (proc.stdout or "").split()
    if ids:
        try:
            run_docker(["rm", "-f", *ids], timeout=60)
        except ContainerError as exc:
            return {"verify_id": verify_id, "removed": [], "error": exc.code, "found": ids}
    return {"verify_id": verify_id, "removed": ids}


def reap_orphans_for_session(session: Any) -> list[str]:
    from sqlalchemy import select

    from aeos.models.workforce import WorkerRun

    active = set(session.scalars(select(WorkerRun.id).where(WorkerRun.execution_status.in_(("LEASED", "RUNNING")))))
    return reap_orphans(active)


# ----------------------------------------------------------------------------- containment suite
PROBE_JS = r"""
const fs = require('fs'), net = require('net'), os = require('os'), path = require('path'), http = require('http');
const a = JSON.parse(process.argv[2]);
const r = {};
const exists = (p) => { try { fs.lstatSync(p); return true; } catch (e) { return false; } };
const canRead = (p) => { try { fs.readFileSync(p); return true; } catch (e) { return false; } };
const canWrite = (p) => { try { fs.writeFileSync(p, 'aeos containment probe'); try { fs.unlinkSync(p); } catch (e) {} return true; } catch (e) { return false; } };
const readText = (p) => { try { return fs.readFileSync(p, 'utf8').trim(); } catch (e) { return null; } };
r.identity = {uid: process.getuid ? process.getuid() : null, gid: process.getgid ? process.getgid() : null};
r.mounts = (readText('/proc/self/mountinfo') || '').split('\n').filter(Boolean).map((l) => {
  const f = l.split(' '); const i = f.indexOf('-'); return {point: f[4], opts: f[5], fstype: i >= 0 ? f[i + 1] : null};
});
r.host_paths = {outside_file: canRead(a.outside_file), db: canRead(a.db_path), data_dir: exists(a.data_dir),
                real_ssh: exists(a.real_ssh), controller: exists(a.controller_root)};
r.home = {HOME: process.env.HOME || '', XDG_CONFIG_HOME: process.env.XDG_CONFIG_HOME || '', TMPDIR: process.env.TMPDIR || ''};
r.home_ssh = exists(path.join(os.homedir(), '.ssh'));
const pat = /(token|secret|passw|api[_-]?key|credential|anthropic|openrouter|github|aws_|vault|bao_)/i;
const vpat = /(sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_|AKIA[0-9A-Z]{16}|aeoscap_|hvs\.)/;
r.env = {secret_names: Object.keys(process.env).filter((k) => pat.test(k)).sort(),
         secret_values: Object.entries(process.env).filter(([k, v]) => vpat.test(v)).map(([k]) => k).sort()};
r.writes = {root: canWrite('/aeos-probe.txt'), etc: canWrite('/etc/aeos-probe.txt'), usr: canWrite('/usr/aeos-probe.txt'),
            config: canWrite('/aeos/aeos-probe.txt'), worktree: canWrite('/workspace/.aeos-containment-probe')};
r.sockets = {docker: exists('/var/run/docker.sock') || exists('/run/docker.sock'), ssh_agent: !!process.env.SSH_AUTH_SOCK,
             dbus: !!process.env.DBUS_SESSION_BUS_ADDRESS || exists('/run/dbus/system_bus_socket') || exists('/var/run/dbus/system_bus_socket'),
             run_user: exists('/run/user')};
r.credentials = {git_credentials: exists(path.join(os.homedir(), '.git-credentials')),
                 gh_hosts: exists(path.join(os.homedir(), '.config', 'gh', 'hosts.yml')),
                 helpers: ['/usr/bin/git-credential-manager', '/usr/local/bin/git-credential-manager', '/usr/bin/gh',
                           '/usr/local/bin/gh', '/usr/bin/secret-tool'].filter(exists)};
r.cgroup = {memory: readText('/sys/fs/cgroup/memory.max') || readText('/sys/fs/cgroup/memory/memory.limit_in_bytes'),
            pids: readText('/sys/fs/cgroup/pids.max') || readText('/sys/fs/cgroup/pids/pids.max')};
const connect = (host, port) => new Promise((res) => {
  const s = net.connect({host, port, timeout: 3000});
  const done = (ok, err) => { s.destroy(); res({host, port, connected: ok, error: err || null}); };
  s.on('connect', () => done(true)); s.on('timeout', () => done(false, 'timeout')); s.on('error', (e) => done(false, e.code || 'error'));
});
const get = (host, port, p) => new Promise((res) => {
  const req = http.get({host, port, path: p, timeout: 5000}, (rr) => {
    let b = ''; rr.on('data', (d) => { if (b.length < 2000) b += d; });
    rr.on('end', () => res({status: rr.statusCode, gateway: rr.headers['x-aeos-gateway'] || null, body: b.slice(0, 300)}));
  });
  req.on('timeout', () => { req.destroy(); res({status: null, error: 'timeout'}); });
  req.on('error', (e) => res({status: null, error: e.code || 'error'}));
});
(async () => {
  r.egress = await Promise.all(a.egress_targets.map((t) => connect(t[0], t[1])));
  r.gateway = await connect(a.gateway, a.gateway_port);
  r.gateway_filter = await get(a.gateway, a.gateway_port, '/api/session');
  r.broker_path = await get(a.gateway, a.gateway_port, '/broker/v1/models');
  process.stdout.write(JSON.stringify(r) + '\n');
})();
"""

ALLOWED_MOUNT_POINTS = frozenset({"/", CONTAINER_WORKTREE, CONTAINER_CONFIG, "/tmp", CONTAINER_HOME,  # noqa: S108
                                  "/etc/hosts", "/etc/hostname", "/etc/resolv.conf"})
SYSTEM_MOUNT_PREFIXES = ("/proc", "/sys", "/dev")
ISOLATION_CHECKS = (
    "non_root", "read_only_root", "config_read_only", "worktree_writable", "mounts_allowlisted",
    "read_outside_worktree", "read_data_db", "real_ssh_dir", "home_ssh_redirected", "env_secrets",
    "home_redirected", "write_outside_worktree", "egress_non_broker", "broker_gateway_reachable",
    "gateway_path_filter", "keyring_unreachable", "docker_socket_absent", "credential_helpers_absent",
    "resource_limits", "cancel_kills_tree",
)


def _r(check: str, ok: bool | None, evidence: str) -> dict[str, Any]:
    status = "NOT_RUN" if ok is None else "PASS" if ok else "FAIL"
    return {"check": check, "status": status, "evidence": evidence}


def _limit_set(value: str | None) -> bool:
    if not value or value == "max":
        return False
    try:
        return int(value) < 2 ** 62  # cgroup v1 "unlimited" is a huge number
    except ValueError:
        return False


def _denied(section: Any, keys: tuple[str, ...]) -> bool | None:
    """True when every key was observed as False (denied), False when any was observed True, None when the
    probe did not report them (never a PASS without evidence)."""
    if not isinstance(section, dict) or any(k not in section for k in keys):
        return None
    if any(section.get(k) is True for k in keys):
        return False
    return all(section.get(k) is False for k in keys) or None


def evaluate_probe(data: dict[str, Any], *, profile: Profile) -> list[dict[str, Any]]:
    """Map the in-container probe output to PASS/FAIL per check (PASS only on positive evidence; a section the
    probe did not report is NOT_RUN)."""
    out: list[dict[str, Any]] = []
    ident = data.get("identity") if isinstance(data.get("identity"), dict) else None
    want_uid = int(profile.user.split(":")[0])
    out.append(_r("non_root", None if ident is None or ident.get("uid") is None
                  else ident.get("uid") not in (0,) and ident.get("uid") == want_uid,
                  f"uid={(ident or {}).get('uid')} gid={(ident or {}).get('gid')} (expected {profile.user})"))
    w = data.get("writes")
    wd = w if isinstance(w, dict) else {}
    out.append(_r("read_only_root", _denied(w, ("root", "etc", "usr")),
                  f"write / {wd.get('root')}, /etc {wd.get('etc')}, /usr {wd.get('usr')}"))
    out.append(_r("config_read_only", _denied(w, ("config",)), f"write {CONTAINER_CONFIG} {wd.get('config')}"))
    out.append(_r("worktree_writable", None if "worktree" not in wd else wd.get("worktree") is True,
                  f"write {CONTAINER_WORKTREE} {wd.get('worktree')} (positive control)"))
    points = [m.get("point") for m in data.get("mounts") or [] if isinstance(m, dict)]
    extra = sorted({p for p in points if p and p not in ALLOWED_MOUNT_POINTS
                    and not any(p == s or p.startswith(s + "/") for s in SYSTEM_MOUNT_PREFIXES)})
    mounts_ok: bool | None = (not extra) if points else None
    out.append(_r("mounts_allowlisted", mounts_ok, "only worktree/config/tmpfs/system mounts" if mounts_ok
                  else f"unexpected mounts: {extra[:10]}" if points else "mount table not readable"))
    hp = data.get("host_paths")
    hpd = hp if isinstance(hp, dict) else {}
    outside = _denied(hp, ("outside_file", "controller"))
    out.append(_r("read_outside_worktree", None if outside is None or mounts_ok is None else outside and mounts_ok,
                  f"host probe file readable={hpd.get('outside_file')}, controller checkout visible="
                  f"{hpd.get('controller')}"))
    # A host path probed by name proves little on a Windows host (a Windows path never exists inside the Linux
    # container), so these checks also need the mount table to show no host mount besides worktree/config.
    db_denied = _denied(hp, ("db", "data_dir"))
    out.append(_r("read_data_db", None if db_denied is None or mounts_ok is None else db_denied and mounts_ok,
                  f"AEOS DB readable={hpd.get('db')}, data dir visible={hpd.get('data_dir')} (content never read); "
                  f"mounts allow-listed={mounts_ok}"))
    ssh_denied = _denied(hp, ("real_ssh",))
    out.append(_r("real_ssh_dir", None if ssh_denied is None or mounts_ok is None else ssh_denied and mounts_ok,
                  f"host ~/.ssh visible={hpd.get('real_ssh')}; mounts allow-listed={mounts_ok}"))
    home = data.get("home") if isinstance(data.get("home"), dict) else None
    hd = home or {}
    out.append(_r("home_ssh_redirected", None if home is None or "home_ssh" not in data
                  else hd.get("HOME") == CONTAINER_HOME and data.get("home_ssh") is False,
                  f"HOME={hd.get('HOME')}, ~/.ssh exists={data.get('home_ssh')}"))
    env = data.get("env") if isinstance(data.get("env"), dict) else None
    bad = sorted(set(((env or {}).get("secret_names") or []) + ((env or {}).get("secret_values") or [])))
    out.append(_r("env_secrets", None if env is None or "secret_names" not in env else not bad,
                  "no secret-looking variables" if not bad else f"secret-looking variables: {bad[:10]}"))
    home_ok = None if home is None else (str(hd.get("HOME")) == CONTAINER_HOME
                                         and str(hd.get("XDG_CONFIG_HOME", "")).startswith(CONTAINER_HOME)
                                         and str(hd.get("TMPDIR")) == "/tmp")  # noqa: S108
    out.append(_r("home_redirected", home_ok, f"{hd}"))
    out.append(_r("write_outside_worktree", _denied(w, ("root", "etc", "usr", "config")),
                  "writes outside the worktree refused by the read-only root/config mounts" if wd else "no data"))
    eg = [e for e in data.get("egress") or [] if isinstance(e, dict) and "connected" in e]
    leaked = [f"{e.get('host')}:{e.get('port')}" for e in eg if e.get("connected") is True]
    out.append(_r("egress_non_broker", (not leaked) if eg else None, "no non-broker endpoint reachable" if eg and
                  not leaked else f"reachable: {leaked}" if leaked else "egress probe did not run"))
    gw = data.get("gateway") if isinstance(data.get("gateway"), dict) else None
    out.append(_r("broker_gateway_reachable", None if gw is None else gw.get("connected") is True, f"gateway: {gw}"))
    gf = data.get("gateway_filter") if isinstance(data.get("gateway_filter"), dict) else None
    out.append(_r("gateway_path_filter", None if gf is None else gf.get("status") == 403 and gf.get("gateway") == "deny",
                  f"GET /api/session via gateway -> {(gf or {}).get('status')} ({(gf or {}).get('gateway')})"))
    so = data.get("sockets")
    sod = so if isinstance(so, dict) else {}
    out.append(_r("keyring_unreachable", _denied(so, ("dbus", "run_user", "ssh_agent")),
                  f"dbus={sod.get('dbus')} /run/user={sod.get('run_user')} ssh-agent={sod.get('ssh_agent')}"))
    out.append(_r("docker_socket_absent", _denied(so, ("docker",)), f"docker socket present={sod.get('docker')}"))
    cr = data.get("credentials") if isinstance(data.get("credentials"), dict) else None
    out.append(_r("credential_helpers_absent", None if cr is None or "helpers" not in cr else
                  (cr.get("git_credentials") is False and cr.get("gh_hosts") is False and not cr.get("helpers")),
                  f"{cr}"))
    cg = data.get("cgroup") if isinstance(data.get("cgroup"), dict) else None
    out.append(_r("resource_limits", None if cg is None else _limit_set(cg.get("memory")) and _limit_set(cg.get("pids")),
                  f"memory.max={(cg or {}).get('memory')} pids.max={(cg or {}).get('pids')}"))
    return out


def _summary(results: list[dict[str, Any]]) -> tuple[str, dict[str, int]]:
    summary: dict[str, int] = {}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    if summary.get("FAIL"):
        return "FAIL", summary
    if summary.get("NOT_RUN") or not set(ISOLATION_CHECKS) <= {r["check"] for r in results}:
        return "NOT_PROVEN", summary
    return "PASS", summary


def _listener() -> tuple[Any, int]:
    import socket

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)

    def accept() -> None:
        srv.settimeout(30)
        try:
            while True:
                conn, _ = srv.accept()
                conn.close()
        except OSError:
            return

    threading.Thread(target=accept, daemon=True).start()
    return srv, srv.getsockname()[1]


def _cancel_check(profile: Profile, image: str, root: Path) -> dict[str, Any]:
    """Real kill-controller path: a container started through processes.spawn is removed by
    processes.kill_run_processes (docker CLI tree + container teardown by label)."""
    from aeos.worker_runtime import processes

    rid = f"containment-cancel-{utcnow().strftime('%H%M%S%f')}"
    wt, cfg = root / "cancel-wt", root / "cancel-cfg"
    wt.mkdir(parents=True, exist_ok=True)
    cfg.mkdir(parents=True, exist_ok=True)
    argv = worker_argv(profile=profile, image=image, run_id=rid, worktree=wt, config_dir=cfg,
                       inner_argv=["-e", "setInterval(() => {}, 1000)"], env=worker_env(rid, {}), entrypoint="node")
    try:
        mp = processes.spawn(rid, argv, cwd=root, env=docker_cli_env(), capture_stdout=False)
    except OSError as exc:
        return _r("cancel_kills_tree", False, f"could not start the cancel probe container: {type(exc).__name__}")
    processes.register_container(rid)
    deadline = time.monotonic() + 60
    seen = False
    while time.monotonic() < deadline:
        ids = remaining(rid)
        if ids:
            seen = True
            break
        if mp.poll() is not None:
            break
        time.sleep(0.5)
    res = processes.kill_run_processes(rid)
    left = remaining(rid)
    cli_alive = processes.is_alive(mp.pid, mp.create_time)
    ok = seen and left == [] and not cli_alive
    return _r("cancel_kills_tree", ok if seen else False,
              f"container started={seen}, containers left={left}, docker CLI alive={cli_alive}; methods={res.get('methods')}")


def containment_report(*, domain: str = Domain.PERSONAL.value) -> dict[str, Any]:
    """Run the container containment suite on this host for one domain profile."""
    from aeos.core.config import get_settings
    from aeos.core.paths import repo_root
    from aeos.worker_runtime.sandbox import sandbox_capabilities

    settings = get_settings()
    checked = utcnow().isoformat()
    profile = profile_for(domain)
    base: dict[str, Any] = {"kind": "container", "domain": profile.domain, "checked_at": checked,
                            "platform": sys.platform, "network": profile.network, "gateway": profile.gateway,
                            "user": profile.user, "evidence_level": "E0"}

    def not_proven(note: str, **extra: Any) -> dict[str, Any]:
        results = [_r(c, None, note) for c in ISOLATION_CHECKS]
        return {**base, **extra, "overall": "NOT_PROVEN", "results": results, "summary": {"NOT_RUN": len(results)},
                "note": note}

    caps = sandbox_capabilities(refresh=True)["levels"]["container"]
    if not caps.get("available"):
        return not_proven(f"container not available: {caps.get('evidence')}")
    image = image_status()
    base.update(image=image["tag"], image_id=image["id"], docker_server=caps.get("server_version"))
    if not image["present"]:
        return not_proven(image["evidence"], remedy=f"docker build -t {image['tag']} sandbox/")
    try:
        ensure_network(profile)
        ensure_gateway(profile, broker_port=int(settings.port), image=image["tag"])
    except ContainerError as exc:
        setup_results = [_r("network_setup", False, f"{exc.code}: {exc.message}"),
                         *(_r(c, None, exc.message) for c in ISOLATION_CHECKS)]
        overall, summary = _summary(setup_results)
        return {**base, "overall": overall, "results": setup_results, "summary": summary, "note": exc.message}
    root = Path(tempfile.mkdtemp(prefix="aeos-ccontain-"))
    wt, cfg = root / "worktree", root / "config"
    wt.mkdir()
    cfg.mkdir()
    (cfg / "probe.js").write_text(PROBE_JS, encoding="utf-8")
    outside = root / "outside-secret-probe.txt"
    outside.write_text("probe", encoding="utf-8")
    srv, port = _listener()
    rid = f"containment-probe-{utcnow().strftime('%H%M%S%f')}"
    args = {"outside_file": str(outside), "db_path": str(settings.layout.db_path), "data_dir": str(settings.data_dir),
            "real_ssh": str(Path.home() / ".ssh"), "controller_root": str(repo_root()),
            "egress_targets": [["host.docker.internal", port], ["1.1.1.1", 443], ["8.8.8.8", 53], ["example.com", 80]],
            "gateway": profile.gateway, "gateway_port": GATEWAY_PORT}
    argv = worker_argv(profile=profile, image=image["tag"], run_id=rid, worktree=wt, config_dir=cfg,
                       inner_argv=[f"{CONTAINER_CONFIG}/probe.js", json.dumps(args)], env=worker_env(rid, {}),
                       entrypoint="node")
    data: dict[str, Any] = {}
    results: list[dict[str, Any]] = []
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
                              env=docker_cli_env(), stdin=subprocess.DEVNULL, check=False)
        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip().startswith("{")]
        data = json.loads(lines[-1]) if lines else {}
        if not data:
            results.append(_r("probe", False, f"probe produced no report (exit {proc.returncode}): {_err(proc)}"))
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        results.append(_r("probe", False, f"probe did not complete: {type(exc).__name__}"))
    finally:
        srv.close()
        teardown(rid)
    if data:
        results.extend(evaluate_probe(data, profile=profile))
    results.append(_cancel_check(profile, image["tag"], root))
    overall, summary = _summary(results)
    functional = {"broker_end_to_end": (data.get("broker_path") or {}) if data else {},
                  "note": "a 401/403 JSON error from the broker means the gateway reaches it; 502 means the AEOS "
                          "server was not running (not part of the isolation verdict)"}
    shutil.rmtree(root, ignore_errors=True)
    return {**base, "overall": overall, "evidence_level": "E2", "results": results, "summary": summary,
            "functional": functional,
            "note": "E2 controlled test executed inside the worker image on this host; PASS only on positive "
                    "evidence of denial (ADR-0051)"}
