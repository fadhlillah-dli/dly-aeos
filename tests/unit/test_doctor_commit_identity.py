"""Doctor ``git.commit_identity.<provider>`` (owner rule 2026-10-26, AGENTS.md invariant 11): CONFIGURED / MISSING /
FAILED (invalid) per registered code host; NOT_APPLICABLE for Bitbucket until a Bitbucket repository is registered.
The remedy names the aeos.yaml key, never an address, and no address is ever stored in the result."""

from __future__ import annotations

import json

import pytest

from aeos.core.enums import Capability
from aeos.doctor import capabilities as caps
from aeos.doctor.checks import REGISTRY, load_all
from aeos.doctor.runner import compute_fingerprint, make_context, run_check
from aeos.models.portfolio import Project, RepositoryReference
from tests.unit.test_inventory_fakes import base_runner

GH = {"name": "Owner Name", "email": "12345+owner@users.noreply.github.com"}
BB = {"name": "Owner Name", "email": "owner@company.example.com"}


def _run(settings, db, cid: str, **identities):
    load_all()
    s = settings.model_copy(update={"commit_identities": identities, "commit_author_name": None,
                                    "commit_author_email": None})
    ctx = make_context(s, db, runner=base_runner())
    return run_check(REGISTRY[cid], ctx), ctx


def _no_address(res) -> None:
    blob = json.dumps(res.as_dict(), default=str)
    assert GH["email"] not in blob and BB["email"] not in blob and "owner@" not in blob


def test_github_identity_configured(settings, db):
    res, _ = _run(settings, db, "git.commit_identity.github", github=GH)
    # CONFIGURED, never VERIFIED: the link to the account is not proven by reading aeos.yaml (evidence honesty)
    assert res.status == "CONFIGURED" and res.observed_evidence["noreply"] is True
    assert res.observed_evidence["config_key"] == "commit_identities.github"
    _no_address(res)


def test_github_identity_missing_names_the_key(settings, db):
    res, _ = _run(settings, db, "git.commit_identity.github")
    assert res.status == "MISSING" and res.error_code == "COMMIT_IDENTITY_MISSING"
    rem = res.remediation
    assert "commit_identities.github" in rem["operation"] and rem["target"] == "aeos.yaml: commit_identities.github"
    assert "@" not in json.dumps(rem) and rem["executed_by"].startswith("MANUSIA")
    _no_address(res)


def test_invalid_identity_fails(settings, db):
    res, _ = _run(settings, db, "git.commit_identity.github", github={"name": "Claude", "email": "noreply@anthropic.com"})
    assert res.status == "FAILED" and res.error_code == "COMMIT_IDENTITY_INVALID"
    assert "noreply@anthropic.com" not in json.dumps(res.as_dict(), default=str)


def test_bitbucket_is_not_applicable_until_a_bitbucket_repository_is_registered(settings, db):
    res, _ = _run(settings, db, "git.commit_identity.bitbucket")
    assert res.status == "NOT_APPLICABLE"
    p = Project(project_key="office-x", name="x", domain="OFFICE")
    db.add(p)
    db.flush()
    db.add(RepositoryReference(project_id=p.id, provider="BITBUCKET", full_name="ws/repo", default_branch="main"))
    db.commit()
    res, _ = _run(settings, db, "git.commit_identity.bitbucket", github=GH)
    assert res.status == "MISSING" and "commit_identities.bitbucket" in res.remediation["operation"]
    res, _ = _run(settings, db, "git.commit_identity.bitbucket", github=GH, bitbucket=BB)
    assert res.status == "CONFIGURED" and "noreply" not in res.observed_evidence
    _no_address(res)


def test_a_config_change_makes_the_result_stale(settings, db):
    _, ctx = _run(settings, db, "git.commit_identity.github", github=GH)
    before = compute_fingerprint(REGISTRY["git.commit_identity.github"], ctx)
    _, ctx2 = _run(settings, db, "git.commit_identity.github", github={**GH, "email": "other@example.com"})
    after = compute_fingerprint(REGISTRY["git.commit_identity.github"], ctx2)
    assert before and after and before != after


@pytest.mark.parametrize(("status", "ready"), [("CONFIGURED", True), ("MISSING", False), ("FAILED", False)])
def test_pr_preparation_is_gated_on_the_github_identity(status, ready):
    load_all()
    from aeos.core.timeutil import utcnow

    rows = {cid: {"check_id": cid, "status": "VERIFIED", "summary": "", "expires_at": None, "last_checked_at": None,
                  "fingerprint": None, "remediation": {}, "observed_evidence": {}}
            for cid in caps.checks_for_capability(Capability.CAN_PREPARE_PR)}
    rows["git.commit_identity.github"]["status"] = status
    st = caps.evaluate(Capability.CAN_PREPARE_PR, rows, now=utcnow(), fps={}, domain="PERSONAL", session=None,
                       project={"id": "p1", "domain": "PERSONAL", "is_platform_repo": True}, project_rows={})
    assert (st.status != "BLOCKED") is ready
    if not ready:
        assert any("git.commit_identity.github" in r for r in st.reasons)
