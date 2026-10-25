"""ContextPack/MemoryRecord hardening found in the completeness review (§13, §24, §50 memory rows)."""

from __future__ import annotations

from datetime import timedelta

from aeos.core.enums import Domain
from aeos.core.hashing import content_hash
from aeos.core.timeutil import utcnow
from aeos.knowledge import memory as memory_mod
from aeos.models.identity import User
from aeos.models.knowledge_ext import ContextPackMeta
from aeos.models.planning import Plan, PlanVersion, TaskSpec
from aeos.models.portfolio import Project
from aeos.models.research import ContextPack, MemoryRecord
from tests.conftest import VIEWER_PW, AuthedClient, create_viewer
from tests.unit.test_portfolio_support import create_project


def _build(client: AuthedClient, pid: str, **body) -> dict:
    r = client.post(f"/api/projects/{pid}/context-packs", json=body)
    assert r.status_code == 200, r.text
    return r.json()["context_pack"]


def _task(db, pid: str, key: str = "T1") -> TaskSpec:
    plan = Plan(project_id=pid, domain="PERSONAL", title="Plan")
    db.add(plan)
    db.flush()
    content = {"tasks": [key]}
    pv = PlanVersion(plan_id=plan.id, project_id=pid, domain="PERSONAL", version=1, content=content,
                     manifest_hash=content_hash(content))
    db.add(pv)
    db.flush()
    task = TaskSpec(plan_version_id=pv.id, plan_version_hash=pv.manifest_hash, project_id=pid, domain="PERSONAL",
                    task_key=key, title="Task", goal="Do the thing", spec_hash="sha256:" + key)
    db.add(task)
    db.commit()
    return task


def test_pack_has_ttl_and_expires(personal_client: AuthedClient, db):
    """§13 ContextPack TTL/expiry: an expired pack is STALE and must be rebuilt."""
    p = create_project(personal_client, name="TTL Pack")
    pack = _build(personal_client, p["id"], ttl_hours=2)
    assert pack["expires_at"] and pack["freshness"] == "CURRENT"
    for bad in (0, -1, 9999):
        r = personal_client.post(f"/api/projects/{p['id']}/context-packs", json={"ttl_hours": bad})
        assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_TTL"
    meta = db.get(ContextPackMeta, pack["id"])
    meta.expires_at = utcnow() - timedelta(minutes=1)
    db.commit()
    got = personal_client.get(f"/api/context-packs/{pack['id']}").json()["context_pack"]
    assert got["freshness"] == "STALE" and "TTL expired" in got["stale_reason"]
    db.expire_all()
    assert db.get(ContextPack, pack["id"]).stale is True  # sticky


def test_newer_task_pack_supersedes_older(personal_client: AuthedClient, db):
    """§13 ContextPack supersession: a new pack for the same task/role makes the old one STALE."""
    p = create_project(personal_client, name="Supersede Pack")
    task = _task(db, p["id"])
    first = _build(personal_client, p["id"], task_id=task.id, role_key="reviewer")
    other_role = _build(personal_client, p["id"], task_id=task.id, role_key="planner")
    second = _build(personal_client, p["id"], task_id=task.id, role_key="reviewer")
    old = personal_client.get(f"/api/context-packs/{first['id']}").json()["context_pack"]
    assert old["freshness"] == "STALE" and old["superseded_by"] == second["id"]
    assert second["id"] in old["stale_reason"]
    assert personal_client.get(f"/api/context-packs/{other_role['id']}").json()["context_pack"]["stale"] is False
    assert personal_client.get(f"/api/context-packs/{second['id']}").json()["context_pack"]["stale"] is False
    # ad-hoc packs (no TaskSpec) are separate explorations and do not supersede each other
    a = _build(personal_client, p["id"], query=["alpha"])
    _build(personal_client, p["id"], query=["beta"])
    assert personal_client.get(f"/api/context-packs/{a['id']}").json()["context_pack"]["stale"] is False


def test_added_policy_file_marks_pack_stale(personal_client: AuthedClient, monkeypatch):
    """§24 policy context: the whole policy file set is snapshotted; an added file is a change."""
    from aeos.knowledge import packs

    p = create_project(personal_client, name="Policy Set")
    pack = _build(personal_client, p["id"])
    real = packs.policy_hashes()
    monkeypatch.setattr(packs, "policy_hashes", lambda: {**real, "new-rule.yaml": "sha256:new"})
    got = personal_client.get(f"/api/context-packs/{pack['id']}").json()["context_pack"]
    assert got["freshness"] == "STALE" and "policy/new-rule.yaml was added" in got["stale_reason"]


def test_shared_memory_respects_principal_project_scope(admin_client: AuthedClient, db):
    """§24/§35: a project-scoped principal never receives another project's DOMAIN-scope memory."""
    admin_client.select_domain(Domain.PERSONAL)
    a = create_project(admin_client, name="Mem Scope A")
    b = create_project(admin_client, name="Mem Scope B")
    db.add(MemoryRecord(project_id=a["id"], domain="PERSONAL", scope="DOMAIN", content="Shared fact from A",
                        classification="INTERNAL", status="ACCEPTED"))
    db.commit()
    proj_b = db.get(Project, b["id"])
    assert [r.content for r in memory_mod.usable_for_context(db, proj_b)] == ["Shared fact from A"]
    assert memory_mod.usable_for_context(db, proj_b, allowed_project_ids=[b["id"]]) == []
    assert len(memory_mod.usable_for_context(db, proj_b, allowed_project_ids=[a["id"], b["id"]])) == 1


def test_pack_metadata_is_project_scoped(personal_client: AuthedClient, db):
    """Metadata context comes from this project's records plus global ones only (no sibling projects). Both plans
    are approved: only accepted records are project context (§24, test_knowledge_precedence)."""
    a = create_project(personal_client, name="Meta A")
    b = create_project(personal_client, name="Meta B")
    db.add_all([Plan(project_id=a["id"], domain="PERSONAL", title="Orion rollout for A", state="APPROVED"),
                Plan(project_id=b["id"], domain="PERSONAL", title="Orion rollout for B", state="APPROVED")])
    db.commit()
    pack = _build(personal_client, a["id"], include=["metadata"], query=["orion"])
    titles = [i["title"] for i in pack["items"] if i["kind"] == "metadata"]
    assert titles == ["Orion rollout for A"]


def test_viewer_scope_applies_to_pack_reads(client: AuthedClient, db):
    """A project-scoped VIEWER cannot open a pack of a project outside its scope (404, no leak)."""
    from tests.conftest import create_admin

    create_admin(db)
    client.login("owner", "correct-horse-battery-staple")
    client.select_domain(Domain.PERSONAL)
    a = create_project(client, name="Pack Scope A")
    b = create_project(client, name="Pack Scope B")
    pack_b = _build(client, b["id"])
    viewer = create_viewer(db)
    u = db.get(User, viewer.id)
    u.project_scope = [a["id"]]
    db.commit()
    client.post("/api/auth/logout")
    client.csrf = None
    client.login("viewer", VIEWER_PW)
    client.select_domain(Domain.PERSONAL)
    assert client.get(f"/api/context-packs/{pack_b['id']}").status_code == 404
    assert client.get(f"/api/projects/{b['id']}/context-packs").status_code == 404
