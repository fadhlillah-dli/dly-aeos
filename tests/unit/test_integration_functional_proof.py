"""Integration Registry health from real use (master §10 "real capability and health per integration", §13
ProjectIntegration "capability status, last_verified_at").

A LIVE publication write that succeeded against an integration's exact approved target is the functional
proof of that integration: capability_status becomes CONNECTED/VERIFIED and last_verified_at is set. A write
against another target, or an integration whose target changed since, proves nothing (never guessed).
"""

from __future__ import annotations

from aeos.models.portfolio import ProjectIntegration
from aeos.portfolio import facade as portfolio
from aeos.publisher import jobs
from tests.unit.test_publisher_jobs import _execute, _preview, env  # noqa: F401  (pytest fixture)


def _integration(db, project, *, provider: str, purpose: str, root_id: str | None) -> ProjectIntegration:
    integ = ProjectIntegration(project_id=project.id, domain=project.domain, provider=provider, purpose=purpose,
                               root_id=root_id, config={})
    db.add(integ)
    db.commit()
    return integ


def test_successful_live_publication_verifies_the_exact_target_integration(env):  # noqa: F811
    db = env["db"]
    docs = _integration(db, env["project"], provider="google-drive", purpose="docs", root_id=None)
    issues = _integration(db, env["project"], provider="github-issues", purpose="issues", root_id="owner/contoh")
    assert (docs.capability_status or {}).get("functional") in (None, "NOT_TESTED") and docs.last_verified_at is None
    pub = _preview(env)
    assert {t.get("integration_id") for t in pub.targets} >= {docs.id, issues.id}
    _execute(env, pub)
    jobs.process_publication(db, pub.id)
    db.commit()
    db.expire_all()
    for integ_id in (docs.id, issues.id):
        integ = db.get(ProjectIntegration, integ_id)
        status = integ.capability_status or {}
        assert status["connectivity"] == "CONNECTED" and status["functional"] == "VERIFIED", status
        assert integ.last_verified_at is not None
        assert status["verification"]["observer"] == "publisher"
        assert status["verification"]["evidence_ref"].startswith("publication_operation:")


def test_a_write_to_another_target_proves_nothing(env):  # noqa: F811
    db = env["db"]
    issues = _integration(db, env["project"], provider="github-issues", purpose="issues", root_id="owner/contoh")
    assert portfolio.record_publication_success(db, issues.id, root_id="owner/other", observer="publisher",
                                                evidence_ref="publication_operation:x") is False
    assert portfolio.record_publication_success(db, "missing", root_id="owner/contoh", observer="publisher",
                                                evidence_ref="publication_operation:x") is False
    db.expire_all()
    integ = db.get(ProjectIntegration, issues.id)
    assert integ.last_verified_at is None and (integ.capability_status or {}).get("functional") != "VERIFIED"
