"""Where the independent verifier executes candidate code (§20 "Verifier ENF: read-only suite, isolated
runner, evidence collector"; §25 "Worker tidak mendapat shell host / credential store").

Everything the verifier runs that imports candidate code (the import preflight, the trusted acceptance
suite against the candidate, the optional baseline, the candidate's own tests, the migration round-trip) goes
through one ``Runner``, and so does the expected-id collection of the trusted suite (trusted code only, same
interpreter and dependency set so the ids are comparable). Two exist, chosen per verification and recorded on every
VERIFICATION_REPORT (``verifier_isolation`` / ``verifier_runner``):

* ``container`` - isolation ``CONTAINER``. Selected only when the worker runtime reports that the verifier
  may use the container boundary on this host now (``aeos.worker_runtime.facade.verifier_sandbox``: a fresh
  PASS container containment report for this host, worker image and domain profile - the same proof that
  admits container workers, ADR-0051 - AND the pinned verifier image). Each step is one ``docker run
  --network none --read-only --cap-drop ALL --security-opt no-new-privileges --pull never`` as a non-root
  user, with only the run directory's snapshot, trusted-suite copy and pytest ini mounted read-only and the
  report directory writable: never the data dir (DB), the controller checkout root, the host home, the docker
  socket, an SSH agent or a credential store. The one step that imports trusted controller code from outside
  the run dir (the expected-id collection) additionally gets that ``backend/`` read-only; no step that runs
  candidate code does.
* ``process`` - isolation ``WEAK``. Everything else (no Docker, unproven boundary, no verifier image).
  Candidate code runs as the controller's OS user with the hardened environment of ``procs.scrubbed_env``, each
  step in its own kill group (``procgroup``: Windows Job Object / POSIX session; survivors are killed and listed,
  the verifier QUARANTINES them).
  That stops discovery of credentials *through the environment* only; the residual risk is stated on the
  report (``residual_risks``) and in the Readiness Doctor (``evidence.verifier``). A TaskSpec
  ``verifier_policy.isolated_runner: required``, every OFFICE verification (§11/§23 OFFICE needs a
  verified strong boundary) and every worker run produced in the container/VM sandbox (isolation is never
  downgraded) is BLOCKED instead of running on the process runner (fail closed).

The run directory (``new_workdir``) is a private temp directory outside the AEOS data dir, so no path handed
to candidate code leads to the live DB.

Neither runner separates candidate code from the *test process* it is imported into: every pytest step that runs
candidate code is sealed by the verifier's own plugin (``aeos.verification.seal``) and the in-process forgery limit
is a residual risk of both runners (``RESULT_FORGERY_RISK``).
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aeos.verification.procs import PIPE_HELD_ERROR, ProcResult, child_env_values, run_process, scrubbed_env
from aeos.verification.seal import PLUGIN_DIR, StepSeal

ISOLATION_WEAK = "WEAK"
ISOLATION_CONTAINER = "CONTAINER"
CONTAINER_ROOT = "/v"
CONTAINER_HOME = "/home/verifier"
CONTAINER_TMP = "/tmp"  # noqa: S108 - container-internal tmpfs
CONTAINER_PATH = "/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"
# Docker's own failures (daemon error, image missing with --pull never, command not invocable): the step did not
# run, which is never a test result (BLOCKED, not FAIL).
DOCKER_FAILURE_CODES = (125, 126, 127)
CONTAINER_GRACE_SECONDS = 60.0
# workdir entries the container sees (read-only unless listed as writable); "vp" holds the verifier's own result-seal
# pytest plugin (aeos.verification.seal), put first on PYTHONPATH of every sealed step
_READ_ONLY_ENTRIES = ("s", "bs", "tr", "fe", "pytest.ini", PLUGIN_DIR)
_WRITABLE_ENTRIES = ("r",)

PROCESS_NOTE = (
    "process runner: candidate code runs as the controller's OS user with a scrubbed, redirected environment "
    "(HOME/USERPROFILE/APPDATA/LOCALAPPDATA/XDG_*/GH_CONFIG_DIR into the run dir, null keyring backend, no git "
    "system/global config or credential helper, no SSH agent socket, no D-Bus session bus) and a fresh temp cwd; "
    "each step in its own kill group (Windows Job Object with KILL_ON_JOB_CLOSE / POSIX new session + marker sweep, "
    "survivors killed and QUARANTINED); no container/VM, no filesystem or egress enforcement (ADR-0051/ADR-0060)")
CONTAINER_NOTE = (
    "container runner: candidate code ran in the pinned verifier image with --network none, read-only root, "
    "non-root user, --cap-drop ALL and no-new-privileges; only the run's snapshot, trusted-suite copy, ini and the "
    "verifier's result-seal plugin (read-only) and its report directory were mounted (plus the trusted controller "
    "backend, read-only, for the expected-id collection step alone), on a host whose container containment report "
    "PASSed")
# In-process result forgery (review W6-054): stated for BOTH runners - a container separates candidate code from the
# host, not from the test process it is imported into.
RESULT_FORGERY_RISK = (
    "Candidate code imported by a test runs inside the same Python test process as the trusted suite (in either "
    "runner), so it can tamper with that process and its report: rewrite the junit file, change the exit code, "
    "patch pytest. The verifier's own result seal (a pytest plugin loaded before collection that streams every "
    "outcome, the session exit status and the junit digest over an HMAC-authenticated, sequence-numbered channel; "
    "cross-checked against the junit, the exit code and the terminal summary) catches a report or exit code changed "
    "after the fact - any disagreement is QUARANTINED, a missing sealed session end FAILS - but candidate code that "
    "deliberately subverts the plugin itself inside the interpreter is not excluded. Black-box assertions from a "
    "separate trusted process against the candidate are not implemented (NOT-PROVEN).")
PROCESS_RESIDUAL_RISKS = (
    "Candidate code runs in a child process of the same OS user as the controller; a deliberately malicious "
    "candidate could still read the controller user's files by absolute path "
    "(data DB, ~/.config/gh/hosts.yml, ~/.ssh, controller checkout) or call the OS credential API directly "
    "(Windows Credential Manager, macOS Keychain, Secret Service). The environment only stops discovery through "
    "environment variables. Writes to the controller checkout/interpreter and to the DB trust tables during the "
    "run are detected afterwards (QUARANTINED; foreign approval/session/grant/route/policy rows are neutralised "
    "automatically, the rest is listed for the owner's special review and admission of new worker runs is paused "
    "until the owner reconciles); reads are not, and neither is a write that "
    "is undone before the window closes. Strong sandbox isolation for verification is "
    "NOT-PROVEN (verifier isolation WEAK; depends on worker_runtime containment).",
    RESULT_FORGERY_RISK,
    "Every step runs in its own kill group (Windows Job Object with KILL_ON_JOB_CLOSE; POSIX new session plus an "
    "environment-marker sweep): a descendant that outlives its step is killed before the tamper window closes and "
    "the verification is QUARANTINED. A process created outside the step's tree on the candidate's behalf (Windows "
    "WMI/Task Scheduler/COM out-of-process servers, a POSIX daemon that leaves the session and drops the marker, "
    "launchd/systemd user units) is not in the group: it can outlive the step and act after the window, undetected.",
    "The dead-proxy environment is an egress deterrent, not an enforced network boundary.",
)
CONTAINER_RESIDUAL_RISKS = (
    "Container isolation is a kernel namespace/cgroup boundary on this host (proven by the containment suite), "
    "not a VM; risk class R4 still needs a VM boundary that V1 does not implement.",
    RESULT_FORGERY_RISK,
    "The container's dependency set is the verifier image's (built from requirements.lock), not a per-candidate "
    "environment: a candidate that changes dependencies stays NOT-PROVEN (§09 Zone 2).",
)


class RunnerError(Exception):
    """The runner itself failed (not a test result): the verifier reports BLOCKED."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def short_basetemp(workdir: Path, name: str) -> Path | None:
    """Windows only: a short pytest basetemp (stable per workdir) under the system temp dir."""
    if sys.platform != "win32":
        return None
    from aeos.core.hashing import sha256_text

    return Path(tempfile.gettempdir()) / f"av{sha256_text(str(workdir))[:8]}{name}"


def _seal_paths(workdir: Path, pythonpath: Path, seal: StepSeal | None) -> list[Path]:
    """PYTHONPATH of a step: the verifier's seal plugin directory first (never shadowed by the candidate), then the
    code under test."""
    return [workdir / PLUGIN_DIR, pythonpath] if seal is not None else [pythonpath]


@dataclass
class ProcessRunner:
    workdir: Path
    reasons: list[str] = field(default_factory=list)
    remedies: list[str] = field(default_factory=list)
    kind: str = "process"
    isolation: str = ISOLATION_WEAK
    steps: list[dict[str, Any]] = field(default_factory=list)

    def cwd(self) -> Path:
        c = self.workdir / "c"
        c.mkdir(exist_ok=True)
        return c

    def env(self, pythonpath: Path | list[Path], *, autoload: bool) -> dict[str, str]:
        wd = self.workdir
        paths = pythonpath if isinstance(pythonpath, list) else [pythonpath]
        return scrubbed_env(pythonpath=paths, home=wd / "h", tmp=wd / "t", data_dir=wd / "d",
                            disable_plugin_autoload=not autoload)

    def python(self, args: list[str], *, pythonpath: Path, autoload: bool, timeout: float,
               seal: StepSeal | None = None, step: str = "") -> ProcResult:
        """One step in its own kill group (``procs.run_process(contain=True)``): survivors are listed on the step
        and killed before the next step / the tamper window close."""
        env = self.env(_seal_paths(self.workdir, pythonpath, seal), autoload=autoload)
        if seal is not None:
            env.update(seal.child_env(str(seal.keyfile)))
        res = run_process([sys.executable, *args], cwd=self.cwd(), env=env, timeout=timeout, contain=True)
        self.steps.append({"step": step or (seal.name if seal else ""), "exit_code": res.returncode,
                           "timed_out": res.timed_out, "duration_s": res.duration_s,
                           "containment": res.containment, "survivors": list(res.survivors),
                           "containment_errors": list(res.containment_errors)})
        return res

    def survivors(self) -> list[dict[str, Any]]:
        """Steps whose kill group still had live processes after the step ended (each listed, then killed). A step
        whose output pipe was still held open after its whole group was killed had a process *outside* the group
        (it escaped it): that is a survivor too, one that could not be killed (fail closed)."""
        out: list[dict[str, Any]] = []
        for s in self.steps:
            listed = list(s.get("survivors") or [])
            if PIPE_HELD_ERROR in (s.get("containment_errors") or []):
                listed.append({"pid": None, "name": "process outside the kill group (held the step's output open)"})
            if listed:
                out.append({"step": s["step"], "survivors": listed})
        return out

    def containment_gaps(self) -> list[str]:
        """Steps whose kill group was weaker than this platform's (no Job Object on Windows, e.g. when the
        verifier itself runs in a job that forbids nesting): survivors were swept by environment marker only."""
        strong = "job-object" if sys.platform == "win32" else "process-group"
        gaps: list[str] = []
        for s in self.steps:
            errors = [e for e in (s.get("containment_errors") or []) if e != PIPE_HELD_ERROR]
            if s.get("containment") != strong or errors:
                gaps.append(f"{s.get('step') or 'step'}: kill group {s.get('containment') or 'none'}"
                            + (f" ({'; '.join(errors)[:200]})" if errors else ""))
        return gaps

    def basetemp(self, name: str) -> str:
        # pytest's per-test temp dirs nest deeply (tmp_path -> data dir -> task-data -> git objects); on Windows
        # that exceeds MAX_PATH under a deep data dir, so the suite gets a short, verifier-owned basetemp directly
        # under the system temp dir (unique per run, removed with the workdir).
        short = short_basetemp(self.workdir, name)
        return str(short if short is not None else self.workdir / name)

    def to_host(self, path: str) -> Path:
        return Path(path)

    def to_container(self, value: str) -> str:
        """Host paths are the child's paths on the process runner (same layout as ``ContainerRunner``)."""
        return value

    def describe(self) -> dict[str, Any]:
        # what the steps actually got (a step whose Job Object could not be created fell back to the marker sweep);
        # before the first step: what this platform provides
        used = sorted({str(s.get("containment")) for s in self.steps if s.get("containment")})
        return {"verifier_runner": self.kind, "verifier_isolation": self.isolation,
                "runner_not_isolated_because": list(self.reasons)[:10],
                "verifier_step_containment": ",".join(used) if used else (
                    "job-object" if sys.platform == "win32" else "process-group")}

    @property
    def note(self) -> str:
        return PROCESS_NOTE

    @property
    def residual_risks(self) -> tuple[str, ...]:
        return PROCESS_RESIDUAL_RISKS


@dataclass
class ContainerRunner:
    workdir: Path
    domain: str
    run_id: str
    image: str
    image_id: str | None = None
    containment: dict[str, Any] = field(default_factory=dict)
    kind: str = "container"
    isolation: str = ISOLATION_CONTAINER
    reasons: list[str] = field(default_factory=list)
    remedies: list[str] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    _extra: dict[str, str] = field(default_factory=dict)  # host dir outside the workdir -> container path (ro)

    # --- path mapping -------------------------------------------------------------------------------------
    def _prefixes(self) -> list[tuple[str, str]]:
        wd = str(self.workdir)
        pairs = [(str(self.workdir / name), f"{CONTAINER_ROOT}/{name}") for name in (*_READ_ONLY_ENTRIES,
                                                                                    *_WRITABLE_ENTRIES)]
        pairs += list(self._extra.items())
        pairs.append((wd, CONTAINER_ROOT))
        return sorted(pairs, key=lambda p: len(p[0]), reverse=True)

    @staticmethod
    def _norm(p: str) -> str:
        return p.lower() if sys.platform == "win32" else p

    def to_container(self, value: str) -> str:
        nv = self._norm(value)
        for host, target in self._prefixes():
            nh = self._norm(host)
            if nv == nh:
                return target
            for sep in ("\\", "/"):
                if nv.startswith(nh + sep):
                    return target + "/" + value[len(host) + 1:].replace("\\", "/")
        return value

    def to_host(self, path: str) -> Path:
        for host, target in self._prefixes():
            if path == target:
                return Path(host)
            if path.startswith(target + "/"):
                return Path(host).joinpath(*path[len(target) + 1:].split("/"))
        return Path(path)

    def share(self, host_dir: Path) -> str:
        """Container path of a host directory: under the run dir it is mapped, anything else (the trusted
        controller backend for the expected-id collection) gets its own read-only mount."""
        mapped = self.to_container(str(host_dir))
        if mapped != str(host_dir):
            return mapped
        key = str(host_dir)
        if key not in self._extra:
            self._extra[key] = f"{CONTAINER_ROOT}/x{len(self._extra)}"
        return self._extra[key]

    def mounts(self, *, extra: str | None = None) -> list[tuple[str, str, bool]]:
        """The run directory's entries, plus - read-only and only when *extra* names it - the one host directory
        outside the run dir the step imports (the trusted controller backend of the expected-id collection).
        Least privilege: a step that runs candidate code never sees the controller checkout (review 2026-10-19,
        K2: every step after the collection used to mount it)."""
        out: list[tuple[str, str, bool]] = []
        for name in _READ_ONLY_ENTRIES:
            p = self.workdir / name
            if p.exists():
                out.append((str(p), f"{CONTAINER_ROOT}/{name}", True))
        for name in _WRITABLE_ENTRIES:
            p = self.workdir / name
            p.mkdir(parents=True, exist_ok=True)
            out.append((str(p), f"{CONTAINER_ROOT}/{name}", False))
        out += [(host, target, True) for host, target in self._extra.items()
                if extra is not None and target == extra and Path(host).exists()]
        return out

    def env(self, pythonpath: str, *, autoload: bool) -> dict[str, str]:
        env = child_env_values(home=CONTAINER_HOME, tmp=CONTAINER_TMP, data_dir=f"{CONTAINER_TMP}/aeos-data",
                               pythonpath=pythonpath, empty_git_config="/dev/null", sep="/")
        env.update({"PATH": CONTAINER_PATH, "LANG": "C.UTF-8", "USER": "verifier", "LOGNAME": "verifier"})
        if not autoload:
            env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        return env

    def _user(self) -> str | None:
        # Linux engine: bind mounts keep host ownership, so the report dir is writable only for the controller's
        # own (non-root) uid; Docker Desktop (Windows/macOS) maps ownership itself and the profile uid is used.
        getuid = getattr(os, "getuid", None)
        getgid = getattr(os, "getgid", None)
        if sys.platform.startswith("linux") and getuid is not None and getgid is not None and getuid() != 0:
            return f"{getuid()}:{getgid()}"
        return None

    def python(self, args: list[str], *, pythonpath: Path, autoload: bool, timeout: float,
               seal: StepSeal | None = None, step: str = "") -> ProcResult:
        from aeos.worker_runtime import facade as worker

        pp = self.share(pythonpath)
        # the seal plugin directory first (read-only /v/vp), never shadowed by the code under test
        env_pp = pp if seal is None else f"{self.share(self.workdir / PLUGIN_DIR)}:{pp}"
        mapped = [self.to_container(a) for a in args]
        verify_id = f"{self.run_id[:12]}-{uuid.uuid4().hex[:10]}"
        env = self.env(env_pp, autoload=autoload)
        if seal is not None:
            env.update(seal.child_env(self.to_container(str(seal.keyfile))))
        try:
            argv = worker.verifier_container_argv(domain=self.domain, verify_id=verify_id,
                                                  mounts=self.mounts(extra=pp),
                                                  env=env,
                                                  inner_argv=["python", *mapped], workdir=CONTAINER_TMP,
                                                  image=self.image, user=self._user())
        except Exception as exc:  # e.g. a refused mount: nothing ran
            raise RunnerError("VERIFIER_RUNNER_FAILED", f"verifier container argv refused: {exc}") from exc
        try:
            res = run_process(argv, cwd=self.workdir, env=worker.docker_cli_env(),
                              timeout=timeout + CONTAINER_GRACE_SECONDS)
        except OSError as exc:
            raise RunnerError("VERIFIER_RUNNER_FAILED", f"docker could not be started: {type(exc).__name__}") from exc
        finally:
            td = worker.verifier_container_teardown(verify_id)
        self.steps.append({"verify_id": verify_id, "step": step or (seal.name if seal else ""),
                           "exit_code": res.returncode, "timed_out": res.timed_out,
                           "duration_s": res.duration_s, "removed": len(td.get("removed") or [])})
        if not res.timed_out and res.returncode in DOCKER_FAILURE_CODES:
            raise RunnerError("VERIFIER_RUNNER_FAILED", f"verifier container did not run (docker exit "
                                                        f"{res.returncode}): {res.tail(400)}")
        return res

    def basetemp(self, name: str) -> str:
        return f"{CONTAINER_TMP}/{name}"

    def survivors(self) -> list[dict[str, Any]]:
        """Nothing outlives a container step: its PID namespace ends with the container (``--rm``, tini as PID 1)
        and the step's containers are removed by label afterwards."""
        return []

    def containment_gaps(self) -> list[str]:
        return []

    def describe(self) -> dict[str, Any]:
        return {"verifier_runner": self.kind, "verifier_isolation": self.isolation, "verifier_image": self.image,
                "verifier_image_id": self.image_id, "verifier_network": "none",
                "verifier_containment": dict(self.containment)}

    @property
    def note(self) -> str:
        return CONTAINER_NOTE

    @property
    def residual_risks(self) -> tuple[str, ...]:
        return CONTAINER_RESIDUAL_RISKS


Runner = ProcessRunner | ContainerRunner


# Sandbox kinds of a worker run whose code was produced behind a kernel boundary: the verifier never executes
# that code outside one (isolation is never downgraded - review 2026-10-19, K2: a container worker would
# otherwise get delayed host code execution through the WEAK process runner whenever the verifier image is
# missing or the containment report went stale between the worker run and its verification).
ISOLATED_RUN_SANDBOXES = frozenset({"container", "vm"})


def isolation_required(policy: dict[str, Any], domain: str, run_sandbox: str | None = None) -> bool:
    """OFFICE always; a run produced in an isolated sandbox (``ISOLATED_RUN_SANDBOXES``); otherwise when the
    TaskSpec says ``verifier_policy.isolated_runner: required``."""
    raw = policy.get("isolated_runner")
    return (domain == "OFFICE" or str(run_sandbox or "").strip().lower() in ISOLATED_RUN_SANDBOXES
            or raw is True or str(raw or "").strip().lower() in ("required", "true", "yes"))


def _required_because(policy: dict[str, Any], domain: str, run_sandbox: str | None) -> str:
    if domain == "OFFICE":
        return "OFFICE verification"
    if str(run_sandbox or "").strip().lower() in ISOLATED_RUN_SANDBOXES:
        return (f"the worker run executed in the {run_sandbox} sandbox; its code is never executed outside a "
                "container (isolation is never downgraded), so this verification")
    return "TaskSpec verifier_policy.isolated_runner"


def runner_status(session: Any, *, domain: str) -> dict[str, Any]:
    """Which runner a verification for *domain* would use now (no candidate needed). Fail closed: any error
    reading the sandbox state means the process runner (WEAK)."""
    try:
        from aeos.worker_runtime import facade as worker

        info = worker.verifier_sandbox(session, domain=domain)
    except Exception as exc:  # never CONTAINER on an error
        info = {"status": "UNAVAILABLE", "reasons": [f"verifier sandbox state unreadable ({type(exc).__name__})"],
                "remedies": []}
    ready = info.get("status") == "READY"
    return {
        "domain": domain,
        "runner": "container" if ready else "process",
        "isolation": ISOLATION_CONTAINER if ready else ISOLATION_WEAK,
        "image": info.get("image"),
        "image_id": info.get("image_id"),
        "containment": info.get("containment") or {},
        "reasons": [str(r) for r in info.get("reasons") or []],
        "remedies": [str(r) for r in info.get("remedies") or []],
        "residual_risks": list(CONTAINER_RESIDUAL_RISKS if ready else PROCESS_RESIDUAL_RISKS),
        "office_requires_container": True,
    }


def select_runner(session: Any, *, domain: str, workdir: Path, run_id: str, policy: dict[str, Any],
                  run_sandbox: str | None = None) -> Runner:
    """The runner for one verification; raises ``RunnerError`` when isolation is required but unavailable.
    *run_sandbox* is the sandbox kind the worker run executed in (``WorkerRun.sandbox_kind``)."""
    st = runner_status(session, domain=domain)
    if st["runner"] == "container":
        return ContainerRunner(workdir=workdir, domain=domain, run_id=run_id, image=str(st["image"]),
                               image_id=st.get("image_id"), containment=dict(st.get("containment") or {}))
    if isolation_required(policy, domain, run_sandbox):
        why = "; ".join(st["reasons"][:3]) or "container runner unavailable"
        raise RunnerError("VERIFIER_ISOLATION_UNAVAILABLE",
                          _required_because(policy, domain, run_sandbox)
                          + f" requires the isolated container runner, which is not available: {why}")
    return ProcessRunner(workdir=workdir, reasons=st["reasons"], remedies=st["remedies"])


def new_workdir() -> Path:
    """A fresh, private (0700) verifier run directory under the system temp dir.

    Never under the AEOS data dir (review 2026-10-19, K2): every path the process runner hands to candidate code
    (HOME, TMP, AEOS_DATA_DIR, PYTHONPATH, cwd, the snapshot's own ``__file__``) lives in this directory, so
    a run dir under the data root let candidate code derive the live DB path from its own environment
    (``Path(HOME).parents[2] / "aeos.sqlite3"``). Short on purpose (Windows MAX_PATH)."""
    return Path(tempfile.mkdtemp(prefix="aeos-vw-")).resolve()
