"""Completeness-review regressions for Knowledge & Context (§24 "Runtime context").

Runtime context = test logs/metrics/traces collected by trusted tools and already reduced/redacted.
Worker-reported evidence ("all tests pass") is never runtime context (§34, §50 "Worker exit 0 /
menulis 'all tests pass'"), other projects/domains never leak in, and MOCK/DEMO evidence keeps its
label.
"""

from __future__ import annotations

from aeos.models.evidence import EvidenceArtifact
from tests.conftest import AuthedClient
from tests.unit.test_portfolio_support import create_project

TOKEN = "ghp_" + "R" * 36


def _build(client: AuthedClient, pid: str, **body) -> dict:
    r = client.post(f"/api/projects/{pid}/context-packs", json=body)
    assert r.status_code == 200, r.text
    return r.json()["context_pack"]


def test_runtime_context_only_from_trusted_collectors(personal_client: AuthedClient, db):
    p = create_project(personal_client, name="Runtime Ctx")
    other = create_project(personal_client, name="Runtime Other")

    def ev(**kw) -> EvidenceArtifact:
        base = {"project_id": p["id"], "domain": "PERSONAL", "kind": "TEST_REPORT", "origin": "verifier",
                "collector_identity": "aeos-verifier", "trusted": True, "evidence_level": "E2"}
        base.update(kw)
        return EvidenceArtifact(**base)

    verifier = ev(content_hash="sha256:a", inline={"passed": 10, "failed": 1, "note": f"token {TOKEN}"},
                  environment={"commit": "abc123"}, claim="pytest tests/unit")
    worker = ev(origin="worker", trusted=False, kind="LOG", content_hash="sha256:b",
                inline={"claim": "all tests pass"})
    mock = ev(origin="collector", kind="PROBE", mode="MOCK", content_hash="sha256:c", inline={"ok": True})
    foreign = ev(project_id=other["id"], content_hash="sha256:d", inline={"other": True})
    wrong_domain = ev(domain="OFFICE", content_hash="sha256:e", inline={"office": True})
    db.add_all([verifier, worker, mock, foreign, wrong_domain])
    db.commit()

    pack = _build(personal_client, p["id"], role_key="reviewer")
    runtime = [i for i in pack["items"] if i["kind"] == "runtime"]
    assert {i["provenance"]["evidence_id"] for i in runtime} == {verifier.id, mock.id}
    assert all(i["trust"] == "RUNTIME_EVIDENCE" for i in runtime)
    by_id = {i["provenance"]["evidence_id"]: i for i in runtime}
    assert by_id[mock.id]["title"].startswith("[MOCK]") and by_id[mock.id]["provenance"]["mode"] == "MOCK"
    assert by_id[verifier.id]["provenance"]["content_hash"] == "sha256:a"
    assert "abc123" in by_id[verifier.id]["content"]
    assert TOKEN not in str(pack) and "all tests pass" not in str(pack)

    # the default role does not pull runtime evidence; an explicit include does
    assert not [i for i in _build(personal_client, p["id"])["items"] if i["kind"] == "runtime"]
    explicit = _build(personal_client, p["id"], include=["runtime"])
    assert {i["provenance"]["evidence_id"] for i in explicit["items"] if i["kind"] == "runtime"} == {verifier.id, mock.id}
