"""`aeos work ...` - scheduler control from the local terminal (ASCII-only output)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.markup import escape

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

app = typer.Typer(help="Workforce scheduler: status, run-once, pause/resume, cancel, demo fixtures; owner actions: "
                       "agent enable/disable, execute an approved plan, role adoption, role-evaluation threshold, "
                       "concurrency mode, "
                       "two-worker experiment grant, investigation choice.",
                  no_args_is_help=True)
console = Console()


def _out(text: str) -> None:
    """ASCII-only, markup-safe output (AGENTS.md: legacy Windows consoles). Reasons can come from
    other modules and contain non-ASCII characters or square brackets."""
    clean = str(text).replace("\u00a7", "s").replace("\u2014", "-").replace("\u2192", "->")
    console.print(escape(clean.encode("ascii", "replace").decode("ascii")), highlight=False)
ACTOR = "cli:local-operator"


def _db() -> None:
    from aeos.cli import ensure_database

    ensure_database()


@app.command("status")
def status(domain: str = typer.Option("PERSONAL", help="PERSONAL or OFFICE")) -> None:
    """Show scheduler state, task counts, active runs and blockers (no LLM)."""
    from aeos.core.db import session_scope
    from aeos.workforce import loop, views

    _db()
    with session_scope() as s:
        data = views.summary(s, domain=domain.upper(), project_ids=None, loop_running=loop.is_running())
        runs = views.list_runs(s, domain=domain.upper(), project_ids=None, active_only=True)
    sch = data["scheduler"]
    _out(f"scheduler enabled={sch['enabled']} paused={sch['paused']} cap={sch['concurrency_cap']} "
                  f"loop_running_in_this_process={sch['loop_running']}")
    _out("tasks: " + ", ".join(f"{k}={v}" for k, v in data["task_counts"].items()))
    _out(f"active runs (LIVE): {data['active_runs']}   DEMO rows: {data['demo_runs']}")
    _out(f"sandbox: {data['sandbox']['status']} kind={data['sandbox']['kind']}   "
                  f"engine: {data['engine']['status']} {data['engine'].get('version') or ''}")
    cost = data["cost"]
    _out(f"worker API cost: {cost['cost_known_usd'] if cost['cost_known_usd'] is not None else 'UNKNOWN'} "
                  f"(status {cost['cost_status']}, unknown entries {cost['cost_unknown_entries']})")
    for r in runs:
        _out(f"  run {r['run_id']} [{r['mode']}] {r['execution_state']}/{r['ui_state']} "
                      f"task={r['task']['key']} action={r['current_action'][:60]}")
    for b in data["blockers"]:
        _out(f"  blocker {b['task_key']} {b['state']} {b['reason_code']}: {b['reason'][:100]}")


@app.command("run-once")
def run_once(no_execute: bool = typer.Option(False, "--no-execute", help="Only reconcile/refresh/admit")) -> None:
    """One scheduler tick in the foreground (admits even if the background loop is disabled, never when paused)."""
    from aeos.core.config import get_settings
    from aeos.workforce import loop

    _db()
    info = loop.run_once(get_settings(), execute=not no_execute, force_admit=True)
    _out(f"reconcile={info.get('reconcile')} refreshed={info.get('refreshed')} leased={info.get('leased')}")
    for r in info.get("results", []):
        _out(f"  run {r.get('run_id')}: {r.get('status')} {r.get('code', '')} {r.get('reason', '')}")
    if info.get("paused"):
        _out("Scheduling is paused. Resume with: aeos work resume")


@app.command("pause")
def pause(reason: str = typer.Option("", help="Reason")) -> None:
    """Pause admission of new runs (running work is not killed; use cancel for that)."""
    from aeos.core.db import session_scope
    from aeos.workforce import scheduler

    _db()
    with session_scope() as s:
        scheduler.set_scheduling_paused(s, True, actor=ACTOR, reason=reason)
    _out("Scheduling paused.")


@app.command("resume")
def resume(reason: str = typer.Option("", help="Reason")) -> None:
    """Resume admission."""
    from aeos.core.db import session_scope
    from aeos.workforce import scheduler

    _db()
    with session_scope() as s:
        scheduler.set_scheduling_paused(s, False, actor=ACTOR, reason=reason)
    _out("Scheduling resumed.")


@app.command("enable")
def enable(reason: str = typer.Option("", help="Reason (stored in the audit trail)")) -> None:
    """Allow the background loop to admit READY tasks (scheduler.enabled = true)."""
    from aeos.core.db import session_scope
    from aeos.workforce import scheduler

    _db()
    with session_scope() as s:
        scheduler.set_scheduler_enabled(s, True, actor=ACTOR, reason=reason)
    _out("Scheduler enabled (admission still requires approval, CAN_RUN_WORKER, route, sandbox, budget).")


@app.command("disable")
def disable(reason: str = typer.Option("", help="Reason (stored in the audit trail)")) -> None:
    """Stop the background loop from admitting new runs."""
    from aeos.core.db import session_scope
    from aeos.workforce import scheduler

    _db()
    with session_scope() as s:
        scheduler.set_scheduler_enabled(s, False, actor=ACTOR, reason=reason)
    _out("Scheduler disabled.")


@app.command("cancel")
def cancel(run: str = typer.Argument(..., help="Run id"), reason: str = typer.Option("cancelled from CLI")) -> None:
    """Cancel a run: revoke capabilities, kill the process tree, release reservations, keep evidence."""
    from aeos.core.db import session_scope
    from aeos.workforce import scheduler

    _db()
    with session_scope() as s:
        r = scheduler.cancel_run(s, run, reason=reason, actor=ACTOR)
        state = r.execution_status
    _out(f"Run {run}: {state}. External side effects that already happened are NOT undone automatically.")


# ----------------------------------------------------------------------------- owner actions (human, password)
AS_OPTION = typer.Option(..., "--as", help="Your SUPER_ADMIN username (a human owner action; password prompted)")
DOMAIN_OPTION = typer.Option(..., "--domain", help="PERSONAL or OFFICE (chosen by you, never inferred)")


def admin_user_id(session, username: str, password: str) -> str:
    """Human-only AUTH (master s19/s20): the actor proves it is an active SUPER_ADMIN by its password - a username alone is
    not enough (any local process could name the owner). The password is never stored or logged."""
    from sqlalchemy import select

    from aeos.core.enums import Role
    from aeos.identity import facade as identity
    from aeos.models.identity import User

    user = session.scalars(select(User).where(User.username == username)).first()
    if user is None or not user.is_active or user.role != Role.SUPER_ADMIN.value:
        raise typer.BadParameter(f"{username!r} is not an active SUPER_ADMIN (owner actions are human-only)")
    if not password or not identity.verify_password(user.password_hash, password):
        raise typer.BadParameter("password check failed - nothing recorded")
    return str(user.id)


def _human(session, as_user: str) -> str:
    """Owner actions are human actions: the SUPER_ADMIN proves it by password (never echoed, never stored)."""
    password = typer.prompt(f"Password for {as_user} (confirms this human action)", hide_input=True, default="",
                            show_default=False)
    return admin_user_id(session, as_user, password)


def _domain(value: str) -> str:
    v = (value or "").strip().upper()
    if v not in ("PERSONAL", "OFFICE"):
        raise typer.BadParameter("domain must be PERSONAL or OFFICE")
    return v


def _owner_action(fn) -> None:
    """Run *fn(session)*; an AeosError prints its code, message and reasons and exits 1 (nothing committed)."""
    from aeos.core.db import session_scope
    from aeos.core.errors import AeosError

    _db()
    try:
        with session_scope() as s:
            fn(s)
    except AeosError as exc:
        _out(f"{exc.code}: {exc.message}")
        for r in (exc.details or {}).get("reasons", [])[:30]:
            _out(f"  - {r}")
        raise typer.Exit(1) from exc


@app.command("agent-prereqs")
def agent_prereqs(agent_key: str = typer.Argument(..., help="AgentDefinition key, e.g. opencode-implementer"),
                  domain: str = DOMAIN_OPTION) -> None:
    """Show what still blocks enabling an agent (read-only; same rule as agent-enable)."""
    from aeos.workforce import agents

    def act(s) -> None:
        pre = agents.prerequisites(s, agents.get_agent(s, agent_key), domain=_domain(domain))
        _out(f"{agent_key}: {'READY to enable' if pre['ready'] else 'NOT READY'} (enabled now: {pre['enabled']})")
        for r in pre["reasons"]:
            _out(f"  - {r}")
        s.rollback()

    _owner_action(act)


@app.command("agent-enable")
def agent_enable(agent_key: str = typer.Argument(..., help="AgentDefinition key, e.g. opencode-implementer"),
                 as_user: str = AS_OPTION, domain: str = DOMAIN_OPTION,
                 reason: str = typer.Option(..., help="Why you enable it (stored in the audit trail)")) -> None:
    """Enable an implementation agent; refused with the list of unmet prerequisites (AGENT_PREREQUISITES)."""
    from aeos.core.errors import Blocked
    from aeos.workforce import agents

    def act(s) -> None:
        uid = _human(s, as_user)
        try:
            out = agents.enable_agent(s, agent_key, domain=_domain(domain), actor=f"user:{uid}", actor_type="user",
                                      reason=reason)
        except Blocked:
            s.commit()  # the DENIED audit row is kept, as in the Control Center (nothing else was changed)
            raise
        _out(f"{agent_key} enabled. Doctor recheck: {out['doctor'].get('status')} "
             f"{out['doctor'].get('results') or out['doctor'].get('reason') or ''}")
        _out("Enabled is not in use: runs start only for an executed approved plan (aeos work execute).")

    _owner_action(act)


@app.command("agent-disable")
def agent_disable(agent_key: str = typer.Argument(..., help="AgentDefinition key"),
                  reason: str = typer.Option(..., help="Why you disable it (stored in the audit trail)")) -> None:
    """Disable an agent (always allowed; running attempts continue until done or cancelled)."""
    from aeos.workforce import agents

    def act(s) -> None:
        agents.disable_agent(s, agent_key, actor=ACTOR, actor_type="cli", reason=reason)
        _out(f"{agent_key} disabled. Running attempts are not killed: aeos work cancel <run_id>.")

    _owner_action(act)


@app.command("execute")
def execute(plan_id: str = typer.Argument(..., help="Plan id with an APPROVED version"), as_user: str = AS_OPTION,
            domain: str = DOMAIN_OPTION,
            reason: str = typer.Option("", help="Reason (stored in the audit trail)")) -> None:
    """Execute the approved plan version: its TaskSpecs are queued and made READY (admission does the rest)."""
    from aeos.core.errors import NotFound
    from aeos.models.planning import Plan
    from aeos.workforce import scheduler

    def act(s) -> None:
        uid = _human(s, as_user)
        plan = s.get(Plan, plan_id)
        if plan is None or plan.domain != _domain(domain):
            raise NotFound("plan not found in this domain")
        out = scheduler.execute_plan(s, plan_id, actor=f"user:{uid}", user_id=uid, reason=reason)
        _out(f"plan {plan_id} version {out['plan_version_id']}: {out['tasks']} task(s), ready={out.get('ready')} "
             f"blocked={out.get('blocked')} scheduler_enabled={out['scheduler_enabled']} paused={out['paused']}")
        _out("READY tasks are admitted by the scheduler loop (aeos work enable) or: aeos work run-once")

    _owner_action(act)


@app.command("adopt-role")
def adopt_role(role_key: str = typer.Argument(..., help="Executor role key, e.g. feature-implementer"),
               eval_run: str = typer.Option(..., help="The role's current PASS EvalRun id"),
               as_user: str = AS_OPTION,
               rationale: str = typer.Option(..., help="Why the result is adopted (min 5 chars)"),
               option: list[str] = typer.Option(..., help="DecisionRecord: an option you weighed (repeatable)"),
               goal: str = typer.Option(..., help="DecisionRecord: the goal"),
               tradeoff: str = typer.Option(..., help="DecisionRecord: cost/quality trade-off"),
               invalidate_if: str = typer.Option(..., help="DecisionRecord: when this decision stops applying"),
               risk_class: list[str] = typer.Option(None, help="Scope: risk class (repeatable; empty = all)"),
               scope_domain: list[str] = typer.Option(None, help="Scope: domain (repeatable; empty = all)"),
               project: list[str] = typer.Option(None, help="Scope: project id (repeatable; empty = all)")) -> None:
    """Adopt a role's PASS evaluation as its admission eligibility (human DecisionRecord, master s44/s46)."""
    from aeos.workforce import scheduler

    def act(s) -> None:
        uid = _human(s, as_user)
        scope = {k: v for k, v in (("risk_classes", risk_class), ("domains", scope_domain), ("project_ids", project))
                 if v}
        out = scheduler.adopt_role_result(s, role_key=role_key, eval_run_id=eval_run, actor=f"user:{uid}",
                                          actor_user_id=uid, rationale=rationale, scope=scope,
                                          decision_record={"options": list(option), "goal": goal,
                                                           "cost_quality_tradeoff": tradeoff,
                                                           "invalidate_if": invalidate_if})
        _out(f"role {role_key}: adopted EvalRun {out['eval_run_id']} (decision {out['decision_id']}, "
             f"scope {out['scope'] or 'every task of this role'})")

    _owner_action(act)


@app.command("role-eval-threshold")
def role_eval_threshold(risk_class: str = typer.Argument(..., help="R0..R4: from this risk class up, admission needs "
                                                                     "a PASS role evaluation or an owner EXCEPTION"),
                        as_user: str = AS_OPTION,
                        rationale: str = typer.Option(..., help="Why (min 5 chars; stored in the audit trail)")) -> None:
    """Set from which risk class admission requires a PASS role evaluation (master s46; pilot default R4).

    A lower class tightens admission (R2 = R2, R3 and R4 need an evaluated role). Same rule as Organisasi & Peran ->
    'Ambang evaluasi peran' and POST /api/workforce/role-eval-threshold; the change is audited and emitted."""
    from aeos.core.errors import ValidationFailed
    from aeos.workforce import scheduler

    value = (risk_class or "").strip().upper()
    if value not in scheduler.RISK_ORDER:
        raise typer.BadParameter(f"risk class must be one of {', '.join(scheduler.RISK_ORDER)}")

    def act(s: Session) -> None:
        current = scheduler.role_eval_threshold(s)
        _out(f"Role evaluation required from {current['required_from']} ({current['source']}) now; you set {value}: "
             f"tasks of {', '.join(scheduler.RISK_ORDER[scheduler.RISK_ORDER.index(value):])} need a PASS role "
             "evaluation (or an owner EXCEPTION)")
        uid = _human(s, as_user)
        if len((rationale or "").strip()) < 5:
            raise ValidationFailed("rationale is required (min 5 chars)", code="RATIONALE_REQUIRED")
        out = scheduler.set_role_eval_threshold(s, risk_class=value, actor=f"user:{uid}", rationale=rationale)
        _out(f"role evaluation threshold {out['previous']} -> {out['threshold']} (audited: workforce.role_eval_threshold)")

    _owner_action(act)


@app.command("concurrency")
def concurrency(max_active: int = typer.Argument(..., min=1, max=2, help="1 (pilot) or 2"),
                as_user: str = AS_OPTION,
                rationale: str = typer.Option("", help="Why (min 10 chars for 2 workers)"),
                eval_run: str = typer.Option(None, help="COMPLETED topology-C EvalRun (2 workers)"),
                baseline_eval_run: str = typer.Option(None, help="COMPLETED topology-A baseline EvalRun (2 workers)"),
                role: list[str] = typer.Option(None, help="Scope: role key (must be the suite's role)"),
                risk_class: list[str] = typer.Option(None, help="Scope: risk class (repeatable)"),
                project: list[str] = typer.Option(None, help="Scope: project id (repeatable)"),
                scope_domain: list[str] = typer.Option(None, help="Scope: domain (repeatable)"),
                option: list[str] = typer.Option(None, help="DecisionRecord: an option you weighed (repeatable)"),
                goal: str = typer.Option(None, help="DecisionRecord: the goal"),
                tradeoff: str = typer.Option(None, help="DecisionRecord: cost/quality trade-off"),
                invalidate_if: str = typer.Option(None, help="DecisionRecord: when this decision stops applying")) -> None:
    """Set the concurrency mode: 1 (pilot, rollback) or 2 (needs a proven topology-C benefit, master s19/s44)."""
    from aeos.workforce import scheduler

    def act(s) -> None:
        uid = _human(s, as_user)
        scope = {k: v for k, v in (("role_keys", role), ("risk_classes", risk_class), ("project_ids", project),
                                   ("domains", scope_domain)) if v}
        out = scheduler.set_concurrency(s, max_active=max_active, actor=f"user:{uid}", actor_user_id=uid,
                                        rationale=rationale, eval_run_id=eval_run, scope=scope,
                                        baseline_eval_run_id=baseline_eval_run,
                                        decision_record={"options": list(option or []), "goal": goal,
                                                         "cost_quality_tradeoff": tradeoff,
                                                         "invalidate_if": invalidate_if})
        detail = scheduler.concurrency_detail(s)
        _out(f"concurrency mode {out['max_active']} (decision {out.get('decision_id') or '-'}); effective cap "
             f"{detail['cap']}: {detail['reason']}")

    _owner_action(act)


@app.command("experiment-grant")
def experiment_grant(eval_run: str = typer.Argument(..., help="RUNNING topology-C EvalRun id"),
                     as_user: str = AS_OPTION, domain: str = DOMAIN_OPTION,
                     rationale: str = typer.Option(..., help="Why two workers for this experiment (min 10 chars)"),
                     days: int = typer.Option(..., min=1, max=30,
                                              help="Expiry in days, 1-30 (an EXCEPTION always expires)")) -> None:
    """Allow two workers ONLY for the TaskSpecs bound to one RUNNING topology-C evaluation run (master s19/s44)."""
    from aeos.workforce import experiments

    def act(s) -> None:
        uid = _human(s, as_user)
        out = experiments.grant_experiment(s, eval_run_id=eval_run, actor=f"user:{uid}", actor_user_id=uid,
                                           rationale=rationale, days=days, domain=_domain(domain))
        _out(f"eval run {eval_run}: experiment grant {out['decision_id']} (max {out['max_active']} workers, "
             f"{out['bound_tasks']} bound task(s), expires {out['expires_at']}, budget: {out['budget']})")
        _out("Every other admission check (write sets, compute, budget, sandbox, route, verifier) still applies.")

    _owner_action(act)


@app.command("investigation")
def investigation_show(task_id: str = typer.Argument(..., help="Investigation TaskSpec id"),
                       domain: str = DOMAIN_OPTION) -> None:
    """Compare the read-only investigation reports per hypothesis (untrusted DATA; evidence refs runtime-checked)."""
    from aeos.core.errors import NotFound
    from aeos.models.planning import TaskSpec
    from aeos.workforce import investigation

    def act(s) -> None:
        task = s.get(TaskSpec, task_id)
        if task is None or task.domain != _domain(domain):
            raise NotFound("task not found in this domain")
        view = investigation.comparison(s, task)
        _out(f"investigation {view['task_key']}: {view['state']}/{view['reason_code'] or '-'} repair="
             f"{view['repair_task_key']} evidenced={','.join(view['evidenced']) or 'none'}")
        for h in view["hypotheses"]:
            refs = ", ".join(f"{r['ref']}({'ok' if r['exists'] else 'missing'})" for r in h["evidence_refs"][:5])
            _out(f"  {h['key']}: {h['outcome_code'] or 'NOT_RUN'} verdict={h['verdict'] or '-'} "
                 f"evidenced={h['evidenced']} refs={refs or '-'}")
        if view["choice"]:
            _out(f"chosen: {view['choice']['hypothesis_key']} (decision {view['choice']['decision_id']})")
        s.rollback()

    _owner_action(act)


@app.command("investigation-choose")
def investigation_choose(task_id: str = typer.Argument(..., help="Investigation TaskSpec id"),
                         hypothesis: str = typer.Argument(..., help="The evidenced hypothesis key you choose"),
                         as_user: str = AS_OPTION, domain: str = DOMAIN_OPTION,
                         rationale: str = typer.Option(..., help="Why this fix (min 10 chars)")) -> None:
    """Integrator step: record the chosen evidenced fix; the plan's repair task then carries it (human APPROVAL)."""
    from aeos.core.errors import NotFound
    from aeos.models.planning import TaskSpec
    from aeos.workforce import investigation

    def act(s) -> None:
        uid = _human(s, as_user)
        task = s.get(TaskSpec, task_id)
        if task is None or task.domain != _domain(domain):
            raise NotFound("task not found in this domain")
        out = investigation.choose_fix(s, task_id, hypothesis_key=hypothesis, actor=f"user:{uid}", actor_user_id=uid,
                                       rationale=rationale)
        _out(f"chose {out['hypothesis_key']} (decision {out['decision_id']}); repair task {out['repair_task_key']} "
             "now carries the fix and is admitted like any implementation task")

    _owner_action(act)


@app.command("demo-fixtures")
def demo_fixtures(remove: bool = typer.Option(False, "--remove", help="Delete DEMO rows instead"),
                  domain: str = typer.Option("PERSONAL", help="Domain for the DEMO rows")) -> None:
    """Insert clearly labelled DEMO runs for UI preview (excluded from counts/cost/acceptance)."""
    from aeos.core.db import session_scope
    from aeos.workforce import demo

    _db()
    with session_scope() as s:
        res = demo.remove(s, actor=ACTOR) if remove else demo.insert(s, domain=domain.upper(), actor=ACTOR)
    _out(f"DEMO fixtures: {res}")
