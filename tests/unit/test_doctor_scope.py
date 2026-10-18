"""Domain scope of Readiness projections and the OFFICE default-deny gate (completeness review).

§50 rows covered:
* "Viewer mengakses detail/search/export inventory Office" -> backend scope applies to counts,
  reasons and remedies too: an OFFICE credential's existence never reaches a PERSONAL / no-domain view.
* "Tool opsional atau Office belum ready" / §23 OFFICE default deny -> only a recorded authorisation
  (policy module) opens an OFFICE gate; a flag in the reviewed defaults file never does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from aeos.core.enums import Capability
from aeos.core.timeutil import utcnow
from aeos.doctor import office
from aeos.doctor.capabilities import checks_for_capability, evaluate
from aeos.doctor.checks import REGISTRY, load_all
from aeos.doctor.checks import host as host_mod
from aeos.doctor.runner import make_context, run_check, run_doctor
from aeos.doctor.views import capabilities_view, checks_view, hidden_check_ids
from aeos.inventory.probes import HttpResult, set_runner_override
from aeos.models.audit import Event
from tests.unit.test_doctor_capabilities import all_ok
from tests.unit.test_inventory_fakes import base_runner, ok

OFFICE_REF = "office/atlassian"


@pytest.fixture
def fake_host(monkeypatch):
    r = base_runner()
    set_runner_override(r)
    monkeypatch.setattr(host_mod, "tcp_in_use", lambda h, p, timeout=1.0: False)
    monkeypatch.setattr(host_mod, "can_bind", lambda h, p: True)
    yield r
    set_runner_override(None)


def _check(cid, settings, db, runner, repo=None):
    load_all()
    return run_check(REGISTRY[cid], make_context(settings, db, runner=runner, repo=repo))


def test_platform_secret_status_never_lists_office_or_project_refs(settings, db, secrets_memory):
    secrets_memory.put(OFFICE_REF, "o" * 30, actor="test")
    secrets_memory.put("projects/p1-github", "p" * 30, actor="test")
    secrets_memory.put("workers/openrouter", "w" * 30, actor="test")
    res = _check("secret.status", settings, db, base_runner())
    assert res.status == "CONFIGURED" and "domain" not in res.scope  # platform row, visible to every domain
    assert "CAN_RUN_WORKER" in res.scope["capabilities"]  # §08 capability scope of the result
    blob = json.dumps(res.as_dict())
    assert "office" not in res.observed_evidence["refs_by_namespace"]
    assert "projects" not in res.observed_evidence["refs_by_namespace"]
    assert res.observed_evidence["refs"] == ["workers/openrouter"]
    assert OFFICE_REF not in blob and "p1-github" not in blob and "1 ref" in res.summary


def test_no_domain_capabilities_do_not_quote_domain_scoped_checks(settings, db, secrets_memory, fake_host):
    secrets_memory.put(OFFICE_REF, "o" * 30, actor="test")
    run_doctor(settings, db, groups=["PROJECT_ADAPTERS"], runner=fake_host)
    db.commit()
    hidden = hidden_check_ids(None)
    assert {"adapter.office_jira", "adapter.github_token"} <= hidden
    caps = {c["capability"]: c for c in capabilities_view(db, domain=None)["items"]}
    for cap in caps.values():
        assert not any(c["check_id"] in hidden for c in cap["checks"])
        assert not any(r.get("check_id") in hidden for r in cap["remedies"])
        text = " ".join(cap["reasons"])
        assert "adapter.office" not in text and "adapter.github_token" not in text and OFFICE_REF not in text
    office_caps = caps["CAN_USE_OFFICE_INTEGRATIONS"]
    assert office_caps["status"] == "BLOCKED" and "OFFICE default deny" in office_caps["reasons"][0]
    assert any("domain lain tidak ditampilkan" in r for r in office_caps["reasons"])
    # an OFFICE session does see its own adapter checks and their reasons
    office_view = {c["capability"]: c for c in capabilities_view(db, domain="OFFICE")["items"]}
    assert any(c["check_id"] == "adapter.office_jira" for c in office_view["CAN_USE_OFFICE_INTEGRATIONS"]["checks"])


def test_run_counts_cover_visible_checks_only(settings, db, secrets_memory, fake_host):
    run_doctor(settings, db, groups=["PROJECT_ADAPTERS", "HOST"], runner=fake_host)
    db.commit()
    personal = checks_view(db, domain="PERSONAL")
    visible = [i for i in personal["items"] if i["run_id"] == personal["last_run"]["id"]]
    assert sum(personal["last_run"]["counts"].values()) == len(visible)
    assert not any(i["check_id"].startswith("adapter.office") for i in visible)
    ev = db.scalar(select(Event).where(Event.kind == "doctor.run.completed"))
    assert ev.domain is None and "counts" not in ev.payload
    platform_specs = {c for c, s in REGISTRY.items() if not s.scope and s.group in ("PROJECT_ADAPTERS", "HOST")}
    assert sum(ev.payload["counts_platform"].values()) == len(platform_specs)


def test_api_run_response_is_domain_scoped(personal_client, fake_host):
    body = personal_client.post("/api/doctor/run", json={"group": "PROJECT_ADAPTERS"}).json()
    ids = {i["check_id"] for i in body["items"]}
    assert "adapter.github_token" in ids and not any(i.startswith("adapter.office") for i in ids)
    assert sum(body["run"]["counts"].values()) == len(body["items"])


def test_office_flag_in_defaults_file_does_not_open_the_gate(db, monkeypatch):
    for flag_caps in (("integrations_allowed", Capability.CAN_USE_OFFICE_INTEGRATIONS),
                      ("claude_max_allowed_for_office_data", Capability.CAN_PLAN_OFFICE)):
        flag, cap = flag_caps
        monkeypatch.setattr(office, "read_office_policy", lambda s, root=None, f=flag: (
            {**office.DEFAULT_DENY, f: True}, "policy/office-defaults.yaml"))
        st = evaluate(cap, all_ok(cap), now=utcnow(), fps={}, domain=None, session=db)
        assert st.status == "BLOCKED", (flag, st.reasons)
        assert "bukan keputusan otorisasi" in st.reasons[0]
    allowed, _ = office.office_gate("CAN_RUN_WORKER", db)
    assert allowed is False
    assert set(checks_for_capability(Capability.CAN_USE_OFFICE_INTEGRATIONS)) >= {"policy.office_defaults"}


@pytest.mark.parametrize("line", [
    "  claude_max_allowed_for_office_data: true\n",
    "  integrations_allowed: true\n",
    "  approved_worker_routes: [some-route]\n",
])
def test_defaults_file_opening_any_office_permission_fails(settings, tmp_path: Path, line):
    repo = tmp_path / "repo"
    (repo / "policy").mkdir(parents=True)
    (repo / "policy" / "office-defaults.yaml").write_text("office:\n  external_worker_allowed: false\n" + line,
                                                         encoding="utf-8")
    res = _check("policy.office_defaults", settings, None, base_runner(), repo=repo)
    assert res.status == "FAILED" and res.error_code == "OFFICE_DEFAULT_NOT_DENY" and res.severity == "CRITICAL"


def test_office_staging_validation_is_gated_like_other_office_execution(db):
    cap = Capability.CAN_VALIDATE_STAGING
    office_st = evaluate(cap, all_ok(cap), now=utcnow(), fps={}, domain="OFFICE", session=db)
    assert office_st.status == "BLOCKED" and "OFFICE default deny" in office_st.reasons[0]
    assert evaluate(cap, all_ok(cap), now=utcnow(), fps={}, domain="PERSONAL", session=db).status == "READY"


@pytest.mark.parametrize("plat,expected", [
    ("win32", "winget install --id OpenJS.NodeJS.LTS -e"),
    ("darwin", "brew install node"),
    ("linux", None),  # distributions differ: point to the official source, never guess a command
])
def test_install_remedies_are_per_os(settings, db, monkeypatch, plat, expected):
    import aeos.doctor.model as model

    monkeypatch.setattr(model.sys, "platform", plat)
    res = _check("runtime.node", settings, db, base_runner())  # node not on the fake PATH
    assert res.status == "MISSING" and res.remediation["command"] == expected
    assert "nodejs.org" in res.remediation["source"]


@pytest.mark.parametrize("plat", ["win32", "darwin", "linux"])
def test_provider_env_remedy_is_a_real_command_per_os(settings, db, monkeypatch, plat):
    import aeos.doctor.checks.claude as claude_mod

    monkeypatch.setattr(claude_mod.sys, "platform", plat)
    r = base_runner(env={"ANTHROPIC_API_KEY": "sk-ant-" + "q" * 30})
    res = _check("claude.env", settings, db, r)
    assert res.status == "FAILED"
    cmd = res.remediation["command"]
    if plat == "win32":
        assert cmd.startswith("[Environment]::SetEnvironmentVariable('ANTHROPIC_API_KEY'")
    else:
        assert cmd == "unset ANTHROPIC_API_KEY" and "profil shell" in res.remediation["operation"]
    assert "q" * 30 not in json.dumps(res.as_dict())


def test_connectivity_observations_never_claim_more_than_was_seen(settings, db, secrets_memory):
    """Missing credential -> NOT_CHECKED (not EXPIRED_AUTH); an answering endpoint with a failed check is
    not UNREACHABLE; a rejected token is EXPIRED_AUTH; a refused connection is UNREACHABLE."""
    def conn(res):
        return next(o["status"] for o in res.observations if o["dimension"] == "connectivity")

    assert conn(_check("adapter.github_token", settings, db, base_runner())) == "NOT_CHECKED"
    r = base_runner()
    r.add_binary("claude")
    r.outputs[("claude", ("auth", "status", "--json"))] = ok(json.dumps(
        {"loggedIn": True, "authMethod": "apiKey", "apiProvider": "firstParty", "subscriptionType": None}))
    assert conn(_check("claude.auth", settings, db, r)) == "UNKNOWN"
    secrets_memory.put("personal/github", "t" * 40, actor="test")
    url = f"https://api.github.com/repos/{settings.platform_repo_slug}"
    r = base_runner()
    r.http[url] = HttpResult(401, {"message": "Bad credentials"})
    load_all()
    res = run_check(REGISTRY["adapter.github_token"], make_context(settings, db, runner=r, functional=True))
    assert conn(res) == "EXPIRED_AUTH"
    r = base_runner()
    r.add_binary("bao")
    res = _check("secret.openbao_health", settings, db, r)  # nothing listens on the loopback address
    assert res.error_code == "OPENBAO_UNREACHABLE" and conn(res) == "UNREACHABLE"


@pytest.mark.parametrize("cid,kind", [
    ("recovery.restore_test", "RESTORE_TEST"),
    ("recovery.publication_idempotency", "PUBLICATION_IDEMPOTENCY_TEST"),
    ("worker.contract", "WORKER_CONTRACT_TEST"),
])
def test_mock_untrusted_or_office_evidence_never_proves_platform_readiness(settings, db, cid, kind):
    """§50 'Mock ... tests ditampilkan' / 'Restore backup belum pernah diuji': only LIVE, trusted,
    non-OFFICE evidence turns these platform checks VERIFIED."""
    from aeos.models.evidence import EvidenceArtifact

    def add(**kw):
        db.add(EvidenceArtifact(kind=kind, origin="verifier", collector_identity="test", content_hash="sha256:x",
                                claim="synthetic", **kw))
        db.commit()

    add(trusted=True, mode="MOCK")
    add(trusted=False, mode="LIVE")
    add(trusted=True, mode="LIVE", domain="OFFICE")
    assert _check(cid, settings, db, base_runner()).status in ("MISSING", "UNKNOWN")
    add(trusted=True, mode="LIVE", domain=None)
    assert _check(cid, settings, db, base_runner()).status == "VERIFIED"
