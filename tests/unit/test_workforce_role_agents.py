"""Category agents and the role -> agent map (ADR-0126, owner decision R31 of 2026-10-28).

Bite tests: each check has a violating case that is red with its own message and a near miss that is green.
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import inspect
import json
import shutil
from types import ModuleType

import pytest
from sqlalchemy import select

from aeos.core.errors import Blocked, ValidationFailed
from aeos.core.paths import repo_root
from aeos.models.audit import AuditEvent
from aeos.models.workforce import AgentDefinition, Lease, TaskExecution
from aeos.workforce import catalog, role_agents, scheduler, seed
from aeos.workforce.limits import HARD_MAX_ACTIVE
from tests.unit.test_workforce_support import (
    FakeModules,
    make_plan,
    make_project,
    make_route,
    process_sandbox_exception,
    seed_and_enable_implementer,
)

ROOT = repo_root()
ADR = ROOT / "docs" / "decisions" / "ADR-0126-per-role-category-agents.md"
SERVABLE_GO = {"kimi-k3.1-pro", "mimo-v2.6-pro", "minimax-m3.1-flash", "kimi-k3", "glm-5.3-flash", "glm-5.3",
               "glm-5.2", "step-5-preview", "kimi-k2.7-code"}
# the owner's brief (2026-10-28): category agent -> the roles it serves
EXPECTED_ROLES = {
    "agentic-coder": {"backend-api-engineer", "frontend-engineer", "db-data-engineer", "integration-engineer",
                      "data-ai-feature-engineer", "platform-devsecops-engineer", "security-engineer"},
    "coder": {"qa-test-designer"},
    "designer": {"ux-ui-designer", "diagram-visual-author"},
    "reasoner": {"solution-architect", "engineering-delivery-analyst", "incident-analyst", "sre-operations-assistant"},
    "researcher": {"ux-research-assistant", "product-analyst", "supply-chain-analyst"},
    "writer": {"technical-writer", "privacy-grc-analyst", "release-coordinator", "retirement-assistant",
               "context-curator"},
    "analyst": {"model-evaluation-analyst", "finops-quota-analyst"},
}


def _ranking_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("role_route_ranking", ROOT / "scripts" / "role_route_ranking.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _defs() -> dict[str, dict]:
    return {a["agent_key"]: a for a in catalog.AGENT_DEFINITIONS}


@pytest.fixture(name="fakes")
def _fakes_fixture(monkeypatch: pytest.MonkeyPatch) -> FakeModules:
    return FakeModules().install(monkeypatch)


def _ready(db, fakes, tasks, *, domain: str = "PERSONAL"):
    project = make_project(db, domain=domain)
    plan, pv, specs = make_plan(db, project, tasks)
    fakes.approved[plan.id] = pv.id
    if fakes.route_key is None:
        make_route(db)
        fakes.route_key = "worker.test-free"
        seed_and_enable_implementer(db)
        process_sandbox_exception(db)
        if domain != "PERSONAL":
            process_sandbox_exception(db, domain=domain)
    scheduler.ensure_executions(db, pv.id, actor="test", requested_by="owner")
    scheduler.refresh_readiness(db, pv.id)
    return specs


def _enable(db, agent_key: str) -> AgentDefinition:
    a = db.scalar(select(AgentDefinition).where(AgentDefinition.agent_key == agent_key))
    assert a is not None
    a.enabled = True
    db.flush()
    return a


# ----------------------------------------------------------------------------- catalog
def test_category_agents_serve_exactly_the_briefed_roles_and_every_role_exists():
    defs = _defs()
    for key, roles in EXPECTED_ROLES.items():
        assert set(defs[key]["allowed_scope"]["roles"]) == roles, key
    assert role_agents.catalog_problems() == []
    assert role_agents.resolve("feature-implementer").agent_key == catalog.IMPLEMENTER_AGENT_KEY
    # 40 seeded roles, 26 executor roles: every executor role but the deterministic verifier has a worker agent
    assert len(catalog.ALL_ROLES) == 40 and len(catalog.EXECUTOR_ROLE_KEYS) == 26
    unmapped = {r for r in catalog.EXECUTOR_ROLE_KEYS if role_agents.agent_key_for_role(r) is None}
    assert unmapped == set(role_agents.UNMAPPED_EXECUTOR_ROLES) == {"independent-verifier"}
    adr = ADR.read_text(encoding="utf-8")
    assert all(f"`{r}`" in adr for r in unmapped), "every unmapped executor role is listed in ADR-0126 with its reason"


def test_catalog_check_bites_on_a_role_that_does_not_exist_and_on_a_role_served_twice():
    defs = copy.deepcopy(catalog.AGENT_DEFINITIONS)
    writer = next(a for a in defs if a["agent_key"] == "writer")
    writer["allowed_scope"]["roles"].append("docs-writer")  # no such role in the 40-role catalog
    assert "agent writer serves role 'docs-writer', which does not exist in the role catalog" in \
        role_agents.catalog_problems(defs)
    defs = copy.deepcopy(catalog.AGENT_DEFINITIONS)
    next(a for a in defs if a["agent_key"] == "coder")["allowed_scope"]["roles"].append("technical-writer")
    assert "role 'technical-writer' is served by more than one agent: coder, writer" in role_agents.catalog_problems(defs)
    defs = copy.deepcopy(catalog.AGENT_DEFINITIONS)
    next(a for a in defs if a["agent_key"] == "writer")["allowed_scope"]["roles"].remove("context-curator")
    assert ("executor role 'context-curator' has no worker agent and is not listed in UNMAPPED_EXECUTOR_ROLES"
            in role_agents.catalog_problems(defs))
    # near miss: moving a role to another agent (still exactly one) is sound
    defs = copy.deepcopy(catalog.AGENT_DEFINITIONS)
    next(a for a in defs if a["agent_key"] == "writer")["allowed_scope"]["roles"].remove("context-curator")
    next(a for a in defs if a["agent_key"] == "researcher")["allowed_scope"]["roles"].append("context-curator")
    assert role_agents.catalog_problems(defs) == []


def test_category_agents_share_the_implementer_worker_configuration_and_are_seeded_disabled(db):
    defs = _defs()
    impl = defs[catalog.IMPLEMENTER_AGENT_KEY]
    for key in catalog.CATEGORY_AGENTS:
        a = defs[key]
        assert (a["harness"], a["tools"], a["limits"], a["deterministic"]) == \
            (impl["harness"], impl["tools"], impl["limits"], impl["deterministic"]), key
        assert {k: v for k, v in a["route_policy"].items() if not k.startswith("preferred_routes")} == \
            impl["route_policy"]
        assert a["route_policy"]["paid_fallback"] is False
        assert {k: v for k, v in a["allowed_scope"].items() if k != "roles"} == impl["allowed_scope"]
        assert a["enabled"] is False
    seed.run(db)
    rows = {a.agent_key: a for a in db.scalars(select(AgentDefinition))}
    assert all(rows[k].enabled is False for k in catalog.CATEGORY_AGENTS)
    assert seed.run(db)["agents_unchanged"] == len(catalog.AGENT_DEFINITIONS)  # idempotent, never enables


def test_implementer_definition_is_unchanged_so_its_doctor_fingerprint_stays():
    from aeos.core.hashing import content_hash

    impl = _defs()[catalog.IMPLEMENTER_AGENT_KEY]
    # revision hash of opencode-implementer on main before ADR-0126 (a change would make worker.agent_definition
    # STALE and CAN_RUN_WORKER re-check on every owner machine)
    assert content_hash({k: impl[k] for k in ("agent_key", *seed._AGENT_FIELDS)}) == \
        "sha256:e19aca6d11217e5992bac4f994a9d2c82ca0e308999207e0b7911689ea963a28"


# ----------------------------------------------------------------------------- preferred routes (contract a)
def test_preferred_routes_are_servable_go_then_free_openrouter_and_never_argon_qwen_or_deepseek():
    for key in catalog.CATEGORY_AGENTS:
        prefs = _defs()[key]["route_policy"]["preferred_routes"]
        assert prefs, key
        go = [p for p in prefs if p.startswith("opencode-go/")]
        free = [p for p in prefs if p.startswith("openrouter/")]
        assert prefs == go + free, "Go routes first, then the free OpenRouter fallback"
        assert {p.removeprefix("opencode-go/") for p in go} == SERVABLE_GO
        assert all(p.endswith(":free") for p in free)
        assert not [p for p in prefs if any(m in p for m in ("argon", "qwen", "deepseek"))]
    assert role_agents.catalog_problems() == []


def test_preferred_route_check_bites_on_argon_qwen_and_deepseek():
    for bad, family in (("opencode-go/gemini-4-argon", "argon"), ("opencode-go/qwen3.8-max", "qwen"),
                        ("opencode-go/deepseek-v4.1-flash", "deepseek")):
        defs = copy.deepcopy(catalog.AGENT_DEFINITIONS)
        next(a for a in defs if a["agent_key"] == "reasoner")["route_policy"]["preferred_routes"].insert(0, bad)
        assert f"agent reasoner prefers route {bad} ({family} is never preferred, ADR-0126)" in \
            role_agents.catalog_problems(defs)
    defs = copy.deepcopy(catalog.AGENT_DEFINITIONS)  # near miss: a servable Go route moved to the front
    prefs = next(a for a in defs if a["agent_key"] == "reasoner")["route_policy"]["preferred_routes"]
    prefs.insert(0, prefs.pop(prefs.index("opencode-go/glm-5.2")))
    assert role_agents.catalog_problems(defs) == []


def test_ranking_data_file_is_the_generator_output_for_the_committed_inputs(tmp_path):
    mod = _ranking_script()
    assert mod.check() == []
    data = json.loads(mod.DATA.read_text(encoding="utf-8"))
    for key, agent in data["agents"].items():
        assert _defs()[key]["route_policy"]["preferred_routes"] == agent["preferred_routes"]
        for e in agent["entries"]:
            for ref in e["source_refs"]:
                assert set(ref) == {"url", "release", "category", "score", "nq", "date"}
                assert ref["release"] == agent["release"]  # one release per category (critic H7)
    # red: a hand edit of one score
    edited = tmp_path / "role_route_preferences.json"
    edited.write_text(mod.DATA.read_text(encoding="utf-8").replace('"score": 39.1', '"score": 49.1', 1),
                      encoding="utf-8")
    findings = mod.check(edited, mod.INPUTS)
    assert len(findings) == 1 and "differs from the generator output for role-ranking-inputs.json at line" in findings[0]
    assert "never edited by hand" in findings[0]
    # near miss: an exact copy
    copy_ok = tmp_path / "copy.json"
    shutil.copyfile(mod.DATA, copy_ok)
    assert mod.check(copy_ok, mod.INPUTS) == []


def test_snapshot_keeps_only_mapped_rows_and_go_allowances_match_the_route_seed():
    from aeos.routing import seed as route_seed

    mod = _ranking_script()
    snap = json.loads(mod.INPUTS.read_text(encoding="utf-8"))
    for release in ("2.0", "v1"):
        assert snap["rows"][release] and all(mod.route_for(release, r["model"]) for r in snap["rows"][release])
    seeded = {m: float(a) for m, _d, a, *_ in (*route_seed.GO_WORKERS, *route_seed.GO_WATCH)}
    for model, allowance in mod.GO_ALLOWANCE.items():
        if model in seeded:
            assert seeded[model] == allowance, model


def test_ranking_buckets_by_noise_band_and_breaks_ties_by_allowance():
    mod = _ranking_script()
    band = mod.noise_band(202)  # Reasoning: about 6.9 points
    order = mod._rank(["opencode-go/glm-5.3-flash", "opencode-go/kimi-k3", "opencode-go/kimi-k3.1-pro",
                       "opencode-go/glm-5.2"],
                      {"opencode-go/kimi-k3.1-pro": 91.1, "opencode-go/kimi-k3": 90.7,
                       "opencode-go/glm-5.3-flash": 77.6}, band)
    assert order == [("opencode-go/kimi-k3.1-pro", 1), ("opencode-go/kimi-k3", 1), ("opencode-go/glm-5.3-flash", 2),
                     ("opencode-go/glm-5.2", None)]
    # within one band the larger allowance goes first, unscored routes always last
    assert mod._rank(["opencode-go/kimi-k3.1-pro", "opencode-go/glm-5.3-flash"],
                     {"opencode-go/kimi-k3.1-pro": 39.1, "opencode-go/glm-5.3-flash": 19.0}, mod.noise_band(18))[0] == \
        ("opencode-go/glm-5.3-flash", 1)


# ----------------------------------------------------------------------------- scheduler (item 3)
def test_writer_role_task_is_never_given_to_agentic_coder(db, fakes):
    specs = _ready(db, fakes, [{"task_key": "doc", "role_key": "technical-writer"}])
    _enable(db, "agentic-coder")
    te = db.scalar(select(TaskExecution).where(TaskExecution.task_id == specs[0].id))
    assert te.assigned_agent_key == "writer"
    adm = scheduler.admission_check(db, specs[0].id)
    assert not adm.admitted and adm.agent is not None and adm.agent.agent_key == "writer"
    assert "AGENT_DISABLED: agent writer is disabled: Nonaktif sampai pemilik mengaktifkannya" in "; ".join(adm.reasons)
    # near miss: the writer agent enabled -> the run is the writer's, never agentic-coder's or the implementer's
    _enable(db, "writer")
    run = scheduler.acquire_lease(db, specs[0].id, actor="t")
    assert run.agent_key == "writer" and run.role_key == "technical-writer"


def test_disabled_category_agent_waits_with_agent_disabled_and_never_falls_back_to_the_implementer(db, fakes):
    specs = _ready(db, fakes, [{"task_key": "api", "role_key": "backend-api-engineer"},
                               {"task_key": "feat", "role_key": "feature-implementer"}])
    adm = scheduler.admission_check(db, specs[0].id)
    assert adm.agent is not None and adm.agent.agent_key == "agentic-coder"
    assert [c for c, _ in adm.waits if c == "AGENT_DISABLED"] == ["AGENT_DISABLED"]
    assert "agent agentic-coder is disabled" in "; ".join(adm.reasons)
    assert "opencode-implementer" not in "; ".join(adm.reasons)
    # near miss: the implementer's own role is admitted on the (enabled) implementer
    near = scheduler.admission_check(db, specs[1].id)
    assert near.admitted, near.reasons
    assert near.agent is not None and near.agent.agent_key == "opencode-implementer"


def test_a_stale_implementer_assignment_does_not_override_the_role_map(db, fakes):
    """An execution enqueued before ADR-0126 carries opencode-implementer; admission follows the role map."""
    specs = _ready(db, fakes, [{"task_key": "doc", "role_key": "technical-writer"}])
    te = db.scalar(select(TaskExecution).where(TaskExecution.task_id == specs[0].id))
    te.assigned_agent_key = catalog.IMPLEMENTER_AGENT_KEY
    db.flush()
    adm = scheduler.admission_check(db, specs[0].id)
    assert not adm.admitted and adm.agent is not None and adm.agent.agent_key == "writer"


def test_unmapped_role_is_blocked_with_role_unknown_never_given_to_the_implementer(db, fakes, monkeypatch):
    writer = next(a for a in catalog.AGENT_DEFINITIONS if a["agent_key"] == "writer")
    monkeypatch.setitem(writer["allowed_scope"], "roles",
                        [r for r in writer["allowed_scope"]["roles"] if r != "context-curator"])
    specs = _ready(db, fakes, [{"task_key": "ctx", "role_key": "context-curator"},
                               {"task_key": "doc", "role_key": "technical-writer"}])
    te = db.scalar(select(TaskExecution).where(TaskExecution.task_id == specs[0].id))
    assert te.assigned_agent_key is None
    assert te.state == "BLOCKED" and te.reason_code == "ROLE_UNKNOWN"
    assert "no agent definition serves role 'context-curator'" in te.reason
    assert "never given to opencode-implementer by default" in te.reason
    # admission refuses it as well (a task that was READY before its role lost its agent)
    te.state = "READY"
    db.flush()
    adm = scheduler.admission_check(db, specs[0].id)
    assert adm.agent is None and not adm.admitted
    assert [w for w in adm.blocks if w[0] == "ROLE_UNKNOWN"] and "AGENT_DISABLED" not in [c for c, _ in adm.waits]
    # near miss: a mapped role of the same plan is READY
    near = db.scalar(select(TaskExecution).where(TaskExecution.task_id == specs[1].id))
    assert near.state == "READY" and near.assigned_agent_key == "writer"


def test_verifier_role_is_refused_as_a_worker_task(db, fakes):
    specs = _ready(db, fakes, [{"task_key": "v", "role_key": "independent-verifier"}])
    te = db.scalar(select(TaskExecution).where(TaskExecution.task_id == specs[0].id))
    assert te.state == "BLOCKED" and te.reason_code == "ROLE_UNKNOWN"
    assert "served by agent independent-verifier, which is not an OpenCode worker agent" in te.reason


def test_repair_stays_on_the_role_agent(db, fakes):
    specs = _ready(db, fakes, [{"task_key": "doc", "role_key": "technical-writer"}])
    _enable(db, "writer")
    tid = specs[0].id
    run = scheduler.acquire_lease(db, tid, actor="t")
    scheduler.transition_task(db, tid, "RUNNING", reason="b", actor="t")
    scheduler.set_run_status(db, run, "RUNNING", reason="b", actor="t")
    scheduler.transition_task(db, tid, "REVIEW", reason="exit 0", actor="t")
    scheduler.set_run_status(db, run, "REVIEW", reason="exit 0", actor="t")
    te = scheduler.request_repair(db, tid, findings=["fix"], review_record_id=None, actor="reviewer")
    assert te.state == "READY" and te.assigned_agent_key == "writer"


def _reassign_audits(db, task_id: str) -> list[AuditEvent]:
    return list(db.scalars(select(AuditEvent).where(AuditEvent.action == "workforce.task.reassign",
                                                    AuditEvent.target_id == task_id)))


def test_reassign_to_an_agent_outside_the_role_is_refused_not_silently_ignored(db, fakes):
    """A reassign the role map would override is refused (AGENT_OUT_OF_SCOPE), never accepted and audited."""
    specs = _ready(db, fakes, [{"task_key": "doc", "role_key": "technical-writer"}])
    tid = specs[0].id
    _enable(db, "agentic-coder")
    for other in ("agentic-coder", catalog.IMPLEMENTER_AGENT_KEY):
        with pytest.raises(ValidationFailed) as exc:
            scheduler.reassign_task(db, tid, agent_key=other, actor="user:o", reason="writer disabled, use coder")
        assert exc.value.code == "AGENT_OUT_OF_SCOPE"
        assert f"agent {other} does not serve role 'technical-writer'" in str(exc.value)
        assert "run only on writer" in str(exc.value)
    te = db.scalar(select(TaskExecution).where(TaskExecution.task_id == tid))
    assert te.state == "READY" and te.assigned_agent_key == "writer"
    assert _reassign_audits(db, tid) == []
    # a live attempt is not stopped by a refused reassign
    _enable(db, "writer")
    run = scheduler.acquire_lease(db, tid, actor="t")
    with pytest.raises(ValidationFailed):
        scheduler.reassign_task(db, tid, agent_key="agentic-coder", actor="user:o")
    assert db.get(Lease, run.lease_id).state == "ACTIVE"
    # near miss: a reassign to the role's own agent restarts the attempt on it
    te = scheduler.reassign_task(db, tid, agent_key="writer", actor="user:o", reason="restart")
    assert te.state == "READY" and te.assigned_agent_key == "writer"
    assert db.get(Lease, run.lease_id).state == "REVOKED"


def test_reassign_of_a_task_whose_role_has_no_agent_is_refused_with_role_unknown(db, fakes, monkeypatch):
    writer = next(a for a in catalog.AGENT_DEFINITIONS if a["agent_key"] == "writer")
    monkeypatch.setitem(writer["allowed_scope"], "roles",
                        [r for r in writer["allowed_scope"]["roles"] if r != "context-curator"])
    specs = _ready(db, fakes, [{"task_key": "ctx", "role_key": "context-curator"}])
    with pytest.raises(Blocked) as exc:
        scheduler.reassign_task(db, specs[0].id, agent_key=catalog.IMPLEMENTER_AGENT_KEY, actor="user:o")
    assert exc.value.code == "ROLE_UNKNOWN"
    assert "no agent definition serves role 'context-curator'" in str(exc.value)
    assert _reassign_audits(db, specs[0].id) == []
    # near miss: the same reassign of the implementer's own role is accepted and audited
    near = _ready(db, fakes, [{"task_key": "feat", "role_key": "feature-implementer"}])
    te = scheduler.reassign_task(db, near[0].id, agent_key=catalog.IMPLEMENTER_AGENT_KEY, actor="user:o", reason="x")
    assert te.assigned_agent_key == catalog.IMPLEMENTER_AGENT_KEY
    assert len(_reassign_audits(db, near[0].id)) == 1


def test_role_map_is_the_same_for_both_domains(db, fakes):
    """L12: the map is domain-independent; an OFFICE task resolves the same agent and still waits for it."""
    personal = _ready(db, fakes, [{"task_key": "p", "role_key": "technical-writer"}])
    office = _ready(db, fakes, [{"task_key": "o", "role_key": "technical-writer"}], domain="OFFICE")
    adm_p = scheduler.admission_check(db, personal[0].id)
    adm_o = scheduler.admission_check(db, office[0].id)
    assert adm_p.agent is not None and adm_o.agent is not None
    assert adm_p.agent.agent_key == adm_o.agent.agent_key == "writer"
    assert "OFFICE" not in "; ".join(r for r in adm_p.reasons if r.startswith("AGENT_"))


# ----------------------------------------------------------------------------- invariants (TRIAL binding)
def _runtime_identity() -> tuple[str | None, str | None, str | None]:
    from aeos.routing.service import current_policy_version, current_worker_runtime
    from aeos.worker_runtime.executor import worker_prompt_revision

    rt = current_worker_runtime()
    return worker_prompt_revision(), current_policy_version(), rt.get("context_strategy")


def test_category_agents_leave_prompt_revision_policy_version_and_context_strategy_byte_identical(db, monkeypatch):
    before = _runtime_identity()
    assert all(before)
    seed.run(db)
    for key in catalog.CATEGORY_AGENTS:
        _enable(db, key)
    assert _runtime_identity() == before
    # red: a change of the worker run message (one of the revision's inputs) is a different revision
    from aeos.worker_runtime import opencode

    monkeypatch.setattr(opencode, "RUN_MESSAGE", opencode.RUN_MESSAGE + " ")
    assert _runtime_identity()[0] != before[0]
    assert _runtime_identity()[1:] == before[1:]


# ----------------------------------------------------------------------------- HARD_MAX_ACTIVE (one source)
VALIDATORS = (("scheduler", "backend/aeos/workforce/scheduler.py"), ("cli", "backend/aeos/workforce/cli.py"),
              ("router", "backend/aeos/workforce/router.py"))


def literal_bounds(name: str, source: str) -> list[str]:
    """Findings: a max_active validator that states its upper bound as a number instead of HARD_MAX_ACTIVE."""
    out: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in ("le", "max", "lt") and isinstance(kw.value, ast.Constant) and \
                        isinstance(kw.value.value, int):
                    out.append(f"{name}: max_active bound {kw.arg}={kw.value.value} is a literal, not HARD_MAX_ACTIVE")
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "max_active":
            for comp in node.comparators:
                if isinstance(comp, ast.Tuple) or (isinstance(comp, ast.Constant) and isinstance(comp.value, int)
                                                   and comp.value > 1):
                    out.append(f"{name}: max_active compared with a literal, not HARD_MAX_ACTIVE")
    return out


def _concurrency_sources() -> dict[str, str]:
    from aeos.workforce import cli, router

    return {"scheduler": inspect.getsource(scheduler.set_concurrency), "cli": inspect.getsource(cli.concurrency),
            "router": inspect.getsource(router.ConcurrencyIn)}


def test_hard_max_active_is_the_one_source_of_every_concurrency_validator(db):
    from aeos.workforce import cli, router

    assert HARD_MAX_ACTIVE == 2 and scheduler.HARD_MAX_ACTIVE is HARD_MAX_ACTIVE
    for name, src in _concurrency_sources().items():
        assert literal_bounds(name, src) == [], name
        assert "HARD_MAX_ACTIVE" in src, name
    meta = router.ConcurrencyIn.model_fields["max_active"].metadata
    assert any(getattr(m, "le", None) == HARD_MAX_ACTIVE for m in meta)
    assert inspect.signature(cli.concurrency).parameters["max_active"].default.max == HARD_MAX_ACTIVE
    with pytest.raises(ValidationFailed, match=f"between 1 and {HARD_MAX_ACTIVE}"):
        scheduler.set_concurrency(db, max_active=HARD_MAX_ACTIVE + 1, actor="t", actor_user_id="u", rationale="x" * 20)


def test_literal_bound_check_bites():
    red = "class ConcurrencyIn(BaseModel):\n    max_active: int = Field(ge=1, le=2)\n"
    assert literal_bounds("router", red) == ["router: max_active bound le=2 is a literal, not HARD_MAX_ACTIVE"]
    red_sched = "def f(max_active):\n    if max_active not in (1, 2):\n        raise X\n"
    assert literal_bounds("scheduler", red_sched) == ["scheduler: max_active compared with a literal, not HARD_MAX_ACTIVE"]
    near = "class ConcurrencyIn(BaseModel):\n    max_active: int = Field(ge=1, le=HARD_MAX_ACTIVE)\n"
    assert literal_bounds("router", near) == []


# ----------------------------------------------------------------------------- owner command dry run (L13)
def test_owner_agent_enable_of_a_category_agent_dry_run(db, monkeypatch):
    """docs/OWNER_ACTIONS.md 1.11: `aeos work agent-prereqs <agent>` then `agent-enable`, run in-process against a
    temporary database. Red: prerequisites missing -> refused and audited; near miss: ready -> enabled, and only
    that agent changes."""
    from typer.testing import CliRunner

    from aeos.cli import app
    from aeos.routing import facade as routing
    from aeos.routing.facade import RouteDecision
    from aeos.workforce import agents
    from tests.conftest import ADMIN_PW, create_admin
    from tests.unit.test_workforce_controls import _ready_prereqs

    runner = CliRunner()
    seed.run(db)
    make_route(db)
    db.commit()
    create_admin(db)
    cmd = ["work", "agent-enable", "writer", "--as", "owner", "--domain", "PERSONAL", "--reason", "writer roles"]
    monkeypatch.setattr(routing, "eligible_routes", lambda session, **kw: RouteDecision(route=None, eligible=[]))
    r = runner.invoke(app, cmd, input=ADMIN_PW + "\n")
    assert r.exit_code == 1 and "AGENT_PREREQUISITES" in r.output
    db.expire_all()
    assert agents.get_agent(db, "writer").enabled is False
    _ready_prereqs(monkeypatch)
    monkeypatch.setattr(routing, "eligible_routes", lambda session, **kw: RouteDecision(route=None, eligible=["x"]))
    pre = runner.invoke(app, ["work", "agent-prereqs", "writer", "--domain", "PERSONAL"])
    assert pre.exit_code == 0 and "READY to enable" in pre.output
    r = runner.invoke(app, cmd, input=ADMIN_PW + "\n")
    assert r.exit_code == 0, r.output
    db.expire_all()
    enabled = {a.agent_key for a in db.scalars(select(AgentDefinition).where(AgentDefinition.enabled.is_(True)))}
    assert enabled == {"writer"}
