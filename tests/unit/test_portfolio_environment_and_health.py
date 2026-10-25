"""Per-project environment targets and Integration Registry health (master §01 "own repository, documents,
issue tracker and environment", §10 Integration Registry "real capability and health", §13
ProjectIntegration capability status + last_verified_at)."""

from __future__ import annotations

from aeos.core.enums import Domain
from aeos.core.timeutil import utcnow
from aeos.models.portfolio import ProjectIntegration
from aeos.portfolio import facade as portfolio
from tests.conftest import AuthedClient
from tests.unit.test_portfolio_support import create_project


def _put(client: AuthedClient, pid: str, **body):
    return client.put(f"/api/projects/{pid}/integrations", json=body)


def test_personal_staging_target_can_be_registered_and_configures_the_doctor_check(personal_client, db, settings):
    from aeos.doctor.checks.delivery import delivery_staging_target
    from aeos.doctor.model import CheckContext

    ctx = CheckContext(settings=settings, session=db, runner=None, repo=settings.data_dir, now=utcnow())  # type: ignore[arg-type]
    assert delivery_staging_target(ctx).status == "MISSING"
    p = create_project(personal_client, name="Env App")
    bad = _put(personal_client, p["id"], provider="local-target", purpose="staging", root_id="my laptop")
    assert bad.status_code == 422
    r = _put(personal_client, p["id"], provider="local-target", purpose="staging", root_id="container:env-app-staging")
    assert r.status_code == 200, r.text
    cap = r.json()["integration"]["capability_status"]
    assert cap["configuration"] == "CONFIGURED" and cap["credential"]["status"] == "NOT_APPLICABLE"
    assert cap["target"] == {"provider": "local-target", "root_id": "container:env-app-staging"}
    assert any("WRITE_ENABLEMENT" in x["issue"] for x in cap["remedies"])  # deploying still needs a human decision
    assert cap["write_enabled"] is False
    assert _put(personal_client, p["id"], provider="local-target", purpose="deploy",
                root_id="host:home-server").status_code == 200
    db.expire_all()
    out = delivery_staging_target(ctx)
    assert out.status == "CONFIGURED" and "local-target:PERSONAL" in out.evidence["targets"]


def test_office_has_no_personal_environment_target(admin_client: AuthedClient):
    admin_client.select_domain(Domain.OFFICE)
    p = create_project(admin_client, domain=Domain.OFFICE, name="Office Env")
    r = _put(admin_client, p["id"], provider="local-target", purpose="staging", root_id="container:x")
    assert r.status_code == 403 and r.json()["error"]["code"] == "PROVIDER_DOMAIN_MISMATCH"
    # §01 (re-audit, owner/M5 dependency): the product states that OFFICE has no environment target yet
    assert "M5" in r.json()["error"]["message"] and "otorisasi perusahaan" in r.json()["error"]["message"]
    env = admin_client.get(f"/api/projects/{p['id']}").json()["project"]["environment"]
    assert env["status"] == "NOT_AVAILABLE" and env["targets"] == [] and "M5" in env["reason"]


def test_personal_environment_state_lists_the_recorded_targets(personal_client: AuthedClient):
    p = create_project(personal_client, name="Env State")
    assert personal_client.get(f"/api/projects/{p['id']}").json()["project"]["environment"]["status"] == "NOT_CONFIGURED"
    r = _put(personal_client, p["id"], provider="local-target", purpose="staging", root_id="container:app")
    assert r.status_code == 200, r.text
    env = personal_client.get(f"/api/projects/{p['id']}").json()["project"]["environment"]
    assert env["status"] == "CONFIGURED" and env["targets"][0]["purpose"] == "staging"


def test_probe_result_is_recorded_for_the_exact_target_and_reset_on_target_change(personal_client, db,
                                                                                  secrets_memory):
    p = create_project(personal_client, name="Health App")
    r = _put(personal_client, p["id"], provider="github-issues", purpose="issues", root_id="octo/health")
    assert r.status_code == 200, r.text
    iid = r.json()["integration"]["id"]
    assert portfolio.record_integration_probe(db, domain="PERSONAL", providers=("github-issues",),
                                              root_id="octo/other", connectivity="CONNECTED",
                                              observer="test") == []  # another target: nothing recorded
    ids = portfolio.record_integration_probe(db, domain="PERSONAL", providers=("github-issues",), root_id="octo/health",
                                             connectivity="CONNECTED", functional="VERIFIED", observer="publisher:probe",
                                             evidence_ref="ev-1")
    db.commit()
    assert ids == [iid]
    integ = db.get(ProjectIntegration, iid)
    assert integ.last_verified_at is not None
    listed = personal_client.get(f"/api/projects/{p['id']}/integrations").json()["items"][0]
    cap = listed["capability_status"]
    assert cap["connectivity"] == "CONNECTED" and cap["functional"] == "VERIFIED"
    assert cap["verification"]["observer"] == "publisher:probe" and "_verification" not in listed["config"]
    # re-saving the SAME target keeps the proof; a different target drops it
    assert _put(personal_client, p["id"], provider="github-issues", purpose="issues",
                root_id="octo/health").json()["integration"]["capability_status"]["functional"] == "VERIFIED"
    moved = _put(personal_client, p["id"], provider="github-issues", purpose="issues", root_id="octo/renamed").json()
    cap = moved["integration"]["capability_status"]
    assert cap["connectivity"] == "NOT_CHECKED" and cap["functional"] == "NOT_TESTED"
    assert moved["integration"]["last_verified_at"] is None
    # the proof can never be supplied through the API
    r = _put(personal_client, p["id"], provider="github-issues", purpose="issues", root_id="octo/renamed",
             config={"_verification": "VERIFIED"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_CONFIG"


def test_repository_view_joins_prs_ci_docs_and_capability(personal_client, db, secrets_memory):
    """§37 user repository: base, PR/CI links, policy/capability and docs folder in one view."""
    from aeos.models.decisions import Decision
    from aeos.models.delivery_ext import PullRequestRecord

    p = create_project(personal_client, name="Repo View")
    r = personal_client.post(f"/api/projects/{p['id']}/repositories",
                             json={"provider": "GITHUB", "owner": "octo", "name": "view"})
    rid = r.json()["repository"]["id"]
    docs = _put(personal_client, p["id"], provider="google-drive", purpose="docs",
                root_id="1AbCdEfGhIjKlMnOpQrStUvWxYz012345").json()["integration"]
    _put(personal_client, p["id"], provider="github-actions", purpose="ci")
    db.add(PullRequestRecord(project_id=p["id"], domain="PERSONAL", task_id="t", run_id="r", acceptance_id="a",
                             repo_full_name="octo/view", number=7, url="https://github.com/octo/view/pull/7",
                             head_ref="aeos/t1", base_ref="main", bundle_hash="sha256:x", state="OPEN"))
    # a decision shaped like no real recorder writes it (target = an integration id) enables nothing (review fix:
    # the view used to read exactly this shape, so a real per-provider enablement never showed as ENABLED)
    db.add(Decision(kind="WRITE_ENABLEMENT", actor_user_id="u", target_type="integration", target_id=docs["id"],
                    status="ACTIVE", allowed_operations=[], auth_scope={}, details={"enabled": True}))
    db.commit()
    view = personal_client.get(f"/api/repositories/{rid}").json()["repository"]
    assert view["base_branch"] == "main"
    assert view["pull_requests"][0]["url"].endswith("/pull/7") and view["pull_requests"][0]["state"] == "OPEN"
    assert view["ci"][0]["provider"] == "github-actions"
    assert view["docs_targets"][0]["root_id"] == "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"
    caps = {c["provider"]: c for c in view["capability"]["write_enablement"]}
    assert caps["google-drive"]["status"] == "NOT_ENABLED" and caps["google-drive"]["decision_id"] is None
    assert caps["github-actions"]["status"] == "NOT_APPLICABLE"  # not a write provider of PERSONAL
    # §26 the human WRITE_ENABLEMENT decision the interlock reads (flag + ACTIVE decision on provider_write/<p>)
    r = personal_client.post("/api/decisions", json={"kind": "WRITE_ENABLEMENT", "provider": "google-drive",
                                                     "enabled": True, "auth_scope": {"scope": "owner"},
                                                     "rationale": "pemilik mengizinkan tulis Drive"})
    assert r.status_code == 201, r.text
    view = personal_client.get(f"/api/repositories/{rid}").json()["repository"]
    caps = {c["provider"]: c for c in view["capability"]["write_enablement"]}
    assert caps["google-drive"]["status"] == "ENABLED" and caps["google-drive"]["decision_id"] == r.json()["id"]
    assert caps["google-drive"]["scope"].startswith("provider")
    personal_client.post(f"/api/decisions/{r.json()['id']}/revoke", json={"reason": "cabut izin tulis Drive"})
    view = personal_client.get(f"/api/repositories/{rid}").json()["repository"]
    assert {c["provider"]: c["status"] for c in view["capability"]["write_enablement"]}["google-drive"] == "NOT_ENABLED"
    assert view["capability"]["merge"].startswith("HUMAN_ONLY")


def test_doctor_github_probe_records_connectivity_only(personal_client, db, settings, secrets_memory, monkeypatch):
    from aeos.doctor.checks import adapters
    from aeos.doctor.model import CheckContext

    p = create_project(personal_client, name="Probe App")
    slug = settings.platform_repo_slug
    _put(personal_client, p["id"], provider="github-issues", purpose="issues", root_id=slug)
    monkeypatch.setattr(adapters, "secret_ref_status", lambda ref: ("PRESENT", {}))

    class Resp:
        status = 200
        body = {"id": 1, "private": True, "default_branch": "main", "permissions": {"pull": True}}
        error = None

    monkeypatch.setattr(adapters, "github_get", lambda ctx, path: Resp())
    ctx = CheckContext(settings=settings, session=db, runner=None, repo=settings.data_dir, now=utcnow(),  # type: ignore[arg-type]
                       functional=True)
    assert adapters.adapter_github(ctx).status == "CONNECTED"
    db.commit()
    cap = personal_client.get(f"/api/projects/{p['id']}/integrations").json()["items"][0]["capability_status"]
    assert cap["connectivity"] == "CONNECTED" and cap["functional"] == "NOT_TESTED"  # read-only: never VERIFIED
