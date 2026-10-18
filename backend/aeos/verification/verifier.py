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

import importlib.util
import json
import logging
import shutil
import sys
import threading
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
from aeos.core.ids import new_id
from aeos.core.paths import UnsafePathError, ensure_within
from aeos.events import service as events
from aeos.models.evidence import EvidenceArtifact
from aeos.models.planning import TaskSpec
from aeos.models.portfolio import Project
from aeos.models.workforce import WorkerRun
from aeos.verification import collector, scope, suite
from aeos.verification.integration import call
from aeos.verification.junit import JUnitParseError, JUnitReport, nodeid_to_key, parse_junit
from aeos.verification.procs import GitError, GitReader, ProcResult, SnapshotError, run_process, scrubbed_env

log = logging.getLogger("aeos.verification.verifier")

VERIFIER_VERSION = "1"
ORIGIN = "verifier"
MARK_EXPR = "not integration and not network"
PASSING = "PASS"
STATUSES = ("PASS", "FAIL", "QUARANTINED", "NOT_PROVEN", "BLOCKED")
MAX_DIFF_BYTES = 20 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
STORED_DIFF_CHARS = 2_000_000

RESIDUAL_RISKS = (
    "Candidate code runs in a child process of the same OS user as the controller; a deliberately "
    "malicious candidate could still tamper with its own test process. Strong sandbox isolation for "
    "verification is NOT-PROVEN (depends on worker_runtime containment).",
    "The dead-proxy environment is an egress deterrent, not an enforced network boundary.",
)

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

_PREFLIGHT = (
    "import importlib, json, sys\n"
    "out = {}\n"
    "for n in sys.argv[1:]:\n"
    "    try:\n"
    "        m = importlib.import_module(n)\n"
    "        out[n] = getattr(m, '__file__', None)\n"
    "    except Exception as e:\n"
    "        out[n] = 'IMPORT_ERROR: ' + type(e).__name__\n"
    "print(json.dumps(out))\n"
)


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
                   ("candidate_tests", "ruff", "suite_timeout_seconds", "unit_timeout_seconds")},
    })


def current_config_hash(task: TaskSpec) -> str | None:
    """Config hash a verification of *task* would have now (None when the trusted suite is unreadable)."""
    try:
        root = suite.trusted_controller_root().resolve()
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
    verifier: str = field(default_factory=verifier_identity)

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
    content: dict[str, Any] | str | None
    level: str
    check: str | None = None


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
        "secret_scan_status": summary.get("secret_scan_status"),
        "tests_passed": summary.get("tests_passed"),
        "tests_failed": summary.get("tests_failed"),
        "tests_skipped": summary.get("tests_skipped"),
        "docs_only": summary.get("docs_only"),
        "impact": summary.get("impact", "UNKNOWN"),
        "security_sensitive": summary.get("security_sensitive"),
        "quarantine_reason": summary.get("quarantine_reason"),
        "blocked_code": summary.get("blocked_code"),
        "summary": summary.get("summary"),
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
        self.identity = verifier_identity()
        self.env_identity: dict[str, Any] = {"verifier": self.identity, "python": sys.version.split()[0],
                                             "platform": sys.platform, "verifier_version": VERIFIER_VERSION}

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
        out.worker_claims = self._worker_claims(run)
        workdir: Path | None = None
        trusted_ran = False
        try:
            candidate = self._candidate(run, project)
            if task is None:
                raise _Blocked("NO_TASKSPEC", "run has no TaskSpec; scope cannot be verified")
            if not project.is_platform:
                raise _Blocked("NO_TRUSTED_SUITE",
                               "no trusted acceptance suite is registered outside the candidate for this project")
            if importlib.util.find_spec("pytest") is None:
                raise _Blocked("PYTEST_UNAVAILABLE", "pytest is not installed in the controller environment")
            trusted_root = suite.trusted_controller_root().resolve()
            self._check_separation(candidate, trusted_root)
            try:
                suite_files = suite.trusted_suite_files(trusted_root)
            except AeosError as exc:
                raise _Blocked(exc.code, exc.message) from exc
            out.suite_hash = suite.hash_files(suite_files)
            self.env_identity["suite_hash"] = out.suite_hash
            policy = dict(task.verifier_policy or {})
            limits = VerifierLimits.from_policy(policy)
            git = self._git(candidate, limits)
            self._resolve_commits(git, run, task)
            diff = self._diff_and_scope(git, task, project)
            out.config_hash = self._config_hash(trusted_root, task, policy)
            self.env_identity["config_hash"] = out.config_hash
            workdir = self._make_workdir()
            snapshot = workdir / "s"
            snapshot.mkdir()
            snap_ok = self._export(git, snapshot)
            self._static_scans(diff)
            if snap_ok:
                trusted_copy = workdir / "tr"
                copied, copy_hash = suite.copy_trusted_suite(trusted_copy, trusted_root)
                if copy_hash != out.suite_hash:
                    raise _Blocked("TRUSTED_SUITE_COPY_MISMATCH", "trusted suite copy does not match its hash")
                ini = workdir / "pytest.ini"
                ini.write_text(PYTEST_INI, encoding="utf-8")
                self._preflight(snapshot, workdir, limits)
                expected = self._collect_expected(trusted_root, trusted_copy, ini, workdir, limits)
                trusted_ran = True
                self._run_trusted_suite(snapshot, trusted_copy, ini, workdir, limits, expected)
                self._check_suite_integrity(copied, trusted_root)
                if policy.get("candidate_tests", True):
                    self._run_candidate_tests(snapshot, ini, workdir, limits)
                else:
                    self._check("candidate_tests", "NOT_RUN", "disabled by TaskSpec verifier_policy")
                if policy.get("ruff", True):
                    self._run_ruff(snapshot, trusted_root, workdir, limits)
                else:
                    self._check("ruff", "NOT_RUN", "disabled by TaskSpec verifier_policy")
            self._decide()
        except _Blocked as b:
            out.status = "BLOCKED"
            out.blocked_code = b.code
            out.reasons.append(b.message)
        finally:
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)
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

    def _git(self, candidate: Path, limits: VerifierLimits) -> GitReader:
        try:
            git = GitReader(candidate, timeout=limits.git_timeout)
            if not git.is_repository():
                raise _Blocked("CANDIDATE_NOT_GIT", "candidate workspace is not a git repository")
        except GitError as exc:
            raise _Blocked("GIT_UNAVAILABLE", str(exc)) from exc
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
            else:
                out.head_commit = git.resolve_commit("HEAD")
                out.not_proven.append("head commit was not recorded by the runtime; verifier used candidate HEAD")
            out.tree_hash = git.tree_of(out.head_commit)
            if not git.is_ancestor(out.base_commit, out.head_commit):
                out.not_proven.append("head does not descend from the recorded base commit")
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
        paths = [p for _, p in changed]
        out.changed_files = sorted(set(paths))
        protected = list(scope.GENERIC_PROTECTED_PATHS)
        if project.is_platform:
            protected += [p for p in scope.PLATFORM_PROTECTED_PATHS if p not in protected]
        result = scope.check_scope(paths, allowed=list(task.allowed_paths or []),
                                   forbidden=list(task.forbidden_paths or []), protected=protected)
        out.docs_only = result.docs_only
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
        # Short names on purpose: Windows MAX_PATH (260) is reached quickly by nested pytest tmp dirs.
        base = get_settings().layout.root / "vw"
        base.mkdir(parents=True, exist_ok=True)
        for _ in range(5):
            wd = base / new_id().split("-")[0]
            try:
                wd.mkdir()
                return wd
            except FileExistsError:
                continue
        raise _Blocked("WORKDIR_UNAVAILABLE", "could not allocate a verifier work directory")

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

    def _static_scans(self, diff_text: str) -> None:
        lines = scope.added_lines(diff_text)
        secrets = scope.scan_added_secrets(lines)
        tamper = scope.scan_harness_tamper(lines)
        status = "FAIL" if secrets else "PASS"
        self._check("secret_scan", status, f"{len(secrets)} credential-shaped value(s) in added lines")
        self.pending.append(_PendingEvidence(
            "SECRET_SCAN", f"Secret scan of added lines: {status}",
            {"status": status, "findings": secrets, "patterns": "aeos.core.redaction high-confidence set"},
            "E1", "secret_scan"))
        if secrets:
            self.out.reasons.append(f"credential-shaped value(s) added in the diff ({len(secrets)})")
        if tamper:
            self._check("harness_tamper", "FAIL", f"{len(tamper)} product-code line(s) reach into the test runner",
                        details={"findings": tamper[:50]})
            self.out.reasons.append("product code reaches into the test runner (harness tamper)")
        else:
            self._check("harness_tamper", "PASS", "no test-runner hooks in product code")

    def _preflight(self, snapshot: Path, workdir: Path, limits: VerifierLimits) -> None:
        backend = snapshot / "backend"
        names = sorted(
            p.name if p.is_dir() else p.stem
            for p in backend.iterdir()
            if (p.is_dir() and (p / "__init__.py").is_file()) or (p.is_file() and p.suffix == ".py")
        ) if backend.is_dir() else []
        if not names:
            raise _Blocked("CANDIDATE_HAS_NO_BACKEND", "candidate snapshot has no importable backend/ package")
        env = self._env(snapshot / "backend", workdir, autoload=False)
        res = run_process([sys.executable, "-c", _PREFLIGHT, *names[:20]], cwd=self._cwd(workdir), env=env,
                          timeout=limits.preflight_timeout)
        try:
            mapping = json.loads((res.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            mapping = {}
        snap = backend.resolve()
        mismatched = [n for n, f in mapping.items()
                      if isinstance(f, str) and not f.startswith("IMPORT_ERROR")
                      and not Path(f).resolve().is_relative_to(snap)]
        if mismatched:
            raise _Blocked("CANDIDATE_IMPORT_MISMATCH",
                           f"candidate packages resolve outside the snapshot: {', '.join(mismatched)}")

    def _cwd(self, workdir: Path) -> Path:
        cwd = workdir / "c"
        cwd.mkdir(exist_ok=True)
        return cwd

    def _env(self, pythonpath: Path, workdir: Path, *, autoload: bool) -> dict[str, str]:
        return scrubbed_env(pythonpath=[pythonpath], home=workdir / "h", tmp=workdir / "t",
                            data_dir=workdir / "d", disable_plugin_autoload=not autoload)

    @staticmethod
    def _basetemp(workdir: Path, name: str) -> list[str]:
        return ["--basetemp", str(workdir / name)]

    def _collect_expected(self, trusted_root: Path, trusted_copy: Path, ini: Path, workdir: Path,
                          limits: VerifierLimits) -> list[str]:
        env = self._env(trusted_root / "backend", workdir, autoload=False)
        args = [sys.executable, "-m", "pytest", "--collect-only", "-q", str(trusted_copy / "tests" / "acceptance"),
                "-c", str(ini), "--rootdir", str(trusted_copy), "--confcutdir", str(trusted_copy),
                "-p", "no:cacheprovider", "-m", MARK_EXPR]
        res = run_process(args, cwd=self._cwd(workdir), env=env, timeout=limits.collect_timeout)
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
        env = self._env(snapshot / "backend", workdir, autoload=False)
        args = [sys.executable, "-m", "pytest", str(trusted_copy / "tests" / "acceptance"), "-c", str(ini),
                "--rootdir", str(trusted_copy), "--confcutdir", str(trusted_copy), "-p", "no:cacheprovider",
                "-q", "--junitxml", str(junit_path), "-m", MARK_EXPR, *self._basetemp(workdir, "b1")]
        res = run_process(args, cwd=self._cwd(workdir), env=env, timeout=limits.suite_timeout)
        report, parse_error = self._parse(junit_path)
        problems: list[str] = []
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
            "output_tail": res.tail(),
        }
        counts = report.to_dict(include_cases=False) if report else {}
        self._check("trusted_suite", status, "; ".join(problems + not_proven) or
                    f"{counts.get('passed', 0)} passed of {len(expected)} expected", details=counts)
        self.pending.append(_PendingEvidence(
            "TEST_REPORT",
            f"Trusted acceptance suite on {(out.head_commit or '')[:12]}: {status} "
            f"({counts.get('passed', 0)} passed, {counts.get('failures', 0)} failed, {counts.get('errors', 0)} errors, "
            f"{counts.get('skipped', 0)} skipped)",
            content, "E2", "trusted_suite"))

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

    @staticmethod
    def _parse(path: Path) -> tuple[JUnitReport | None, str | None]:
        try:
            return parse_junit(path), None
        except JUnitParseError as exc:
            return None, str(exc)

    def _run_candidate_tests(self, snapshot: Path, ini: Path, workdir: Path, limits: VerifierLimits) -> None:
        dirs = [snapshot / "tests" / d for d in ("unit", "integration") if (snapshot / "tests" / d).is_dir()]
        if not dirs:
            self._check("candidate_tests", "NOT_RUN", "candidate has no tests/unit or tests/integration")
            return
        junit_path = workdir / "r" / "candidate-junit.xml"
        junit_path.parent.mkdir(parents=True, exist_ok=True)
        env = self._env(snapshot / "backend", workdir, autoload=True)
        args = [sys.executable, "-m", "pytest", *[str(d) for d in dirs], "-c", str(ini), "--rootdir", str(snapshot),
                "--confcutdir", str(snapshot), "-p", "no:cacheprovider", "-q", "--junitxml", str(junit_path),
                "-m", MARK_EXPR, *self._basetemp(workdir, "b2")]
        res = run_process(args, cwd=self._cwd(workdir), env=env, timeout=limits.unit_timeout)
        report, parse_error = self._parse(junit_path)
        if res.timed_out:
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
             "report": report.to_dict() if report else None, "parse_error": parse_error, "output_tail": res.tail()},
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
        elif out.not_proven and (trusted is None or trusted.status != "PASS"):
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
                environment=dict(self.env_identity), classification=classification, mode=mode,
            )
            out.evidence_ids.append(ev.id)
            if pe.check and pe.check in out.checks:
                out.checks[pe.check].evidence_id = ev.id
        checks = {k: v.status for k, v in out.checks.items()}
        trusted_counts = out.checks["trusted_suite"].details if "trusted_suite" in out.checks else {}
        report = {
            "status": out.status,
            "summary": (out.reasons[0] if out.reasons else (out.not_proven[0] if out.not_proven else
                        "trusted acceptance suite passed; scope clean"))[:300],
            "blocked_code": out.blocked_code,
            "reasons": out.reasons[:30],
            "not_proven": out.not_proven[:30],
            "quarantine_reason": out.quarantine_reason,
            "trusted_suite_status": checks.get("trusted_suite", "NOT_RUN"),
            "candidate_tests_status": checks.get("candidate_tests", "NOT_RUN"),
            "ruff_status": checks.get("ruff", "NOT_RUN"),
            "scope_status": checks.get("scope", "NOT_RUN"),
            "secret_scan_status": checks.get("secret_scan", "NOT_RUN"),
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
            "residual_risks": list(RESIDUAL_RISKS),
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
        if not run.task_id:
            return
        if out.status == "QUARANTINED":
            out.task_transition = call(
                workforce.transition_task, self.session, run.task_id, ExecutionState.BLOCKED.value,
                reason=f"QUARANTINED: {out.quarantine_reason}"[:1000], actor=ORIGIN,
                expected_from=ExecutionState.REVIEW.value,
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

    def _audit_and_emit(self) -> None:
        out, run = self.out, self.run_row
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
