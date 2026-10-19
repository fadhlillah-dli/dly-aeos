"""§45 selective skill-pack import after review/license/pin; §50 T2 2.13 "Candidate skill meminta auto-update
policy -> ditolak; perubahan masuk review/versioned promotion"."""

from __future__ import annotations

import copy

from sqlalchemy import select

from aeos.models.decisions import Decision
from aeos.models.workforce import RoleSkillPack
from aeos.workforce import catalog, roles

PACK = {
    "role_key": "ext-api-reviewer", "name": "API Contract Helper", "group": "fe-api-data",
    "description": "Checks API changes against the contract",
    "inputs": ["Kontrak API"], "outputs": ["Daftar perbedaan kontrak"], "boundaries": ["Read-only"],
    "forbidden_actions": list(catalog.COMMON_FORBIDDEN), "verification": ["Contract test"],
    "failure_modes": ["Perbedaan tidak dilaporkan"],
}
BODY = {"pack": PACK, "source_url": "https://github.com/example/agency-agents", "commit_pin": "a" * 40,
        "license_spdx": "MIT", "rationale": "dipilih setelah review lisensi dan isi"}


def test_pack_requesting_auto_update_or_hooks_is_refused(personal_client, db):
    for field in ({"auto_update": True}, {"hooks": {"post": "curl x"}}, {"settings": {"policy": "open"}},
                  {"meta": {"permissions": ["merge"]}}):
        body = copy.deepcopy(BODY)
        body["pack"].update(field)
        r = personal_client.post("/api/workforce/role-packs/import", json=body)
        assert r.status_code == 422 and r.json()["error"]["code"] == "SKILL_PACK_FORBIDDEN_FIELD", field
    assert db.scalar(select(RoleSkillPack).where(RoleSkillPack.role_key == "ext-api-reviewer")) is None


def test_reviewed_pinned_licensed_pack_becomes_a_versioned_candidate(personal_client, db):
    r = personal_client.post("/api/workforce/role-packs/import", json=BODY)
    assert r.status_code == 201, r.text
    assert r.json()["mode"] == "CANDIDATE" and r.json()["eval_status"] == "NOT_EVALUATED" and r.json()["version"] == "1"
    d = db.scalar(select(Decision).where(Decision.target_type == "role_skill_pack"))
    assert d.kind == "PROMOTION" and d.actor_kind == "HUMAN" and d.target_hash == r.json()["revision_hash"]
    assert "ext-api-reviewer" not in roles.executor_role_keys()  # a candidate never runs before review/eval
    again = personal_client.post("/api/workforce/role-packs/import", json=BODY)
    assert again.status_code == 409 and again.json()["error"]["code"] == "SKILL_PACK_UNCHANGED"
    changed = copy.deepcopy(BODY)
    changed["pack"]["outputs"] = ["Daftar perbedaan kontrak", "Saran kompatibilitas"]
    changed["commit_pin"] = "b" * 40
    r2 = personal_client.post("/api/workforce/role-packs/import", json=changed)
    assert r2.status_code == 201 and r2.json()["version"] == "2"  # versioned promotion, never a silent update
    assert db.scalar(select(Decision).where(Decision.target_hash == r2.json()["revision_hash"])) is not None


def test_unpinned_or_unlicensed_pack_is_refused(personal_client):
    for patch, code in (({"commit_pin": "main".ljust(40, "x")}, "SKILL_PACK_PIN"),
                        ({"license_spdx": "UNKNOWN"}, "SKILL_PACK_LICENSE"),
                        ({"source_url": "http://insecure.example/x"}, "SKILL_PACK_SOURCE")):
        r = personal_client.post("/api/workforce/role-packs/import", json={**BODY, **patch})
        assert r.status_code == 422 and r.json()["error"]["code"] == code, patch
    body = copy.deepcopy(BODY)
    body["pack"]["forbidden_actions"] = ["Mengubah policy/MCI"]
    r = personal_client.post("/api/workforce/role-packs/import", json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "SKILL_PACK_INVALID"
