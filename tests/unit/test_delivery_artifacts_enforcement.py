"""Build once / digest / promotion (master §31, W6-195) and the provider-side enforcement test (§31, §49 M4,
W6-206/W6-139). Every provider call is a GET (respx); nothing here merges, pushes or deploys.

* provider CI artifact digests of a commit (GitHub Actions runs -> artifacts' ``digest`` -> attestations) are
  recorded as provider-ci ARTIFACT_DIGEST evidence; only a successful run's unexpired artifact is eligible;
* the deployment observer compares the digest a target reports; the promotion view is PASS only with G5 and G7
  PASS and every registered target serving the recorded digest;
* GitHub's ruleset evaluation history (rule suites) shows a refused change by an ACTIVE ruleset; only then is
  PROVIDER_ENFORCEMENT_TEST evidence recorded, and ``enforcement_tested`` is VERIFIED for exactly that repository,
  branch and unchanged ruleset (STALE once it changes; an owner attestation is only CLAIMED).
"""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import select

from aeos.delivery import deployment, gates, provider
from aeos.models.delivery_ext import PullRequestRecord
from aeos.models.evidence import EvidenceArtifact
from aeos.verification import acceptance
from tests.unit.test_deployment_support import deploy_target
from tests.unit.test_verification_support import (
    FakeWorkforce,
    install_planning,
    make_review,
    make_world,
    record_release_approvers,
    record_verification,
)

MERGE = "e" * 40
DIGEST = "sha256:" + "a1" * 32
OTHER = "sha256:" + "b2" * 32
API = "https://api.github.com/repos/o/r"
PROD = "https://app.example.org"


@pytest.fixture
def wf(monkeypatch) -> FakeWorkforce:
    return FakeWorkforce().install(monkeypatch)


def _mock_artifacts(mock: respx.MockRouter) -> None:
    mock.get(url__regex=rf"^{API}/actions/runs(\?.*)?$").mock(return_value=httpx.Response(200, json={
        "total_count": 3, "workflow_runs": [
            {"id": 11, "name": "build", "head_sha": MERGE, "status": "completed", "conclusion": "success",
             "event": "push"},
            {"id": 12, "name": "nightly", "head_sha": MERGE, "status": "completed", "conclusion": "failure"},
            {"id": 13, "name": "foreign", "head_sha": "f" * 40, "status": "completed", "conclusion": "success"}]}))
    mock.get(url__regex=rf"^{API}/actions/runs/11/artifacts(\?.*)?$").mock(return_value=httpx.Response(200, json={
        "artifacts": [{"id": 1, "name": "app", "digest": DIGEST, "expired": False, "size_in_bytes": 10},
                      {"id": 3, "name": "old", "digest": "sha256:" + "c3" * 32, "expired": True}]}))
    mock.get(url__regex=rf"^{API}/actions/runs/12/artifacts(\?.*)?$").mock(return_value=httpx.Response(200, json={
        "artifacts": [{"id": 2, "name": "broken", "digest": OTHER, "expired": False}]}))
    mock.get(url__regex=rf"^{API}/attestations/{DIGEST}$").mock(
        return_value=httpx.Response(200, json={"attestations": [{"bundle": {}}]}))
    mock.get(url__regex=rf"^{API}/attestations/sha256:.*$").mock(return_value=httpx.Response(404, json={}))


def test_artifact_digests_are_read_with_get_only_and_only_a_successful_build_is_eligible(db):
    w = make_world(db, repo="o/r")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        _mock_artifacts(mock)
        result = provider.fetch_artifact_digests("o/r", MERGE, token="t0ken-value-for-test")
        assert all(c.request.method == "GET" for c in mock.calls)
        assert not any("/runs/13/" in str(c.request.url) for c in mock.calls), "another commit's run is ignored"
    assert result.status == "RECORDED" and result.error is None
    assert result.eligible_digests == [DIGEST], "failed run and expired artifact are never eligible"
    app = next(a for a in result.artifacts if a["name"] == "app")
    assert app["attestation"] == {"status": "PRESENT_UNVERIFIED", "count": 1, "signature_verified": False}
    ev = provider.record_artifact_evidence(db, w.project, result)
    db.commit()
    assert ev.kind == "ARTIFACT_DIGEST" and ev.origin == "provider-ci" and ev.trusted and ev.subject_id == MERGE
    digests, ev_id = provider.recorded_artifact_digests(db, w.project.id, MERGE)
    assert digests == {DIGEST} and ev_id == ev.id


def test_a_failed_artifact_read_is_never_recorded(db):
    w = make_world(db, repo="o/r")
    assert provider.fetch_artifact_digests("o/r", MERGE, token=None).error
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(url__regex=rf"^{API}/actions/runs(\?.*)?$").mock(return_value=httpx.Response(403, json={
            "message": "Resource not accessible"}))
        result = provider.fetch_artifact_digests("o/r", MERGE, token="t0ken-value-for-test")
    assert result.status == "UNKNOWN" and "403" in (result.error or "")
    assert provider.record_artifact_evidence(db, w.project, result) is None


def _merged_world(db, monkeypatch):
    w = make_world(db, repo="o/r")
    install_planning(monkeypatch, w.version)
    report = record_verification(db, w)
    make_review(db, w, report)
    acc = acceptance.evaluate_acceptance(db, w.run.id)
    db.add(PullRequestRecord(project_id=w.project.id, domain="PERSONAL", task_id=w.task.id, run_id=w.run.id,
                             acceptance_id=acc.id, repo_full_name="o/r", number=9, head_ref="aeos/t1", base_ref="main",
                             accepted_head_sha=acc.head_commit, observed_head_sha=acc.head_commit,
                             bundle_hash="sha256:b", state="MERGED", merged=True, merged_by_login="fadhlillah-dli",
                             merged_by_type="User", merge_commit_sha=MERGE,
                             details={"merge_delta": {"files": [], "truncated": False}}))
    db.commit()
    record_release_approvers(db, w.project, ["fadhlillah-dli"])
    ci = provider.parse_check_runs("o/r", MERGE, [{"check_runs": [
        {"name": "backend (ubuntu-latest)", "status": "completed", "conclusion": "success", "head_sha": MERGE}]}])
    provider.record_ci_evidence(db, w.project, ci, task_id=w.task.id)
    db.commit()
    return w


def _observe_prod(db, project, body):
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(f"{PROD}/health").mock(return_value=httpx.Response(200, json=body))
        out = deployment.observe(db, project.id, "production", MERGE, actor="owner")
    db.commit()
    return out


def test_promotion_is_pass_only_with_g5_g7_and_the_recorded_digest_on_every_target(db, wf, monkeypatch):
    w = _merged_world(db, monkeypatch)
    r = gates.compute_gates(db, w.task.id)
    by = {g["gate"]: g["status"] for g in r["gates"]}
    assert by["G5"] == "PASS" and by["G7"] == "PASS"
    promo = r["promotion"]
    assert promo["status"] == "NOT_PROVEN" and promo["authority"] == "HUMAN_ONLY"
    assert any("ARTIFACT_DIGEST" in x for x in promo["reasons"])
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        _mock_artifacts(mock)
        provider.record_artifact_evidence(db, w.project, provider.fetch_artifact_digests(
            "o/r", MERGE, token="t0ken-value-for-test"))
    db.commit()
    deploy_target(db, w.project, "production", PROD)
    promo = gates.compute_gates(db, w.task.id)["promotion"]
    assert promo["status"] == "NOT_PROVEN" and promo["targets"]["production"]["status"] == "NO_OBSERVATION"
    out = _observe_prod(db, w.project, {"status": "ok", "commit": MERGE, "artifact_digest": DIGEST})
    assert out["status"] == "PASS" and out["artifact"]["status"] == "MATCH"
    promo = gates.compute_gates(db, w.task.id)["promotion"]
    assert promo["status"] == "PASS" and promo["recorded_digests"] == [DIGEST]
    # the target serves another artifact than the one CI built for the merge commit -> FAIL, never PASS
    _observe_prod(db, w.project, {"status": "ok", "commit": MERGE, "artifact_digest": OTHER})
    promo = gates.compute_gates(db, w.task.id)["promotion"]
    assert promo["status"] == "FAIL" and promo["targets"]["production"]["status"] == "MISMATCH"


def test_the_owner_can_require_the_digest_for_the_observation_itself(db, wf, monkeypatch):
    w = _merged_world(db, monkeypatch)
    deploy_target(db, w.project, "production", PROD, {"require_artifact_digest": True})
    out = _observe_prod(db, w.project, {"status": "ok", "commit": MERGE})
    assert out["status"] == "NOT_PROVEN" and out["artifact"]["status"] == "NOT_REPORTED"
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        _mock_artifacts(mock)
        provider.record_artifact_evidence(db, w.project, provider.fetch_artifact_digests(
            "o/r", MERGE, token="t0ken-value-for-test"))
    db.commit()
    out = _observe_prod(db, w.project, {"status": "ok", "commit": MERGE, "digest": OTHER})
    assert out["status"] == "FAIL" and out["failure_conditions"] == ["artifact_digest_mismatch"]
    assert _observe_prod(db, w.project, {"status": "ok", "commit": MERGE, "digest": DIGEST})["status"] == "PASS"


# ----------------------------------------------------------------------------------------- enforcement test

RULESETS = [{"id": 101, "name": "main-protect", "enforcement": "active", "updated_at": "2026-10-01T00:00:00Z"},
            {"id": 102, "name": "trial", "enforcement": "evaluate", "updated_at": "2026-10-02T00:00:00Z"}]
SUITES = [
    {"id": 900, "actor_name": "probe-user", "ref": "refs/heads/main", "result": "fail", "evaluation_result": "fail",
     "pushed_at": "2026-10-20T10:00:00Z"},
    {"id": 901, "actor_name": "admin", "ref": "refs/heads/main", "result": "bypass", "evaluation_result": "fail",
     "pushed_at": "2026-10-20T11:00:00Z"},
    {"id": 902, "actor_name": "x", "ref": "refs/heads/other", "result": "fail", "evaluation_result": "fail"},
    {"id": 903, "actor_name": "y", "ref": "refs/heads/main", "result": "fail", "evaluation_result": "fail"},
]
DETAILS = {
    900: {"rule_evaluations": [{"rule_source": {"type": "ruleset", "id": 101, "name": "main-protect"},
                                "enforcement": "active", "result": "fail", "rule_type": "pull_request"}]},
    901: {"rule_evaluations": [{"rule_source": {"type": "ruleset", "id": 101}, "enforcement": "active",
                                "result": "fail"}]},
    902: {"rule_evaluations": [{"rule_source": {"type": "ruleset", "id": 101}, "enforcement": "active",
                                "result": "fail"}]},
    # an evaluate-mode ruleset only reports; it never proves enforcement
    903: {"rule_evaluations": [{"rule_source": {"type": "ruleset", "id": 102}, "enforcement": "evaluate",
                                "result": "fail"}]},
}


def test_only_a_refusal_by_an_active_ruleset_of_this_branch_counts():
    active = [r for r in RULESETS if r["enforcement"] == "active"]
    hits = provider.enforcement_refusals(SUITES, DETAILS, active, ref="refs/heads/main")
    assert [h["rule_suite_id"] for h in hits] == [900]
    assert hits[0]["rulesets"][0]["ruleset_id"] == 101 and hits[0]["rulesets"][0]["ruleset_updated_at"]
    assert provider.enforcement_refusals(SUITES, DETAILS, [], ref="refs/heads/main") == [], "ruleset gone"


def _mock_enforcement(mock: respx.MockRouter, suites=SUITES, rulesets=RULESETS) -> None:
    mock.get(url__regex=rf"^{API}/rulesets(\?.*)?$").mock(return_value=httpx.Response(200, json=rulesets))
    mock.get(url__regex=rf"^{API}/rulesets/rule-suites(\?.*)?$").mock(return_value=httpx.Response(200, json=suites))
    for sid, detail in DETAILS.items():
        mock.get(f"{API}/rulesets/rule-suites/{sid}").mock(return_value=httpx.Response(200, json=detail))


def _caps(updated_at: str = "2026-10-01T00:00:00Z") -> provider.ProviderCapabilities:
    caps = provider.ProviderCapabilities(provider="GITHUB", repo="o/r", branch="main", checked_at="now",
                                         credential="AVAILABLE", merge_gate="CONFIGURED",
                                         residual_risks=[provider.ENFORCEMENT_UNTESTED])
    caps.checks.append(provider._rulesets_check(httpx.Response(200, json=[{**RULESETS[0], "updated_at": updated_at},
                                                                       RULESETS[1]]), private=True, plan="pro"))
    return caps


def test_the_rule_history_reader_records_evidence_only_for_an_observed_refusal(db):
    from aeos.doctor.checks.delivery import _merge_gate_outcome

    w = make_world(db, repo="o/r")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        _mock_enforcement(mock)
        obs = provider.fetch_enforcement_observation("o/r", "main", token="t0ken-value-for-test")
        assert all(c.request.method == "GET" for c in mock.calls)
        sent = next(c.request for c in mock.calls if c.request.url.path.endswith("/rule-suites"))
        assert sent.url.params["ref"] == "refs/heads/main" and sent.url.params["rule_suite_result"] == "fail"
    assert obs.status == "OBSERVED" and [x["rule_suite_id"] for x in obs.refusals] == [900]
    ev = provider.record_enforcement_evidence(db, w.project, obs)
    db.commit()
    assert ev.kind == "PROVIDER_ENFORCEMENT_TEST" and ev.trusted and ev.environment["branch"] == "main"
    assert ev.environment["rulesets"] == [{"id": 101, "updated_at": "2026-10-01T00:00:00Z"}]
    # bound to the unchanged active ruleset: VERIFIED, and the Doctor says so for this repo/branch only
    caps = provider.apply_enforcement_evidence(db, _caps())
    assert caps.enforcement_tested == "VERIFIED" and caps.enforcement_test["evidence_id"] == ev.id
    assert provider.ENFORCEMENT_UNTESTED not in caps.residual_risks
    assert _merge_gate_outcome(caps.to_dict(), origin="test").status == "VERIFIED"
    other = _caps()
    other.branch = "release"
    assert provider.apply_enforcement_evidence(db, other).enforcement_tested == "NOT-PROVEN"
    # the ruleset changed since: the proof no longer holds (STALE), the Doctor is back to CONFIGURED
    stale = provider.apply_enforcement_evidence(db, _caps(updated_at="2026-10-22T00:00:00Z"))
    assert stale.enforcement_tested == "STALE"
    out = _merge_gate_outcome(stale.to_dict(), origin="test")
    assert out.status == "CONFIGURED" and provider.ENFORCEMENT_UNTESTED in out.evidence["residual_risks"]


def test_a_probe_branch_refusal_proves_main_only_when_the_same_ruleset_applies_to_main(db):
    """W6-139: the opt-in live bypass test uses a dedicated probe branch (never main); its refusal proves the
    ruleset - and so main - only while that ruleset also applies to main."""
    w = make_world(db, repo="o/r")
    probe = [{**s, "ref": "refs/heads/aeos-probe/bypass"} for s in SUITES[:1]]
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        _mock_enforcement(mock, suites=probe)
        obs = provider.fetch_enforcement_observation("o/r", "aeos-probe/bypass", token="t0ken-value-for-test")
    assert obs.status == "OBSERVED"
    provider.record_enforcement_evidence(db, w.project, obs)
    db.commit()

    def main_caps(ruleset_ids):
        caps = _caps()
        caps.checks.append(provider._branch_rules_check(httpx.Response(200, json=[
            {"type": "pull_request", "ruleset_id": i} for i in ruleset_ids])))
        return provider.apply_enforcement_evidence(db, caps)

    proven = main_caps([101])
    assert proven.enforcement_tested == "VERIFIED"
    assert proven.enforcement_test["observed_on_branch"] == "aeos-probe/bypass"
    assert main_caps([555]).enforcement_tested == "NOT-PROVEN", "another ruleset protects main: not proven"


@pytest.mark.parametrize(("suites", "code"), [([], 200), (None, 403)])
def test_no_observed_refusal_is_not_proof(db, suites, code):
    w = make_world(db, repo="o/r")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(url__regex=rf"^{API}/rulesets(\?.*)?$").mock(return_value=httpx.Response(200, json=RULESETS))
        mock.get(url__regex=rf"^{API}/rulesets/rule-suites(\?.*)?$").mock(
            return_value=httpx.Response(code, json=suites if suites is not None else {"message": "forbidden"}))
        obs = provider.fetch_enforcement_observation("o/r", "main", token="t0ken-value-for-test")
    assert obs.status == ("NOT_OBSERVED" if code == 200 else "UNKNOWN")
    if code == 403:
        assert "Administration: read" in (obs.error or "")
    assert provider.record_enforcement_evidence(db, w.project, obs) is None
    assert db.scalars(select(EvidenceArtifact).where(EvidenceArtifact.kind == "PROVIDER_ENFORCEMENT_TEST")).first() \
        is None


def test_an_owner_attestation_is_a_claim_never_verified(db):
    from aeos.doctor.checks.delivery import _merge_gate_outcome

    w = make_world(db, repo="o/r")
    ev = provider.record_owner_attestation(db, w.project, repo="o/r", branch="main",
                                           statement="git push origin HEAD:main was refused with GH013",
                                           actor_user_id="owner")
    db.commit()
    assert not ev.trusted and ev.evidence_level == "E0" and ev.origin == "human"
    caps = provider.apply_enforcement_evidence(db, _caps())
    assert caps.enforcement_tested == "CLAIMED"
    assert _merge_gate_outcome(caps.to_dict(), origin="test").status == "CONFIGURED"


def test_api_records_artifacts_and_enforcement_for_the_owner(personal_client, db, monkeypatch):
    w = make_world(db, repo="o/r")
    monkeypatch.setattr(provider, "github_token", lambda domain: ("t0ken-value-for-test", "AVAILABLE"))
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        _mock_artifacts(mock)
        _mock_enforcement(mock)
        r = personal_client.post("/api/delivery/artifacts/record", json={"project_id": w.project.id, "sha": MERGE})
        assert r.status_code == 200 and r.json()["eligible_digests"] == [DIGEST] and r.json()["evidence_id"], r.text
        e = personal_client.post("/api/delivery/enforcement/observe", json={"repo": "o/r", "branch": "main"})
        assert e.status_code == 200 and e.json()["status"] == "OBSERVED" and e.json()["evidence_id"], e.text
        assert all(c.request.method == "GET" for c in mock.calls)
    a = personal_client.post("/api/delivery/enforcement/attest",
                             json={"repo": "o/r", "branch": "main", "statement": "short"})
    assert a.status_code == 422 and a.json()["error"]["code"] == "ATTESTATION_TOO_SHORT"
