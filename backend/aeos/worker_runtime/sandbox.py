"""Sandbox isolation levels and the policy requirement for running workers (§11, §25, §50).

Levels, reported honestly:

* ``process`` — always available, but WEAK: same OS user, no filesystem or egress enforcement on
  Windows/macOS/Linux; only env scrubbing, HOME redirection, cwd = worktree, OpenCode tool
  permissions and process-tree kill. Allowed only with an ACTIVE owner EXCEPTION decision for the
  PERSONAL domain (ADR-0051); never for OFFICE.
* ``container`` — available only when the docker CLI *and* daemon respond. Availability is not
  containment: it becomes usable only after a containment report for ``container`` passed.
* ``vm`` — not implemented in V1 (reported as unavailable).

If the required boundary is not available the work is BLOCKED (§25 "Jika boundary yang disyaratkan
belum tersedia, pekerjaan tersebut BLOCKED").
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from typing import Any

from sqlalchemy.orm import Session

from aeos.core.enums import Domain
from aeos.core.timeutil import utcnow

PROCESS_SANDBOX_TARGET = ("worker_sandbox", "process")  # Decision target_type / target_id
CONTAINMENT_SETTING_PREFIX = "worker_runtime.containment."
PROCESS_RESIDUAL_RISKS = [
    "Tidak ada penegakan egress jaringan: proses worker dapat menghubungi host selain broker.",
    "User OS yang sama dengan controller: file yang dapat dibaca user (DB data dir, ~/.ssh asli, keyring OS) "
    "tidak diblokir oleh sistem operasi; hanya izin tool OpenCode (external_directory=deny) yang membatasi.",
    "Penulisan di luar worktree tidak dicegah OS; hanya terdeteksi untuk path di dalam repository.",
    "Isolasi bergantung pada env yang dibersihkan dan HOME yang dialihkan, bukan batas keamanan kernel.",
    "Jika bash diizinkan, worker menjalankan perintah sebagai user OS yang sama, termasuk credential helper OS "
    "(mis. Git Credential Manager): direct push/credential escape tidak dicegah (master s31: NOT-PROVEN). "
    "Karena itu bash di process sandbox memerlukan details.allow_bash=true pada keputusan pengecualian.",
]
SHELL_TOOLS = ("bash",)


def task_wants_shell(tools_allowed: list[str] | None) -> bool:
    """True when the TaskSpec grants a shell tool (``bash`` or ``bash:<pattern>``)."""
    return any(str(t).split(":", 1)[0].strip().lower() in SHELL_TOOLS for t in (tools_allowed or []))


def process_shell_allowed(session: Session, decision_id: str | None) -> bool:
    """A shell under the WEAK process sandbox needs the owner's exception to say so explicitly
    (``details.allow_bash is True``): with a shell the worker acts as the same OS user (ADR-0051)."""
    from aeos.models.decisions import Decision

    d = session.get(Decision, decision_id) if decision_id else None
    return bool(d is not None and d.status == "ACTIVE" and (d.details or {}).get("allow_bash") is True)


_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "value": None}
CACHE_SECONDS = 60


def _docker_probe(timeout: float = 10.0) -> dict[str, Any]:
    docker = shutil.which("docker")
    if not docker:
        return {"available": False, "evidence": "docker CLI not found on PATH", "cli": None, "server_version": None}
    try:
        proc = subprocess.run([docker, "version", "--format", "{{.Server.Version}}"], capture_output=True,
                              text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "evidence": f"docker daemon did not respond: {type(exc).__name__}",
                "cli": docker, "server_version": None}
    version = (proc.stdout or "").strip()
    if proc.returncode != 0 or not version:
        return {"available": False, "evidence": f"docker daemon not reachable (exit {proc.returncode})",
                "cli": docker, "server_version": None}
    return {"available": True, "evidence": f"docker server {version} responded", "cli": docker,
            "server_version": version}


def sandbox_capabilities(*, refresh: bool = False) -> dict[str, Any]:
    """Which isolation levels this host offers, with evidence (no LLM, no network)."""
    with _cache_lock:
        if not refresh and _cache["value"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS:
            return dict(_cache["value"])
    docker = _docker_probe()
    value = {
        "checked_at": utcnow().isoformat(),
        "platform": sys.platform,
        "levels": {
            "process": {
                "available": True,
                "strength": "WEAK",
                "enforces": {"filesystem": False, "egress": False, "same_os_user": True,
                             "env_scrub": True, "home_redirect": True, "kill_tree": True},
                "evidence": "always available; no OS-level filesystem/egress enforcement on "
                            f"{sys.platform} (see containment report)",
            },
            "container": {
                "available": bool(docker["available"]),
                "strength": "NOT_PROVEN",
                "evidence": docker["evidence"],
                "server_version": docker["server_version"],
            },
            "vm": {"available": False, "strength": "NOT_IMPLEMENTED", "evidence": "VM/microVM sandbox not implemented in V1"},
        },
    }
    value["best_available"] = "container" if docker["available"] else "process"
    with _cache_lock:
        _cache.update(at=time.monotonic(), value=value)
    return dict(value)


def _last_containment(session: Session, kind: str) -> dict[str, Any] | None:
    from aeos.models.identity import SystemSetting

    row = session.get(SystemSetting, CONTAINMENT_SETTING_PREFIX + kind)
    return row.value if row and isinstance(row.value, dict) else None


def process_sandbox_exception(session: Session):  # -> Decision | None
    """ACTIVE owner EXCEPTION allowing the weak process sandbox for PERSONAL (ADR-0051)."""
    from aeos.workforce.integrations import active_exception_decision

    d = active_exception_decision(session, target_type=PROCESS_SANDBOX_TARGET[0], target_id=PROCESS_SANDBOX_TARGET[1])
    if d is None:
        return None
    if d.domain != Domain.PERSONAL.value or (d.details or {}).get("allow_process_sandbox") is not True:
        return None
    return d


def sandbox_readiness(session: Session, *, domain: str) -> dict[str, Any]:
    """Policy requirement check used by admission and (via the facade) by the Readiness Doctor."""
    caps = sandbox_capabilities()
    base: dict[str, Any] = {"domain": domain, "checked_at": caps["checked_at"], "capabilities": caps["levels"],
                            "required": "container", "residual_risks": []}
    container = caps["levels"]["container"]
    report = _last_containment(session, "container") if container["available"] else None
    container_proven = bool(report and report.get("overall") == "PASS")

    if domain == Domain.OFFICE.value:
        # §11/§23: OFFICE needs a verified strong boundary incl. egress; nothing in V1 proves that.
        reasons = ["OFFICE requires verified container/VM isolation with egress enforcement (master s11/s23)"]
        if not container["available"]:
            reasons.append("container runtime not available: " + container["evidence"])
        elif not container_proven:
            reasons.append("container containment NOT_PROVEN (run `aeos worker containment --kind container`)")
        return {**base, "status": "BLOCKED", "kind": None, "reasons": reasons,
                "remedies": ["Use PERSONAL or provide an approved isolated runtime"]}

    if container["available"] and container_proven:
        return {**base, "status": "READY", "kind": "container", "reasons": [], "remedies": [],
                "containment": {"overall": report.get("overall"), "checked_at": report.get("checked_at")}}

    exc = process_sandbox_exception(session)
    if exc is not None:
        return {**base, "status": "READY", "kind": "process", "decision_id": exc.id, "reasons": [],
                "remedies": [], "residual_risks": PROCESS_RESIDUAL_RISKS, "strength": "WEAK"}

    reasons = []
    if not container["available"]:
        reasons.append("container sandbox not available: " + container["evidence"])
    else:
        reasons.append("container available but containment NOT_PROVEN")
    reasons.append("process sandbox is WEAK and needs an ACTIVE owner EXCEPTION decision "
                   "(target worker_sandbox/process, details.allow_process_sandbox=true, domain PERSONAL)")
    return {**base, "status": "BLOCKED", "kind": None, "reasons": reasons,
            "remedies": ["Install/start Docker and run `aeos worker containment --kind container`",
                         "or record the owner EXCEPTION decision described in ADR-0051"],
            "residual_risks": PROCESS_RESIDUAL_RISKS}


def container_command(
    *, image: str, run_id: str, worktree: str, config_dir: str, inner_argv: list[str], env: dict[str, str],
    network: str = "aeos-worker", memory: str = "2g", cpus: str = "2", pids: int = 256,
) -> list[str]:
    """docker argv for the worker image in ``sandbox/Dockerfile`` (NOT_PROVEN on this build: the
    containment suite for ``container`` must pass before admission may select it)."""
    docker = shutil.which("docker") or "docker"
    argv = [
        docker, "run", "--rm", "-i", "--name", f"aeos-run-{run_id[:12]}",
        "--user", "10001:10001", "--read-only", "--tmpfs", "/tmp:rw,size=256m",  # noqa: S108 - container-internal tmpfs
        "--tmpfs", "/home/worker:rw,size=256m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", str(pids),
        "--memory", memory, "--cpus", cpus, "--network", network,
        "--mount", f"type=bind,source={worktree},target=/workspace",
        "--mount", f"type=bind,source={config_dir},target=/aeos,readonly",
        "--label", f"aeos.run_id={run_id}",
    ]
    for k, v in sorted(env.items()):
        argv += ["-e", f"{k}={v}"]
    return [*argv, image, *inner_argv]
