"""Sandbox isolation levels and the policy requirement for running workers (§11, §25, §50).

Levels, reported honestly:

* ``process`` — always available, but WEAK: same OS user, no filesystem or egress enforcement on
  Windows/macOS/Linux; only env scrubbing, HOME redirection, cwd = worktree, OpenCode tool
  permissions, process-tree kill and resource watchdog. Allowed only with an ACTIVE owner EXCEPTION
  decision for the PERSONAL domain (ADR-0051); never for OFFICE, never while the controller runs
  elevated (root / elevated Administrator, §25 "non-root"), never for a TaskSpec that grants a shell
  (§25 "Worker tidak mendapat shell host") or egress, and for risk class R3 only when the exception
  explicitly covers it.
* ``container`` — the hardened worker image (``sandbox/Dockerfile``) on an internal docker network
  whose only reachable endpoint is the AEOS broker gateway (``aeos.worker_runtime.container``).
  Available only when the docker CLI *and* daemon respond; availability is not containment: it is
  selected only after ``aeos worker containment --kind container`` produced a passing, fresh report
  for the current image on this host (per domain profile).
* ``vm`` — not implemented in V1: risk class R4 requires it, so R4 work is BLOCKED.

If the required boundary is not available the work is BLOCKED (§25 "Jika boundary yang disyaratkan
belum tersedia, pekerjaan tersebut BLOCKED").
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from aeos.core.enums import Domain
from aeos.core.timeutil import ensure_aware, utcnow


def parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None

PROCESS_SANDBOX_TARGET = ("worker_sandbox", "process")  # Decision target_type / target_id
CONTAINMENT_SETTING_PREFIX = "worker_runtime.containment."
CONTAINMENT_MAX_AGE = timedelta(days=7)
# The owner's process-sandbox exception covers R0-R2. R3 (privileged / protected-path work) needs the
# container, or this operation listed explicitly in the exception's allowed_operations (ADR-0051 amendment).
PROCESS_R3_OPERATION = "run_worker_process_sandbox_r3"
RISK_ORDER = ("R0", "R1", "R2", "R3", "R4")
PROCESS_RESIDUAL_RISKS = [
    "Tidak ada penegakan egress jaringan oleh OS: karena itu TaskSpec dengan egress/webfetch tidak dijalankan di "
    "process sandbox.",
    "User OS yang sama dengan controller: file yang dapat dibaca user (DB data dir, ~/.ssh asli, keyring OS) "
    "tidak diblokir oleh sistem operasi; hanya izin tool OpenCode (external_directory=deny) yang membatasi.",
    "Checkout controller (kode yang sedang berjalan, policy/) dan DB data dir dapat ditulis oleh user OS yang sama: "
    "perubahan checkout controller dideteksi oleh integrity check sebelum/sesudah run (run gagal + karantina), "
    "penulisan DB tidak terdeteksi.",
    "Penulisan di luar worktree tidak dicegah OS; hanya terdeteksi untuk path di dalam repository dan checkout "
    "controller.",
    "Isolasi bergantung pada env yang dibersihkan dan HOME yang dialihkan, bukan batas keamanan kernel.",
    "Shell (bash) TIDAK pernah diberikan di process sandbox (master s25 'Worker tidak mendapat shell host'): "
    "TaskSpec dengan bash memerlukan sandbox container yang containment-nya lulus; details.allow_bash pada "
    "keputusan pengecualian tidak lagi membuka shell.",
    "Kode yang ditulis worker dieksekusi kemudian oleh verifier (suite tepercaya terhadap kandidat, tes kandidat, "
    "round-trip migrasi). Tanpa runner container verifier yang terbukti di host ini, eksekusi itu berjalan sebagai "
    "user OS controller (isolasi verifier WEAK, master s20): env dialihkan ke direktori run (tanpa keyring, gh, "
    "kredensial git, agent SSH), penulisan ke checkout/interpreter controller dan tabel kepercayaan DB dideteksi "
    "lalu dikarantina, tetapi pembacaan file user lewat path absolut atau API kredensial OS tidak dicegah.",
]
SHELL_TOOLS = ("bash",)
EGRESS_TOOLS = ("webfetch",)


def task_wants_shell(tools_allowed: list[str] | None) -> bool:
    """True when the TaskSpec grants a shell tool (``bash`` or ``bash:<pattern>``)."""
    return any(str(t).split(":", 1)[0].strip().lower() in SHELL_TOOLS for t in (tools_allowed or []))


def task_wants_egress(tools_allowed: list[str] | None, egress_allowed: list[str] | None) -> bool:
    """True when the TaskSpec would give the worker network egress beyond the broker (``webfetch`` with an
    egress allow-list is exactly when the OpenCode config allows webfetch)."""
    tools = {str(t).split(":", 1)[0].strip().lower() for t in (tools_allowed or [])}
    return bool(tools & set(EGRESS_TOOLS)) and bool(egress_allowed)


def process_shell_allowed(session: Session, decision_id: str | None) -> bool:
    """Whether the owner's exception records ``details.allow_bash is True``. Informational since the
    2026-10-19 amendment of ADR-0051: a shell is never admitted under the process sandbox."""
    from aeos.models.decisions import Decision

    d = session.get(Decision, decision_id) if decision_id else None
    return bool(d is not None and d.status == "ACTIVE" and (d.details or {}).get("allow_bash") is True)


# ----------------------------------------------------------------------------- non-root (§25)
def _elevation_probe() -> bool:
    if sys.platform == "win32":
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            return True  # unknown -> treated as elevated (fail closed)
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid is not None and geteuid() == 0)


def host_is_elevated() -> bool:
    """True when the controller (and therefore a process-sandbox worker) runs as root / elevated Administrator."""
    return _elevation_probe()


# ----------------------------------------------------------------------------- capabilities
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


def _image_probe() -> dict[str, Any]:
    try:
        from aeos.worker_runtime import container

        return container.image_status()
    except Exception as exc:  # never READY on a probe error
        return {"tag": None, "present": False, "id": None, "evidence": f"image probe failed ({type(exc).__name__})"}


def sandbox_capabilities(*, refresh: bool = False) -> dict[str, Any]:
    """Which isolation levels this host offers, with evidence (no LLM, no network)."""
    with _cache_lock:
        if not refresh and _cache["value"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS:
            return dict(_cache["value"])
    docker = _docker_probe()
    image = _image_probe() if docker["available"] else {"tag": None, "present": False, "id": None,
                                                         "evidence": "docker not available"}
    value = {
        "checked_at": utcnow().isoformat(),
        "platform": sys.platform,
        "elevated": host_is_elevated(),
        "levels": {
            "process": {
                "available": True,
                "strength": "WEAK",
                "enforces": {"filesystem": False, "egress": False, "same_os_user": True, "shell": False,
                             "env_scrub": True, "home_redirect": True, "kill_tree": True, "resource_watchdog": True},
                "evidence": "always available; no OS-level filesystem/egress enforcement on "
                            f"{sys.platform} (see containment report); shell/egress TaskSpecs never run here",
            },
            "container": {
                "available": bool(docker["available"]),
                "strength": "NOT_PROVEN",
                "evidence": docker["evidence"],
                "server_version": docker["server_version"],
                "image": image,
                "implementation": "aeos.worker_runtime.container (internal network + broker gateway); selected "
                                  "only after a passing containment report for the current image",
            },
            "vm": {"available": False, "strength": "NOT_IMPLEMENTED", "evidence": "VM/microVM sandbox not implemented "
                   "in V1; risk class R4 requires it and stays BLOCKED"},
        },
    }
    value["best_available"] = "container" if docker["available"] else "process"
    with _cache_lock:
        _cache.update(at=time.monotonic(), value=value)
    return dict(value)


def containment_setting_key(kind: str, domain: str | None = None) -> str:
    """SystemSetting key of the latest containment report (the OFFICE container profile has its own)."""
    suffix = ".office" if kind == "container" and domain == Domain.OFFICE.value else ""
    return CONTAINMENT_SETTING_PREFIX + kind + suffix


def _last_containment(session: Session, kind: str, domain: str | None = None) -> dict[str, Any] | None:
    from aeos.models.identity import SystemSetting

    row = session.get(SystemSetting, containment_setting_key(kind, domain))
    return row.value if row and isinstance(row.value, dict) else None


def container_report_problems(report: dict[str, Any] | None, *, domain: str, caps: dict[str, Any]) -> list[str]:
    """Why a recorded container containment report does NOT prove the boundary now (empty = proven)."""
    if not report:
        return ["container containment NOT_PROVEN on this host (run `aeos worker containment --kind container"
                + (" --domain OFFICE" if domain == Domain.OFFICE.value else "") + "`)"]
    problems: list[str] = []
    if report.get("kind") != "container" or report.get("overall") != "PASS":
        problems.append(f"last container containment report is {report.get('overall') or 'UNKNOWN'}, not PASS")
    if (report.get("domain") or Domain.PERSONAL.value) != domain:
        problems.append(f"containment report is for domain {report.get('domain')}, not {domain}")
    if report.get("platform") not in (None, sys.platform):
        problems.append(f"containment report was recorded on {report.get('platform')}, not {sys.platform}")
    image = (caps.get("container") or {}).get("image") or {}
    if not image.get("present"):
        problems.append(f"worker image not present: {image.get('evidence') or 'unknown'}")
    elif report.get("image") != image.get("tag") or (image.get("id") and report.get("image_id") != image.get("id")):
        problems.append("containment report was recorded for a different worker image; re-run the suite")
    checked = parse_iso(str(report.get("checked_at") or ""))
    if checked is None or utcnow() - ensure_aware(checked) > CONTAINMENT_MAX_AGE:  # type: ignore[operator]
        problems.append(f"containment report older than {CONTAINMENT_MAX_AGE.days} days (or undated); re-run the suite")
    return problems


def process_sandbox_exception(session: Session):  # -> Decision | None
    """ACTIVE owner EXCEPTION allowing the weak process sandbox for PERSONAL (ADR-0051)."""
    from aeos.workforce.integrations import active_exception_decision

    d = active_exception_decision(session, target_type=PROCESS_SANDBOX_TARGET[0], target_id=PROCESS_SANDBOX_TARGET[1])
    if d is None:
        return None
    if d.domain != Domain.PERSONAL.value or (d.details or {}).get("allow_process_sandbox") is not True:
        return None
    return d


def _container_remedy(domain: str) -> str:
    from aeos.worker_runtime.opencode import PINNED_VERSION

    extra = " --domain OFFICE" if domain == Domain.OFFICE.value else ""
    return (f"Install/start Docker, build the worker image (`docker build --build-arg OPENCODE_VERSION={PINNED_VERSION} "
            f"-t dly-aeos-worker:{PINNED_VERSION} sandbox/`) and run `aeos worker containment --kind container{extra}` "
            "until it reports PASS")


def sandbox_readiness(session: Session, *, domain: str) -> dict[str, Any]:
    """Policy requirement check used by admission and (via the facade) by the Readiness Doctor."""
    caps_all = sandbox_capabilities()
    caps = caps_all["levels"]
    base: dict[str, Any] = {"domain": domain, "checked_at": caps_all["checked_at"], "capabilities": caps,
                            "required": "container", "residual_risks": []}
    container = caps["container"]
    report = _last_containment(session, "container", domain) if container["available"] else None
    problems = container_report_problems(report, domain=domain, caps=caps) if container["available"] else \
        ["container sandbox not available: " + container["evidence"]]

    if domain == Domain.OFFICE.value:
        # §11/§23: OFFICE needs a verified strong boundary with its own profile (separate network, gateway,
        # container user and data partition) and egress enforcement; the company authorisation is gated by
        # the scheduler separately (OFFICE_DENIED).
        if not problems:
            return {**base, "status": "READY", "kind": "container", "profile": "OFFICE", "reasons": [], "remedies": [],
                    "containment": {"overall": report.get("overall"), "checked_at": report.get("checked_at")}}
        reasons = ["OFFICE requires verified container isolation with its own profile and egress enforcement "
                   "(master s11/s23)", *problems]
        return {**base, "status": "BLOCKED", "kind": None, "reasons": reasons,
                "remedies": [_container_remedy(domain), "or use PERSONAL"]}

    if not problems:
        return {**base, "status": "READY", "kind": "container", "profile": "PERSONAL", "reasons": [], "remedies": [],
                "containment": {"overall": report.get("overall"), "checked_at": report.get("checked_at")}}

    exc = process_sandbox_exception(session)
    if exc is not None and not caps_all.get("elevated"):
        return {**base, "status": "READY", "kind": "process", "decision_id": exc.id, "reasons": [],
                "remedies": [], "residual_risks": PROCESS_RESIDUAL_RISKS, "strength": "WEAK",
                "covers_r3": PROCESS_R3_OPERATION in (exc.allowed_operations or []),
                "allow_bash_recorded": (exc.details or {}).get("allow_bash") is True}

    reasons = list(problems)
    if exc is not None:
        reasons.append("the controller runs elevated (root / elevated Administrator): a process-sandbox worker would "
                       "run elevated too (master s25 non-root); restart AEOS as a normal user")
    else:
        reasons.append("process sandbox is WEAK and needs an ACTIVE owner EXCEPTION decision "
                       "(target worker_sandbox/process, details.allow_process_sandbox=true, domain PERSONAL)")
    return {**base, "status": "BLOCKED", "kind": None, "reasons": reasons,
            "remedies": [_container_remedy(domain), "or record the owner EXCEPTION decision described in ADR-0051"
                         + (" and run AEOS without elevation" if exc is not None else "")],
            "residual_risks": PROCESS_RESIDUAL_RISKS}


def required_boundary(risk_class: str | None) -> str:
    """§25 'Pilihan VM/microVM/sandbox untuk risiko lebih tinggi': R0-R2 -> process (with the owner's
    exception) or container; R3 -> container (or explicit owner coverage); R4 -> vm. Unknown -> container."""
    rc = str(risk_class or "").upper()
    if rc not in RISK_ORDER:
        return "container"
    if rc == "R4":
        return "vm"
    return "container" if rc == "R3" else "process"


def task_sandbox_readiness(session: Session, *, domain: str, risk_class: str | None, tools_allowed: list[str] | None,
                           egress_allowed: list[str] | None) -> dict[str, Any]:
    """The boundary THIS TaskSpec needs (domain + risk tier + shell/egress grants). READY carries the kind
    to launch; BLOCKED carries a code and reasons. Used by admission and by the executor (fail closed)."""
    sb = sandbox_readiness(session, domain=domain)
    out: dict[str, Any] = {**sb, "risk_class": risk_class, "required_for_task": required_boundary(risk_class)}
    if sb["status"] != "READY":
        return {**out, "code": "SANDBOX_NOT_READY"}
    problems: list[tuple[str, str]] = []
    need = out["required_for_task"]
    if need == "vm":
        problems.append(("VM_SANDBOX_REQUIRED", f"risk class {risk_class} requires a VM/microVM boundary, which is not "
                         "implemented in V1 (master s25): the task stays BLOCKED"))
    if task_wants_egress(tools_allowed, egress_allowed):
        problems.append(("EGRESS_NOT_ENFORCEABLE", "TaskSpec grants egress (webfetch + egress_allowed): the process "
                         "sandbox cannot restrict egress and the container network reaches only the broker; a per-host "
                         "egress allow-list proxy is not implemented (master s25 egress default deny)"))
    if sb["kind"] == "process":
        if task_wants_shell(tools_allowed):
            problems.append(("SHELL_NEEDS_CONTAINER", "TaskSpec grants bash: under the WEAK process sandbox that is a "
                             "host shell as the owner's OS user (master s25 'Worker tidak mendapat shell host'); shell "
                             "tools need the container sandbox with a passing containment report - details.allow_bash "
                             "on the process exception no longer admits it (ADR-0051 amendment 2026-10-19)"))
        if need == "container" and not sb.get("covers_r3"):
            problems.append(("STRONG_SANDBOX_REQUIRED", f"risk class {risk_class} needs the container sandbox; the "
                             "process-sandbox exception covers R3 only when its allowed_operations list "
                             f"{PROCESS_R3_OPERATION} (ADR-0051 amendment)"))
    if problems:
        return {**out, "status": "BLOCKED", "kind": None, "code": problems[0][0],
                "reasons": [why for _c, why in problems], "task_codes": [c for c, _w in problems],
                "remedies": [_container_remedy(domain)]}
    return {**out, "code": None}


def container_name(run_id: str) -> str:
    """Docker name for one launch of a run: the whole run id (sanitised to the docker name alphabet) plus a
    random suffix. Truncating the id let two runs sharing a prefix (e.g. the containment probe and cancel
    runs) collide on the name and refuse to start; the suffix also covers a retry while the previous
    container of the same run is still being removed. Teardown and reaping go by the aeos.run_id label."""
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", run_id).strip("-.") or "run"
    return f"aeos-run-{safe[:40]}-{uuid.uuid4().hex[:8]}"


def container_command(
    *, image: str, run_id: str, worktree: str, config_dir: str, inner_argv: list[str], env: dict[str, str],
    network: str = "aeos-worker", memory: str = "2g", cpus: str = "2", pids: int = 256,
    user: str = "10001:10001", docker: list[str] | None = None, entrypoint: str | None = None,
    labels: dict[str, str] | None = None, workdir: str = "/workspace",
) -> list[str]:
    """docker argv for the worker image in ``sandbox/Dockerfile``: non-root, read-only root fs, tmpfs /tmp and
    HOME, no capabilities, no new privileges, pid/memory/cpu limits, the internal broker-only network, only
    the run worktree (rw) and the per-run config dir (ro) mounted - never the docker socket, host home, SSH
    agent, data dir or a credential store."""
    cli = list(docker) if docker else [shutil.which("docker") or "docker"]
    argv = [
        *cli, "run", "--rm", "-i", "--name", container_name(run_id),
        "--user", user, "--read-only", "--tmpfs", "/tmp:rw,size=256m",  # noqa: S108 - container-internal tmpfs
        "--tmpfs", f"/home/worker:rw,size=256m,uid={user.split(':')[0]},gid={user.split(':')[-1]}",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", str(pids),
        "--memory", memory, "--memory-swap", memory, "--cpus", cpus, "--network", network,
        "--mount", f"type=bind,source={worktree},target=/workspace",
        "--mount", f"type=bind,source={config_dir},target=/aeos,readonly",
        "--workdir", workdir,
        "--label", f"aeos.run_id={run_id}",
    ]
    for k, v in sorted((labels or {}).items()):
        argv += ["--label", f"{k}={v}"]
    if entrypoint:
        argv += ["--entrypoint", entrypoint]
    for k, v in sorted(env.items()):
        argv += ["-e", f"{k}={v}"]
    return [*argv, image, *inner_argv]
