"""Task DAG compiler and projection (§13, §14, §21, §35 "Task DAG").

* ``validate_graph`` rejects unknown nodes, self-dependencies and cycles (tasks and features), and
  task paths that could leave the workspace (``..``, drive letters, UNC, ``~``).
* ``compile_version`` turns an approved PlanVersion into Milestone/Feature/TaskSpec/TaskDependency
  rows with a default for every §21 field. It is idempotent per version (TaskSpec is append-only).
  For the platform project (§09) the AGENTS.md protected paths are forbidden by default; a task that
  explicitly targets them is flagged for quarantine/special review and raised to risk class R3.
* ``dag_view`` is a read-only projection joining TaskSpec with runtime TaskExecution, leases and
  acceptance records. It never mutates runtime state and never infers acceptance from status.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeos.audit import service as audit
from aeos.core import protected_paths as _pp
from aeos.core.enums import Domain
from aeos.core.errors import ValidationFailed
from aeos.core.hashing import content_hash
from aeos.core.paths import repo_root
from aeos.events import service as events
from aeos.models.planning import Feature, Milestone, Plan, PlanVersion, TaskDependency, TaskSpec
from aeos.models.portfolio import Project
from aeos.models.workforce import Lease, TaskExecution

# One shared definition with the worker runtime and the verifier (aeos.core.protected_paths).
FALLBACK_PROTECTED_PATHS: tuple[str, ...] = _pp.PLATFORM_PROTECTED_PATHS
# Trusted grader/pipeline/policy/agent-config paths that no worker of ANY managed project may change without
# quarantine review (§21 "grader/held-out tests ... pipeline tepercaya", §25). Anchored paths only: the
# unanchored restricted areas (IaC/IAM/secrets/deploy) are matched by runtime and verifier against the
# concrete changed paths. The platform project adds the full AGENTS.md protected list (§09).
GENERIC_TRUSTED_PATHS: tuple[str, ...] = _pp.GENERIC_TRUSTED_PATHS
DEFAULT_LIMITS = {"max_attempts": 2, "timeout_seconds": 3600, "max_output_tokens": 32000}
MAX_ATTEMPTS_CAP = 2  # §21 pilot cap: at most two implementation attempts per TaskSpec
# §21 resource caps of the pilot: one active worker per task, no automatic nested fan-out, and the per-run compute
# (§19 "batas resource", §22 "mencadangkan compute") written into the TaskSpec so that the runtime enforces and the
# scheduler reserves the SAME values: max_memory_mb is the runtime's RSS/--memory cap (default of
# aeos.worker_runtime.processes.DEFAULT_MAX_MEMORY_MB), max_cpus the container --cpus cap and the CPU reservation.
# Fixed by the control plane (never taken from model output); raising them is a recorded human decision.
RUN_MEMORY_MB = 4096
RUN_CPUS = 1
# wave 7 (W6-055): the process-count cap is carried in the TaskSpec too, so both sandboxes enforce the SAME value
# (before, the runtime defaults differed: 64 in the process sandbox, 256 in the container). 256 is the container's
# proven --pids-limit (it counts processes AND threads there); in the process sandbox it caps live processes
# (Windows Job Object ActiveProcessLimit + the cross-platform watchdog).
RUN_MAX_PROCESSES = 256
RESOURCE_LIMITS = {"max_active_workers": 1, "nested_fan_out": False, "max_memory_mb": RUN_MEMORY_MB,
                   "max_cpus": RUN_CPUS, "max_processes": RUN_MAX_PROCESSES}
DEFAULT_TOOLS = ["fs.read", "fs.write:allowed_paths", "tests.run"]
DEFAULT_STOP_CONDITIONS = [
    "quota exhausted", "deadline/timeout reached", "lease lost", "policy changed", "scope violated (write outside "
    "allowed_paths)", "evidence invalid", "owner cancelled", "attempt limit reached",
    "scope grows or the contract cannot be met -> stop and file a blocker/decision request (no self-split)",
]
DEFAULT_EVIDENCE = [
    "independent verifier test report (trusted collector)", "diff against the recorded base commit",
    "acceptance criteria -> evidence mapping",
]
# §16 role-specific evidence the reviewer asks for (added to the TaskSpec, never removable by the planner):
# Integration Engineer "contract test provider; target/tenant dan idempotency wajib", UX/UI Designer "desain
# diverifikasi terhadap AC dan pengguna yang dituju" (states + accessibility), Backend/API Engineer "tes negatif",
# DB/Data Engineer "uji migration/rollback/restore; credential migration dipisah runtime". A line that names a
# verifier check in parentheses is decided by that check on the verified report (acceptance.evidence_requirements_
# status, wave 7); the others are reviewer-attested.
ROLE_EVIDENCE: dict[str, list[str]] = {
    "integration-engineer": [
        "provider contract test of the adapter in the trusted suite",
        "test asserting the explicit target/tenant binding and the idempotency key (no global credential)",
    ],
    "ux-ui-designer": [
        "loading/error/empty state renders and an accessibility probe of the built UI by the verifier (ui_a11y)",
    ],
    "backend-api-engineer": [
        "refusal/negative tests of the changed API surface executed by the verifier (negative_tests)",
    ],
    "db-data-engineer": [
        "migration upgrade/downgrade, seeded backup/restore and rollback with data run by the verifier "
        "(migration_rollback)",
        "migration credential reference separate from the runtime credential reference (db_credentials)",
    ],
}
CLASS_ORDER = ["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"]
RISK_ORDER = ["R0", "R1", "R2", "R3", "R4"]
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


# ---------------------------------------------------------------- protected paths / overlap


def protected_paths() -> tuple[str, ...]:
    """Protected paths from AGENTS.md (§09 self-build zones); falls back to the known list."""
    try:
        text = (repo_root() / "AGENTS.md").read_text(encoding="utf-8")
    except OSError:
        return FALLBACK_PROTECTED_PATHS
    found = _pp.parse_agents_md(text)
    # The canonical code list always applies; AGENTS.md can only add to it, never shrink it.
    return tuple(dict.fromkeys(FALLBACK_PROTECTED_PATHS + found))


def protected_for(project: Project | None) -> tuple[str, ...]:
    """Default forbidden paths for a project's TaskSpecs: the AGENTS.md list for the platform project,
    the generic trusted grader/pipeline paths for every other project."""
    if project is not None and project.is_platform:
        return tuple(dict.fromkeys(protected_paths() + GENERIC_TRUSTED_PATHS))
    return GENERIC_TRUSTED_PATHS


def _norm(p: str) -> str:
    p = str(p).strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def unsafe_path(p: Any) -> str | None:
    """Why a TaskSpec path glob may not be used (§50 "File path traversal/symlink keluar sandbox"), or None.

    Paths are repository-relative globs. Parent traversal, drive letters, UNC shares, home (``~``)
    expansion and control characters could address files outside the task workspace on Windows,
    macOS or Linux, so they are rejected instead of being normalised away (fail closed)."""
    if not isinstance(p, str) or not p.strip():
        return "empty path"
    raw = p.strip()
    if any(ord(c) < 32 for c in raw):
        return "control character in path"
    slashed = raw.replace("\\", "/")
    if slashed.startswith("//"):
        return "UNC/network path"
    if _DRIVE_RE.match(slashed):
        return "absolute drive path"
    if slashed.startswith("~"):
        return "home-relative path"
    if any(part == ".." for part in slashed.split("/")):
        return "parent traversal (..)"
    return None


def _static_prefix(p: str) -> str:
    out = []
    for part in _norm(p).split("/"):
        if not part or any(c in part for c in "*?[{"):
            break
        out.append(part)
    return "/".join(out)


def paths_overlap(a: str, b: str) -> bool:
    """Conservative overlap test for two path globs (shared-write detection, §19, §22).

    Case-insensitive on purpose: Windows and default macOS volumes are case-insensitive, so
    ``Policy/x.yaml`` writes the protected ``policy/**`` tree there (conservative = more overlap)."""
    pa, pb = _static_prefix(a).casefold(), _static_prefix(b).casefold()
    if not pa or not pb:
        return True  # a root-level glob overlaps everything
    return pa == pb or pa.startswith(pb + "/") or pb.startswith(pa + "/")


def _overlapping(paths_a: list[str], paths_b: list[str]) -> list[str]:
    return sorted({f"{a} ~ {b}" for a in paths_a for b in paths_b if paths_overlap(a, b)})


# ---------------------------------------------------------------- validation


def _find_cycle(nodes: list[str], edges: dict[str, list[str]]) -> list[str] | None:
    color = dict.fromkeys(nodes, 0)
    parent: dict[str, str | None] = {}
    for start in nodes:
        if color[start]:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        parent[start] = None
        color[start] = 1
        while stack:
            node, idx = stack[-1]
            deps = edges.get(node, [])
            if idx < len(deps):
                stack[-1] = (node, idx + 1)
                nxt = deps[idx]
                if nxt not in color:
                    continue
                if color[nxt] == 0:
                    color[nxt] = 1
                    parent[nxt] = node
                    stack.append((nxt, 0))
                elif color[nxt] == 1:
                    cycle = [nxt]
                    cur: str | None = node
                    while cur is not None and cur != nxt:
                        cycle.append(cur)
                        cur = parent.get(cur)
                    cycle.append(nxt)
                    return list(reversed(cycle))
            else:
                color[node] = 2
                stack.pop()
    return None


def _graph_errors(kind: str, items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[dict[str, Any]] = []
    keys: list[str] = []
    for item in items:
        key = item.get("key")
        if not isinstance(key, str) or not _KEY_RE.match(key):
            errors.append({"code": "INVALID_KEY", "kind": kind, "key": key})
            continue
        if key in keys:
            errors.append({"code": "DUPLICATE_KEY", "kind": kind, "key": key})
            continue
        keys.append(key)
    known = set(keys)
    edges: dict[str, list[str]] = {}
    for item in items:
        key = item.get("key")
        if key not in known:
            continue
        deps = item.get("depends_on") or []
        edges[key] = []
        for dep in deps:
            if dep == key:
                errors.append({"code": "SELF_DEPENDENCY", "kind": kind, "key": key})
            elif dep not in known:
                errors.append({"code": "UNKNOWN_DEPENDENCY", "kind": kind, "key": key, "depends_on": dep})
            elif dep not in edges[key]:
                edges[key].append(dep)
    cycle = _find_cycle(keys, edges)
    if cycle:
        errors.append({"code": "CYCLE", "kind": kind, "path": cycle})
    return errors, keys


def _dicts(v: Any) -> list[dict[str, Any]]:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def task_ancestors(tasks: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Transitive dependencies per task key (unknown keys ignored; cycles are reported elsewhere)."""
    deps = {str(t.get("key")): [str(d) for d in (t.get("depends_on") or [])] for t in tasks if t.get("key")}
    out: dict[str, set[str]] = {}
    for key in deps:
        seen: set[str] = set()
        stack = list(deps.get(key, []))
        while stack:
            n = stack.pop()
            if n in seen or n not in deps:
                continue
            seen.add(n)
            stack.extend(deps.get(n, []))
        out[key] = seen
    return out


DEFAULT_ROLE = "feature-implementer"


def _contract_errors(content: dict[str, Any], task_keys: list[str], f_keys: list[str]) -> list[dict[str, Any]]:
    """§19/§22 contract-first: every consumer of an interface contract depends (transitively) on the task that
    defines it, so parallel work against the interface starts only after the contract task is accepted."""
    errors: list[dict[str, Any]] = []
    known = set(task_keys)
    ancestors = task_ancestors(_dicts(content.get("tasks")))
    seen: set[str] = set()
    for c in _dicts(content.get("interface_contracts")):
        key = c.get("key")
        if not isinstance(key, str) or not _KEY_RE.match(key):
            errors.append({"code": "INVALID_KEY", "kind": "interface_contract", "key": key})
            continue
        if key in seen:
            errors.append({"code": "DUPLICATE_KEY", "kind": "interface_contract", "key": key})
            continue
        seen.add(key)
        provider = str(c.get("provider_task_key") or "")
        if provider not in known:
            errors.append({"code": "UNKNOWN_CONTRACT_TASK", "kind": "interface_contract", "key": key,
                           "task_key": provider or None, "field": "provider_task_key"})
            continue
        for fk in c.get("feature_keys") or []:
            if fk not in f_keys:
                errors.append({"code": "UNKNOWN_FEATURE", "kind": "interface_contract", "key": key, "feature_key": fk})
        for consumer in c.get("consumer_task_keys") or []:
            consumer = str(consumer)
            if consumer == provider:
                continue
            if consumer not in known:
                errors.append({"code": "UNKNOWN_CONTRACT_TASK", "kind": "interface_contract", "key": key,
                               "task_key": consumer, "field": "consumer_task_keys"})
            elif provider not in ancestors.get(consumer, set()):
                errors.append({"code": "CONTRACT_NOT_FIRST", "kind": "interface_contract", "key": key,
                               "task_key": consumer, "provider_task_key": provider,
                               "reason": "consumer must depend on the contract task (contract-first, §19/§22)"})
    return errors


def _governance_errors(content: dict[str, Any], f_keys: list[str], ms_keys: list[str]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for g in _dicts(content.get("feature_governance")):
        fk = g.get("feature_key")
        if fk not in f_keys:
            errors.append({"code": "UNKNOWN_FEATURE", "kind": "feature_governance", "feature_key": fk})
        elif fk in seen:
            errors.append({"code": "DUPLICATE_KEY", "kind": "feature_governance", "key": fk})
        seen.add(str(fk))
    for kind in ("rfcs", "tdds"):
        keys: set[str] = set()
        for d in _dicts(content.get(kind)):
            key = d.get("key")
            if not isinstance(key, str) or not _KEY_RE.match(key):
                errors.append({"code": "INVALID_KEY", "kind": kind, "key": key})
            elif key in keys:
                errors.append({"code": "DUPLICATE_KEY", "kind": kind, "key": key})
            keys.add(str(key))
            if d.get("feature_key") and d.get("feature_key") not in f_keys:
                errors.append({"code": "UNKNOWN_FEATURE", "kind": kind, "key": key, "feature_key": d.get("feature_key")})
    ms_items = []
    for m in _dicts(content.get("milestone_governance")):
        if m.get("milestone_key") not in ms_keys:
            errors.append({"code": "UNKNOWN_MILESTONE", "kind": "milestone_governance",
                           "milestone": m.get("milestone_key")})
            continue
        ms_items.append({"key": m.get("milestone_key"), "depends_on": list(m.get("depends_on") or [])})
    if ms_items:
        g_errors, _ = _graph_errors("milestone", ms_items)
        errors += g_errors
    return errors


def _specialist_errors(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """§18 "aktifkan hanya peran yang punya output konkret dan acceptance berbeda": a specialist task (any role
    other than the feature implementer) needs its own expected outputs and acceptance criteria."""
    errors: list[dict[str, Any]] = []
    signature: dict[tuple[tuple[str, ...], tuple[str, ...]], str] = {}
    for t in tasks:
        outs = tuple(sorted(str(x).strip().casefold() for x in t.get("expected_outputs") or [] if str(x).strip()))
        acs = tuple(sorted(str(x).strip().casefold() for x in t.get("acceptance_criteria") or [] if str(x).strip()))
        role = str(t.get("role_key") or DEFAULT_ROLE)
        key = t.get("key")
        if role != DEFAULT_ROLE:
            if not outs or not acs:
                errors.append({"code": "SPECIALIST_WITHOUT_DISTINCT_OUTPUT", "kind": "task", "key": key,
                               "role_key": role, "reason": "a specialist task needs concrete expected_outputs and "
                                                           "acceptance_criteria"})
                continue
            other = signature.get((outs, acs))
            if other is not None:
                errors.append({"code": "SPECIALIST_WITHOUT_DISTINCT_OUTPUT", "kind": "task", "key": key,
                               "role_key": role, "duplicate_of": other,
                               "reason": "same outputs and acceptance as another task"})
                continue
        if outs or acs:
            signature.setdefault((outs, acs), str(key))
    return errors


def validate_graph(content: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = content.get("tasks") or []
    features = content.get("features") or []
    milestones = content.get("milestones") or []
    errors: list[dict[str, Any]] = []
    ms_errors, ms_keys = _graph_errors("milestone", [{"key": m.get("key"), "depends_on": []} for m in milestones])
    errors += ms_errors
    f_errors, f_keys = _graph_errors("feature", features)
    errors += f_errors
    t_errors, t_keys = _graph_errors("task", tasks)
    errors += t_errors
    errors += _contract_errors(content, t_keys, f_keys)
    errors += _governance_errors(content, f_keys, ms_keys)
    errors += _specialist_errors(_dicts(tasks))
    for f in features:
        ms = f.get("milestone")
        if ms and ms not in ms_keys:
            errors.append({"code": "UNKNOWN_MILESTONE", "kind": "feature", "key": f.get("key"), "milestone": ms})
    for t in tasks:
        fk = t.get("feature_key")
        if fk and fk not in f_keys:
            errors.append({"code": "UNKNOWN_FEATURE", "kind": "task", "key": t.get("key"), "feature_key": fk})
        # Write scopes only: a forbidden path can never grant access, so it is not a reason to fail.
        for p in t.get("allowed_paths") or []:
            why = unsafe_path(p)
            if why:
                errors.append({"code": "UNSAFE_PATH", "kind": "task", "key": t.get("key"), "field": "allowed_paths",
                               "path": str(p)[:200], "reason": why})
    return errors


def require_valid_graph(content: dict[str, Any]) -> None:
    errors = validate_graph(content)
    if errors:
        raise ValidationFailed("plan DAG is invalid (unknown node, self-dependency, cycle, unsafe path, contract "
                               "not first or specialist without distinct output)",
                               code="DAG_INVALID", details={"errors": errors})


# ---------------------------------------------------------------- non-blocking findings (reviewer / owner)


def _executor_roles() -> frozenset[str] | None:
    try:
        from aeos.workforce import facade as workforce
    except ImportError:  # workforce absent: role check UNKNOWN (the scheduler still refuses unknown roles)
        return None
    return workforce.executor_role_keys()


def _side(paths: list[str]) -> str | None:
    tops = {_norm(p).split("/", 1)[0].casefold() for p in paths if _norm(p)}
    if tops and tops <= {"frontend", "web", "ui", "client"}:
        return "frontend"
    if tops and tops <= {"backend", "server", "api"}:
        return "backend"
    return None


def plan_warnings(content: dict[str, Any]) -> list[dict[str, Any]]:
    """Findings that do not invalidate the DAG but must be seen by the challenger/final reviewer and the owner:
    unknown roles (never admitted by the scheduler), FE/BE split without a contract (§22), cross-feature shared
    writes without a dependency (§17 principal review)."""
    tasks = _dicts(content.get("tasks"))
    out: list[dict[str, Any]] = []
    roles = _executor_roles()
    for t in tasks:
        role = str(t.get("role_key") or DEFAULT_ROLE)
        if roles is not None and role not in roles:
            out.append({"code": "ROLE_UNKNOWN", "severity": "HIGH", "kind": "task", "key": t.get("key"),
                        "role_key": role, "note": "role_key is not an executor RoleSkillPack; the scheduler will not "
                                                  "admit this task"})
    contracts_by_feature: dict[str, int] = {}
    contract_tasks: set[str] = set()
    for c in _dicts(content.get("interface_contracts")):
        for fk in c.get("feature_keys") or []:
            contracts_by_feature[str(fk)] = contracts_by_feature.get(str(fk), 0) + 1
        contract_tasks |= {str(c.get("provider_task_key") or "")} | {str(x) for x in c.get("consumer_task_keys") or []}
    ancestors = task_ancestors(tasks)
    by_feature: dict[str, list[dict[str, Any]]] = {}
    for t in tasks:
        by_feature.setdefault(str(t.get("feature_key") or ""), []).append(t)
    for fk, ts in by_feature.items():
        fe = [t for t in ts if _side(_str_list(t.get("allowed_paths"))) == "frontend"]
        be = [t for t in ts if _side(_str_list(t.get("allowed_paths"))) == "backend"]
        if not fe or not be or contracts_by_feature.get(fk):
            continue
        linked = any(str(b.get("key")) in ancestors.get(str(f.get("key")), set())
                     or str(f.get("key")) in ancestors.get(str(b.get("key")), set()) for f in fe for b in be)
        if not linked and not ({str(t.get("key")) for t in fe + be} & contract_tasks):
            out.append({"code": "FE_BE_SPLIT_WITHOUT_CONTRACT", "severity": "MEDIUM", "kind": "feature",
                        "feature_key": fk or None, "frontend_tasks": [t.get("key") for t in fe],
                        "backend_tasks": [t.get("key") for t in be],
                        "note": "frontend and backend of one feature are split without an interface contract or "
                                "dependency; build an end-to-end slice or add a contract task (§22)"})
    keys = [str(t.get("key")) for t in tasks if t.get("key")]
    by_key = {str(t.get("key")): t for t in tasks if t.get("key")}
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            ta, tb = by_key[a], by_key[b]
            if (ta.get("feature_key") or "") == (tb.get("feature_key") or ""):
                continue
            overlap = _overlapping(_str_list(ta.get("allowed_paths")), _str_list(tb.get("allowed_paths")))
            if overlap and a not in ancestors.get(b, set()) and b not in ancestors.get(a, set()):
                out.append({"code": "CROSS_FEATURE_CONFLICT", "severity": "MEDIUM", "kind": "task", "tasks": [a, b],
                            "features": [ta.get("feature_key"), tb.get("feature_key")], "paths": overlap[:5],
                            "note": "two features write the same area without a dependency; serialised by the "
                                    "scheduler, needs a principal/architecture review of the conflict (§17)"})
    return out


def quality_gaps(content: dict[str, Any]) -> list[dict[str, Any]]:
    """§46 quality targets: a feature without non-functional requirements is a blocking review question
    (NFR_MISSING), never a silent pass. The plan may still be approved by the owner (human AUTH)."""
    prds = {str(r.get("feature_key")): r for r in _dicts(content.get("requirements"))}
    gaps = []
    for f in _dicts(content.get("features")):
        prd = prds.get(str(f.get("key")))
        if prd is None or not [x for x in prd.get("non_functional_requirements") or [] if str(x).strip()]:
            gaps.append({"code": "NFR_MISSING", "feature_key": f.get("key"),
                         "question": "Target non-fungsional (keamanan, keandalan, performa, usability) untuk fitur "
                                     "ini belum dinyatakan sebagai requirement yang dapat diuji."})
    return gaps


# ---------------------------------------------------------------- TaskSpec defaults (§21)


def _clamp(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    if v <= 0:
        return default
    return max(lo, min(hi, v))


def _max_class(a: str, b: str) -> str:
    ia = CLASS_ORDER.index(a) if a in CLASS_ORDER else 1
    ib = CLASS_ORDER.index(b) if b in CLASS_ORDER else 1
    return CLASS_ORDER[max(ia, ib)]


def _str_list(v: Any) -> list[str]:
    return [str(x) for x in v if isinstance(x, (str, int, float))] if isinstance(v, list) else []


def role_revision(session: Session | None, role_key: str) -> str | None:
    """Revision hash of the executor RoleSkillPack the TaskSpec is compiled against (None = not a catalog
    executor role; the scheduler refuses such a task)."""
    try:
        from aeos.workforce import facade as workforce
    except ImportError:
        return None
    return workforce.role_revision(session, role_key)


def artifact_index(session: Session | None, version: PlanVersion) -> dict[str, dict[str, Any]]:
    """Resolvable input artifacts of a version (§21 "input artifacts"): the plan's PRD sections and the planning
    context attachments that were READ. Keys are lower-cased labels the planner may use."""
    idx: dict[str, dict[str, Any]] = {}
    content = version.content or {}
    for prd in _dicts(content.get("requirements")):
        fk = str(prd.get("feature_key") or "")
        if not fk:
            continue
        entry = {"ref": f"plan:requirements/{fk}", "kind": "plan_section", "sha256": content_hash(prd),
                 "name": f"PRD {fk}", "content": prd}
        for label in (f"prd {fk}", fk, str(prd.get("title") or "")):
            if label.strip():
                idx.setdefault(label.strip().casefold(), entry)
    if session is not None:
        from aeos.models.planning import PlanningContext

        plan = session.get(Plan, version.plan_id)
        ctx = session.get(PlanningContext, plan.context_id) if plan is not None and plan.context_id else None
        for a in (ctx.inputs if ctx is not None else []) or []:
            if a.get("status") != "READ":
                continue
            # the text itself stays in the PlanningContext; the runtime loads it by ref and checks the hash
            entry = {"ref": f"attachment:{a.get('id')}", "kind": "attachment", "sha256": a.get("sha256"),
                     "name": a.get("filename"), "trusted": False}
            for label in (a.get("filename"), a.get("id")):
                if label:
                    idx.setdefault(str(label).casefold(), entry)
        # §41 documentation checkpoint packages recorded by docs_engine (``doc_checkpoint:<revision id>`` = exactly that
        # package; ``documentation_checkpoint:<project>:<kind>:<ref>`` = its newest package): the Tech Writer task of
        # the next plan version reads exactly the recorded, hash-bound package
        try:
            from aeos.docs_engine import facade as docs
        except ImportError:  # docs engine absent: the label stays visibly unresolved
            docs = None  # type: ignore[assignment]
        if docs is not None:
            for label, entry in docs.checkpoint_artifacts(session, version.project_id):
                idx.setdefault(label.strip().casefold(), entry)
    return idx


def resolve_input_artifacts(labels: list[str], index: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Labels -> {ref, kind, sha256, label} bound by hash; unresolved labels stay visibly unresolved (never
    silently dropped or guessed)."""
    out = []
    for label in labels:
        hit = index.get(str(label).strip().casefold())
        if hit is None:
            out.append({"ref": None, "kind": "label", "label": str(label), "resolved": False})
        else:
            out.append({**hit, "label": str(label), "resolved": True})
    return out


def task_spec_fields(task: dict[str, Any], *, project: Project, version: PlanVersion,
                     protected: tuple[str, ...], role_revision_hash: str | None = None,
                     artifacts: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    # Second guard for the preview projection (compile itself requires a valid graph): an unsafe write
    # path never becomes an allowed path.
    allowed = [_norm(p) for p in _str_list(task.get("allowed_paths")) if _norm(p) and not unsafe_path(p)]
    forbidden = [_norm(p) for p in _str_list(task.get("forbidden_paths")) if _norm(p)]
    touches_protected = [p for p in protected if any(paths_overlap(p, a) for a in allowed)]
    for p in protected:
        if p not in touches_protected and p not in forbidden:
            forbidden.append(p)
    limits_in = task.get("limits") or {}
    limits = {
        "max_attempts": _clamp(limits_in.get("max_attempts"), 1, MAX_ATTEMPTS_CAP, DEFAULT_LIMITS["max_attempts"]),
        "timeout_seconds": _clamp(limits_in.get("timeout_seconds"), 60, 14400, DEFAULT_LIMITS["timeout_seconds"]),
        "max_output_tokens": _clamp(limits_in.get("max_output_tokens"), 1000, 128000,
                                    DEFAULT_LIMITS["max_output_tokens"]),
        **RESOURCE_LIMITS,
    }
    cap_in = task.get("capability_scope") or {}
    route_in = task.get("model_route_policy") or {}
    reviewer_in = task.get("reviewer_policy") or {}
    verifier_in = task.get("verifier_policy") or {}
    risk = task.get("risk_class") if task.get("risk_class") in RISK_ORDER else "R1"
    reviewer = {
        "required": True,  # an independent checkpoint review is always required (§19, §22)
        "reviewer_role": str(reviewer_in.get("reviewer_role") or "claude-checkpoint-reviewer"),
        "checkpoint": str(reviewer_in.get("checkpoint") or "after verifier evidence"),
        "self_review_forbidden": True,
    }
    if touches_protected:
        risk = RISK_ORDER[max(RISK_ORDER.index(risk), RISK_ORDER.index("R3"))]
        reviewer["quarantine_special_review"] = True
        reviewer["protected_paths_touched"] = touches_protected
    return {
        "project_id": project.id,
        "domain": project.domain,
        "plan_version_id": version.id,
        "plan_version_hash": version.manifest_hash,
        "task_key": str(task["key"]),
        "feature_key": str(task.get("feature_key") or "") or None,
        "title": str(task.get("title") or task["key"])[:256],
        "goal": str(task.get("goal") or ""),
        "role_key": str(task.get("role_key") or DEFAULT_ROLE)[:64],
        # §19 diagram "katalog role/skill -> task compiler": the role pack revision is part of the spec hash
        "role_revision_hash": role_revision_hash,
        "input_artifacts": (resolve_input_artifacts(_str_list(task.get("input_artifacts")), artifacts)
                            if artifacts is not None else _str_list(task.get("input_artifacts"))),
        "expected_outputs": _str_list(task.get("expected_outputs")),
        "acceptance_criteria": _str_list(task.get("acceptance_criteria")),
        "depends_on": list(dict.fromkeys(_str_list(task.get("depends_on")))),
        "base_commit": None,  # pinned by the scheduler at lease time from the known repository state
        "allowed_paths": allowed,
        "forbidden_paths": forbidden,
        "tools_allowed": _str_list(task.get("tools_allowed")) or list(DEFAULT_TOOLS),
        "egress_allowed": _str_list(task.get("egress_allowed")),  # default deny = empty
        "capability_scope": {
            "kind": "TASK_WORKSPACE", "branch_prefix": "aeos/", "operations": _str_list(cap_in.get("operations")),
            "paths": allowed, "notes": str(cap_in.get("notes") or ""),
        },
        "capability_ttl_seconds": _clamp(task.get("capability_ttl_seconds"), 300, 86400, 3600),
        "data_classification": _max_class(project.data_classification or "INTERNAL",
                                          str(task.get("data_classification") or "INTERNAL")),
        "model_route_policy": {
            "pool": route_in.get("pool") if route_in.get("pool") in ("WORKER", "LOCAL") else "WORKER",
            "requires_tools": route_in.get("requires_tools") is not False,
            "min_context_tokens": _clamp(route_in.get("min_context_tokens"), 0, 2_000_000, 0),
            "notes": str(route_in.get("notes") or ""),
            "eligible_routes_only": True, "paid_fallback": False, "supervisor_downgrade": False,
            "office_external_allowed": False if project.domain == Domain.OFFICE.value else None,
        },
        "reviewer_policy": reviewer,
        "verifier_policy": {
            "required": True, "independent": True, "suite_read_only": True,
            "suites": _str_list(verifier_in.get("suites")) or ["unit", "integration", "acceptance"],
            "negative_tests_required": verifier_in.get("negative_tests_required") is not False,
        },
        "limits": limits,
        "stop_conditions": list(dict.fromkeys(_str_list(task.get("stop_conditions")) + DEFAULT_STOP_CONDITIONS)),
        "evidence_requirements": list(dict.fromkeys(_str_list(task.get("evidence_requirements")) + DEFAULT_EVIDENCE
                                                    + ROLE_EVIDENCE.get(str(task.get("role_key") or DEFAULT_ROLE), []))),
        "risk_class": risk,
        "artifact_schema_version": str(task.get("artifact_schema_version") or "1")[:16],
        "mode": version.mode if version.mode in ("MOCK", "DEMO") else "LIVE",
    }


# ---------------------------------------------------------------- compile


def _owner(value: Any) -> str:
    """Owner text from the plan, ``UNKNOWN`` when not given (owners are never invented, §27)."""
    text = str(value or "").strip()
    return text[:128] if text else "UNKNOWN"


def compiled_tasks(session: Session, version_id: str) -> list[TaskSpec]:
    return list(session.scalars(select(TaskSpec).where(TaskSpec.plan_version_id == version_id)
                                .order_by(TaskSpec.created_at, TaskSpec.task_key)))


def compile_version(session: Session, version: PlanVersion, *, actor: str) -> dict[str, Any]:
    existing = compiled_tasks(session, version.id)
    if existing:
        return {"created": False, "plan_version_id": version.id, "tasks": len(existing)}
    content = version.content or {}
    require_valid_graph(content)
    project = session.get(Project, version.project_id)
    if project is None:
        raise ValidationFailed("project of the plan version no longer exists")
    protected = protected_for(project)
    ms_gov = {str(g.get("milestone_key")): g for g in _dicts(content.get("milestone_governance"))}
    f_gov = {str(g.get("feature_key")): g for g in _dicts(content.get("feature_governance"))}

    for idx, m in enumerate(content.get("milestones") or []):
        g = ms_gov.get(str(m["key"]), {})
        session.add(Milestone(plan_version_id=version.id, milestone_key=str(m["key"]), title=str(m.get("title") or m["key"])[:256],
                              exit_evidence=str(m.get("exit_evidence") or ""), order_index=int(m.get("order") or idx),
                              priority=str(g.get("priority") or "")[:16] or None,
                              acceptance=_str_list(g.get("acceptance")) or (
                                  [str(m.get("exit_evidence"))] if m.get("exit_evidence") else []),
                              depends_on=_str_list(g.get("depends_on")), owner=_owner(g.get("owner"))))
    for f in content.get("features") or []:
        session.add(Feature(plan_version_id=version.id, feature_key=str(f["key"]), title=str(f.get("title") or f["key"])[:256],
                            requirement=str(f.get("requirement") or ""), priority=str(f.get("priority") or "P2")[:16],
                            acceptance=_str_list(f.get("acceptance")), depends_on=_str_list(f.get("depends_on")),
                            milestone_key=str(f.get("milestone") or "") or None,
                            owner=_owner(f_gov.get(str(f["key"]), {}).get("owner"))))
    by_key: dict[str, TaskSpec] = {}
    artifacts = artifact_index(session, version)
    for t in content.get("tasks") or []:
        rev = role_revision(session, str(t.get("role_key") or DEFAULT_ROLE))
        fields = task_spec_fields(t, project=project, version=version, protected=protected, role_revision_hash=rev,
                                  artifacts=artifacts)
        spec = TaskSpec(**fields, spec_hash=content_hash(fields))
        session.add(spec)
        by_key[spec.task_key] = spec
    session.flush()
    for spec in by_key.values():
        for dep in spec.depends_on:
            session.add(TaskDependency(plan_version_id=version.id, task_id=spec.id, depends_on_task_id=by_key[dep].id))
    session.flush()
    audit.record(session, actor_type="user" if not actor.startswith("system") else "system", actor_id=actor,
                 action="plan.dag.compile", target_type="plan_version", target_id=version.id,
                 project_id=version.project_id, domain=version.domain,
                 details={"tasks": len(by_key), "manifest_hash": version.manifest_hash,
                          "protected_paths_default": list(protected), "platform_project": bool(project.is_platform)})
    events.emit(session, source="planning", kind="plan.dag.compiled", entity_type="plan_version", entity_id=version.id,
                entity_version=version.manifest_hash, project_id=version.project_id, domain=version.domain,
                payload={"plan_id": version.plan_id, "tasks": len(by_key)})
    return {"created": True, "plan_version_id": version.id, "tasks": len(by_key)}


# ---------------------------------------------------------------- projection


def _acceptance_for(session: Session, task_id: str, plan_version_hash: str) -> dict[str, Any]:
    """Acceptance from AcceptanceRecord only. Validity is checked against the *TaskSpec's* plan version
    hash (a record accepted for another plan identity is stale, §14); UNKNOWN validity stays None."""
    try:
        from aeos.verification import facade as verification

        rec = verification.latest_acceptance(session, task_id)
    except (ImportError, ModuleNotFoundError):
        return {"status": "UNKNOWN", "source": "verification module unavailable", "valid": None}
    if rec is None:
        return {"status": "NONE", "valid": False, "source": "acceptance_records"}
    try:
        valid = verification.acceptance_is_valid(session, task_id, plan_version_hash=plan_version_hash)
    except (ImportError, ModuleNotFoundError):
        valid = None
    return {"status": rec.acceptance_status, "valid": valid, "record_id": rec.id, "source": "acceptance_records"}


def dag_view(session: Session, plan: Plan, version: PlanVersion) -> dict[str, Any]:
    specs = compiled_tasks(session, version.id)
    persisted = bool(specs)
    if persisted:
        tasks = [{
            "key": s.task_key, "id": s.id, "title": s.title, "feature_key": s.feature_key, "goal": s.goal,
            "role_key": s.role_key, "risk_class": s.risk_class, "allowed_paths": s.allowed_paths or [],
            "forbidden_paths": s.forbidden_paths or [], "depends_on": s.depends_on or [], "spec_hash": s.spec_hash,
            "limits": s.limits or {}, "data_classification": s.data_classification,
            "quarantine": bool((s.reviewer_policy or {}).get("quarantine_special_review")),
            "acceptance_criteria": s.acceptance_criteria or [], "mode": s.mode or "LIVE",
            "expected_outputs": s.expected_outputs or [], "role_revision_hash": s.role_revision_hash,
        } for s in specs]
    else:
        project = session.get(Project, version.project_id)
        protected = protected_for(project)
        tasks = []
        for t in (version.content or {}).get("tasks") or []:
            if not isinstance(t, dict) or not t.get("key"):
                continue
            f = task_spec_fields(t, project=project, version=version, protected=protected) if project else {}
            tasks.append({
                "key": str(t["key"]), "id": None, "title": f.get("title", t.get("title")), "feature_key": f.get("feature_key"),
                "goal": f.get("goal", ""), "role_key": f.get("role_key"), "risk_class": f.get("risk_class"),
                "allowed_paths": f.get("allowed_paths", []), "forbidden_paths": f.get("forbidden_paths", []),
                "depends_on": f.get("depends_on", []), "spec_hash": None, "limits": f.get("limits", {}),
                "data_classification": f.get("data_classification"),
                "quarantine": bool((f.get("reviewer_policy") or {}).get("quarantine_special_review")),
                "acceptance_criteria": f.get("acceptance_criteria", []), "mode": f.get("mode", version.mode or "LIVE"),
                "expected_outputs": f.get("expected_outputs", []), "role_revision_hash": None,
            })
    keys = {t["key"] for t in tasks}
    by_key = {t["key"]: t for t in tasks}

    for t in tasks:
        runtime: dict[str, Any] = {"state": "NOT_SCHEDULED", "reason": "", "attempts_used": 0, "active_run_id": None}
        lease_info = None
        acceptance: dict[str, Any] = {"status": "NONE", "valid": False}
        if t["id"]:
            te = session.scalar(select(TaskExecution).where(TaskExecution.task_id == t["id"]))
            if te is not None:
                runtime = {"state": te.state, "reason": te.reason, "attempts_used": te.attempts_used,
                           "active_run_id": te.active_run_id, "updated_at": te.updated_at.isoformat()}
            lease = session.scalar(select(Lease).where(Lease.task_id == t["id"], Lease.state == "ACTIVE")
                                   .order_by(Lease.acquired_at.desc()))
            if lease is not None:
                lease_info = {"owner": lease.owner, "run_id": lease.run_id, "expires_at": lease.expires_at.isoformat(),
                              "heartbeat_at": lease.heartbeat_at.isoformat() if lease.heartbeat_at else None}
            acceptance = _acceptance_for(session, t["id"], version.manifest_hash)
        t["runtime"] = runtime
        t["lease"] = lease_info
        t["acceptance"] = acceptance
        t["warnings"] = [] if t["allowed_paths"] else ["NO_ALLOWED_PATHS: the scheduler blocks this task"]
        if not t["expected_outputs"]:
            t["warnings"].append("NO_EXPECTED_OUTPUT: the scheduler blocks this task (no declared output)")
        # §35 Live Work "progress dari AC/evidence nyata": counts only, from AcceptanceRecord (never a %).
        total_ac = len(t["acceptance_criteria"] or [])
        accepted_valid = acceptance.get("status") == "ACCEPTED" and acceptance.get("valid") is True
        t["progress"] = {"acceptance_criteria_total": total_ac,
                         "criteria_with_trusted_evidence": total_ac if accepted_valid else 0,
                         "basis": "AcceptanceRecord (ACCEPTED + valid) covers the TaskSpec acceptance criteria"}

    for t in tasks:
        # Only an ACCEPTED record whose validity was positively confirmed counts (UNKNOWN never passes,
        # an external "Done" never counts, §14/§50).
        accepted = [d for d in t["depends_on"] if d in by_key and by_key[d]["acceptance"].get("status") == "ACCEPTED"
                    and by_key[d]["acceptance"].get("valid") is True]
        t["accepted_prerequisites"] = accepted
        t["dependencies_ready"] = len(accepted) == len([d for d in t["depends_on"] if d in keys])

    # downstream reachability for blocker impact and concurrency
    children: dict[str, list[str]] = {k: [] for k in keys}
    for t in tasks:
        for d in t["depends_on"]:
            if d in children:
                children[d].append(t["key"])

    def downstream(k: str) -> set[str]:
        seen: set[str] = set()
        stack = list(children.get(k, []))
        while stack:
            n = stack.pop()
            if n not in seen:
                seen.add(n)
                stack.extend(children.get(n, []))
        return seen

    reach = {k: downstream(k) for k in keys}
    blockers = []
    for t in tasks:
        state = t["runtime"]["state"]
        if state in ("BLOCKED", "FAILED"):
            blocked = sorted(reach[t["key"]])
            blockers.append({"task_key": t["key"], "state": state, "reason": t["runtime"]["reason"],
                             "blocks": blocked, "impact": len(blocked)})
    blockers.sort(key=lambda b: -b["impact"])

    conflicts = []
    ordered = sorted(keys)
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            overlap = _overlapping(by_key[a]["allowed_paths"], by_key[b]["allowed_paths"])
            if not overlap:
                continue
            serialized = b in reach[a] or a in reach[b]
            conflicts.append({"tasks": [a, b], "paths": overlap, "serialized_by_dependency": serialized,
                              "concurrent_risk": not serialized})
    for t in tasks:
        t["conflicts_with"] = sorted({x for c in conflicts if c["concurrent_risk"] and t["key"] in c["tasks"]
                                      for x in c["tasks"] if x != t["key"]})

    edges = [{"id": f"{d}->{t['key']}", "source": d, "target": t["key"]} for t in tasks for d in t["depends_on"]
             if d in keys]
    errors = validate_graph(version.content or {})
    accepted_n = sum(1 for t in tasks if t["acceptance"].get("status") == "ACCEPTED" and t["acceptance"].get("valid") is True)
    return {
        "plan_id": plan.id, "plan_version_id": version.id, "version": version.version,
        "manifest_hash": version.manifest_hash, "approved": plan.approved_version_id == version.id,
        "compiled": persisted, "mode": "LIVE" if persisted else "PREVIEW",
        # Provenance of the plan content itself: a MOCK/DEMO version stays labelled as such (§35, §49).
        "data_mode": version.mode or "LIVE",
        "nodes": tasks, "edges": edges, "critical_blockers": blockers, "shared_write_conflicts": conflicts,
        "validation_errors": errors,
        "plan_warnings": plan_warnings(version.content or {}),
        "progress": {"accepted_tasks": accepted_n, "total_tasks": len(tasks),
                     "acceptance_criteria_total": sum(t["progress"]["acceptance_criteria_total"] for t in tasks),
                     "criteria_with_trusted_evidence": sum(t["progress"]["criteria_with_trusted_evidence"]
                                                           for t in tasks),
                     "basis": "AcceptanceRecord ACCEPTED + valid per TaskSpec; no estimated percentage"},
        "runtime_note": "Runtime state comes from TaskExecution/Lease/AcceptanceRecord; NOT_SCHEDULED means no "
                        "runtime record exists yet (no execution is implied).",
    }
