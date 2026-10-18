"""§38 auditable reconciliation and §36 drift classes, with fake read-only probes.

§50 rows covered:
* "Tool ditemukan dalam README atau lockfile" -> declaration (build dependency), not a running process.
* "Tool installed dan enabled tanpa run yang teramati" -> not IN_USE.
* "Binary/model route/config berganti dari approved pin" -> drift visible.
* "Komponen ditemukan di managed scope tanpa registrasi" -> UNREGISTERED observation, not adopted.
* "Inventory/stream gagal atau offline" -> UNKNOWN, partial status kept per component.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from aeos.core.errors import NotFound, ValidationFailed
from aeos.inventory.observations import record_observation
from aeos.inventory.probes import HttpResult, logical_location
from aeos.inventory.reconcile import reconcile
from aeos.inventory.seed import run as seed_run
from aeos.models.audit import Event
from aeos.models.inventory import ComponentObservation, ComponentReference, ComponentRelation
from aeos.models.inventory_ext import DriftFinding, ReconciliationRun
from tests.unit.test_inventory_fakes import base_runner, ok


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A tiny managed repo: pyproject, package-lock, a workflow and an MCP config."""
    root = tmp_path / "repo"
    (root / "frontend").mkdir(parents=True)
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "dly-aeos"\nversion = "0.1.0"\ndependencies = ["keyring>=25", "httpx>=0.27"]\n'
        '[project.optional-dependencies]\ndev = ["pytest>=8"]\n', encoding="utf-8")
    lock = {"lockfileVersion": 3, "packages": {
        "": {"dependencies": {"react": "^19.0.0"}, "devDependencies": {"vitest": "^5.0.0"}},
        "node_modules/react": {"version": "19.3.1"}, "node_modules/vitest": {"version": "5.0.5"},
        "node_modules/loose-envify": {"version": "1.4.0"}}}
    (root / "frontend" / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    (root / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  a:\n    steps:\n      - uses: actions/checkout@" + "b" * 40 + "\n      - uses: actions/setup-node@v4\n",
        encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"mystery-server": {"command": "npx"},
                                                               "context7": {"command": "npx"}}}), encoding="utf-8")
    return root


@pytest.fixture
def seeded(db):
    seed_run(db)
    db.commit()
    return db


def _open(db, cid: str | None = None, cls: str | None = None) -> list[DriftFinding]:
    q = select(DriftFinding).where(DriftFinding.status == "OPEN")
    if cid:
        q = q.where(DriftFinding.component_id == cid)
    if cls:
        q = q.where(DriftFinding.drift_class == cls)
    return list(db.scalars(q))


def test_reconcile_records_observations_with_identity_scope_expiry(seeded, settings, repo):
    runner = base_runner()
    runner.add_binary("node", "v24.15.0")
    run = reconcile(seeded, settings, runner=runner, repo=repo, actor="tester")
    seeded.commit()
    assert run.status in ("COMPLETED", "PARTIAL")
    obs = seeded.scalar(select(ComponentObservation).where(ComponentObservation.component_id == "nodejs-runtime",
                                                           ComponentObservation.dimension == "installation"))
    assert obs.status == "INSTALLED" and obs.observed_version == "24.15.0"
    assert obs.observer == "reconciler" and obs.expires_at is not None and obs.expires_at > obs.observed_at
    assert obs.operation_ref == "node --version"
    assert obs.scope.get("run_id") == run.id
    assert obs.observed_location and not obs.observed_location.startswith("/opt")  # logical, not raw path
    repo_obs = seeded.scalar(select(ComponentObservation).where(
        ComponentObservation.component_id == "dly-aeos-platform-repo", ComponentObservation.dimension == "configuration"))
    assert repo_obs.status == "CONFIGURED" and repo_obs.observed_identity == "github:fadhlillah-dli/dly-aeos"
    events = seeded.scalar(select(func.count()).select_from(Event).where(Event.kind == "inventory.observation"))
    assert events and events >= run.observations_recorded - 1
    assert seeded.scalar(select(func.count()).select_from(Event).where(Event.kind == "inventory.reconcile.completed")) == 1


def test_lockfile_and_config_reads_are_declarations_not_processes(seeded, settings, repo):
    reconcile(seeded, settings, runner=base_runner(), repo=repo)
    seeded.commit()
    decl = seeded.scalar(select(ComponentObservation).where(ComponentObservation.component_id == "control-center-frontend",
                                                            ComponentObservation.dimension == "declaration"))
    assert decl.status == "DECLARED"
    assert decl.detail["transitive_packages"] == 1
    assert {p["name"] for p in decl.detail["packages"]} == {"react", "vitest"}
    vitest = seeded.scalar(select(ComponentObservation).where(ComponentObservation.component_id == "vitest",
                                                              ComponentObservation.dimension == "declaration"))
    assert vitest.observed_version == "5.0.5"
    actions = seeded.scalar(select(ComponentObservation).where(ComponentObservation.component_id == "github-actions"))
    assert actions.detail["unpinned"] == ["actions/setup-node"]
    usage = seeded.scalar(select(func.count()).select_from(ComponentObservation).where(
        ComponentObservation.dimension == "usage"))
    assert usage == 0  # nothing claims IN_USE from a file read
    run = seeded.scalar(select(ReconciliationRun))
    srcs = {s["source"]: s["status"] for s in run.sources}
    assert srcs["requirements.lock"] == "MISSING"  # coverage gap, not "nothing there"
    assert srcs["frontend/package-lock.json"] == "READ"


def test_every_drift_class_is_produced(seeded, settings, repo):
    # resolved-version-mismatch: git pinned to a different version
    git = seeded.get(ComponentReference, "git")
    git.approved_pin = "2.40.0"
    # enabled-but-unverified: claude-code enabled by decision, no functional proof
    claude = seeded.get(ComponentReference, "claude-code")
    claude.enablement = "ENABLED"
    # obsolete-reference: a live CONFIG relation pointing at a tombstoned component
    ralph = seeded.get(ComponentReference, "ralph")
    from aeos.core.timeutil import utcnow

    ralph.tombstoned_at = utcnow()
    seeded.add(ComponentRelation(source_id="dly-aeos-control-plane", target_id="ralph", relation="calls", lens="CONFIG"))
    seeded.commit()
    runner = base_runner()
    runner.outputs[("git", ("--version",))] = ok("git version 2.45.1\n")
    runner.add_binary("claude", "2.1.302 (Claude Code)")
    run = reconcile(seeded, settings, runner=runner, repo=repo)
    seeded.commit()
    classes = {f.drift_class for f in _open(seeded)}
    assert {"planned-but-missing", "installed-but-disabled", "enabled-but-unverified", "resolved-version-mismatch",
            "observed-but-unregistered", "obsolete-reference"} <= classes
    assert _open(seeded, "opencode", "planned-but-missing")  # REQUIRED engine not found
    assert _open(seeded, "git", "resolved-version-mismatch")[0].severity == "HIGH"
    assert _open(seeded, "claude-code", "enabled-but-unverified")
    assert _open(seeded, "keyring-backend", "installed-but-disabled") or _open(seeded, "git", "installed-but-disabled")
    unreg = _open(seeded, "unregistered:mcp:mystery-server")
    assert unreg and unreg[0].drift_class == "observed-but-unregistered"
    # never adopted: no component reference was created for the unregistered server
    assert seeded.get(ComponentReference, "unregistered:mcp:mystery-server") is None
    # context7 is registered: its MCP entry is a declaration, not unregistered
    assert not _open(seeded, "unregistered:mcp:context7")
    assert run.summary["drift"]["new"] >= 6
    assert seeded.scalar(select(func.count()).select_from(Event).where(Event.kind == "inventory.drift.detected")) >= 6


def test_drift_is_resolved_when_later_observation_matches(seeded, settings, repo):
    reconcile(seeded, settings, runner=base_runner(), repo=repo)
    seeded.commit()
    assert _open(seeded, "opencode", "planned-but-missing")
    runner = base_runner()
    runner.add_binary("opencode", "opencode 1.2.3")
    reconcile(seeded, settings, runner=runner, repo=repo, component_id="opencode")
    seeded.commit()
    assert not _open(seeded, "opencode", "planned-but-missing")
    resolved = seeded.scalar(select(DriftFinding).where(DriftFinding.component_id == "opencode",
                                                        DriftFinding.status == "RESOLVED"))
    assert resolved is not None and resolved.resolved_at is not None  # history kept, not deleted


def test_partial_reconcile_keeps_other_components_status(seeded, settings, repo):
    reconcile(seeded, settings, runner=base_runner(), repo=repo)
    seeded.commit()
    before = {f.id for f in _open(seeded)}
    run = reconcile(seeded, settings, runner=base_runner(), repo=repo, component_id="git")
    seeded.commit()
    assert list(run.component_status) == ["git"]
    after = {f.id for f in _open(seeded)}
    resolved = [seeded.get(DriftFinding, fid) for fid in before - after]
    assert all(f.component_id == "git" for f in resolved)  # only the refreshed component can change
    assert _open(seeded, "opencode", "planned-but-missing")  # untouched by a scoped refresh of git
    assert run.scope == {"component_id": "git"}


def test_failing_probe_is_unknown_not_green(seeded, settings, repo):
    runner = base_runner()
    runner.add_binary("node")
    runner.outputs["node"] = RuntimeError("boom")
    run = reconcile(seeded, settings, runner=runner, repo=repo)
    seeded.commit()
    assert run.status == "PARTIAL"
    assert run.component_status["nodejs-runtime"]["status"] == "FAILED"
    obs = seeded.scalar(select(ComponentObservation).where(ComponentObservation.component_id == "nodejs-runtime"))
    assert obs.status == "UNKNOWN"


def test_health_probe_is_loopback_only(seeded, settings, repo, monkeypatch):
    monkeypatch.setattr(settings, "openbao_addr", "https://vault.example.com:8200")
    runner = base_runner()
    reconcile(seeded, settings, runner=runner, repo=repo, component_id="openbao")
    seeded.commit()
    assert runner.http_calls == []  # never left loopback
    conn = seeded.scalar(select(ComponentObservation).where(ComponentObservation.component_id == "openbao",
                                                            ComponentObservation.dimension == "connectivity"))
    assert conn.status == "UNKNOWN"


def test_openbao_health_connected_is_not_verified(seeded, settings, repo):
    runner = base_runner()
    runner.http["http://127.0.0.1:8200/v1/sys/health"] = HttpResult(200, {"initialized": True, "sealed": False,
                                                                          "version": "2.1.0"})
    reconcile(seeded, settings, runner=runner, repo=repo, component_id="openbao")
    seeded.commit()
    conn = seeded.scalar(select(ComponentObservation).where(ComponentObservation.component_id == "openbao",
                                                            ComponentObservation.dimension == "connectivity"))
    assert conn.status == "CONNECTED" and conn.detail["identity_confirmed"] is True
    func_obs = seeded.scalar(select(ComponentObservation).where(ComponentObservation.component_id == "openbao",
                                                                ComponentObservation.dimension == "functional"))
    assert func_obs is None  # CONNECTED never becomes VERIFIED by itself


def test_probes_are_scoped_no_machine_wide_scan(seeded, settings, repo):
    runner = base_runner()
    reconcile(seeded, settings, runner=runner, repo=repo)
    declared = set()
    for c in seeded.scalars(select(ComponentReference)):
        for b in (c.probe or {}).get("binaries") or []:
            declared.add(b["name"])
    assert set(runner.which_calls) <= declared | {"git"}
    for call in runner.calls:
        assert all("ANTHROPIC" not in k for k in call["env"])  # provider vars scrubbed (§12)


def test_claude_probe_env_is_scrubbed(seeded, settings, repo):
    runner = base_runner(env={"ANTHROPIC_API_KEY": "x" * 12, "PATH": "/usr/bin"})
    runner.add_binary("git")
    from tests.unit.test_inventory_fakes import git_handler

    runner.outputs["git"] = git_handler()
    runner.add_binary("claude", "2.1.302 (Claude Code)")
    reconcile(seeded, settings, runner=runner, repo=repo, component_id="claude-code")
    claude_calls = [c for c in runner.calls if c["argv"][0].endswith("claude")]
    assert claude_calls and "ANTHROPIC_API_KEY" not in claude_calls[0]["env"]
    assert "--bare" not in claude_calls[0]["argv"]


def test_unknown_component_and_tombstoned_refresh(seeded, settings, repo):
    with pytest.raises(NotFound):
        reconcile(seeded, settings, runner=base_runner(), repo=repo, component_id="nope")
    from aeos.core.errors import Conflict
    from aeos.core.timeutil import utcnow

    seeded.get(ComponentReference, "ralph").tombstoned_at = utcnow()
    seeded.commit()
    with pytest.raises(Conflict):
        reconcile(seeded, settings, runner=base_runner(), repo=repo, component_id="ralph")


def test_reconcile_loads_desired_state_when_db_is_empty(db, settings, repo):
    run = reconcile(db, settings, runner=base_runner(), repo=repo)
    db.commit()
    assert run.manifest_hash and db.scalar(select(func.count()).select_from(ComponentReference)) >= 100


def test_record_observation_validates_and_emits_event(seeded):
    with pytest.raises(ValidationFailed):
        record_observation(seeded, component_id="git", dimension="installation", status="ACTIVE", observer="t")
    with pytest.raises(ValidationFailed):
        record_observation(seeded, component_id="git", dimension="vibes", status="INSTALLED", observer="t")
    obs = record_observation(seeded, component_id="jira", dimension="connectivity", status="CONNECTED",
                             observer="broker", detail={"token": "abcdefgh", "location": "<home>/.../x"})
    assert obs.detail["token"] == "[REDACTED]" and obs.observed_location == "<home>/.../x"
    ev = seeded.scalar(select(Event).where(Event.kind == "inventory.observation", Event.entity_id == "jira"))
    assert ev.domain == "OFFICE"  # OFFICE-only component events are tagged so Personal streams never see them


def test_logical_location_hides_local_paths(tmp_path: Path):
    home_bin = Path.home() / "AppData" / "x" / "tool.exe"
    assert logical_location(str(home_bin)) == "<home>/.../tool.exe"
    assert logical_location(str(tmp_path / "a" / "b"), repo=tmp_path) == "<repo>/a/b"
    assert logical_location(None) is None
