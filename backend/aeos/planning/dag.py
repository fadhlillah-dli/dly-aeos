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
from aeos.core.enums import Domain
from aeos.core.errors import ValidationFailed
from aeos.core.hashing import content_hash
from aeos.core.paths import repo_root
from aeos.events import service as events
from aeos.models.planning import Feature, Milestone, Plan, PlanVersion, TaskDependency, TaskSpec
from aeos.models.portfolio import Project
from aeos.models.workforce import Lease, TaskExecution

FALLBACK_PROTECTED_PATHS: tuple[str, ...] = (
    "policy/**", "backend/aeos/policy/**", "backend/aeos/verification/**", "backend/aeos/evaluation/**",
    "backend/aeos/broker/**",
    "backend/aeos/identity/**", "tests/acceptance/**", ".github/workflows/**", "install-manifest/**",
    "backend/aeos/migrations/**",
)
# Trusted grader/pipeline paths that no worker of ANY managed project may change without quarantine
# review (§21 "grader/held-out tests ... pipeline tepercaya"). The platform project adds the full
# AGENTS.md protected list (§09).
GENERIC_TRUSTED_PATHS: tuple[str, ...] = ("tests/acceptance/**", ".github/workflows/**", "bitbucket-pipelines.yml")
DEFAULT_LIMITS = {"max_attempts": 2, "timeout_seconds": 3600, "max_output_tokens": 32000}
MAX_ATTEMPTS_CAP = 2  # §21 pilot cap: at most two implementation attempts per TaskSpec
# §21 resource caps of the pilot: one active worker per task, no automatic nested fan-out. Fixed by the
# control plane (never taken from model output); raising them is a recorded human decision.
RESOURCE_LIMITS = {"max_active_workers": 1, "nested_fan_out": False}
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
    m = re.search(r"^## Protected paths.*?$(.*?)(?=^## |\Z)", text, flags=re.M | re.S)
    if not m:
        return FALLBACK_PROTECTED_PATHS
    found = [p for p in re.findall(r"`([^`]+)`", m.group(1)) if "/" in p]
    return tuple(dict.fromkeys(found)) or FALLBACK_PROTECTED_PATHS


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


def validate_graph(content: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = content.get("tasks") or []
    features = content.get("features") or []
    milestones = content.get("milestones") or []
    errors: list[dict[str, Any]] = []
    ms_errors, ms_keys = _graph_errors("milestone", [{"key": m.get("key"), "depends_on": []} for m in milestones])
    errors += ms_errors
    f_errors, f_keys = _graph_errors("feature", features)
    errors += f_errors
    t_errors, _t_keys = _graph_errors("task", tasks)
    errors += t_errors
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
        raise ValidationFailed("plan DAG is invalid (unknown node, self-dependency, cycle or unsafe path)",
                               code="DAG_INVALID", details={"errors": errors})


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


def task_spec_fields(task: dict[str, Any], *, project: Project, version: PlanVersion,
                     protected: tuple[str, ...]) -> dict[str, Any]:
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
        "role_key": str(task.get("role_key") or "feature-implementer")[:64],
        "input_artifacts": _str_list(task.get("input_artifacts")),
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
        "evidence_requirements": list(dict.fromkeys(_str_list(task.get("evidence_requirements")) + DEFAULT_EVIDENCE)),
        "risk_class": risk,
        "artifact_schema_version": str(task.get("artifact_schema_version") or "1")[:16],
        "mode": version.mode if version.mode in ("MOCK", "DEMO") else "LIVE",
    }


# ---------------------------------------------------------------- compile


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

    for idx, m in enumerate(content.get("milestones") or []):
        session.add(Milestone(plan_version_id=version.id, milestone_key=str(m["key"]), title=str(m.get("title") or m["key"])[:256],
                              exit_evidence=str(m.get("exit_evidence") or ""), order_index=int(m.get("order") or idx)))
    for f in content.get("features") or []:
        session.add(Feature(plan_version_id=version.id, feature_key=str(f["key"]), title=str(f.get("title") or f["key"])[:256],
                            requirement=str(f.get("requirement") or ""), priority=str(f.get("priority") or "P2")[:16],
                            acceptance=_str_list(f.get("acceptance")), depends_on=_str_list(f.get("depends_on")),
                            milestone_key=str(f.get("milestone") or "") or None))
    by_key: dict[str, TaskSpec] = {}
    for t in content.get("tasks") or []:
        fields = task_spec_fields(t, project=project, version=version, protected=protected)
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
        t["warnings"] = [] if t["allowed_paths"] else ["NO_ALLOWED_PATHS: the scheduler must block this task"]

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
    return {
        "plan_id": plan.id, "plan_version_id": version.id, "version": version.version,
        "manifest_hash": version.manifest_hash, "approved": plan.approved_version_id == version.id,
        "compiled": persisted, "mode": "LIVE" if persisted else "PREVIEW",
        # Provenance of the plan content itself: a MOCK/DEMO version stays labelled as such (§35, §49).
        "data_mode": version.mode or "LIVE",
        "nodes": tasks, "edges": edges, "critical_blockers": blockers, "shared_write_conflicts": conflicts,
        "validation_errors": errors,
        "runtime_note": "Runtime state comes from TaskExecution/Lease/AcceptanceRecord; NOT_SCHEDULED means no "
                        "runtime record exists yet (no execution is implied).",
    }
