"""Independent Verifier (§09, §16, §19, §20, §34).

The verifier runs **outside the candidate trust context**:

* it verifies the exact committed tree of the candidate ``head`` (materialised through git
  plumbing — no hooks, filters, attributes or textconv), never the live worker directory;
* the **trusted acceptance suite** is copied from the trusted controller checkout
  (``aeos.verification.suite.trusted_controller_root()`` → ``repo_root()``) — never the candidate's
  own ``tests/acceptance`` — and run against the candidate code in a child process with
  ``PYTHONPATH=<snapshot>/backend``, a fresh temp ``cwd``, a scrubbed environment, a verifier-owned
  pytest ini (the candidate's pytest/ruff configuration is ignored) and timeouts;
* the expected test ids are collected first with the *trusted controller code*; a candidate run
  that drops, deselects or skips trusted tests cannot pass;
* the candidate's own unit tests and ruff (with the trusted ruff config) are additional,
  lower-trust signals: failing them fails verification, passing them proves nothing alone;
* changed files come from ``git diff base..head`` and are checked against the TaskSpec and the
  protected-path list (§50 "Worker mengubah grader/policy/pipeline" → FAIL / QUARANTINED);
* worker claims ("exit 0", "all tests pass", worker attachments) are listed but never counted
  (§50 "Worker exit 0 / menulis all tests pass" → REVIEW/NOT-PROVEN until verifier evidence).

Verifier status: PASS only when the trusted suite passed completely and no scope/secret/tamper
violation exists. Otherwise FAIL, QUARANTINED (protected path explicitly allowed — special review),
NOT_PROVEN (e.g. skipped trusted tests) or BLOCKED (prerequisite missing; nothing was proven).
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import logging
import shutil
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos import __version__
from aeos.audit import service as audit
from aeos.core.config import get_settings
from aeos.core.enums import DataClassification, DataMode, Domain, ExecutionState
from aeos.core.errors import AeosError, Conflict, NotFound
from aeos.core.hashing import content_hash, file_sha256, sha256_bytes
from aeos.core.paths import UnsafePathError, ensure_within
from aeos.events import service as events
from aeos.models.evidence import EvidenceArtifact
from aeos.models.planning import TaskSpec
from aeos.models.portfolio import Project
from aeos.models.workforce import WorkerRun
from aeos.verification import (
    adversarial,
    collector,
    contracts,
    controlled,
    db_evidence,
    envs,
    frontend,
    neutralize,
    runner,
    scope,
    seal,
    steps,
    suite,
    tamper,
)
from aeos.verification import impact as impact_map
from aeos.verification.integration import call
from aeos.verification.junit import JUnitParseError, JUnitReport, nodeid_to_key, parse_junit
from aeos.verification.procs import (
    GitError,
    GitReader,
    ProcResult,
    SnapshotError,
    run_process,
    scrubbed_env,
    valid_branch,
)

log = logging.getLogger("aeos.verification.verifier")

# v2: impact-driven checks (negative tests, dependency diff / Zone 2, frontend + migration honesty),
# TaskSpec suite selection, E4 protected-suite references.
# v3: isolated runner selection (container when proven, else WEAK with the hardened environment), controller
# checkout/interpreter + DB trust-table/schema tamper window around candidate code and the migration round-trip
# (impact adds steps the verifier runs itself; it does not yet widen the trusted test selection, §22/§31 - stated
# as NOT-PROVEN, not claimed).
# v4 (review W6-054/W6-057): the verifier-owned result seal on every pytest step that runs candidate code and on the
# migration round-trip (a forged junit/exit code is QUARANTINED), every process-runner step in its own kill group
# (a survivor is QUARANTINED), and automatic neutralisation of foreign approval/session/grant/route/policy rows.
# v4 (wave 7 E1, same version - both landed together): impact -> executed checks (aeos.verification.impact):
# AST-detected negative tests executed, verifier-owned adversarial probes + trusted modules per category, contract
# diff (facades/OpenAPI) + contract tests, opt-in integration-marked tests, per-candidate dependency environment from
# the candidate lockfile, frontend step (npm ci --ignore-scripts, vitest with the verifier's config, type check,
# build) + UI states/a11y probe, seeded backup/restore + rollback-with-data in the migration round-trip,
# migration/runtime credential split.
# v5 (wave 7b, ADR-0120 integration note): dependency_set_hash of the per-candidate environment covers only the
# candidate's installed distributions, read by the verifier from their metadata (no candidate process lists them;
# the trusted harness is recorded separately); the venv creation and the pip install run the controller
# interpreter in isolated mode (-I) in their own kill groups; the frontend tool steps run in kill groups; the
# OpenAPI dump and the UI driver are sealed with the digests of the files they write. A v4 report's hashes mean
# something else, so it is never reused as a v5 result: the version is part of the config hash.
# A new version makes earlier acceptance STALE.
VERIFIER_VERSION = "5"
ORIGIN = "verifier"
MARK_EXPR = "not integration and not network"
PASSING = "PASS"
STATUSES = ("PASS", "FAIL", "QUARANTINED", "NOT_PROVEN", "BLOCKED")
MAX_DIFF_BYTES = 20 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
STORED_DIFF_CHARS = 2_000_000
MAX_PROTECTED_IDS = 2000

# §20 "Verifier ENF: read-only suite, isolated runner, evidence collector": the suite is read-only, the
# collector is enforced and candidate code runs through ``aeos.verification.runner``: the container runner
# (isolation CONTAINER) only on a host whose container boundary is proven, otherwise the process runner
# (isolation WEAK). Every VERIFICATION_REPORT states which (``verifier_isolation`` / ``verifier_runner``) with
# its residual risks; WEAK is never presented as isolated. ``VERIFIER_ISOLATION`` is the default (process).
VERIFIER_ISOLATION = runner.ISOLATION_WEAK
VERIFIER_ISOLATION_NOTE = runner.PROCESS_NOTE
RESIDUAL_RISKS = runner.PROCESS_RESIDUAL_RISKS
# Negative/refusal tests are detected in the syntax tree and executed (aeos.verification.controlled, wave 7): the
# former regex over added lines (comments included) is gone.
_PY_DEPENDENCY_FILES = ("requirements.lock", "requirements", "pyproject.toml", "poetry.lock", "uv.lock",
                        "Pipfile.lock")
# verifier_policy.suites -> candidate test directories the verifier runs (the trusted acceptance suite always runs)
SUITE_DIRS = {"unit": "unit", "integration": "integration"}

PYTEST_INI = """[pytest]
markers =
    integration: needs local tools (claude, opencode, openbao, docker); skipped when absent
    acceptance: trusted acceptance suite (read-only for workers)
    network: needs internet access
junit_family = xunit2
junit_logging = no
filterwarnings =
    ignore::DeprecationWarning
"""

# Modules the verifier's test process itself depends on. A candidate that ships a top-level module
# with one of these names (or any stdlib name) in backend/ would run *its* test runner instead of the
# trusted one (PYTHONPATH precedes site-packages and the stdlib), so the trusted suite is not run.
HARNESS_MODULES = frozenset({
    "pytest", "_pytest", "pluggy", "iniconfig", "packaging", "py", "tests", "conftest", "sitecustomize",
    "usercustomize", "ruff", "xdist", "exceptiongroup", "tomli", "colorama", "_distutils_hack", "setuptools",
    "pip", "typing_extensions", seal.PLUGIN_MODULE,
})
_MODULE_SUFFIXES = (".py", ".pyc", ".pyo", ".pyd", ".so", ".dylib")
# runner steps that import trusted code only (controller checkout / reviewed base commit + trusted suite). A process
# outliving any OTHER step - every step that executes candidate code, and any unnamed or future step (fail closed) -
# is tampering (see _guard_close). The frontend's npm ci and the per-candidate pip install execute no candidate code
# (install scripts off, hash-pinned wheels only) but install candidate-chosen packages: not trusted-only (fail closed).
# The per-candidate venv is created by the controller's own interpreter (stdlib ``venv``, isolated mode ``-I``: nothing
# of the run directory is importable) before anything is installed (``envs.venv_command``).
TRUSTED_ONLY_STEPS = frozenset({"collect_expected", "baseline", "contract_openapi_base", "dependency_env_venv"})

_PREFLIGHT = (
    "import importlib, json, sys\n"
    # the verifier's result seal, imported before any candidate module: the host only uses the mapping sealed at
    # the very end, never a line an imported candidate module printed itself (review W6-054)
    "try:\n"
    "    import aeos_vseal as _vseal\n"
    "except Exception:\n"
    "    _vseal = None\n"
    "out = {}\n"
    "for n in sys.argv[1:]:\n"
    "    try:\n"
    "        m = importlib.import_module(n)\n"
    "        out[n] = getattr(m, '__file__', None)\n"
    "    except BaseException as e:\n"  # SystemExit at import too: reported, never the end of the driver
    "        out[n] = 'IMPORT_ERROR: ' + type(e).__name__\n"
    "if _vseal is not None:\n"
    "    _vseal.emit('result', d=out)\n"
    "print(json.dumps(out))\n"
)


# Candidate-code driver for schema migrations (``packs/migration_roundtrip.py``): upgrade -> downgrade(base) ->
# upgrade on a throwaway SQLite DB, seeded backup -> delete -> restore -> compare, and one-revision rollback with the
# seeded data present. Runs under the verifier runner (inside the tamper window): it executes the candidate's
# migration/env code. It imports the verifier's result seal before any candidate code and seals its result at the
# very end; the host only uses that sealed result (review W6-054).
MIGRATION_DRIVER = "migration_roundtrip.py"
# pytest basetemps of the run's steps (trusted suite, candidate tests, baseline, negative tests, integration trusted/
# candidate, adversarial probes, per-candidate environment); removed with the run dir.
BASETEMP_NAMES = ("b1", "b2", "b3", "b4", "b5", "b6", "b7", "b8")


def top_level_modules(backend: Path) -> list[str]:
    """Importable top-level names a ``backend/`` directory puts on ``sys.path`` (packages and modules)."""
    if not backend.is_dir():
        return []
    names: set[str] = set()
    for p in backend.iterdir():
        if p.is_dir() and (p / "__init__.py").is_file():
            names.add(p.name)
        elif p.is_file() and p.name.lower().endswith(_MODULE_SUFFIXES):
            names.add(p.name.split(".", 1)[0])
    return sorted(names)


def shadowed_modules(candidate_backend: Path, trusted_backend: Path) -> list[str]:
    """Candidate top-level modules that would shadow the stdlib, the test harness or any module the
    controller environment already provides — except the platform's own packages (present in the
    trusted controller's ``backend/``), which the candidate is expected to change."""
    own = set(top_level_modules(trusted_backend))
    out: list[str] = []
    for name in top_level_modules(candidate_backend):
        if name in own:
            continue
        if name in HARNESS_MODULES or name in sys.stdlib_module_names or name in sys.builtin_module_names:
            out.append(name)
            continue
        try:
            if importlib.util.find_spec(name) is not None:
                out.append(name)
        except (ImportError, ValueError):
            out.append(name)  # unresolvable spec: treat as shadowing (fail closed)
    return out


def verifier_identity() -> str:
    py = ".".join(str(x) for x in sys.version_info[:3])
    return f"aeos-verifier/{__version__}+v{VERIFIER_VERSION} (python {py}; {sys.platform})"


def compute_config_hash(*, suite_hash: str, trusted_root: Path, task_spec_hash: str | None,
                        policy: dict[str, Any]) -> str:
    """Identity of everything that decides a verification result besides the candidate itself.

    A change of grader (suite hash), lint configuration, verifier version, interpreter, TaskSpec or
    verifier policy changes this hash and therefore makes earlier acceptance STALE (§08, §14, §50).
    """
    pyproject = trusted_root / "pyproject.toml"
    return content_hash({
        "verifier_version": VERIFIER_VERSION,
        "suite_hash": suite_hash,
        "ruff_config_hash": file_sha256(pyproject) if pyproject.is_file() else None,
        "task_spec_hash": task_spec_hash,
        "python": ".".join(str(x) for x in sys.version_info[:3]),
        "mark_expr": MARK_EXPR,
        "policy": {k: policy[k] for k in sorted(policy) if k in
                   ("candidate_tests", "ruff", "suite_timeout_seconds", "unit_timeout_seconds", "integration_tests",
                    "contract_breaking", "isolated_dependencies", "negative_tests_required", "migration_roundtrip")},
    })


def current_config_hash(task: TaskSpec, session: Session | None = None) -> str | None:
    """Config hash a verification of *task* would have now (None when the trusted suite is unreadable).

    The suite is the project's trusted suite: the owner-registered copy when the project has a
    registration (integration amendment, ADR-0060), otherwise the controller checkout (as before — a
    project without a registration can never be verified PASS, and a hash computed from another suite
    never matches a registered one, so this fallback cannot validate anything). A registered copy that
    is missing or tampered gives None (UNKNOWN, fail closed)."""
    from sqlalchemy.orm import object_session

    try:
        s = session or object_session(task)
        project = s.get(Project, task.project_id) if s is not None else None
        root = suite.trusted_controller_root().resolve()
        if project is not None and not project.is_platform and suite.has_registration(s, project.id):
            root = suite.resolve_trusted_suite(s, project).root
        return compute_config_hash(suite_hash=suite.trusted_suite_hash(root), trusted_root=root,
                                   task_spec_hash=task.spec_hash, policy=dict(task.verifier_policy or {}))
    except (AeosError, OSError):
        return None


@dataclass
class VerifierLimits:
    suite_timeout: float = 900.0
    unit_timeout: float = 900.0
    ruff_timeout: float = 180.0
    collect_timeout: float = 300.0
    preflight_timeout: float = 120.0
    git_timeout: float = 120.0

    @classmethod
    def from_policy(cls, policy: dict[str, Any]) -> VerifierLimits:
        lim = cls()

        def bounded(key: str, default: float) -> float:
            try:
                v = float(policy.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(10.0, min(v, 3600.0))

        lim.suite_timeout = bounded("suite_timeout_seconds", lim.suite_timeout)
        lim.unit_timeout = bounded("unit_timeout_seconds", lim.unit_timeout)
        return lim


@dataclass
class CheckOutcome:
    name: str
    status: str  # PASS | FAIL | NOT_RUN | UNKNOWN | NOT_PROVEN | BLOCKED
    summary: str = ""
    evidence_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerificationOutcome:
    run_id: str
    task_id: str | None
    project_id: str
    domain: str
    status: str = "BLOCKED"
    reasons: list[str] = field(default_factory=list)
    not_proven: list[str] = field(default_factory=list)
    blocked_code: str | None = None
    quarantine_reason: str | None = None
    base_commit: str | None = None
    head_commit: str | None = None
    tree_hash: str | None = None
    artifact_hash: str | None = None
    config_hash: str | None = None
    suite_hash: str | None = None
    changed_files: list[str] = field(default_factory=list)
    docs_only: bool = False
    high_impact_paths: list[str] = field(default_factory=list)
    security_sensitive_paths: list[str] = field(default_factory=list)
    checks: dict[str, CheckOutcome] = field(default_factory=dict)
    worker_claims: list[dict[str, Any]] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    report_evidence_id: str | None = None
    failing_tests: list[str] = field(default_factory=list)
    run_update: str = "NOT_ATTEMPTED"
    task_transition: str = "NOT_ATTEMPTED"
    repair: str = "NOT_ATTEMPTED"
    compute_usage: str = "NOT_RECORDED"
    verifier: str = field(default_factory=verifier_identity)
    verifier_isolation: str = VERIFIER_ISOLATION
    verifier_runner: str = "process"
    tamper_findings: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["checks"] = {k: asdict(v) for k, v in self.checks.items()}
        return d


class _Blocked(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _PendingEvidence:
    kind: str
    claim: str
    content: dict[str, Any] | str | bytes | None
    level: str
    check: str | None = None
    env_extra: dict[str, Any] = field(default_factory=dict)


_dependency_set_cache: dict[str, Any] = {}


def dependency_set() -> dict[str, Any]:
    """Identity of the controller interpreter's installed distributions (what the trusted suite ran with).

    §09 Zone 2: the verifier does not build a per-candidate environment; recording this hash makes the
    dependency set of every verification explicit (and comparable) instead of implied."""
    if "hash" not in _dependency_set_cache:
        pins: set[str] = set()
        for dist in importlib.metadata.distributions():
            # PackageMetadata protocol only (``get`` is not part of it before 3.12): first Name header, a dist
            # without one is skipped
            name = str((dist.metadata.get_all("Name") or [""])[0] or "").strip().lower()
            if name:
                pins.add(f"{name}=={dist.version}")
        ordered = sorted(pins)
        _dependency_set_cache.update(hash=content_hash(ordered), count=len(ordered))
    return dict(_dependency_set_cache)


_locks_guard = threading.Lock()
_run_locks: dict[str, threading.Lock] = {}


def _lock_for(run_id: str) -> threading.Lock:
    with _locks_guard:
        return _run_locks.setdefault(run_id, threading.Lock())


def latest_report(session: Session, run_id: str) -> EvidenceArtifact | None:
    return session.scalar(
        select(EvidenceArtifact)
        .where(EvidenceArtifact.run_id == run_id, EvidenceArtifact.kind == "VERIFICATION_REPORT",
               EvidenceArtifact.origin == ORIGIN, EvidenceArtifact.trusted.is_(True))
        .order_by(EvidenceArtifact.created_at.desc())
        .limit(1)
    )


def report_refs(ev: EvidenceArtifact | None) -> list[str]:
    if ev is None:
        return []
    full = collector.read_json(ev) or collector.evidence_summary(ev)
    return [r for r in (full.get("evidence_refs") or []) if isinstance(r, str)]


def related_evidence(session: Session, report: EvidenceArtifact | None, kind: str) -> EvidenceArtifact | None:
    """The artifact of *kind* referenced by a VERIFICATION_REPORT (e.g. SCOPE_CHECK, DIFF)."""
    refs = report_refs(report)
    if not refs:
        return None
    return session.scalar(select(EvidenceArtifact).where(EvidenceArtifact.id.in_(refs), EvidenceArtifact.kind == kind)
                          .order_by(EvidenceArtifact.created_at.desc()).limit(1))


def report_view(ev: EvidenceArtifact | None) -> dict[str, Any] | None:
    """Status + identities of a VERIFICATION_REPORT (summary fields and environment)."""
    if ev is None:
        return None
    summary = collector.evidence_summary(ev)
    env = ev.environment or {}
    return {
        "evidence_id": ev.id,
        "status": summary.get("status", "UNKNOWN"),
        "created_at": ev.created_at.isoformat(),
        "mode": ev.mode,
        "trusted": ev.trusted,
        "evidence_level": ev.evidence_level,
        "head_commit": env.get("commit"),
        "base_commit": env.get("base_commit"),
        "tree_hash": env.get("tree"),
        "artifact_hash": env.get("artifact_hash"),
        "config_hash": env.get("config_hash"),
        "suite_hash": env.get("suite_hash"),
        "verifier": env.get("verifier"),
        "trusted_suite_status": summary.get("trusted_suite_status"),
        "candidate_tests_status": summary.get("candidate_tests_status"),
        "ruff_status": summary.get("ruff_status"),
        "scope_status": summary.get("scope_status"),
        # "leak_scan" (not "secret_scan"): keys containing secret/token/credential are masked by redact().
        "leak_scan_status": summary.get("leak_scan_status"),
        "requested_head": env.get("requested_head"),
        "tests_passed": summary.get("tests_passed"),
        "tests_failed": summary.get("tests_failed"),
        "tests_skipped": summary.get("tests_skipped"),
        "docs_only": summary.get("docs_only"),
        "impact": summary.get("impact", "UNKNOWN"),
        "security_sensitive": summary.get("security_sensitive"),
        "quarantine_reason": summary.get("quarantine_reason"),
        "blocked_code": summary.get("blocked_code"),
        "summary": summary.get("summary"),
        # reports written before v2 carry no isolation field: the runner was never isolated (WEAK)
        "verifier_isolation": summary.get("verifier_isolation") or env.get("verifier_isolation") or VERIFIER_ISOLATION,
        "verifier_runner": summary.get("verifier_runner") or env.get("verifier_runner") or "process",
        "dependency_set_hash": env.get("dependency_set_hash"),
    }


def verify_run(
    session: Session, run_id: str, *, actor: str, actor_type: str = "user", candidate_path: str | None = None,
    request_repair_on_fail: bool = True,
) -> VerificationOutcome:
    """Verify a WorkerRun in REVIEW. Writes evidence; never commits (caller commits)."""
    lock = _lock_for(run_id)
    if not lock.acquire(blocking=False):
        raise Conflict("verification of this run is already in progress", code="VERIFICATION_IN_PROGRESS")
    try:
        return _Verifier(session, run_id, actor=actor, actor_type=actor_type, candidate_path=candidate_path,
                         request_repair_on_fail=request_repair_on_fail).run()
    finally:
        lock.release()


class _Verifier:
    def __init__(self, session: Session, run_id: str, *, actor: str, actor_type: str, candidate_path: str | None,
                 request_repair_on_fail: bool):
        self.session = session
        self.run_id = run_id
        self.actor = actor
        self.actor_type = actor_type
        self.candidate_path = candidate_path
        self.request_repair_on_fail = request_repair_on_fail
        self.pending: list[_PendingEvidence] = []
        # Not-proven items that alone keep the result from PASS (e.g. head not descending from base).
        self.blocking_not_proven: list[str] = []
        self.changed_entries: list[tuple[str, str]] = []
        self.numstat: dict[str, dict[str, int | None]] = {}
        self.identity = verifier_identity()
        self.started = time.monotonic()
        deps = dependency_set()
        self.env_identity: dict[str, Any] = {"verifier": self.identity, "python": sys.version.split()[0],
                                             "platform": sys.platform, "verifier_version": VERIFIER_VERSION,
                                             "dependency_set_hash": deps["hash"],
                                             "verifier_isolation": VERIFIER_ISOLATION, "verifier_runner": "process"}
        self.diff_text = ""
        self.runner: runner.ProcessRunner | runner.ContainerRunner | None = None
        self._guard: dict[str, Any] | None = None
        self.tamper_findings: list[dict[str, str]] = []
        self.tamper_checks: dict[str, Any] = {}
        self.migration_roundtrip: dict[str, Any] | None = None
        self.result_seals: dict[str, dict[str, Any]] = {}  # step -> seal.SealResult.to_dict()
        self.neutralized: dict[str, Any] = {}
        self.foreign_all: list[dict[str, str]] = []  # every foreign trust-table row, full keys (tamper.Window)
        # wave 7 E1: inputs of the impact-selected steps (set in run())
        self.git: GitReader | None = None
        self.suite_kind = "CONTROLLER"
        self.code_root: Path | None = None
        self.trusted_copy: Path | None = None
        self.ini: Path | None = None
        self.expected: list[str] = []
        self.trusted_cases: dict[str, str] = {}
        self.impact_summary: dict[str, dict[str, Any]] = {}
        self._base_dir: Path | None = None
        self._base_failed = False

    # ------------------------------------------------------------------ orchestration

    def run(self) -> VerificationOutcome:
        s = self.session
        run = s.get(WorkerRun, self.run_id)
        if run is None:
            raise NotFound("run not found")
        if run.execution_status != ExecutionState.REVIEW.value:
            raise Conflict("only runs in REVIEW can be verified", code="RUN_NOT_IN_REVIEW",
                           details={"execution_status": run.execution_status})
        project = s.get(Project, run.project_id)
        if project is None:
            raise NotFound("project not found")
        task = s.get(TaskSpec, run.task_id) if run.task_id else None
        self.run_row, self.project, self.task = run, project, task
        out = VerificationOutcome(run_id=run.id, task_id=run.task_id, project_id=project.id, domain=project.domain)
        self.out = out
        # The head the runtime asked to verify — recorded even when nothing could be resolved, so the
        # deterministic trigger never re-runs a BLOCKED verification for the same head (loop.py).
        self.env_identity["requested_head"] = run.head_commit
        out.worker_claims = self._worker_claims(run)
        workdir: Path | None = None
        trusted_ran = False
        try:
            candidate = self._candidate(run, project)
            if task is None:
                raise _Blocked("NO_TASKSPEC", "run has no TaskSpec; scope cannot be verified")
            try:
                # platform: the trusted controller checkout; other projects: the owner-registered copy
                # bound to a POLICY decision on its hash (integration amendment, ADR-0060)
                trusted_suite = suite.resolve_trusted_suite(s, project)
            except AeosError as exc:
                raise _Blocked(exc.code, exc.message) from exc
            if importlib.util.find_spec("pytest") is None:
                raise _Blocked("PYTEST_UNAVAILABLE", "pytest is not installed in the controller environment")
            trusted_root = trusted_suite.root
            self.env_identity["suite_kind"] = trusted_suite.kind
            if trusted_suite.decision_id:
                self.env_identity["suite_decision_id"] = trusted_suite.decision_id
            self._check_separation(candidate, trusted_root)
            try:
                suite_files = suite.trusted_suite_files(trusted_root)
            except AeosError as exc:
                raise _Blocked(exc.code, exc.message) from exc
            out.suite_hash = suite.hash_files(suite_files)
            self.env_identity["suite_hash"] = out.suite_hash
            policy = dict(task.verifier_policy or {})
            limits = VerifierLimits.from_policy(policy)
            git = self._git(candidate, limits, run)
            self.git, self.suite_kind = git, trusted_suite.kind
            self._resolve_commits(git, run, task)
            diff = self._diff_and_scope(git, task, project)
            self.diff_text = diff
            out.config_hash = self._config_hash(trusted_root, task, policy)
            self.env_identity["config_hash"] = out.config_hash
            workdir = self._make_workdir()
            try:
                # §20 isolated runner: the container when the boundary is proven on this host, else WEAK
                # (OFFICE / a run produced in a container / verifier_policy.isolated_runner=required -> BLOCKED
                # instead, fail closed: isolation is never downgraded)
                self.runner = runner.select_runner(s, domain=project.domain, workdir=workdir, run_id=run.id,
                                                   policy=policy, run_sandbox=run.sandbox_kind)
            except runner.RunnerError as exc:
                raise _Blocked(exc.code, exc.message) from exc
            self.env_identity.update(self.runner.describe())
            out.verifier_isolation = self.runner.isolation
            out.verifier_runner = self.runner.kind
            snapshot = workdir / "s"
            snapshot.mkdir()
            snap_ok = self._export(git, snapshot)
            self._static_scans(diff, snapshot if snap_ok else None)
            # Trusted *code* for collecting the expected test ids: the controller checkout for the
            # platform, the human-reviewed base commit of the project otherwise (never the candidate head).
            code_root = trusted_root if trusted_suite.kind == "CONTROLLER" else self._export_base(git, workdir)
            self.code_root = code_root
            if snap_ok:
                snap_ok = self._check_shadowing(snapshot, code_root)
            if snap_ok:
                trusted_copy = workdir / "tr"
                copied, copy_hash = suite.copy_trusted_suite(trusted_copy, trusted_root)
                if copy_hash != out.suite_hash:
                    raise _Blocked("TRUSTED_SUITE_COPY_MISMATCH", "trusted suite copy does not match its hash")
                ini = workdir / "pytest.ini"
                ini.write_text(PYTEST_INI, encoding="utf-8")
                self.trusted_copy, self.ini = trusted_copy, ini
                # the verifier-owned result seal (aeos.verification.seal): first on PYTHONPATH of every sealed step
                seal.install_plugin(workdir)
                self.env_identity["result_seal_sha256"] = seal.PLUGIN_SHA256
                # §09 Zone 1 / §21: candidate code is about to run; fingerprint the controller checkout +
                # interpreter and the DB trust tables now, compare after the last candidate step.
                self._guard_open()
                try:
                    self._preflight(snapshot, workdir, limits)
                    expected = self._collect_expected(code_root, trusted_copy, ini, workdir, limits)
                    self.expected = expected
                    trusted_ran = True
                    self._run_trusted_suite(snapshot, trusted_copy, ini, workdir, limits, expected)
                    self._check_suite_integrity(copied, trusted_root)
                    if policy.get("baseline"):
                        base_code = code_root if trusted_suite.kind != "CONTROLLER" else None
                        self._run_baseline(git, workdir, trusted_copy, ini, limits, base_code)
                    if policy.get("candidate_tests", True):
                        self._run_candidate_tests(snapshot, ini, workdir, limits, suites=self._suites(policy))
                    else:
                        self._check("candidate_tests", "NOT_RUN", "disabled by TaskSpec verifier_policy")
                    self._run_impact_steps(snapshot, workdir, limits, policy)
                finally:
                    self._guard_close()
                if policy.get("ruff", True):
                    self._run_ruff(snapshot, trusted_root, workdir, limits)
                else:
                    self._check("ruff", "NOT_RUN", "disabled by TaskSpec verifier_policy")
            self._impact_checks(snapshot if snap_ok else None, policy)
            self._decide()
        except _Blocked as b:
            out.status = "BLOCKED"
            out.blocked_code = b.code
            out.reasons.append(b.message)
        except runner.RunnerError as exc:  # the runner itself failed mid-way: nothing about the candidate is proven
            out.status = "BLOCKED"
            out.blocked_code = exc.code
            out.reasons.append(exc.message)
        finally:
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)
                for name in BASETEMP_NAMES:
                    short = _short_basetemp(workdir, name)
                    if short is not None:
                        shutil.rmtree(short, ignore_errors=True)
        self._apply_tamper()
        self._neutralize()
        self._persist(trusted_ran)
        self._side_effects()
        self._audit_and_emit()
        return out

    # ------------------------------------------------------------------ inputs

    def _worker_claims(self, run: WorkerRun) -> list[dict[str, Any]]:
        claims: list[dict[str, Any]] = []
        if run.worker_reported_status:
            claims.append({"source": "worker_run.worker_reported_status", "claim": run.worker_reported_status})
        if run.exit_code is not None:
            claims.append({"source": "worker_run.exit_code", "claim": f"worker exit code {run.exit_code}"})
        for ev in self.session.scalars(
            select(EvidenceArtifact).where(EvidenceArtifact.run_id == run.id, EvidenceArtifact.trusted.is_(False))
        ):
            claims.append({"source": f"evidence:{ev.id}", "claim": ev.claim[:300], "level": ev.evidence_level,
                           "origin": ev.origin})
        for c in claims:
            c["counted"] = False  # worker claims never count towards the verdict (§34)
        return claims

    def _candidate(self, run: WorkerRun, project: Project) -> Path:
        raw = self.candidate_path or run.workspace_ref
        if not raw:
            raise _Blocked("NO_CANDIDATE", "run has no candidate workspace reference")
        cand = Path(raw).expanduser()
        if not cand.is_absolute():
            raise _Blocked("CANDIDATE_PATH_INVALID", "candidate path must be absolute")
        try:
            root = get_settings().layout.task_data(Domain(project.domain), project.project_key)
            resolved = ensure_within(root, cand)
        except UnsafePathError as exc:
            raise _Blocked("CANDIDATE_OUTSIDE_TASK_DATA",
                           "candidate must live in the project's managed task-data directory") from exc
        if not resolved.is_dir():
            raise _Blocked("CANDIDATE_MISSING", "candidate workspace does not exist")
        return resolved

    @staticmethod
    def _check_separation(candidate: Path, trusted_root: Path) -> None:
        if candidate == trusted_root or trusted_root in candidate.parents or candidate in trusted_root.parents:
            raise _Blocked("TRUST_ZONE_OVERLAP", "candidate workspace overlaps the trusted controller checkout (§09)")

    def _git(self, candidate: Path, limits: VerifierLimits, run: WorkerRun) -> GitReader:
        """Git reads of the candidate through the trusted git dir the runtime recorded (owner rule R44, ADR-0132):
        the work tree's ``.git`` pointer is worker-editable and never followed (a planted git dir could borrow the
        trusted objects through alternates and add its own attributes or replace refs). Missing: BLOCKED. A git dir
        whose replace refs, grafts or ``info/attributes`` could change what the reads show: BLOCKED."""
        git_dir = Path(run.git_dir_ref) if run.git_dir_ref else None
        if git_dir is None or not git_dir.is_dir():
            raise _Blocked("GIT_DIR_UNKNOWN", "the runtime recorded no trusted git dir for the candidate; the "
                                              "worker-editable .git pointer of the work tree is never followed")
        try:
            git = GitReader(candidate, timeout=limits.git_timeout, git_dir=git_dir)
            if not git.is_repository():
                raise _Blocked("CANDIDATE_NOT_GIT", "candidate workspace is not a git repository")
            problem = git.integrity_problem()
        except GitError as exc:
            raise _Blocked("GIT_UNAVAILABLE", str(exc)) from exc
        if problem:
            raise _Blocked("GIT_CONTROL_UNTRUSTED", f"the candidate's git dir could change what the verifier and "
                                                    f"the reviewer see: {problem}")
        return git

    def _resolve_commits(self, git: GitReader, run: WorkerRun, task: TaskSpec) -> None:
        out = self.out
        base = run.base_commit or task.base_commit
        if not base:
            raise _Blocked("BASE_UNKNOWN", "base commit is not recorded on the run or TaskSpec")
        try:
            out.base_commit = git.resolve_commit(base)
            if run.head_commit:
                out.head_commit = git.resolve_commit(run.head_commit)
            elif run.branch and valid_branch(run.branch):
                # the run's recorded task branch, not the worker-controlled checkout HEAD
                out.head_commit = git.branch_head(run.branch)
                out.not_proven.append(f"head commit was not recorded by the runtime; verifier used the tip of "
                                      f"branch {run.branch}")
            else:
                # the trusted git dir's HEAD is not the candidate's (a worktree shares the owner's repository): no
                # recorded head and no task branch is no identity at all
                raise _Blocked("HEAD_UNKNOWN", "the runtime recorded neither the head commit nor the task branch")
            out.tree_hash = git.tree_of(out.head_commit)
            if not git.is_ancestor(out.base_commit, out.head_commit):
                # base..head is then not "the worker's change": the diff/scope would also contain the
                # reversal of unrelated commits, so the change identity cannot be proven.
                msg = "head does not descend from the recorded base commit; change identity NOT-PROVEN"
                out.not_proven.append(msg)
                self.blocking_not_proven.append(msg)
        except GitError as exc:
            raise _Blocked("COMMIT_UNRESOLVED", str(exc)) from exc
        self.env_identity.update({"commit": out.head_commit, "base_commit": out.base_commit, "tree": out.tree_hash})

    def _diff_and_scope(self, git: GitReader, task: TaskSpec, project: Project) -> str:
        out = self.out
        try:
            changed = git.changed_files(out.base_commit or "", out.head_commit or "")
            stats = git.numstat(out.base_commit or "", out.head_commit or "")
            raw_diff = git.diff_bytes(out.base_commit or "", out.head_commit or "")
        except GitError as exc:
            raise _Blocked("DIFF_FAILED", str(exc)) from exc
        if len(raw_diff) > MAX_DIFF_BYTES:
            raise _Blocked("DIFF_TOO_LARGE", "diff exceeds the verifier limit; split the task")
        out.artifact_hash = content_hash({"tree": out.tree_hash, "diff_sha256": sha256_bytes(raw_diff)})
        self.env_identity["artifact_hash"] = out.artifact_hash
        self.changed_entries, self.numstat = changed, stats
        paths = [p for _, p in changed]
        out.changed_files = sorted(set(paths))
        protected = list(scope.GENERIC_PROTECTED_PATHS)
        if project.is_platform:
            protected += [p for p in scope.PLATFORM_PROTECTED_PATHS if p not in protected]
        result = scope.check_scope(paths, allowed=list(task.allowed_paths or []),
                                   forbidden=list(task.forbidden_paths or []), protected=protected)
        out.docs_only = result.docs_only
        self.categories = result.impact_categories
        out.high_impact_paths = result.high_impact
        out.security_sensitive_paths = result.security_sensitive
        out.quarantine_reason = result.quarantine_reason
        self.scope_result = result
        content = result.to_dict()
        content["name_status"] = [{"status": st, "path": p} for st, p in changed]
        content["numstat"] = stats
        self._check("scope", result.status, self._scope_summary(result))
        self.pending.append(_PendingEvidence("SCOPE_CHECK", f"Scope check of {len(paths)} changed file(s): {result.status}",
                                             content, "E1", "scope"))
        diff_text = raw_diff.decode("utf-8", errors="replace")
        stored = diff_text if len(diff_text) <= STORED_DIFF_CHARS else diff_text[:STORED_DIFF_CHARS] + "\n[TRUNCATED]\n"
        self.pending.append(_PendingEvidence("DIFF", f"git diff {out.base_commit[:12] if out.base_commit else ''}.."
                                                     f"{out.head_commit[:12] if out.head_commit else ''}",
                                             stored, "E1"))
        return diff_text

    @staticmethod
    def _scope_summary(result: scope.ScopeResult) -> str:
        if not result.violations:
            return f"{len(result.changed)} file(s) within TaskSpec scope"
        rules: dict[str, int] = {}
        for v in result.violations:
            rules[v.rule] = rules.get(v.rule, 0) + 1
        return ", ".join(f"{k}={n}" for k, n in sorted(rules.items()))

    def _config_hash(self, trusted_root: Path, task: TaskSpec, policy: dict[str, Any]) -> str:
        return compute_config_hash(suite_hash=self.out.suite_hash or "", trusted_root=trusted_root,
                                   task_spec_hash=task.spec_hash, policy=policy)

    @staticmethod
    def _make_workdir() -> Path:
        # Private run dir under the system temp dir, never under the data dir: candidate code must not be able
        # to derive the live DB path from the paths it is handed (runner.new_workdir). Short names on purpose:
        # Windows MAX_PATH (260) is reached quickly by nested pytest tmp dirs.
        try:
            return runner.new_workdir()
        except OSError as exc:
            raise _Blocked("WORKDIR_UNAVAILABLE",
                           f"could not allocate a verifier work directory ({type(exc).__name__})") from exc

    def _export_base(self, git: GitReader, workdir: Path) -> Path:
        """Materialise the recorded base commit (human-reviewed state) for collecting the expected ids."""
        base_dir = workdir / "bs"
        base_dir.mkdir()
        try:
            git.export_tree(self.out.base_commit or "", base_dir, max_bytes=MAX_SNAPSHOT_BYTES)
        except (SnapshotError, GitError) as exc:
            raise _Blocked("BASE_UNREADABLE", f"base commit could not be materialised for trusted collection: {exc}") \
                from exc
        return base_dir

    def _export(self, git: GitReader, snapshot: Path) -> bool:
        try:
            info = git.export_tree(self.out.head_commit or "", snapshot, max_bytes=MAX_SNAPSHOT_BYTES)
        except (SnapshotError, GitError) as exc:
            self._check("snapshot", "FAIL", str(exc))
            self.out.reasons.append(f"candidate head could not be materialised safely: {exc}")
            return False
        skipped = info.get("skipped")
        if isinstance(skipped, list) and skipped:
            self.out.not_proven.append(f"{len(skipped)} tree entr(y/ies) not materialised (symlink/submodule)")
        self._check("snapshot", "PASS", f"{info['files']} file(s) from head tree", details=info)
        return True

    # ------------------------------------------------------------------ checks

    def _check(self, name: str, status: str, summary: str = "", details: dict[str, Any] | None = None) -> None:
        self.out.checks[name] = CheckOutcome(name=name, status=status, summary=summary[:500], details=details or {})

    def _binary_paths(self) -> list[str]:
        """Added/modified files git reports as binary (numstat ``-``): invisible to the added-line scan."""
        return scope.binary_changed_paths(self.changed_entries, self.numstat)

    def _static_scans(self, diff_text: str, snapshot: Path | None) -> None:
        lines = scope.added_lines(diff_text)
        secrets = scope.scan_added_secrets(lines)
        tamper = scope.scan_harness_tamper(lines)
        binary = self._binary_paths()
        scanned_binary = 0
        for rel in binary:
            target = snapshot.joinpath(*rel.split("/")) if snapshot is not None else None
            if target is None or not target.is_file():
                # not materialised (snapshot failed, symlink, submodule): the content was not scanned
                msg = f"binary file {rel} could not be scanned for credentials"
                self.out.not_proven.append(msg)
                self.blocking_not_proven.append(msg)
                continue
            data = target.read_bytes()
            secrets += scope.scan_binary_secrets(rel, data)
            scanned_binary += 1
            if len(data) > scope.BINARY_SCAN_MAX_BYTES:
                msg = f"binary file {rel} is larger than the scan limit; only its first part was scanned"
                self.out.not_proven.append(msg)
                self.blocking_not_proven.append(msg)
        status = "FAIL" if secrets else "PASS"
        self._check("leak_scan", status, f"{len(secrets)} credential-shaped value(s) in added lines "
                                         f"and {scanned_binary} changed binary file(s)")
        self.pending.append(_PendingEvidence(
            "SECRET_SCAN", f"Secret scan of added lines and changed binary files: {status}",
            {"status": status, "findings": secrets, "binary_files_scanned": scanned_binary,
             "binary_files": binary[:200], "patterns": "aeos.core.redaction high-confidence set"},
            "E1", "leak_scan"))
        if secrets:
            self.out.reasons.append(f"credential-shaped value(s) added in the diff ({len(secrets)})")
        if tamper:
            self._check("harness_tamper", "FAIL", f"{len(tamper)} product-code line(s) reach into the test runner",
                        details={"findings": tamper[:50]})
            self.out.reasons.append("product code reaches into the test runner (harness tamper)")
        else:
            self._check("harness_tamper", "PASS", "no test-runner hooks in product code")

    def _check_shadowing(self, snapshot: Path, trusted_root: Path) -> bool:
        """Refuse candidates whose backend/ would replace the test runner or stdlib modules."""
        shadowed = shadowed_modules(snapshot / "backend", trusted_root / "backend")
        if shadowed:
            names = ", ".join(shadowed[:20])
            self._check("harness_shadow", "FAIL", f"candidate backend/ shadows harness/stdlib module(s): {names}",
                        details={"modules": shadowed[:50]})
            self.out.reasons.append(f"HARNESS_TAMPER: candidate backend/ shadows {names}; trusted suite not run")
            return False
        self._check("harness_shadow", "PASS", "no candidate module shadows the test harness or stdlib")
        return True

    def _preflight(self, snapshot: Path, workdir: Path, limits: VerifierLimits) -> None:
        backend = snapshot / "backend"
        names = sorted(
            p.name if p.is_dir() else p.stem
            for p in backend.iterdir()
            if (p.is_dir() and (p / "__init__.py").is_file()) or (p.is_file() and p.suffix == ".py")
        ) if backend.is_dir() else []
        if not names:
            raise _Blocked("CANDIDATE_HAS_NO_BACKEND", "candidate snapshot has no importable backend/ package")
        rn = self._runner()
        step_seal = seal.StepSeal.new(workdir, "preflight")
        res = rn.python(["-c", _PREFLIGHT, *names[:20]], pythonpath=snapshot / "backend", autoload=False,
                        timeout=limits.preflight_timeout, seal=step_seal, step="preflight")
        # importing the candidate's packages runs candidate code: only the mapping sealed at the driver's end counts
        sealed = self._driver_seal(step_seal, res)
        if sealed is None:
            # the driver never reached its sealed end (os._exit/crash/kill at import, or a forged stream): where
            # the candidate's packages resolve is unknown, so the trusted suite result cannot be attributed to the
            # candidate - never PASS on it (fail closed)
            msg = ("candidate import preflight produced no sealed result (exit "
                   f"{'timeout' if res.timed_out else res.returncode}); whether the candidate's packages resolve "
                   "inside its snapshot is NOT-PROVEN")
            self.out.not_proven.append(msg)
            self.blocking_not_proven.append(msg)
            return
        mapping = sealed
        snap = backend.resolve()
        mismatched = [n for n, f in mapping.items()
                      if isinstance(f, str) and not f.startswith("IMPORT_ERROR")
                      and not rn.to_host(f).resolve().is_relative_to(snap)]
        if mismatched:
            raise _Blocked("CANDIDATE_IMPORT_MISMATCH",
                           f"candidate packages resolve outside the snapshot: {', '.join(mismatched)}")

    def _cwd(self, workdir: Path) -> Path:
        cwd = workdir / "c"
        cwd.mkdir(exist_ok=True)
        return cwd

    def _env(self, pythonpath: Path, workdir: Path, *, autoload: bool) -> dict[str, str]:
        """Host environment for steps that never execute candidate code (ruff)."""
        return scrubbed_env(pythonpath=[pythonpath], home=workdir / "h", tmp=workdir / "t",
                            data_dir=workdir / "d", disable_plugin_autoload=not autoload)

    def _runner(self) -> runner.ProcessRunner | runner.ContainerRunner:
        if self.runner is None:  # pragma: no cover - selected in run() before any candidate step
            raise _Blocked("VERIFIER_RUNNER_MISSING", "no verifier runner selected")
        return self.runner

    def _basetemp(self, workdir: Path, name: str) -> list[str]:
        return ["--basetemp", self._runner().basetemp(name)]

    def _collect_expected(self, trusted_root: Path, trusted_copy: Path, ini: Path, workdir: Path,
                          limits: VerifierLimits) -> list[str]:
        args = ["-m", "pytest", "--collect-only", "-q", str(trusted_copy / "tests" / "acceptance"),
                "-c", str(ini), "--rootdir", str(trusted_copy), "--confcutdir", str(trusted_copy),
                "-p", "no:cacheprovider", "-m", MARK_EXPR]
        # same runner (interpreter + dependency set) as the suite run, so the collected ids are comparable;
        # only trusted code is imported here (controller checkout / reviewed base commit + trusted suite)
        res = self._runner().python(args, pythonpath=trusted_root / "backend", autoload=False,
                                    timeout=limits.collect_timeout, step="collect_expected")
        if res.timed_out or res.returncode not in (0, 5):
            raise _Blocked("TRUSTED_COLLECTION_FAILED",
                           "trusted suite could not be collected with the trusted controller code: " + res.tail(600))
        keys: dict[str, None] = {}
        for ln in res.stdout.splitlines():
            if "::" in ln and not ln.startswith((" ", "<", "=")) and ln.split("::", 1)[0].endswith(".py"):
                keys.setdefault(nodeid_to_key(ln.strip()), None)
        if not keys:
            raise _Blocked("TRUSTED_SUITE_EMPTY", "trusted acceptance suite collected no tests")
        return list(keys)

    def _run_trusted_suite(self, snapshot: Path, trusted_copy: Path, ini: Path, workdir: Path,
                           limits: VerifierLimits, expected: list[str]) -> None:
        out = self.out
        junit_path = workdir / "r" / "trusted-junit.xml"
        junit_path.parent.mkdir(parents=True, exist_ok=True)
        step_seal = seal.StepSeal.new(workdir, "trusted_suite")
        args = ["-m", "pytest", "-p", seal.PLUGIN_MODULE, str(trusted_copy / "tests" / "acceptance"), "-c", str(ini),
                "--rootdir", str(trusted_copy), "--confcutdir", str(trusted_copy), "-p", "no:cacheprovider",
                "-q", "--junitxml", str(junit_path), "-m", MARK_EXPR, *self._basetemp(workdir, "b1")]
        res = self._runner().python(args, pythonpath=snapshot / "backend", autoload=False,
                                    timeout=limits.suite_timeout, seal=step_seal, step="trusted_suite")
        report, parse_error = self._parse(junit_path)
        sealed = self._check_seal(step_seal, res, junit_path, report, label="trusted suite")
        if report is not None:  # per-test outcomes for the impact-selected trusted modules (adversarial/contract)
            self.trusted_cases = {c.key: c.outcome for c in report.cases}
        problems: list[str] = list(sealed.problems if sealed.status != "PASS" else [])
        not_proven: list[str] = []
        if res.timed_out:
            problems.append(f"trusted suite timed out after {limits.suite_timeout:.0f}s")
        if report is None:
            problems.append(f"trusted suite produced no readable report ({parse_error}; exit code {res.returncode})")
        else:
            reported = {c.key for c in report.cases}
            missing = [k for k in expected if k not in reported]
            out.failing_tests = report.failing_keys
            if report.failures or report.errors:
                problems.append(f"trusted suite: {report.failures} failed, {report.errors} error(s)")
            if missing:
                problems.append(f"{len(missing)} trusted test(s) missing from the candidate run report")
            if not res.timed_out and ((res.returncode == 0) != (report.failures + report.errors == 0)):
                problems.append(f"pytest exit code {res.returncode} inconsistent with the report")
            if not res.timed_out and res.returncode not in (0, 1):
                problems.append(f"pytest exited with code {res.returncode}")
            if report.passed == 0 and not problems:
                not_proven.append("trusted suite passed no tests")
            if report.skipped:
                not_proven.append(f"{report.skipped} trusted test(s) skipped — skipped is not a pass")
        status = "FAIL" if problems else ("NOT_PROVEN" if not_proven else "PASS")
        out.reasons.extend(problems)
        out.not_proven.extend(not_proven)
        content: dict[str, Any] = {
            "suite": "trusted-acceptance",
            "trust": "HIGH — copied from the trusted controller checkout, not from the candidate",
            "suite_hash": out.suite_hash,
            "status": status,
            "exit_code": res.returncode,
            "timed_out": res.timed_out,
            "duration_s": res.duration_s,
            "expected_tests": len(expected),
            "missing_tests": [k for k in expected if report is None or k not in {c.key for c in report.cases}][:200],
            "report": report.to_dict() if report else None,
            "parse_error": parse_error,
            "result_seal": sealed.to_dict(),
            "output_tail": res.tail(),
        }
        counts = report.to_dict(include_cases=False) if report else {}
        self._check("trusted_suite", status, "; ".join(problems + not_proven) or
                    f"{counts.get('passed', 0)} passed of {len(expected)} expected", details=counts)
        # §34 E4 "regression-protected": a fully passing trusted suite references the protected suite by hash
        # and the passing test ids. The collector assigns E4 only while that hash is the *current* protected
        # suite (otherwise it caps the claim at E2), so a stale or foreign suite can never reach E4.
        level, env_extra = "E2", {}
        if status == "PASS" and report is not None and out.suite_hash:
            passing = sorted(c.key for c in report.cases if c.outcome == "passed")
            if passing:
                level = "E4"
                env_extra = {"protected_suite": {"suite_hash": out.suite_hash,
                                                 "test_ids": passing[:MAX_PROTECTED_IDS],
                                                 "truncated": len(passing) > MAX_PROTECTED_IDS}}
        self.pending.append(_PendingEvidence(
            "TEST_REPORT",
            f"Trusted acceptance suite on {(out.head_commit or '')[:12]}: {status} "
            f"({counts.get('passed', 0)} passed, {counts.get('failures', 0)} failed, {counts.get('errors', 0)} errors, "
            f"{counts.get('skipped', 0)} skipped)",
            content, level, "trusted_suite", env_extra))

    def _check_seal(self, step_seal: seal.StepSeal, res: ProcResult, junit_path: Path,
                    report: JUnitReport | None, *, label: str) -> seal.SealResult:
        """Cross-check a sealed pytest step (``aeos.verification.seal``). A disagreement is result tampering (a
        ``result`` tamper finding: QUARANTINED); no sealed session end means the step cannot pass (fail closed)."""
        consumed = step_seal.discard()
        result = seal.check_pytest(step_seal, stdout=res.stdout, returncode=res.returncode, timed_out=res.timed_out,
                                   junit_path=junit_path, report=report, key_consumed=consumed,
                                   channel=res.channel)
        if result.status == "MISSING" and res.timed_out:
            result.problems = []  # the timeout is the finding; a killed session never ends sealed
        if result.status == "FAIL":
            self.tamper_findings += [{"kind": "result", "path": step_seal.name, "change": p[:300]}
                                     for p in result.problems[:5]]
        result.problems = [f"result integrity ({label}): {p}" for p in result.problems]
        self.result_seals[step_seal.name] = result.to_dict()
        return result

    def _driver_seal(self, step_seal: seal.StepSeal, res: ProcResult, *,
                     out_dir: Path | None = None) -> dict[str, Any] | None:
        """The result a non-pytest driver step (import preflight, migration round-trip, OpenAPI dump, UI state
        driver) sealed at its very end (``seal.sealed_result``), recorded under the step's name. Forged or
        out-of-sequence channel lines naming this step are result tampering (a ``result`` finding: QUARANTINED)
        whether or not a sealed result exists (another session's seal lines are ignored), and so is an output the
        driver sealed by digest under *out_dir* that changed after it (``seal.output_problems``); no sealed result at
        all, or a key file the plugin did not remove, is MISSING and nothing is returned (the caller fails closed)."""
        checked = seal.check_driver(step_seal, res.channel or res.stdout, out_dir=out_dir)
        if checked.forged:
            self.tamper_findings += [{"kind": "result", "path": step_seal.name, "change": p[:300]}
                                     for p in checked.forged[:5]]
        self.result_seals[step_seal.name] = checked.record()
        return checked.payload

    def _cached_baseline(self) -> dict[str, Any] | None:
        out = self.out
        rows = self.session.scalars(select(EvidenceArtifact).where(
            EvidenceArtifact.project_id == self.project.id, EvidenceArtifact.kind == "TEST_REPORT",
            EvidenceArtifact.origin == ORIGIN, EvidenceArtifact.trusted.is_(True))
            .order_by(EvidenceArtifact.created_at.desc()).limit(200))
        for ev in rows:
            env = ev.environment or {}
            if env.get("baseline_commit") == out.base_commit and env.get("suite_hash") == out.suite_hash:
                data = collector.read_json(ev)
                if data and isinstance(data.get("passing"), list):
                    return {**data, "evidence_id": ev.id}
        return None

    def _run_baseline(self, git: GitReader, workdir: Path, trusted_copy: Path, ini: Path, limits: VerifierLimits,
                      base_code: Path | None) -> None:
        """§34 "baseline set before the change": the trusted suite measured on the recorded base commit
        (cached per base commit + suite hash). Head failures that passed on the base are regressions; failures
        already present on the base are reported as pre-existing — the head still has to pass (no waiver)."""
        out = self.out
        cached = self._cached_baseline()
        if cached is None:
            base_dir = base_code or self._export_base(git, workdir)
            junit_path = workdir / "r" / "baseline-junit.xml"
            junit_path.parent.mkdir(parents=True, exist_ok=True)
            args = ["-m", "pytest", str(trusted_copy / "tests" / "acceptance"), "-c", str(ini),
                    "--rootdir", str(trusted_copy), "--confcutdir", str(trusted_copy), "-p", "no:cacheprovider",
                    "-q", "--junitxml", str(junit_path), "-m", MARK_EXPR, *self._basetemp(workdir, "b3")]
            res = self._runner().python(args, pythonpath=base_dir / "backend", autoload=False,
                                        timeout=limits.suite_timeout, step="baseline")
            report, parse_error = self._parse(junit_path)
            if report is None:
                msg = f"baseline run on {(out.base_commit or '')[:12]} produced no report ({parse_error})"
                self._check("baseline", "UNKNOWN", msg)
                out.not_proven.append(msg)
                return
            cached = {"suite": "trusted-acceptance-baseline", "base_commit": out.base_commit,
                      "suite_hash": out.suite_hash, "status": "PASS" if not report.failing_keys else "FAIL",
                      "passing": sorted(c.key for c in report.cases if c.outcome == "passed"),
                      "failing": sorted(report.failing_keys), "skipped": sorted(report.skipped_keys),
                      "exit_code": res.returncode, "duration_s": res.duration_s}
            self.pending.append(_PendingEvidence(
                "TEST_REPORT", f"Baseline: trusted acceptance suite on base {(out.base_commit or '')[:12]}: "
                               f"{cached['status']} ({len(cached['passing'])} passed, {len(cached['failing'])} failed)",
                cached, "E2", None, {"baseline_commit": out.base_commit}))
        base_pass, base_fail = set(cached.get("passing") or []), set(cached.get("failing") or [])
        regressions = sorted(t for t in out.failing_tests if t in base_pass)
        preexisting = sorted(t for t in out.failing_tests if t in base_fail)
        self.regressions, self.preexisting = regressions, preexisting
        summary = (f"{len(regressions)} regression(s) vs base {(out.base_commit or '')[:12]}"
                   + (f"; {len(preexisting)} failure(s) already present on the base" if preexisting else ""))
        self._check("baseline", "FAIL" if regressions else "PASS", summary,
                    details={"regressions": regressions[:50], "preexisting_failures": preexisting[:50],
                             "cached_evidence_id": cached.get("evidence_id")})
        if regressions:
            out.reasons.append(f"critical regression: {len(regressions)} trusted test(s) passed on the base and "
                               f"fail on the head ({', '.join(regressions[:5])})")

    def _check_suite_integrity(self, copied: list[tuple[str, Path]], trusted_root: Path) -> None:
        try:
            intact = (suite.hash_files(copied) == self.out.suite_hash
                      and suite.trusted_suite_hash(trusted_root) == self.out.suite_hash)
        except (AeosError, OSError):
            intact = False
        if intact:
            self._check("trusted_suite_integrity", "PASS", "trusted suite unchanged before/after the run")
        else:
            self._check("trusted_suite_integrity", "FAIL",
                        "trusted suite files changed while candidate code was running (tamper)")
            self.out.reasons.append("trusted suite files changed during verification (tamper detected)")

    # ------------------------------------------------------------------ tamper window (§09 Zone 1, §21)

    def _guard_open(self) -> None:
        """Fingerprint what candidate code must not change before the first candidate step: the running
        controller checkout + interpreter site-packages and the DB trust tables (approval records, policy,
        routing, agents, budgets, users, TaskSpecs, acceptance/evidence)."""
        from aeos.worker_runtime import facade as worker

        guard: dict[str, Any] = {"controller": None, "controller_error": None, "window": None}
        try:
            guard["controller"] = worker.controller_integrity_snapshot()
        except Exception as exc:  # unverifiable: treated as modified on close (fail closed)
            guard["controller_error"] = type(exc).__name__
        from sqlalchemy.engine import Connection

        bind = self.session.get_bind()
        engine = bind.engine if isinstance(bind, Connection) else bind
        guard["window"] = tamper.Window(engine)
        self._guard = guard

    def _guard_close(self) -> None:
        from aeos.worker_runtime import facade as worker

        guard, self._guard = self._guard, None
        if guard is None:
            return
        # Kill groups of the process runner (aeos.verification.procgroup): a process that outlived its step was
        # killed when the step ended, before this window closes - but its existence is tampering (it could have
        # written after the window, review W6-054/W6-057): QUARANTINED, never PASS.
        all_survivors = self.runner.survivors() if self.runner is not None else []
        # only a step that executes candidate code can be tampered with by it; a survivor of a trusted-only step
        # (expected-id collection, base-commit baseline) is a harness defect - stated, never blamed on the candidate
        survivors = [s for s in all_survivors if s["step"] not in TRUSTED_ONLY_STEPS]
        trusted_survivors = [s for s in all_survivors if s["step"] in TRUSTED_ONLY_STEPS]
        gaps = self.runner.containment_gaps() if self.runner is not None else []
        n_surv = sum(len(s["survivors"]) for s in survivors)
        if gaps and not survivors:
            # a weaker kill group (environment-marker sweep only) cannot prove that nothing outlived its step
            msg = ("verifier step kill group degraded (" + "; ".join(gaps[:3]) + "): a process that outlived its "
                   "step is NOT-PROVEN absent")
            self.out.not_proven.append(msg[:500])
        if trusted_survivors:
            self.out.not_proven.append(
                f"{sum(len(s['survivors']) for s in trusted_survivors)} process(es) outlived a trusted-only verifier "
                f"step ({', '.join(s['step'] or '?' for s in trusted_survivors)}) and were killed - not candidate "
                "code; check the trusted suite for leaked processes")
        self._check("step_containment", "FAIL" if survivors else ("NOT_PROVEN" if gaps or trusted_survivors
                                                                   else "PASS"),
                    f"{n_surv} process(es) outlived their verifier step and were killed "
                    f"({', '.join(s['step'] or '?' for s in survivors)})" if survivors else
                    ("kill group degraded: " + "; ".join(gaps[:3]) if gaps else
                     "a process outlived a trusted-only step (killed)" if trusted_survivors else
                     "no process outlived its verifier step"),
                    details={"survivors": survivors[:20], "trusted_step_survivors": trusted_survivors[:20],
                             "gaps": gaps[:20]})
        self.tamper_findings += [{"kind": "process", "path": s["step"] or "step",
                                  "change": f"{len(s['survivors'])} process(es) outlived the step: " + ", ".join(
                                      f"{p.get('name')}#{p.get('pid')}" for p in s["survivors"][:5])}
                                 for s in survivors]
        if self.result_seals:
            st = [r.get("status") for r in self.result_seals.values()]
            agg = "FAIL" if "FAIL" in st else ("NOT_PROVEN" if "MISSING" in st else "PASS")
            self._check("result_seal", agg, "; ".join(f"{k}: {v.get('status')}" for k, v in self.result_seals.items()),
                        details={"steps": self.result_seals, "plugin_sha256": seal.PLUGIN_SHA256})
        if guard["controller"] is None:
            changed = [{"path": "*", "change": f"integrity snapshot failed ({guard['controller_error']})"}]
        else:
            try:
                changed = worker.controller_integrity_compare(guard["controller"])
            except Exception as exc:
                changed = [{"path": "*", "change": f"integrity check failed ({type(exc).__name__})"}]
        db_result = guard["window"].close()
        self.foreign_all = list(getattr(guard["window"], "foreign_all", None) or [])
        checked = len((guard["controller"] or {}).get("files") or {})
        self._check("controller_integrity", "FAIL" if changed else "PASS",
                    f"{len(changed)} controller checkout/interpreter file(s) changed while candidate code ran"
                    if changed else f"{checked} controller checkout/interpreter file(s) unchanged",
                    details={"changed": changed[:50], "checked_files": checked})
        foreign = list(db_result.get("foreign") or [])
        db_status = db_result.get("status", "UNKNOWN")
        self._check("trust_tables", db_status,
                    f"{db_result.get('foreign_count', 0)} foreign write(s) to DB trust tables while candidate code ran"
                    if db_status == "FAIL" else ("DB trust tables unchanged by any other process "
                                                 f"({db_result.get('rows', 0)} row(s), "
                                                 f"{db_result.get('own_writes', 0)} own controller write(s))"
                                                 if db_status == "PASS" else
                                                 f"DB trust-table check UNKNOWN ({db_result.get('error')})"),
                    details={"foreign": foreign[:50], "own_tables": db_result.get("own_tables") or []})
        if db_status == "UNKNOWN":
            msg = "DB trust-table tamper check could not be evaluated; foreign writes NOT-PROVEN absent"
            self.out.not_proven.append(msg)
            self.blocking_not_proven.append(msg)
        self.tamper_findings += [{"kind": "controller", "path": c.get("path", "*"), "change": c.get("change", "")}
                                 for c in changed]
        self.tamper_findings += [{"kind": "db", "path": f"{f['table']}:{f['id']}", "change": f["change"]}
                                 for f in foreign]
        self.tamper_checks = {"controller": {"changed": changed[:100], "checked_files": checked},
                              "trust_tables": db_result, "step_containment": {"survivors": survivors[:20]},
                              "result_seal": dict(self.result_seals)}
        self.pending.append(_PendingEvidence(
            "CONTAINMENT_TEST",
            f"Tamper window around candidate code: controller {'MODIFIED' if changed else 'unchanged'}, "
            f"DB trust tables {db_status}, {n_surv} process(es) outlived their step",
            {"status": "FAIL" if self.tamper_findings else ("UNKNOWN" if db_status == "UNKNOWN" else "PASS"),
             "runner": (self.runner.describe() if self.runner else {}), **self.tamper_checks,
             "note": "writes are detected after the fact; reads are not (see residual_risks)"},
            "E2", "controller_integrity"))

    def _apply_tamper(self) -> None:
        """Candidate code changed the controller or the DB trust tables: never PASS/FAIL-and-repair - QUARANTINED
        for the owner's special review, with a HIGH alert (same rule as a worker run touching the controller)."""
        self.out.tamper_findings = list(self.tamper_findings)
        if not self.tamper_findings:
            return
        out = self.out
        where = ", ".join(sorted({f"{f['kind']}:{f['path']}" for f in self.tamper_findings})[:5])
        kinds = {f["kind"] for f in self.tamper_findings}
        what = [w for k, w in (("controller", "changed the controller"), ("db", "changed control-plane records"),
                               ("result", "forged or broke its test result"),
                               ("process", "left processes running after its step")) if k in kinds]
        msg = f"VERIFIER_TAMPER: candidate code {' / '.join(what) or 'tampered'} while the verifier ran it ({where})"
        out.reasons.insert(0, msg)
        out.status = "QUARANTINED"
        out.blocked_code = None
        out.quarantine_reason = msg[:500]

    def _neutralize(self) -> None:
        """§21 "workers may not change approval records / policy / routing policy": a foreign row found in the
        tamper window must not stay in effect while the owner reviews the quarantine (review W6-057/W6-056/W6-060).
        Approval decisions are INVALIDATED, open decision requests WITHDRAWN, acceptance records INVALIDATED,
        server-side sessions and capability grants revoked, routes DISABLED (ineligible until a human re-activates
        the configuration hash), foreign policy snapshots deactivated (the active one rebuilt from files +
        decisions), containment proofs removed and write enablement switched off. Everything else is listed for
        the owner's special review (``manual``). Whatever stays in effect - a changed controller file (policy/**,
        controller code, interpreter), a row without a safe inverse, a failed neutralisation - puts the control
        plane on an integrity hold: admission of new worker runs is paused until the owner reconciles and resumes
        it (review W6-056 "nothing pauses admission"). The caller commits."""
        db = (self.tamper_checks or {}).get("trust_tables") or {}
        # every foreign row (full keys), not only the ones the report lists
        foreign = [f for f in (self.foreign_all or db.get("foreign") or []) if isinstance(f, dict)]
        if foreign:
            try:
                # one savepoint around the whole neutralisation: an unexpected error (beyond the per-row handling)
                # leaves the session usable, so the QUARANTINE, the report and the alert are still recorded
                with self.session.begin_nested():
                    self.neutralized = neutralize.neutralize_foreign_rows(
                        self.session, foreign, run_id=self.run_id, project_id=self.out.project_id,
                        domain=self.out.domain, foreign_count=max(len(foreign), int(db.get("foreign_count") or 0)))
            except Exception as exc:  # never hides the findings: everything stays in effect -> integrity hold
                log.exception("tamper neutralisation failed")
                self.neutralized = {"actions": [], "manual": [], "not_listed": 0,
                                    "failed": [{"table": "*", "id": "*", "change": f"{len(foreign)} foreign row(s)",
                                                "error": type(exc).__name__}],
                                    "counts": {"actions": 0, "manual": 0, "failed": 1}}
            counts = (self.neutralized or {}).get("counts") or {}
            self.out.reasons.append(
                f"VERIFIER_TAMPER: {counts.get('actions', 0)} foreign control-plane row(s) neutralised, "
                f"{counts.get('manual', 0)} item(s) need the owner's review, {counts.get('failed', 0)} "
                "neutralisation(s) failed (report tamper.neutralized and the HIGH alert list each row and remedy)")
        self._integrity_hold()

    def _integrity_hold(self) -> None:
        from aeos.workforce import facade as workforce

        controller = [f for f in self.tamper_findings if f["kind"] == "controller"]
        manual = list((self.neutralized or {}).get("manual") or [])
        failed = list((self.neutralized or {}).get("failed") or [])
        if not (controller or manual or failed):
            return
        remedies = neutralize.controller_remedies([str(c.get("path") or "*") for c in controller])
        reason = (f"VERIFIER_TAMPER integrity hold (run {self.run_id}): {len(controller)} controller file(s) changed, "
                  f"{len(manual)} item(s) for manual review, {len(failed)} neutralisation(s) failed - reconcile, then "
                  "resume admission (aeos work resume)")
        res = call(workforce.set_scheduling_paused, self.session, True, actor=ORIGIN, reason=reason[:300])
        self.neutralized = {**(self.neutralized or {}), "integrity_hold": {
            "admission_paused": res.ok, "status": res.label(), "reason": reason[:300],
            "controller_changes": [{"path": str(c.get("path") or "*"), "change": str(c.get("change") or "")[:200]}
                                   for c in controller[:20]],
            "remedies": remedies,
            "resume": "after reconciling: aeos work resume --reason \"VERIFIER_TAMPER reconciled\" "
                      "(or POST /api/workforce/resume)"}}
        if not res.ok:
            # the hold could not be recorded (workforce module unavailable / refused): said, never assumed
            self.out.reasons.append(f"integrity hold not applied ({res.label()}): pause admission manually "
                                    "(aeos work pause) until the tamper findings are reconciled")

    @staticmethod
    def _parse(path: Path) -> tuple[JUnitReport | None, str | None]:
        try:
            return parse_junit(path), None
        except JUnitParseError as exc:
            return None, str(exc)

    @staticmethod
    def _suites(policy: dict[str, Any]) -> list[str]:
        """TaskSpec ``verifier_policy.suites`` (default unit + integration + acceptance). The trusted
        acceptance suite always runs whatever the list says; it selects the candidate test directories."""
        raw = policy.get("suites")
        if not isinstance(raw, list) or not raw:
            return ["unit", "integration", "acceptance"]
        return [str(s).strip().lower() for s in raw if str(s).strip()]

    def _run_candidate_tests(self, snapshot: Path, ini: Path, workdir: Path, limits: VerifierLimits,
                             suites: list[str] | None = None) -> None:
        suites = suites or ["unit", "integration", "acceptance"]
        wanted = [SUITE_DIRS[s] for s in suites if s in SUITE_DIRS]
        dirs =[snapshot / "tests" / d for d in wanted if (snapshot / "tests" / d).is_dir()]
        if not dirs:
            self._check("candidate_tests", "NOT_RUN",
                        f"candidate has no tests/{' or tests/'.join(wanted) or '<none selected>'} "
                        f"(TaskSpec suites: {', '.join(suites)})")
            return
        junit_path = workdir / "r" / "candidate-junit.xml"
        junit_path.parent.mkdir(parents=True, exist_ok=True)
        step_seal = seal.StepSeal.new(workdir, "candidate_tests")
        args = ["-m", "pytest", "-p", seal.PLUGIN_MODULE, *[str(d) for d in dirs], "-c", str(ini),
                "--rootdir", str(snapshot), "--confcutdir", str(snapshot), "-p", "no:cacheprovider", "-q",
                "--junitxml", str(junit_path), "-m", MARK_EXPR, *self._basetemp(workdir, "b2")]
        res = self._runner().python(args, pythonpath=snapshot / "backend", autoload=True,
                                    timeout=limits.unit_timeout, seal=step_seal, step="candidate_tests")
        report, parse_error = self._parse(junit_path)
        sealed = self._check_seal(step_seal, res, junit_path, report, label="candidate tests")
        if sealed.status != "PASS":
            status, summary = "FAIL", "; ".join(sealed.problems)[:500]
        elif res.timed_out:
            status, summary = "FAIL", f"candidate tests timed out after {limits.unit_timeout:.0f}s"
        elif res.returncode == 5:
            status, summary = "NOT_RUN", "candidate test dirs contain no selected tests"
        elif report is None:
            status, summary = "FAIL", f"candidate tests produced no report ({parse_error}; exit {res.returncode})"
        elif report.failures or report.errors or res.returncode != 0:
            status = "FAIL"
            summary = f"candidate tests: {report.failures} failed, {report.errors} error(s) (exit {res.returncode})"
        else:
            status, summary = "PASS", f"{report.passed} candidate test(s) passed (lower-trust signal)"
        if status == "FAIL":
            self.out.reasons.append(summary)
        self._check("candidate_tests", status, summary, details=report.to_dict(include_cases=False) if report else {})
        self.pending.append(_PendingEvidence(
            "TEST_REPORT", f"Candidate's own tests (worker-authored, lower trust): {status}",
            {"suite": "candidate-own-tests", "trust": "LOW — tests authored inside the candidate", "status": status,
             "exit_code": res.returncode, "timed_out": res.timed_out, "duration_s": res.duration_s,
             "report": report.to_dict() if report else None, "parse_error": parse_error,
             "result_seal": sealed.to_dict(), "output_tail": res.tail()},
            "E2", "candidate_tests"))

    def _run_ruff(self, snapshot: Path, trusted_root: Path, workdir: Path, limits: VerifierLimits) -> None:
        if importlib.util.find_spec("ruff") is None:
            self._check("ruff", "UNKNOWN", "ruff is not installed in the controller environment")
            self.out.not_proven.append("ruff not available; G1 lint not verified by the verifier")
            return
        targets = [d for d in ("backend", "tests") if (snapshot / d).is_dir()]
        if not targets:
            self._check("ruff", "NOT_RUN", "no backend/ or tests/ directory to lint")
            return
        cfg = trusted_root / "pyproject.toml"
        cfg_args = ["--config", str(cfg)] if cfg.is_file() else ["--isolated"]
        env = self._env(snapshot / "backend", workdir, autoload=False)
        res: ProcResult = run_process(
            [sys.executable, "-m", "ruff", "check", "--no-cache", "--output-format", "json", *cfg_args, *targets],
            cwd=snapshot, env=env, timeout=limits.ruff_timeout)
        diagnostics: list[dict[str, Any]] = []
        try:
            raw = json.loads(res.stdout or "[]")
            for d in raw[:200]:
                loc = d.get("location") or {}
                fname = str(d.get("filename") or "")
                try:
                    fname = Path(fname).resolve().relative_to(snapshot.resolve()).as_posix()
                except ValueError:
                    fname = Path(fname).name
                diagnostics.append({"code": d.get("code"), "file": fname, "row": loc.get("row"),
                                    "message": str(d.get("message", ""))[:300]})
            count = len(raw)
        except (ValueError, TypeError):
            count = -1
        if res.timed_out or res.returncode not in (0, 1) or count < 0:
            status, summary = "UNKNOWN", f"ruff did not complete (exit {res.returncode})"
            self.out.not_proven.append("ruff result unknown")
        elif res.returncode == 1 or count > 0:
            status, summary = "FAIL", f"ruff: {count} finding(s)"
            self.out.reasons.append(summary)
        else:
            status, summary = "PASS", "ruff: no findings (trusted configuration)"
        self._check("ruff", status, summary)
        self.pending.append(_PendingEvidence(
            "LINT_REPORT", f"ruff check (trusted config) on {(self.out.head_commit or '')[:12]}: {status}",
            {"status": status, "count": count, "diagnostics": diagnostics, "config": "trusted controller pyproject.toml"
             if cfg.is_file() else "ruff defaults (--isolated)", "exit_code": res.returncode}, "E1", "ruff"))

    # ------------------------------------------------------------------ impact-driven checks (§16, §22, §31)

    def _run_impact_steps(self, snapshot: Path, workdir: Path, limits: VerifierLimits,
                          policy: dict[str, Any] | None = None) -> None:
        """Steps the impact of the change adds to what the verifier itself executes (candidate code, so they run
        inside the tamper window and through the runner): the schema-migration round-trip, the change's negative
        tests, opt-in integration-marked tests, the trusted adversarial pack, the contract diff and contract tests,
        the migration/runtime credential split, the per-candidate dependency environment and the frontend step
        (``aeos.verification.impact`` maps each category to these checks)."""
        policy = policy or {}
        categories: dict[str, list[str]] = getattr(self, "categories", {}) or {}
        if categories.get("migration"):
            self._run_migration_roundtrip(snapshot, workdir, limits)
        ctx = self._step_context(snapshot, workdir, limits, policy)
        risky = sorted({p for c in ("api", "auth") for p in categories.get(c, [])})
        if policy.get("negative_tests_required") and risky:
            self._step("negative_tests", lambda: controlled.run_negative_tests(ctx, risky))
        self._step("integration_tests", lambda: controlled.run_integration_tests(ctx, self._suites(policy)))
        self._step("adversarial_pack", lambda: adversarial.run(ctx))
        self._step("contract_diff", lambda: contracts.run_contract_diff(ctx))
        self._step("contract_tests", lambda: contracts.run_contract_tests(ctx))
        self._step("db_credentials", lambda: db_evidence.run(ctx))
        if categories.get("dependency"):
            pins_changed = self._python_pins_changed(categories["dependency"])
            self._step("dependency_env", lambda: envs.run(ctx, pins_changed))
        if categories.get("frontend"):
            try:
                results = frontend.run(ctx)
            except (_Blocked, runner.RunnerError):
                raise
            except Exception as exc:  # a step bug never passes silently: stated, blocking
                results = [self._step_error("frontend_tests", exc)]
            for res in results:
                self._apply_step(res)

    def _step_context(self, snapshot: Path, workdir: Path, limits: VerifierLimits,
                      policy: dict[str, Any]) -> steps.StepContext:
        task = self.task
        return steps.StepContext(
            runner=self._runner(), snapshot=snapshot, workdir=workdir, diff_text=self.diff_text,
            changed=list(self.out.changed_files), categories=dict(getattr(self, "categories", {}) or {}),
            policy=dict(policy), timeout=limits.unit_timeout, suite_kind=self.suite_kind,
            is_platform=bool(self.project.is_platform), role=str(getattr(task, "role_key", "") or ""),
            ini=self.ini, trusted_copy=self.trusted_copy, expected=list(self.expected),
            trusted_cases=dict(self.trusted_cases), base_tree=lambda: self._base_tree(workdir),
            basetemp=lambda name: self._basetemp(workdir, name), session=self.session, project=self.project,
            # the impact-selected pytest steps run candidate code: sealed like the trusted suite (review W6-054); so
            # are the verifier's own drivers (OpenAPI dump, UI state driver) and the outputs they write
            seal_step=lambda name: seal.StepSeal.new(workdir, name), check_seal=self._check_seal,
            check_driver_seal=self._driver_seal)

    def _base_tree(self, workdir: Path) -> Path | None:
        """The recorded base commit materialised once (reused when the trusted collection already did it)."""
        if self._base_dir is not None:
            return self._base_dir
        if self._base_failed or self.git is None:
            return None  # a failed (possibly partial) export is never reused as the base
        existing = workdir / "bs"
        if existing.is_dir():
            self._base_dir = existing
            return existing
        try:
            self._base_dir = self._export_base(self.git, workdir)
        except _Blocked:
            self._base_failed = True
            return None
        return self._base_dir

    def _step(self, name: str, fn: Any) -> None:
        try:
            res = fn()
        except (_Blocked, runner.RunnerError):
            raise
        except Exception as exc:  # a step bug never passes silently: stated, blocking
            res = self._step_error(name, exc)
        self._apply_step(res)

    def _step_error(self, name: str, exc: Exception) -> steps.StepResult:
        log.exception("verifier step %s failed", name)
        msg = f"verifier step {name} failed internally ({type(exc).__name__}); its check is NOT-PROVEN"
        return steps.StepResult(name, "UNKNOWN", msg, not_proven=[msg], blocking=[msg])

    def _apply_step(self, res: steps.StepResult | None) -> None:
        if res is None:
            return
        self._check(res.check, res.status, res.summary, details=res.details)
        self.out.reasons.extend(res.reasons)
        self.out.not_proven.extend(res.not_proven)
        self.blocking_not_proven.extend(res.blocking)
        for ev in res.evidence:
            self.pending.append(_PendingEvidence(ev.kind, ev.claim, ev.content, ev.level, ev.check, dict(ev.env_extra)))

    @staticmethod
    def _find_alembic_dir(snapshot: Path) -> Path | None:
        """An Alembic migrations tree in the candidate snapshot: a directory with ``env.py`` and a ``versions``
        sub-directory holding revision modules. Prefers the platform location, else the first one found."""
        preferred = snapshot / "backend" / "aeos" / "migrations"
        if (preferred / "env.py").is_file() and any((preferred / "versions").glob("*.py")):
            return preferred
        for env in sorted(snapshot.rglob("env.py")):
            parent = env.parent
            if "__pycache__" in parent.parts:
                continue
            if (parent / "versions").is_dir() and any((parent / "versions").glob("*.py")):
                return parent
        return None

    def _run_migration_roundtrip(self, snapshot: Path, workdir: Path, limits: VerifierLimits) -> None:
        """§16/§31 "migration/rollback/restore tests": run the candidate's Alembic history forward, back to base
        and forward again on a throwaway SQLite DB, seed it, back it up, delete and restore it and compare the
        seeded rows, then roll one revision back and forward with the data present (``packs/migration_roundtrip``).
        Proven PASS/FAIL when the candidate has an Alembic migrations tree and alembic is available; otherwise
        NOT_PROVEN with the reason."""
        mig_dir = self._find_alembic_dir(snapshot)
        if mig_dir is None:
            self.migration_roundtrip = {"status": "NOT_PROVEN",
                                        "reason": "candidate has no Alembic migrations tree (env.py + versions/) "
                                                  "to round-trip"}
            return
        rn = self._runner()
        # the throwaway DB lives in the report dir: the only directory the container runner mounts writable
        # (its root filesystem and the rest of the run dir are read-only)
        reports = workdir / "r"
        reports.mkdir(parents=True, exist_ok=True)
        args = ["-c", steps.pack_text(MIGRATION_DRIVER), rn.to_container(str(mig_dir)),
                rn.to_container(str(reports / "mig.sqlite3"))]
        step_seal = seal.StepSeal.new(workdir, "migration_roundtrip")
        res = rn.python(args, pythonpath=snapshot / "backend", autoload=False, timeout=limits.unit_timeout,
                        seal=step_seal, step="migration_roundtrip")
        # only the result the driver sealed at its very end counts (candidate migration code can print anything)
        data: dict[str, Any] = dict(self._driver_seal(step_seal, res) or {})
        seal_problems = list(self.result_seals["migration_roundtrip"].get("problems") or [])
        ok = bool(data.get("ok")) and not res.timed_out and res.returncode == 0
        if res.timed_out:
            self.migration_roundtrip = {"status": "FAIL", "reason": "migration round-trip timed out"}
        elif not data:
            self.migration_roundtrip = {"status": "NOT_PROVEN",
                                        "reason": f"migration round-trip produced no sealed report (exit "
                                                  f"{res.returncode}): " + "; ".join(seal_problems[:2]),
                                        "output_tail": res.tail(400)}
        else:
            self.migration_roundtrip = {"status": "PASS" if ok else "FAIL", **data}
        self.pending.append(_PendingEvidence(
            "TEST_REPORT", f"Migration upgrade/downgrade/restore round-trip: {self.migration_roundtrip['status']}",
            {"suite": "migration-roundtrip", "trust": "controlled test run by the verifier", **self.migration_roundtrip,
             "exit_code": res.returncode, "duration_s": res.duration_s}, "E2", None))

    def _impact_checks(self, snapshot: Path | None, policy: dict[str, Any]) -> None:
        """Test scope follows impact, not file count: each impact category of the change adds what the
        verifier must prove — or states honestly that it did not prove it (never a silent pass)."""
        out = self.out
        categories: dict[str, list[str]] = getattr(self, "categories", {}) or {}
        suites = self._suites(policy)
        if "integration" in suites and "integration_tests" not in out.checks:
            out.not_proven.append("integration-marked tests (real tools) are not run by the verifier; only tests "
                                  f"selectable with '{MARK_EXPR}' ran (opt in with TaskSpec "
                                  "verifier_policy.integration_tests: run|required; the planner's TaskSpec contract "
                                  "v1 cannot carry this opt-in yet, so planner-compiled tasks never run them)")
        # Negative / permission tests for API and auth changes (TaskSpec verifier_policy.negative_tests_required):
        # executed in _run_impact_steps; when the candidate could not be run at all nothing was proven.
        risky = sorted({p for c in ("api", "auth") for p in categories.get(c, [])})
        if policy.get("negative_tests_required") and risky and "negative_tests" not in out.checks:
            msg = ("negative tests required for the changed API/auth surface (" + ", ".join(risky[:5]) +
                   "): the verifier could not execute the candidate's tests")
            self._check("negative_tests", "NOT_PROVEN", msg)
            out.not_proven.append(msg)
            self.blocking_not_proven.append(msg)
        # Schema/data migrations: the verifier runs an upgrade/downgrade/restore round-trip itself (§16/§31).
        if categories.get("migration"):
            rt = getattr(self, "migration_roundtrip", None)
            label = ", ".join(categories["migration"][:5])
            if rt and rt.get("status") == "PASS":
                seeded = rt.get("seeded_tables")
                self._check("migration_rollback", "PASS",
                            f"migration changed ({label}): upgrade->downgrade(base)->upgrade round-trip passed (head "
                            f"{str(rt.get('head') or '')[:16]}, {rt.get('tables', '?')} tables); {seeded} seeded "
                            f"table(s) backed up, restored and compared; rollback to "
                            f"{str(rt.get('data_rollback_target') or '?')[:24]} and forward with data passed",
                            details={k: rt.get(k) for k in ("head", "tables", "seeded_tables", "seed_refused_tables",
                                                             "restore_ok", "data_rollback_ok",
                                                             "data_rollback_target")})
                if not seeded:
                    out.not_proven.append(f"migration changed ({label}): no table accepted a generated seed row, so "
                                          "the restore of populated data is NOT-PROVEN")
            elif rt and rt.get("status") == "FAIL":
                leg = rt.get("leg")
                msg = (f"migration changed ({label}): upgrade/downgrade/restore round-trip FAILED"
                       + (f" in leg '{leg}'" if leg and leg != "done" else "") +
                       f": {rt.get('reason') or rt.get('error') or ''}")
                self._check("migration_rollback", "FAIL", msg)
                out.reasons.append(msg)
            else:
                reason = (rt or {}).get("reason") if rt else "the verifier did not run the round-trip"
                msg = f"migration changed ({label}): upgrade/downgrade/restore round-trip NOT-PROVEN ({reason})"
                # §31 G2 "migration/rollback ... per change": a migration change whose round-trip the verifier
                # could not run never passes on its own (fail closed), unless the (human-approved) TaskSpec
                # explicitly makes the round-trip optional for this task.
                optional = str(policy.get("migration_roundtrip") or "").strip().lower() == "optional"
                if optional:
                    msg += "; non-blocking: TaskSpec verifier_policy.migration_roundtrip=optional"
                self._check("migration_rollback", "NOT_PROVEN", msg)
                out.not_proven.append(msg)
                if not optional:
                    self.blocking_not_proven.append(msg)
        # Frontend: executed by the frontend step; a candidate that could not be materialised proves nothing.
        if categories.get("frontend") and "frontend_tests" not in out.checks:
            msg = ("frontend changed: the verifier could not run the frontend step (no candidate snapshot); "
                   "unit/browser/visual/a11y tests NOT-PROVEN, G3 needs the provider CI frontend job")
            self._check("frontend_tests", "NOT_PROVEN", msg)
            out.not_proven.append(msg)
        # Dependencies: diff evidence (§16 Supply-chain) and Zone 2 honesty (§09).
        if categories.get("dependency"):
            self._dependency_diff(categories["dependency"])
        # §31: which checks each impact category added, and whether they executed (report section impact_checks)
        self.impact_summary = impact_map.summarize(categories, out.checks)
        idle = sorted(c for c, e in self.impact_summary.items() if not e["executed"])
        if idle:
            out.not_proven.append(f"impact categories without an executed verifier check: {', '.join(idle)} "
                                  "(see impact_checks on the report)")

    def _python_pins_changed(self, paths: list[str]) -> bool:
        changes = scope.dependency_changes(self.diff_text, paths)
        return any(
            (c["pins_added"] or c["pins_removed"]) for p, c in changes.items()
            if any(p == f or p.endswith("/" + f) or p.rsplit("/", 1)[-1].startswith(f) for f in _PY_DEPENDENCY_FILES))

    def _dependency_diff(self, paths: list[str]) -> None:
        out = self.out
        changes = scope.dependency_changes(self.diff_text, paths)
        packages = scope.summarize_dependency_changes(changes)
        python_pins_changed = self._python_pins_changed(paths)
        status = "NOT_PROVEN"
        msg = (f"dependency manifests changed ({', '.join(paths[:5])}): {len(packages['added'])} added, "
               f"{len(packages['removed'])} removed, {len(packages['upgraded'])} upgraded; licences UNKNOWN "
               "(not verified)")
        self._check("dependency_diff", status, msg, details={"packages": packages})
        out.not_proven.append(msg)
        self.pending.append(_PendingEvidence(
            "DEPENDENCY_DIFF", f"Dependency diff of {len(paths)} manifest/lock file(s): {status}",
            {"status": status, "files": changes, "packages": packages, "licenses": "UNKNOWN",
             "license_note": "licence/SBOM/provenance of changed packages is not verified by the verifier",
             "controller_dependency_set_hash": self.env_identity.get("dependency_set_hash"),
             "auto_upgrade": False}, "E1", "dependency_diff"))
        env_check = out.checks.get("dependency_env")
        if python_pins_changed and (env_check is None or env_check.status not in ("PASS", "FAIL")):
            # §09 Zone 2: proven only by the trusted suite in the per-candidate environment (dependency_env PASS);
            # a FAIL there is already a failure reason.
            why = f" ({env_check.summary})" if env_check is not None else ""
            msg = ("candidate changes the Python dependency set; the trusted suite ran with the controller's "
                   f"installed dependencies ({self.env_identity.get('dependency_set_hash')}), so the change is "
                   f"not verified in an isolated dependency environment (§09 Zone 2){why}")
            out.not_proven.append(msg[:600])
            self.blocking_not_proven.append(msg[:600])

    # ------------------------------------------------------------------ decision & persistence

    def _decide(self) -> None:
        out = self.out
        scope_status = self.scope_result.status
        if scope_status == "FAIL":
            for v in self.scope_result.failing[:20]:
                out.reasons.append(f"{v.rule}: {v.path} ({v.detail})")
        trusted = out.checks.get("trusted_suite")
        if trusted is None and not out.reasons:
            out.reasons.append("trusted suite did not run")
        if out.reasons:
            out.status = "FAIL"
        elif scope_status == "QUARANTINED":
            out.status = "QUARANTINED"
        elif self.blocking_not_proven or (out.not_proven and (trusted is None or trusted.status != "PASS")):
            # blocking items (e.g. head not descending from base) keep the result from PASS on their own
            out.status = "NOT_PROVEN"
        elif trusted is not None and trusted.status == "PASS":
            out.status = "PASS"
        else:
            out.status = "NOT_PROVEN"

    def _persist(self, trusted_ran: bool) -> None:
        out, run, project, task = self.out, self.run_row, self.project, self.task
        classification = (task.data_classification if task and task.data_classification in
                          {c.value for c in DataClassification} else "INTERNAL")
        mode = run.mode if run.mode in {m.value for m in DataMode} else "LIVE"
        for pe in self.pending:
            ev = collector.collect_evidence(
                self.session, kind=pe.kind, origin=ORIGIN, collector_identity=self.identity, claim=pe.claim,
                content=pe.content, evidence_level=pe.level, subject_type="run", subject_id=run.id,
                project_id=project.id, domain=project.domain, run_id=run.id, task_id=run.task_id,
                environment={**self.env_identity, **pe.env_extra}, classification=classification, mode=mode,
            )
            out.evidence_ids.append(ev.id)
            if pe.check and pe.check in out.checks:
                out.checks[pe.check].evidence_id = ev.id
        checks = {k: v.status for k, v in out.checks.items()}
        trusted_counts = out.checks["trusted_suite"].details if "trusted_suite" in out.checks else {}
        passed_summary = "trusted acceptance suite passed; scope clean" + (
            f" ({len(out.not_proven)} NOT-PROVEN item(s) listed)" if out.not_proven else "")
        rn = self.runner
        report = {
            "status": out.status,
            "summary": (out.reasons[0] if out.reasons else passed_summary if out.status == "PASS" else
                        (out.not_proven[0] if out.not_proven else passed_summary))[:300],
            "verifier_isolation": rn.isolation if rn is not None else VERIFIER_ISOLATION,
            "verifier_isolation_note": rn.note if rn is not None else VERIFIER_ISOLATION_NOTE,
            "verifier_runner": rn.kind if rn is not None else "process",
            "runner": ({**rn.describe(), "remedies": list(rn.remedies)[:5],
                        "steps": list(getattr(rn, "steps", []))[:50]} if rn is not None else {}),
            "tamper": ({"findings": self.tamper_findings[:50], **self.tamper_checks,
                        "neutralized": self.neutralized} if self.tamper_checks else {}),
            "result_seal": dict(self.result_seals),
            "dependency_set_hash": self.env_identity.get("dependency_set_hash"),
            "impact_categories": sorted(getattr(self, "categories", {}) or {}),
            # §31 "test scope follows impact": per category, the checks it selected and which ones executed
            "impact_checks": self.impact_summary,
            "regressions": list(getattr(self, "regressions", []))[:50],
            "preexisting_failures": list(getattr(self, "preexisting", []))[:50],
            "blocked_code": out.blocked_code,
            "reasons": out.reasons[:30],
            "not_proven": out.not_proven[:30],
            "quarantine_reason": out.quarantine_reason,
            "trusted_suite_status": checks.get("trusted_suite", "NOT_RUN"),
            "candidate_tests_status": checks.get("candidate_tests", "NOT_RUN"),
            "ruff_status": checks.get("ruff", "NOT_RUN"),
            "scope_status": checks.get("scope", "NOT_RUN"),
            "leak_scan_status": checks.get("leak_scan", "NOT_RUN"),
            "tests_passed": trusted_counts.get("passed"),
            "tests_failed": (trusted_counts.get("failures") or 0) + (trusted_counts.get("errors") or 0)
            if trusted_counts else None,
            "tests_skipped": trusted_counts.get("skipped"),
            "docs_only": out.docs_only,
            "impact": "HIGH" if out.high_impact_paths else "NORMAL",
            "security_sensitive": bool(out.security_sensitive_paths),
            "changed_files_count": len(out.changed_files),
            "checks": {k: {"status": v.status, "summary": v.summary, "evidence_id": v.evidence_id}
                       for k, v in out.checks.items()},
            "failing_tests": out.failing_tests[:50],
            "evidence_refs": list(out.evidence_ids),
            "worker_claims_ignored": out.worker_claims[:20],
            "tested_artifact": "exact tree of the head commit, materialised via git plumbing (no hooks/filters)",
            "residual_risks": list(rn.residual_risks if rn is not None else RESIDUAL_RISKS),
        }
        level = "E2" if trusted_ran else "E1"
        ev = collector.collect_evidence(
            self.session, kind="VERIFICATION_REPORT", origin=ORIGIN, collector_identity=self.identity,
            claim=f"Independent verification of run {run.id} attempt {run.attempt}: {out.status}",
            content=report, evidence_level=level, subject_type="run", subject_id=run.id, project_id=project.id,
            domain=project.domain, run_id=run.id, task_id=run.task_id, environment=dict(self.env_identity),
            classification=classification, mode=mode,
        )
        out.report_evidence_id = ev.id

    def _side_effects(self) -> None:
        from aeos.workforce import facade as workforce

        out, run = self.out, self.run_row
        out.run_update = call(workforce.update_run, self.session, run.id, actor=ORIGIN,
                              verifier_status=out.status).label()
        self._record_compute()
        if not run.task_id:
            return
        if out.status == "QUARANTINED":
            # BLOCKED with the QUARANTINED code waits for the special review (owner action); the
            # scheduler never turns it into a new attempt on its own (integration amendment, ADR-0050).
            out.task_transition = call(
                workforce.transition_task, self.session, run.task_id, ExecutionState.BLOCKED.value,
                reason=f"QUARANTINED: {out.quarantine_reason}"[:1000], actor=ORIGIN,
                expected_from=ExecutionState.REVIEW.value, reason_code="QUARANTINED",
            ).label()
        elif out.status == "FAIL" and self.request_repair_on_fail:
            findings: list[dict[str, Any]] = [
                {"source": "verifier", "severity": "HIGH", "title": r[:300], "evidence_ref": out.report_evidence_id}
                for r in out.reasons[:20]
            ]
            findings += [{"source": "verifier", "severity": "HIGH", "title": f"failing trusted test {t}"}
                         for t in out.failing_tests[:20]]
            out.repair = call(workforce.request_repair, self.session, run.task_id, findings=findings,
                              review_record_id=None, actor=ORIGIN).label()

    def _record_compute(self) -> None:
        """§42 units kept separate: the verifier's wall-clock compute seconds (cash cost UNKNOWN)."""
        from aeos.cost import facade as cost

        run = self.run_row
        seconds = round(max(0.0, time.monotonic() - self.started), 3)
        mode = run.mode if run.mode in {m.value for m in DataMode} else "LIVE"
        rec = call(cost.record_unit_usage, self.session, provider="aeos-verifier", category="COMPUTE",
                   source="verifier", quantity=seconds, unit="seconds", project_id=self.out.project_id,
                   domain=self.out.domain, run_id=run.id, task_id=run.task_id, mode=mode)
        self.out.compute_usage = f"{seconds}s {rec.label()}"

    def _audit_and_emit(self) -> None:
        out, run = self.out, self.run_row
        if self.tamper_findings:
            # §09/§21: a HIGH alert, like a worker run that touched the running controller (worker_runtime.alert)
            audit.record(
                self.session, actor_type="system", actor_id=ORIGIN, action="verification.controller_modified",
                result="FAILED", target_type="worker_run", target_id=run.id, project_id=out.project_id,
                domain=out.domain, reason=(out.quarantine_reason or "")[:500],
                details={"findings": self.tamper_findings[:50], "runner": self.env_identity.get("verifier_runner")},
            )
            events.emit(
                self.session, source=ORIGIN, kind="verification.alert", entity_type="worker_run", entity_id=run.id,
                entity_version=out.head_commit, project_id=out.project_id, domain=out.domain, run_id=run.id,
                attempt=run.attempt, mode=run.mode if run.mode in {m.value for m in DataMode} else "LIVE",
                payload={"severity": "HIGH", "code": "VERIFIER_TAMPER", "findings": self.tamper_findings[:20],
                         "report_evidence_id": out.report_evidence_id,
                         "neutralized": (self.neutralized.get("actions") or [])[:20],
                         "manual_review": (self.neutralized.get("manual") or [])[:20],
                         "integrity_hold": self.neutralized.get("integrity_hold")},
            )
            # wave 7 (W6-005): no further worker is admitted until the owner reconciles (fail closed)
            from aeos.worker_runtime import facade as worker

            worker.raise_integrity_hold(self.session, source="verification", subject_id=run.id,
                                        findings=self.tamper_findings[:50], project_id=out.project_id,
                                        domain=out.domain)
        audit.record(
            self.session, actor_type=self.actor_type, actor_id=self.actor, action="verification.run",
            result="FAILED" if out.status == "BLOCKED" else "SUCCESS", target_type="worker_run", target_id=run.id,
            project_id=out.project_id, domain=out.domain, reason=out.reasons[0] if out.reasons else "",
            details={"status": out.status, "head": out.head_commit, "report": out.report_evidence_id,
                     "suite_hash": out.suite_hash, "blocked_code": out.blocked_code},
        )
        events.emit(
            self.session, source=ORIGIN, kind="verification.completed", entity_type="worker_run", entity_id=run.id,
            entity_version=out.head_commit, project_id=out.project_id, domain=out.domain, run_id=run.id,
            attempt=run.attempt, mode=run.mode if run.mode in {m.value for m in DataMode} else "LIVE",
            payload={"status": out.status, "report_evidence_id": out.report_evidence_id, "task_id": run.task_id},
        )


def _short_basetemp(workdir: Path, name: str) -> Path | None:
    """Windows only: a short pytest basetemp (stable per workdir) under the system temp dir."""
    return runner.short_basetemp(workdir, name)
