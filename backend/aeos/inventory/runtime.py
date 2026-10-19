"""Usage/liveness and observed relations from the control plane's own run/operation records (§36, §38).

"Digunakan saat ini" needs a relevant observation reference (§38): an active run attempt with a
valid lease, a broker/provider operation in the shown time window, or a finished operation
(LAST_USED, never "still running"). The records that prove this already exist in the single control
plane, so this module projects them — it creates no second state and no producer has to be
trusted beyond the module that owns the record:

* ``WorkerRun`` + ``Lease`` (workforce/worker runtime): engine/harness IN_USE while the run's lease is
  valid, STALE when the lease expired without the run finishing, LAST_USED once it finished;
* ``UsageLedgerEntry`` (cost ledger, written by the broker model proxy and the Claude bridge): the
  provider component LAST_USED with the request's run/route, plus the broker/bridge that called it;
* ``PublicationOperation`` (publisher outbox, LIVE publications): the target provider LAST_USED by
  the publisher for a SUCCEEDED write;
* ``ClaudeCapabilityTest`` (bridge): Claude Code LAST_USED by the bridge.

Each projected value names its source record, run/operation reference and time window, and is
scoped: rows of another domain are never read into a session's view. Relations seen in those
records are OBSERVED edges (traffic/event), distinct from DESIGN (manifest) and CONFIG edges; they
turn STALE after ``OBSERVED_EDGE_TTL`` like any observed edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aeos.core.timeutil import ensure_aware, utcnow
from aeos.inventory.configlens import (
    HARNESS_COMPONENTS,
    INTEGRATION_COMPONENTS,
    PROVIDER_COMPONENTS,
    route_eligible,
)

ACTIVE_RUN_STATES = ("LEASED", "RUNNING")
USAGE_WINDOW = timedelta(days=30)
LEDGER_CALLERS = {"broker": "capability-broker", "claude-bridge": "claude-native-bridge"}
LEDGER_CALL_RELATION = {"broker": "calls", "claude-bridge": "runs_with"}
DOC_OPERATIONS = frozenset({"create_doc", "update_doc"})


@dataclass
class UsageFact:
    component_id: str
    status: str  # IN_USE | LAST_USED | STALE
    at: datetime | None
    source: str
    reason: str
    refs: dict[str, Any] = field(default_factory=dict)
    expires_at: datetime | None = None

    def as_dim(self) -> dict[str, Any]:
        at, exp = ensure_aware(self.at), ensure_aware(self.expires_at)
        return {"value": self.status, "effective": self.status, "at": at.isoformat() if at else None,
                "expires_at": exp.isoformat() if exp else None, "stale": self.status == "STALE",
                "source": self.source, "observation_id": None, "version": None, "reason": self.reason,
                "refs": dict(self.refs)}


@dataclass
class EdgeFact:
    source_id: str
    target_id: str
    relation: str
    at: datetime | None
    source: str
    refs: dict[str, Any] = field(default_factory=dict)


def _visible(row_domain: str | None, domain: str | None) -> bool:
    return row_domain is None or row_domain == domain


def _project_ok(project_id: str | None, project_ids: list[str] | None) -> bool:
    """RBAC project scope (``principal.allowed_project_ids``; None = every project of the domain), the same
    filter the workforce run views apply: a run/request of a project the user may not see is never projected."""
    return project_ids is None or project_id is None or project_id in project_ids


def _newer(a: datetime | None, b: datetime | None) -> bool:
    a, b = ensure_aware(a), ensure_aware(b)
    return a is not None and (b is None or a > b)


def _route_providers(session: Session) -> dict[str, str]:
    from aeos.models.routing import ModelRoute

    return {k: p for k, p in session.execute(select(ModelRoute.route_key, ModelRoute.provider)) if k and p}


def _publication_component(provider: str, operation: str) -> str | None:
    if provider == "google-drive" and operation in DOC_OPERATIONS:
        return "google-docs"
    return INTEGRATION_COMPONENTS.get(provider)


def collect(session: Session, *, domain: str | None, now: datetime | None = None,
            project_ids: list[str] | None = None
            ) -> tuple[dict[str, UsageFact], list[EdgeFact], list[dict[str, Any]]]:
    """(usage per component, observed edges, active runs) from the run/operation records of *domain*, limited to
    the projects the viewer may see (*project_ids*, None = all of the domain)."""
    from aeos.models.claude_bridge_ext import ClaudeCapabilityTest
    from aeos.models.cost import UsageLedgerEntry
    from aeos.models.publishing import Publication, PublicationOperation
    from aeos.models.workforce import Lease, WorkerRun

    now = now or utcnow()
    since = now - USAGE_WINDOW
    usage: dict[str, UsageFact] = {}
    edges: dict[tuple[str, str, str], EdgeFact] = {}
    active_runs: list[dict[str, Any]] = []
    routes = _route_providers(session)

    def use(fact: UsageFact) -> None:
        cur = usage.get(fact.component_id)
        rank = {"IN_USE": 2, "STALE": 1, "LAST_USED": 0}
        if cur is None or rank[fact.status] > rank[cur.status] or (
                rank[fact.status] == rank[cur.status] and _newer(fact.at, cur.at)):
            usage[fact.component_id] = fact

    def edge(src: str, tgt: str, rel: str, at: datetime | None, source: str, refs: dict[str, Any]) -> None:
        key = (src, tgt, rel)
        cur = edges.get(key)
        if cur is None or _newer(at, cur.at):
            edges[key] = EdgeFact(src, tgt, rel, at, source, refs)

    # --- worker runs (active with lease -> IN_USE; finished -> LAST_USED)
    q = select(WorkerRun).where((WorkerRun.execution_status.in_(ACTIVE_RUN_STATES))
                                | (WorkerRun.finished_at >= since))
    leases = {ls.id: ls for ls in session.scalars(select(Lease).where(Lease.state == "ACTIVE"))}
    for run in session.scalars(q):
        if not _visible(run.domain, domain) or (run.mode or "LIVE") != "LIVE":
            continue
        if not _project_ok(run.project_id, project_ids):
            continue
        engine_cid = HARNESS_COMPONENTS.get(run.engine or "")
        provider_cid = PROVIDER_COMPONENTS.get(routes.get(run.model_route_key or "", ""))
        refs = {"run_id": run.id, "attempt": run.attempt, "task_key": run.task_key, "lease_id": run.lease_id,
                "route_key": run.model_route_key, "project_id": run.project_id, "record": "worker_run"}
        targets = [c for c in (engine_cid, "worker-runtime", provider_cid) if c]
        # §38: only the processes bound to the run (engine, worker runtime) are "in use"; a model provider is never a
        # running session — its finished requests are LAST_USED (usage ledger), even while the run is active.
        processes = [c for c in (engine_cid, "worker-runtime") if c]
        if run.execution_status in ACTIVE_RUN_STATES and run.finished_at is None:
            lease = leases.get(run.lease_id or "")
            exp = ensure_aware(lease.expires_at) if lease is not None else None
            live = exp is not None and exp > now
            label = run.task_key or (run.task_id or "")[:8] or run.id[:8]
            for cid in processes:
                use(UsageFact(cid, "IN_USE" if live else "STALE", run.started_at or run.created_at,
                              "worker_run + lease",
                              f"Sedang digunakan oleh task {label} (run {run.id[:8]}, attempt {run.attempt})" if live
                              else f"run {run.id[:8]} belum selesai tetapi lease tidak berlaku lagi; bukan bukti "
                                   "sedang dipakai", refs, exp))
            if live:
                active_runs.append({"run_id": run.id, "task_key": run.task_key, "status": run.execution_status,
                                    "attempt": run.attempt, "components": processes, "lease_expires_at":
                                    exp.isoformat() if exp else None, "project_id": run.project_id})
                if engine_cid:
                    edge("worker-runtime", engine_cid, "runs_with", now, "worker_run (aktif)", refs)
        elif run.finished_at is not None:
            for cid in targets:
                use(UsageFact(cid, "LAST_USED", run.finished_at, "worker_run",
                              f"terakhir dipakai run {run.id[:8]} ({run.execution_status}) untuk task "
                              f"{run.task_key or '-'}", refs))
            if engine_cid:
                edge("worker-runtime", engine_cid, "runs_with", run.finished_at, "worker_run", refs)

    # --- usage ledger (broker model proxy / Claude bridge): latest LIVE request per provider+source
    ul = UsageLedgerEntry
    latest = (select(ul.provider, ul.source, ul.domain, ul.project_id, func.max(ul.at).label("m"))
              .where(ul.mode == "LIVE", ul.at >= since).group_by(ul.provider, ul.source, ul.domain, ul.project_id))
    for provider, source, row_domain, row_project, at in session.execute(latest):
        if not _visible(row_domain, domain) or not _project_ok(row_project, project_ids):
            continue
        # the representative row is taken from the same domain/project group (never another domain's request)
        row = session.scalar(select(ul).where(
            ul.provider == provider, ul.source == source, ul.at == at, ul.mode == "LIVE",
            ul.domain.is_(None) if row_domain is None else ul.domain == row_domain,
            ul.project_id.is_(None) if row_project is None else ul.project_id == row_project).limit(1))
        cid = PROVIDER_COMPONENTS.get(provider)
        refs = {"ledger_entry_id": row.id if row else None, "run_id": row.run_id if row else None,
                "route_key": row.route_key if row else None, "category": row.category if row else None,
                "record": "usage_ledger", "source": source}
        if cid:
            use(UsageFact(cid, "LAST_USED", at, f"usage ledger ({source})",
                          f"permintaan model selesai (LAST_USED, bukan proses yang masih hidup) via {source}", refs))
        caller = LEDGER_CALLERS.get(source)
        if caller:
            use(UsageFact(caller, "LAST_USED", at, f"usage ledger ({source})",
                          f"meneruskan permintaan ke {provider}", refs))
            if cid:
                edge(caller, cid, LEDGER_CALL_RELATION[source], at, f"usage ledger ({source})", refs)

    # --- publisher outbox: SUCCEEDED writes of LIVE publications
    po, pub = PublicationOperation, Publication
    rows = session.execute(select(po.provider, po.operation, pub.domain, func.max(po.updated_at).label("m"))
                           .join(pub, pub.id == po.publication_id)
                           .where(po.status == "SUCCEEDED", pub.mode == "LIVE", po.updated_at >= since)
                           .group_by(po.provider, po.operation, pub.domain))
    for provider, operation, row_domain, at in rows:
        if not _visible(row_domain, domain):
            continue
        cid = _publication_component(provider, operation)
        refs = {"provider": provider, "operation": operation, "record": "publication_operation"}
        use(UsageFact("publisher", "LAST_USED", at, "publication outbox", f"write {operation} ke {provider}", refs))
        if cid:
            use(UsageFact(cid, "LAST_USED", at, "publication outbox",
                          f"publisher menulis ({operation}); operasi selesai", refs))
            edge("publisher", cid, "publishes_to", at, "publication outbox", refs)

    # --- Claude bridge capability tests (LIVE)
    t = session.scalar(select(ClaudeCapabilityTest).where(ClaudeCapabilityTest.mode == "LIVE")
                       .order_by(ClaudeCapabilityTest.created_at.desc()).limit(1))
    if t is not None and _newer(t.created_at, since):
        refs = {"capability_test_id": t.id, "record": "claude_capability_test", "status": t.status}
        for cid in ("claude-code", "claude-native-bridge"):
            use(UsageFact(cid, "LAST_USED", t.created_at, "ClaudeCapabilityTest",
                          f"capability test {t.status} (satu panggilan selesai)", refs))
        edge("claude-native-bridge", "claude-code", "runs_with", t.created_at, "ClaudeCapabilityTest", refs)
    return usage, list(edges.values()), active_runs


def consuming(session: Session, component_id: str, *, domain: str | None, limit: int = 10,
              project_ids: list[str] | None = None) -> dict[str, Any]:
    """Routes, agents and recent runs that consume *component_id* (§37 "consuming tasks/services/routes")."""
    from aeos.models.routing import ModelRoute
    from aeos.models.workforce import AgentDefinition, WorkerRun

    providers = {p for p, cid in PROVIDER_COMPONENTS.items() if cid == component_id}
    harnesses = {h for h, cid in HARNESS_COMPONENTS.items() if cid == component_id}
    route_rows = list(session.scalars(select(ModelRoute).where(ModelRoute.provider.in_(providers)))) if providers else []
    routes = [{"route_key": r.route_key, "status": r.status, "pool": r.pool, "provider": r.provider,
               "eligible_domains": r.eligible_domains or []} for r in route_rows
              if route_eligible(r, domain)]
    agents = [{"agent_key": a.agent_key, "role_key": a.role_key, "enabled": a.enabled, "harness": a.harness,
               "revision_hash": a.revision_hash}
              for a in session.scalars(select(AgentDefinition).where(AgentDefinition.harness.in_(harnesses)))] \
        if harnesses else []
    route_keys = {r.route_key for r in route_rows}
    runs = []
    if harnesses or route_keys:
        q = select(WorkerRun).order_by(WorkerRun.created_at.desc()).limit(200)
        for r in session.scalars(q):
            if not _visible(r.domain, domain) or not _project_ok(r.project_id, project_ids):
                continue
            if (r.engine in harnesses) or (r.model_route_key in route_keys):
                runs.append({"run_id": r.id, "task_key": r.task_key, "task_id": r.task_id,
                             "status": r.execution_status, "engine": r.engine, "route_key": r.model_route_key,
                             "started_at": (ensure_aware(r.started_at).isoformat() if r.started_at else None),
                             "finished_at": (ensure_aware(r.finished_at).isoformat() if r.finished_at else None),
                             "mode": r.mode})
            if len(runs) >= limit:
                break
    status = "KNOWN" if (routes or agents or runs) else "NONE_RECORDED"
    reason = None if routes or agents or runs else (
        "belum ada route/agent/run yang menautkan komponen ini" if providers or harnesses
        else "komponen ini tidak dikonsumsi lewat route/agent/run (bukan provider/harness)")
    return {"routes": routes, "agents": agents, "items": runs, "status": status, "reason": reason,
            "source": "ModelRoute + AgentDefinition + WorkerRun"}


def eval_runs(session: Session, component_id: str, *, domain: str | None, limit: int = 10) -> list[dict[str, Any]]:
    """EvalRun ids of routes whose provider is *component_id* (§37 Bukti: eval IDs)."""
    from aeos.models.research import EvalRun

    providers = {p for p, cid in PROVIDER_COMPONENTS.items() if cid == component_id}
    if not providers:
        return []
    keys = {k for k, p in _route_providers(session).items() if p in providers}
    if not keys:
        return []
    out = []
    for r in session.scalars(select(EvalRun).where(EvalRun.route_key.in_(keys)).order_by(EvalRun.created_at.desc())
                             .limit(100)):
        if not _visible(r.submitted_domain, domain):
            continue
        out.append({"id": r.id, "suite_key": r.suite_key, "route_key": r.route_key, "status": r.status,
                    "mode": r.mode, "finished_at": ensure_aware(r.finished_at).isoformat() if r.finished_at else None})
        if len(out) >= limit:
            break
    return out
