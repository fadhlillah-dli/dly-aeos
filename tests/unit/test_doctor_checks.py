"""Readiness Doctor checks (§08) with fake probes: statuses, evidence, remedies, no secrets.

§50 rows covered:
* "Dependency inti belum terpasang" -> reason + remedy, never a silent failure.
* "Port lokal konflik" -> explained; no public bind, no auth disabling.
* "Doctor hanya berhasil connect" -> CONNECTED, never VERIFIED.
* "Restore backup belum pernah diuji" -> not claimed ready.
* "Trace berisi secret/PII" -> redaction verified; persisted secrets detected without showing values.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from aeos.core.paths import repo_root
from aeos.doctor.checks import REGISTRY, load_all
from aeos.doctor.checks import host as host_mod
from aeos.doctor.model import GROUP_IDS
from aeos.doctor.runner import make_context, run_check
from aeos.inventory.probes import HttpResult
from tests.unit.test_inventory_fakes import base_runner, ok

AUTH_ARGS = ("auth", "status", "--json")


def _ctx(settings, db, runner, *, functional=False, repo=None):
    return make_context(settings, db, runner=runner, functional=functional, repo=repo)


def check(cid, ctx):
    load_all()
    return run_check(REGISTRY[cid], ctx)


def claude_runner(status: dict | str, env: dict | None = None):
    r = base_runner(env=env or {})
    r.add_binary("claude")
    r.outputs[("claude", ("--version",))] = ok("2.1.302 (Claude Code)\n")
    text = status if isinstance(status, str) else json.dumps(status)
    r.outputs[("claude", AUTH_ARGS)] = ok(text)
    return r


NATIVE = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max",
          "email": "owner@example.com", "orgName": "Owner Org", "orgId": "org-123", "configDirectory": "/home/x/.claude"}


def test_every_section_08_group_has_checks():
    load_all()
    assert {s.group for s in REGISTRY.values()} == set(GROUP_IDS)
    assert len(REGISTRY) >= 50


def test_claude_auth_native_max_is_connected_without_identity(settings, db):
    r = claude_runner(NATIVE, env={"ANTHROPIC_API_KEY": "x" * 30, "PATH": "/usr/bin"})
    res = check("claude.auth", _ctx(settings, db, r))
    assert res.status == "CONNECTED"  # login confirmed; bridge function not proven by this
    blob = json.dumps(res.as_dict())
    assert "owner@example.com" not in blob and "Owner Org" not in blob and "org-123" not in blob
    assert set(res.observed_evidence) == {"loggedIn", "authMethod", "apiProvider", "subscriptionType", "operation_ref"}
    call = next(c for c in r.calls if c["argv"][1:] == list(AUTH_ARGS))
    assert "ANTHROPIC_API_KEY" not in call["env"] and "--bare" not in call["argv"]


@pytest.mark.parametrize("patch,status,code", [
    ({"loggedIn": False}, "MISSING", "NOT_LOGGED_IN"),
    ({"authMethod": "api_key"}, "FAILED", "AUTH_METHOD_NOT_NATIVE"),
    ({"apiProvider": "bedrock"}, "FAILED", "API_PROVIDER_NOT_FIRST_PARTY"),
    ({"subscriptionType": "pro"}, "FAILED", "SUBSCRIPTION_NOT_MAX"),
])
def test_claude_auth_rejects_non_native_modes(settings, db, patch, status, code):
    res = check("claude.auth", _ctx(settings, db, claude_runner({**NATIVE, **patch})))
    assert (res.status, res.error_code) == (status, code)
    assert res.remediation["executed_by"].startswith("MANUSIA")


def test_claude_auth_unparseable_is_unknown(settings, db):
    assert check("claude.auth", _ctx(settings, db, claude_runner("not json"))).status == "UNKNOWN"


def test_claude_env_with_api_key_fails_without_showing_value(settings, db):
    secret_value = "z" * 32
    r = base_runner(env={"ANTHROPIC_API_KEY": secret_value, "ANTHROPIC_BASE_URL": "http://proxy"})
    res = check("claude.env", _ctx(settings, db, r))
    assert res.status == "FAILED" and res.severity == "CRITICAL"
    assert res.observed_evidence["variables_present"] == ["ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"]
    assert secret_value not in json.dumps(res.as_dict())
    assert check("claude.env", _ctx(settings, db, base_runner())).status == "VERIFIED"


@pytest.mark.parametrize("plat,expected", [("win32", "winget install --id Anthropic.ClaudeCode -e"),
                                            ("darwin", "brew install --cask claude-code"), ("linux", None)])
def test_claude_binary_missing_and_found(settings, db, monkeypatch, plat, expected):
    import aeos.doctor.checks.claude as claude_mod

    monkeypatch.setattr("aeos.doctor.model.sys.platform", plat)
    monkeypatch.setattr(claude_mod.sys, "platform", plat)
    missing = check("claude.binary", _ctx(settings, db, base_runner()))
    # official package-manager build per OS; Linux distributions differ -> no guessed command, docs only
    assert missing.status == "MISSING" and missing.remediation["command"] == expected
    assert "curl" not in json.dumps(missing.remediation) and "| bash" not in json.dumps(missing.remediation)
    found = check("claude.binary", _ctx(settings, db, claude_runner(NATIVE)))
    assert found.status == "VERIFIED" and found.component_version == "2.1.302"
    assert found.observations[0] == {"component_id": "claude-code", "dimension": "installation",
                                     "status": "INSTALLED", "observed_version": "2.1.302"}


def test_claude_capability_test_is_unknown_without_bridge_and_verified_with_it(settings, db, monkeypatch, absent_modules):
    absent_modules("aeos.claude_bridge.bridge")
    res = check("claude.capability_test", _ctx(settings, db, base_runner(), functional=True))
    assert res.status == "UNKNOWN" and res.error_code == "BRIDGE_NOT_IMPLEMENTED"
    import aeos.claude_bridge.facade as bridge

    # the bridge's capability-test contract: status VERIFIED|DEGRADED|FAILED|BLOCKED, mode LIVE
    monkeypatch.setattr(bridge, "capability_test", lambda: {"status": "VERIFIED", "model_resolved": "opus-x",
                                                            "engine_version": "2.1.302", "mode": "LIVE"})
    res = check("claude.capability_test", _ctx(settings, db, base_runner(), functional=True))
    assert res.status == "VERIFIED" and any(o["component_id"] == "claude-code" for o in res.observations)
    monkeypatch.setattr(bridge, "capability_test", lambda: {"status": "FAILED", "quota_exhausted": True, "mode": "LIVE"})
    res = check("claude.capability_test", _ctx(settings, db, base_runner(), functional=True))
    assert (res.status, res.error_code) == ("FAILED", "QUOTA_EXHAUSTED")
    monkeypatch.setattr(bridge, "capability_test", lambda: {"status": "VERIFIED", "mode": "MOCK"})
    assert check("claude.capability_test", _ctx(settings, db, base_runner(), functional=True)).status == "UNKNOWN"


def test_openbao_health_states(settings, db, monkeypatch):
    url = "http://127.0.0.1:8200/v1/sys/health"
    r = base_runner()
    assert check("secret.openbao_health", _ctx(settings, db, r)).status == "MISSING"  # no binary, unreachable
    r.add_binary("bao")
    assert check("secret.openbao_health", _ctx(settings, db, r)).status == "FAILED"  # binary, not running
    r.http[url] = HttpResult(503, {"initialized": True, "sealed": True})
    res = check("secret.openbao_health", _ctx(settings, db, r))
    assert (res.status, res.error_code) == ("FAILED", "OPENBAO_SEALED")
    r.http[url] = HttpResult(501, {"initialized": False, "sealed": True})
    res = check("secret.openbao_health", _ctx(settings, db, r))
    assert res.error_code == "OPENBAO_NOT_INITIALIZED" and res.requires_destructive is True
    r.http[url] = HttpResult(200, {"initialized": True, "sealed": False, "version": "2.1.0"})
    res = check("secret.openbao_health", _ctx(settings, db, r))
    assert res.status == "CONNECTED"  # health only; auth/policy/TTL not verified
    monkeypatch.setattr(settings, "openbao_addr", "https://vault.example.com")
    r2 = base_runner()
    res = check("secret.openbao_health", _ctx(settings, db, r2))
    assert res.error_code == "OPENBAO_NOT_LOOPBACK" and r2.http_calls == []


def test_port_conflict_is_explained_never_rebinds_publicly(settings, db, monkeypatch):
    r = base_runner()
    monkeypatch.setattr(host_mod, "tcp_in_use", lambda h, p, timeout=1.0: True)
    res = check("host.port", _ctx(settings, db, r))
    assert res.status == "FAILED" and res.error_code == "PORT_IN_USE"
    assert "0.0.0.0" not in json.dumps(res.remediation) and "--port 8788" in res.remediation["operation"]
    assert "#" not in res.remediation["command"]  # zsh treats '#' literally in interactive shells
    r.http[f"http://127.0.0.1:{settings.port}/api/health"] = HttpResult(200, {"status": "ok", "version": "0.1.0"})
    assert check("host.port", _ctx(settings, db, r)).status == "CONNECTED"
    monkeypatch.setattr(host_mod, "tcp_in_use", lambda h, p, timeout=1.0: False)
    monkeypatch.setattr(host_mod, "can_bind", lambda h, p: True)
    assert check("host.port", _ctx(settings, db, r)).status == "VERIFIED"


def test_missing_data_dir_reports_remedy_without_creating_it(settings, db, tmp_path, monkeypatch):
    target = tmp_path / "not-created"
    monkeypatch.setattr(settings, "data_dir", target)
    res = check("host.data_dir", _ctx(settings, None, base_runner()))
    assert res.status == "MISSING" and "db upgrade" in res.remediation["command"]
    assert not target.exists()


def test_needs_db_checks_are_unknown_without_database(settings):
    res = check("auth.bootstrap", _ctx(settings, None, base_runner()))
    assert res.status == "UNKNOWN" and res.error_code == "DB_NOT_INITIALISED"


def test_mci_is_never_verified(settings, db, tmp_path):
    from tests.unit import mci_support as ms

    live = check("policy.mci", _ctx(settings, db, base_runner()))  # the owner's reference: true in every state
    assert live.status not in ("VERIFIED", "READY", "PASS") and live.observed_evidence["compliance"] == "NOT-PROVEN"
    repo = ms.fake_repo(tmp_path)
    res = check("policy.mci", _ctx(settings, db, base_runner(), repo=repo))
    assert res.status == "MISSING" and res.observed_evidence["alignment"] == "UNVERIFIED"
    assert str(settings.layout.canonical_policy) in res.remediation["operation"]  # local, never committed
    ref = repo / "policy" / "mci-reference.yaml"
    ref.write_text("mci:\n  status: VERIFIED\n  version: '2'\n", encoding="utf-8")
    res = check("policy.mci", _ctx(settings, db, base_runner(), repo=repo))
    # a file claim is not verification: a declared VERIFIED that does not verify FAILS (never CONFIGURED)
    assert res.status == "FAILED" and res.error_code == "MCI_REFERENCE_INCOMPLETE"


def test_mci_declared_verified_must_match_the_local_source_file(settings, db, tmp_path):
    """§08 policy.mci reuses the policy module's verification of the owner's LOCAL source (<data dir>/policy/canonical;
    the public repository never holds it): a declared VERIFIED that does not verify is FAILED with the reason."""
    from aeos.doctor.runner import compute_fingerprint
    from tests.unit import mci_support as ms

    repo = ms.fake_repo(tmp_path)
    local = settings.layout.canonical_policy
    digest = ms.write_source(local)
    ref = repo / "policy" / "mci-reference.yaml"
    in_repo = repo / "policy" / "canonical" / ms.SOURCE_NAME
    in_repo.parent.mkdir(parents=True)
    in_repo.write_bytes((local / ms.SOURCE_NAME).read_bytes())
    cases = ((ms.verified_reference(digest, location="local:missing.md"), "MCI_SOURCE_MISSING"),
             (ms.verified_reference("sha256:" + "0" * 64), "MCI_HASH_MISMATCH"),
             (ms.verified_reference(digest, location="local:../escape.md"), "MCI_LOCATION_OUTSIDE_LOCAL_DIR"),
             (ms.verified_reference(digest, location="policy/canonical/MCI_V2_SOURCE_OF_TRUTH.md"),
              "MCI_LOCATION_NOT_LOCAL"),
             ("mci:\n  status: CERTIFIED\n", "MCI_STATUS_UNKNOWN"))
    for text, code in cases:
        ref.write_text(text, encoding="utf-8")
        res = check("policy.mci", _ctx(settings, db, base_runner(), repo=repo))
        assert (res.status, res.error_code) == ("FAILED", code), text
        assert res.severity == "HIGH" and res.observed_evidence["source_status"] == "UNVERIFIED"
    ref.write_text(ms.verified_reference("sha256:" + "0" * 64), encoding="utf-8")
    res = check("policy.mci", _ctx(settings, db, base_runner(), repo=repo))
    assert str(local / ms.SOURCE_NAME) in res.remediation["command"] and "sha256" in res.remediation["command"]
    ref.write_text(ms.verified_reference(digest), encoding="utf-8")
    ctx = _ctx(settings, db, base_runner(), repo=repo)
    res = check("policy.mci", ctx)
    assert res.status == "CONFIGURED" and res.error_code is None  # the source is present - not VERIFIED alignment
    assert res.observed_evidence["source_status"] == "VERIFIED" and res.observed_evidence["compliance"] == "NOT-PROVEN"
    assert res.observed_evidence["source_location"] == ms.LOCATION and res.component_version == "test-2"
    before = compute_fingerprint(REGISTRY["policy.mci"], ctx)
    ms.write_source(local, "# a different document of another size\n")  # replacing the MCI makes the result STALE
    assert compute_fingerprint(REGISTRY["policy.mci"], ctx) != before
    before = compute_fingerprint(REGISTRY["policy.mci"], ctx)
    ms.write_mapping(local, "packs: []\n")  # so does the owner's local pack mapping
    assert compute_fingerprint(REGISTRY["policy.mci"], ctx) != before
    ref.write_text("mci:\n  status: REFERENCED\n", encoding="utf-8")
    assert check("policy.mci", _ctx(settings, db, base_runner(), repo=repo)).status == "CONFIGURED"


def test_office_defaults_deny_and_tampered_file_fails(settings, db, tmp_path):
    assert check("policy.office_defaults", _ctx(settings, db, base_runner())).status == "VERIFIED"
    repo = tmp_path / "repo"
    (repo / "policy").mkdir(parents=True)
    (repo / "policy" / "office-defaults.yaml").write_text("office:\n  external_worker_allowed: true\n", encoding="utf-8")
    res = check("policy.office_defaults", _ctx(settings, None, base_runner(), repo=repo))
    assert res.status == "FAILED" and res.error_code == "OFFICE_DEFAULT_NOT_DENY"


def test_bootstrap_and_default_credentials(settings, db):
    from aeos.identity.passwords import hash_password
    from aeos.models.identity import User
    from tests.conftest import create_admin

    assert check("auth.bootstrap", _ctx(settings, db, base_runner())).status == "MISSING"
    create_admin(db)
    assert check("auth.bootstrap", _ctx(settings, db, base_runner())).status == "VERIFIED"
    assert check("auth.default_credentials", _ctx(settings, db, base_runner())).status == "VERIFIED"
    db.add(User(username="legacy", password_hash=hash_password("changeme1234"), role="VIEWER"))
    db.commit()
    res = check("auth.default_credentials", _ctx(settings, db, base_runner()))
    assert res.status == "FAILED" and "changeme" not in json.dumps(res.as_dict())


def test_github_adapter_states_and_token_never_exposed(settings, db, secrets_memory):
    url = f"https://api.github.com/repos/{settings.platform_repo_slug}"
    r = base_runner()
    res = check("adapter.github_token", _ctx(settings, db, r))
    assert res.status == "MISSING" and "secrets set personal/github" in res.remediation["command"]
    token = "t" * 40
    secrets_memory.put("personal/github", token, actor="test")
    assert check("adapter.github_token", _ctx(settings, db, r)).status == "CONFIGURED"
    assert r.http_calls == []  # no network without --functional
    r.http[url] = HttpResult(200, {"private": True, "default_branch": "main", "permissions": {"push": True}, "id": 1})
    res = check("adapter.github_token", _ctx(settings, db, r, functional=True))
    assert res.status == "CONNECTED" and r.http_calls[-1]["allow_remote"] is True
    assert token not in json.dumps(res.as_dict())
    r.http[url] = HttpResult(401, {"message": "Bad credentials"})
    assert check("adapter.github_token", _ctx(settings, db, r, functional=True)).status == "EXPIRED"


def test_github_token_privilege_is_reported_from_scopes(settings, db, secrets_memory):
    """§07 step 7 least privilege: a classic/OAuth token with repo/workflow over all repositories is a residual
    risk the Doctor shows (from X-OAuth-Scopes); a fine-grained PAT is least privilege by construction."""
    url = f"https://api.github.com/repos/{settings.platform_repo_slug}"
    body = {"private": True, "default_branch": "main", "permissions": {"push": True}, "id": 1}
    secrets_memory.put("personal/github", "gho_" + "x" * 36, actor="test")
    r = base_runner()
    r.http[url] = HttpResult(200, body, headers={"x-oauth-scopes": "repo, workflow, read:org"})
    res = check("adapter.github_token", _ctx(settings, db, r, functional=True))
    priv = res.observed_evidence["token_privilege"]
    assert res.status == "CONNECTED" and priv["least_privilege"] is False and priv["broad_scopes"] == ["repo",
                                                                                                         "workflow"]
    assert "fine-grained" in res.observed_evidence["residual_risks"][0]
    assert "gho_" not in json.dumps(res.as_dict())  # only the documented prefix kind, never the value
    secrets_memory.put("personal/github", "github_pat_" + "y" * 40, actor="test")
    r.http[url] = HttpResult(200, body)  # fine-grained tokens carry no X-OAuth-Scopes
    repos_url = "https://api.github.com/user/repos?per_page=100"
    r.http[repos_url] = HttpResult(200, [{"full_name": settings.platform_repo_slug}])  # only the selected repo
    res = check("adapter.github_token", _ctx(settings, db, r, functional=True))
    assert res.observed_evidence["token_privilege"]["least_privilege"] is True
    assert "residual_risks" not in res.observed_evidence


def test_public_repositories_a_fine_grained_pat_can_read_are_not_a_breach(settings, db, secrets_memory, monkeypatch):
    """GitHub lists every public repository for a fine-grained PAT ("Also includes public repositories
    (read-only)"), so an unregistered public repository is no breach but does not prove the selection either (None);
    an unregistered private one (or one whose visibility is not reported) still is."""
    from aeos.doctor.checks import adapters as adapters_mod

    slug = settings.platform_repo_slug
    url = f"https://api.github.com/repos/{slug}"
    repos_url = "https://api.github.com/user/repos?per_page=100"
    secrets_memory.put("personal/github", "github_pat_" + "y" * 40, actor="test")
    monkeypatch.setattr(adapters_mod, "_administration_grant", lambda ctx, slug, branch: "NOT_GRANTED")
    r = base_runner()
    r.http[url] = HttpResult(200, {"private": False, "default_branch": "main", "id": 1,
                                   "permissions": {"admin": True, "push": True}})
    r.http[repos_url] = HttpResult(200, [{"full_name": slug, "private": False},
                                         {"full_name": "owner/other-public", "private": False}])
    res = check("adapter.github_token", _ctx(settings, db, r, functional=True))
    priv = res.observed_evidence["token_privilege"]
    assert res.status == "CONNECTED" and priv["least_privilege"] is None and "residual_risks" not in res.observed_evidence
    assert priv["beyond_registered_public"] == ["owner/other-public"] and priv["beyond_registered"] == []
    r.http[repos_url] = HttpResult(200, [{"full_name": slug, "private": False},
                                         {"full_name": "owner/secret-stuff", "private": True}])
    priv = check("adapter.github_token", _ctx(settings, db, r, functional=True)).observed_evidence["token_privilege"]
    assert priv["least_privilege"] is False and "private" in priv["risk"]
    r.http[repos_url] = HttpResult(200, [{"full_name": slug, "private": False}])
    priv = check("adapter.github_token", _ctx(settings, db, r, functional=True)).observed_evidence["token_privilege"]
    assert priv["least_privilege"] is True


def test_github_token_expiry_is_read_and_warned_two_weeks_ahead(settings, db, secrets_memory):
    """A fine-grained PAT reports its expiry in GitHub-Authentication-Token-Expiration (``... UTC`` or ``... +0700``);
    the Doctor records it and warns GITHUB_EXPIRY_WARN_DAYS ahead without turning the check red."""
    from datetime import timedelta

    from aeos.core.timeutil import utcnow

    slug = settings.platform_repo_slug
    url = f"https://api.github.com/repos/{slug}"
    body = {"private": False, "default_branch": "main", "permissions": {"push": True}, "id": 1}
    secrets_memory.put("personal/github", "github_pat_" + "y" * 40, actor="test")
    r = base_runner()
    r.http["https://api.github.com/user/repos?per_page=100"] = HttpResult(200, [{"full_name": slug}])
    far = (utcnow() + timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S UTC")
    r.http[url] = HttpResult(200, body, headers={"github-authentication-token-expiration": far})
    res = check("adapter.github_token", _ctx(settings, db, r, functional=True))
    assert res.status == "CONNECTED" and res.observed_evidence["token_expires_at"].startswith(far[:10])
    assert "kedaluwarsa" not in res.summary and not res.remediation
    near = (utcnow() + timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S +0700")
    r.http[url] = HttpResult(200, body, headers={"github-authentication-token-expiration": near})
    res = check("adapter.github_token", _ctx(settings, db, r, functional=True))
    assert res.status == "CONNECTED" and "kedaluwarsa" in res.summary and "hari lagi" in res.summary
    assert "secrets set personal/github" in json.dumps(res.remediation)
    assert "kedaluwarsa" in res.observed_evidence["residual_risks"][0]
    r.http[url] = HttpResult(200, body, headers={"github-authentication-token-expiration": "soon"})
    assert check("adapter.github_token", _ctx(settings, db, r, functional=True)).observed_evidence[
        "token_expires_at"] is None  # unreadable header: no expiry claimed


def test_github_token_recorded_expiry_warns_without_a_probe(settings, db, secrets_memory, monkeypatch):
    """`aeos secrets set personal/github --expires-at <date>` is enough for the plain Doctor to warn."""
    from datetime import timedelta

    from aeos.core.timeutil import utcnow
    from aeos.doctor.checks import adapters as adapters_mod

    soon = (utcnow() + timedelta(days=3)).isoformat()
    monkeypatch.setattr(adapters_mod, "secret_ref_status",
                        lambda ref: ("PRESENT", {"ref": ref, "backend": "memory", "expires_at": soon}))
    res = check("adapter.github_token", _ctx(settings, db, base_runner()))
    assert res.status == "CONFIGURED" and "kedaluwarsa" in res.summary and res.remediation
    later = (utcnow() + timedelta(days=90)).isoformat()
    monkeypatch.setattr(adapters_mod, "secret_ref_status",
                        lambda ref: ("PRESENT", {"ref": ref, "backend": "memory", "expires_at": later}))
    res = check("adapter.github_token", _ctx(settings, db, base_runner()))
    assert res.status == "CONFIGURED" and "kedaluwarsa" not in res.summary and not res.remediation


def test_fine_grained_pat_is_judged_by_what_it_reaches(settings, db, secrets_memory):
    """§07 step 7: a fine-grained PAT is not least privilege "by construction" - a token created for 'All
    repositories' or with Administration (permissions.admin) is reported, and an unreadable repository
    selection stays NOT proven (never True)."""
    url = f"https://api.github.com/repos/{settings.platform_repo_slug}"
    repos_url = "https://api.github.com/user/repos?per_page=100"
    secrets_memory.put("personal/github", "github_pat_" + "y" * 40, actor="test")
    r = base_runner()
    r.http[url] = HttpResult(200, {"private": True, "default_branch": "main", "id": 1,
                                   "permissions": {"admin": True, "push": True}})
    r.http[repos_url] = HttpResult(200, [{"full_name": settings.platform_repo_slug}])
    # permissions.admin is the ACCOUNT's role (the owner of a personal repo always shows it): never read as the
    # token's grant. Unobserved -> NOT proven, never True and never a false "has Administration" claim.
    priv = check("adapter.github_token", _ctx(settings, db, r, functional=True)).observed_evidence["token_privilege"]
    assert priv["least_privilege"] is None and priv["admin"] is True and priv["administration_permission"] == "UNKNOWN"
    assert "risk" not in priv
    prot_url = f"https://api.github.com/repos/{settings.platform_repo_slug}/branches/main/protection"
    r.http[prot_url] = HttpResult(403, {"message": "Resource not accessible by personal access token"})
    priv = check("adapter.github_token", _ctx(settings, db, r, functional=True)).observed_evidence["token_privilege"]
    assert priv["least_privilege"] is True and priv["administration_permission"] == "NOT_GRANTED"
    r.http[prot_url] = HttpResult(404, {"message": "Branch not protected"})  # the token itself holds Administration
    priv = check("adapter.github_token", _ctx(settings, db, r, functional=True)).observed_evidence["token_privilege"]
    assert priv["least_privilege"] is False and priv["administration_permission"] == "GRANTED"
    assert "Administration" in priv["risk"]
    r.http[prot_url] = HttpResult(403, {"message": "Upgrade to GitHub Pro or make this repository public"})
    priv = check("adapter.github_token", _ctx(settings, db, r, functional=True)).observed_evidence["token_privilege"]
    assert priv["least_privilege"] is None and priv["administration_permission"] == "UNKNOWN"
    del r.http[prot_url]
    r.http[url] = HttpResult(200, {"private": True, "default_branch": "main", "id": 1, "permissions": {"push": True}})
    r.http[repos_url] = HttpResult(200, [{"full_name": settings.platform_repo_slug}, {"full_name": "owner/private-x"},
                                         {"full_name": "owner/other"}])
    res = check("adapter.github_token", _ctx(settings, db, r, functional=True))
    priv = res.observed_evidence["token_privilege"]
    assert priv["least_privilege"] is False and priv["beyond_registered"] == ["owner/other", "owner/private-x"]
    assert res.observed_evidence["residual_risks"] and "All repositories" in res.observed_evidence["residual_risks"][0]
    from aeos.models.portfolio import Project, RepositoryReference

    p = Project(project_key="app", name="app", domain="PERSONAL")
    db.add(p)
    db.flush()
    for name in ("owner/private-x", "owner/other"):  # registered project repositories are legitimate targets
        db.add(RepositoryReference(project_id=p.id, provider="GITHUB", full_name=name))
    db.commit()
    assert check("adapter.github_token", _ctx(settings, db, r, functional=True)).observed_evidence[
        "token_privilege"]["least_privilege"] is True
    r.http[repos_url] = HttpResult(403, {"message": "Resource not accessible"})
    res = check("adapter.github_token", _ctx(settings, db, r, functional=True))
    assert res.observed_evidence["token_privilege"]["least_privilege"] is None
    assert "belum terbukti" in res.summary


def test_google_adapter_reads_the_token_google_connect_writes(settings, db, secrets_memory):
    """W1 seam: the check reads personal/google-oauth-token (written by `aeos publish google-connect`, read by
    the publisher) and its remedy is that consent flow - never `aeos secrets set` of a typed-in token."""
    r = base_runner()
    res = check("adapter.google_oauth", _ctx(settings, db, r))
    assert res.status == "MISSING" and res.error_code == "GOOGLE_OAUTH_MISSING"
    assert "publish google-connect" in res.remediation["command"] and "secrets set" not in res.remediation["command"]
    assert res.remediation["executed_by"].startswith("MANUSIA") and res.remediation["command"].isascii()
    secrets_memory.put("personal/google-oauth-client", '{"installed": {"client_id": "fake"}}', actor="test")
    secrets_memory.put("personal/google-oauth-token", '{"token": "fake-not-real"}', actor="test")
    res = check("adapter.google_oauth", _ctx(settings, db, r))
    assert res.status == "CONFIGURED" and "fake-not-real" not in json.dumps(res.as_dict())
    assert r.http_calls == []  # configuration only; no network


@pytest.mark.parametrize("status,body,code,plan", [
    (403, {"message": "Upgrade to GitHub Pro or make this repository public to enable this feature."},
     "BRANCH_PROTECTION_NOT_ENFORCEABLE", "free"),
    (404, {"message": "Branch not protected"}, "BRANCH_PROTECTION_NOT_ENFORCEABLE", "free"),  # private Free: 404
    (404, {"message": "Branch not protected"}, "BRANCH_NOT_PROTECTED", "pro"),
])
def test_branch_protection_residual_risk(settings, db, secrets_memory, status, body, code, plan):
    """The functional probe goes through aeos.delivery.provider.check_capabilities (GET only, faked)."""
    import respx
    from httpx import Response

    secrets_memory.put("personal/github", "t" * 40, actor="test")
    slug = settings.platform_repo_slug
    r = base_runner()
    with respx.mock(assert_all_called=False) as gh:
        gh.get(f"https://api.github.com/repos/{slug}").mock(return_value=Response(200, json={
            "default_branch": "main", "private": True, "visibility": "private",
            "owner": {"type": "User", "login": slug.split("/")[0]}}))
        gh.get("https://api.github.com/user").mock(return_value=Response(200, json={
            "login": slug.split("/")[0], "plan": {"name": plan}}))
        gh.get(f"https://api.github.com/repos/{slug}/branches/main/protection").mock(
            return_value=Response(status, json=body))
        gh.get(f"https://api.github.com/repos/{slug}/rulesets").mock(return_value=Response(status, json=body))
        gh.get(f"https://api.github.com/repos/{slug}/rules/branches/main").mock(return_value=Response(200, json=[]))
        gh.get(f"https://api.github.com/repos/{slug}/environments").mock(
            return_value=Response(200, json={"total_count": 0, "environments": []}))
        res = check("delivery.branch_protection", _ctx(settings, db, r, functional=True))
        assert all(c.request.method == "GET" for c in gh.calls)
    assert res.status == "MISSING" and res.error_code == code
    assert res.observed_evidence["enforceable"] is False and res.observed_evidence["residual_risks"]
    assert "t" * 40 not in json.dumps(res.as_dict())


def test_worker_engine_status_through_facade(settings, db, monkeypatch, absent_modules):
    absent_modules("aeos.worker_runtime.opencode")
    res = check("worker.opencode", _ctx(settings, db, base_runner()))
    assert res.status == "UNKNOWN" and res.error_code == "WORKER_RUNTIME_NOT_IMPLEMENTED"
    import aeos.worker_runtime.facade as worker

    # worker_runtime contract: status INSTALLED|FAILED|MISSING, binary, version, pinned_version, drift
    monkeypatch.setattr(worker, "opencode_status", lambda: {
        "status": "INSTALLED", "version": "1.19.3", "pinned_version": "1.19.3", "drift": False,
        "binary": str(Path.home() / "bin" / "opencode"), "functional": "NOT_PROVEN"})
    res = check("worker.opencode", _ctx(settings, db, base_runner()))
    assert res.status == "VERIFIED" and res.observed_evidence["location"].startswith("<home>")
    monkeypatch.setattr(worker, "opencode_status", lambda: {"status": "MISSING", "pinned_version": "1.19.3"})
    res = check("worker.opencode", _ctx(settings, db, base_runner()))
    assert res.status == "MISSING" and "opencode-ai@1.19.3" in res.remediation["command"]
    monkeypatch.setattr(worker, "opencode_status", lambda: {"status": "OK", "version": "1.2.3"})
    assert check("worker.opencode", _ctx(settings, db, base_runner())).status == "UNKNOWN"  # unknown contract


def test_secret_store_fail_closed_and_status_without_values(settings, db, secrets_memory):
    res = check("secret.status", _ctx(settings, db, base_runner()))
    assert res.status == "CONFIGURED"
    secrets_memory.put("workers/openrouter", "v" * 30, actor="test")
    res = check("secret.status", _ctx(settings, db, base_runner()))
    assert res.observed_evidence["refs"] == ["workers/openrouter"] and "v" * 30 not in json.dumps(res.as_dict())
    key = check("route.worker_key", _ctx(settings, db, base_runner()))
    assert key.status == "CONFIGURED" and key.observed_evidence["paid_call_made"] is False


def test_worker_key_remedy_names_the_eligible_routes_own_provider(settings, db, secrets_memory, monkeypatch):
    """An eligible OpenCode Go route (ADR-0041 amendment 2026-10-26) needs workers/opencode-go: the check and its remedy
    name that key, never the OpenRouter default."""
    from aeos.doctor.checks import model_route
    from aeos.routing import seed

    seed.run(db)
    db.commit()
    monkeypatch.setattr(model_route, "eligible_worker_routes", lambda s: (["opencode-go/glm-5.3-flash"], {}, None))
    res = check("route.worker_key", _ctx(settings, db, base_runner()))
    assert res.status == "MISSING" and res.error_code == "WORKER_KEY_MISSING"
    assert "workers/opencode-go" in res.observed_evidence["ref_states"]
    assert "secrets set workers/opencode-go" in res.remediation["command"]
    assert "OpenCode Go" in json.dumps(res.remediation) and "OpenRouter" not in json.dumps(res.remediation)
    secrets_memory.put("workers/opencode-go", "g" * 30, actor="test")
    res = check("route.worker_key", _ctx(settings, db, base_runner()))
    assert res.status == "CONFIGURED" and "g" * 30 not in json.dumps(res.as_dict())


def test_secret_store_unavailable_is_missing(settings, db):
    assert check("secret.status", _ctx(settings, db, base_runner())).status == "MISSING"
    res = check("route.worker_key", _ctx(settings, db, base_runner()))
    assert res.status == "MISSING" and res.error_code == "SECRET_STORE_UNAVAILABLE"


def test_persisted_raw_secret_is_detected_without_showing_it(settings, db):
    from aeos.models.audit import Event

    raw = "ghp_" + "A1b2C3d4" * 4
    db.add(Event(source="test", kind="x", payload={"note": raw}))  # bypasses emit() redaction on purpose
    db.commit()
    res = check("secret.no_raw_exposure", _ctx(settings, db, base_runner()))
    assert res.status == "FAILED" and res.observed_evidence["findings"]["events"] == 1
    assert raw not in json.dumps(res.as_dict())
    assert check("evidence.trace_redaction", _ctx(settings, db, base_runner())).status == "VERIFIED"


def test_recovery_and_evidence_honesty(settings, db):
    assert check("recovery.restore_test", _ctx(settings, db, base_runner())).summary.startswith("Restore belum diuji")
    assert check("recovery.backups", _ctx(settings, db, base_runner())).status == "MISSING"
    assert check("evidence.acceptance_suite", _ctx(settings, db, base_runner())).status in ("MISSING", "CONFIGURED")
    assert check("evidence.verifier", _ctx(settings, db, base_runner())).status in ("MISSING", "CONFIGURED")
    assert check("runtime.db", _ctx(settings, db, base_runner())).status == "VERIFIED"
    res = check("runtime.storage", _ctx(settings, db, base_runner()))
    assert res.status == "VERIFIED" and res.observations[0]["dimension"] == "functional"


def test_ci_workflow_pinning(settings, db, tmp_path):
    assert check("delivery.ci_workflow", _ctx(settings, db, base_runner())).status == "VERIFIED"  # repo CI is pinned
    repo = tmp_path / "repo"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text("steps:\n  - uses: actions/checkout@v4\n", encoding="utf-8")
    res = check("delivery.ci_workflow", _ctx(settings, db, base_runner(), repo=repo))
    assert res.status == "FAILED" and res.observed_evidence["unpinned"] == ["actions/checkout@v4"]


def test_optional_docker_missing_is_low_severity(settings, db):
    res = check("containment.docker", _ctx(settings, db, base_runner()))
    assert res.status == "MISSING" and res.severity == "LOW"


def test_raising_check_is_unknown_not_green(settings, db, monkeypatch):
    load_all()
    spec = REGISTRY["host.platform"]

    def boom(ctx):
        raise RuntimeError("probe exploded")

    monkeypatch.setitem(REGISTRY, "host.platform", dataclasses.replace(spec, fn=boom))
    res = run_check(REGISTRY["host.platform"], _ctx(settings, db, base_runner()))
    assert res.status == "UNKNOWN" and res.error_code == "PROBE_ERROR"


def test_remedies_are_proposals_with_all_fields(settings, db):
    load_all()
    ctx = _ctx(settings, db, base_runner())
    fields = {"operation", "command", "source", "target", "privilege", "cost", "rollback", "evidence_to_collect",
              "executed_by"}
    for cid in ("claude.binary", "secret.openbao_binary", "worker.opencode", "repo.lockfiles", "recovery.restore_test"):
        rem = run_check(REGISTRY[cid], ctx).remediation
        if rem:
            assert fields <= set(rem), cid
            assert rem["executed_by"].startswith("MANUSIA")


def test_repo_checks_against_real_worktree(settings, db):
    r = base_runner()
    res = check("repo.root", _ctx(settings, db, r, repo=repo_root()))
    assert res.status == "VERIFIED"
    origin = check("repo.origin", _ctx(settings, db, r))
    assert origin.status == "VERIFIED" and origin.target_identity == f"github:{settings.platform_repo_slug}"
    from tests.unit.test_inventory_fakes import git_handler

    r.outputs["git"] = git_handler(remote="https://github.com/someone/fork.git")
    assert check("repo.origin", _ctx(settings, db, r)).error_code == "ORIGIN_MISMATCH"
    r.outputs["git"] = git_handler(remote="https://user:ghp_" + "x" * 30 + "@github.com/a/b.git")
    res = check("repo.origin", _ctx(settings, db, r))
    assert "ghp_" not in json.dumps(res.as_dict())


def test_system_probe_runner_keeps_only_the_listed_response_headers():
    """The real runner passes the token expiry through and never keeps other headers (cookies, request ids)."""
    import httpx
    import respx

    from aeos.inventory.probes import SystemProbeRunner

    with respx.mock() as m:
        m.get("https://api.github.com/repos/o/r").mock(return_value=httpx.Response(200, json={"id": 1}, headers={
            "GitHub-Authentication-Token-Expiration": "2027-01-24 10:57:27 UTC", "X-OAuth-Scopes": "repo",
            "Set-Cookie": "session=secret", "X-GitHub-Request-Id": "ABC"}))
        hr = SystemProbeRunner().http_get("https://api.github.com/repos/o/r", timeout=5.0, allow_remote=True)
    assert hr.headers == {"github-authentication-token-expiration": "2027-01-24 10:57:27 UTC",
                          "x-oauth-scopes": "repo"}
