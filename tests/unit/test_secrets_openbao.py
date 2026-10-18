"""OpenBao provider (U-13, §26, S24) via respx: AppRole login from the OS keyring bootstrap store,
KV v2 read/write/metadata/list, sealed/uninitialised/unreachable -> Blocked, scope enforcement,
token renew/re-login, redaction of every fetched value (§50 'Worker membaca vault', 'Trace berisi
secret/PII')."""

from __future__ import annotations

import json
import re

import httpx
import pytest
import respx

from aeos.core.errors import Blocked, PolicyDenied, ValidationFailed
from aeos.core.redaction import redact_text
from aeos.secrets.openbao import OpenBaoClient, OpenBaoUnavailable
from aeos.secrets.provider import (
    KeyringBootstrapStore,
    OpenBaoProvider,
    bootstrap_store,
    check_purpose,
    reset_provider_cache,
)
from tests.unit.test_routing_fakes import memory_keyring  # noqa: F401 - fixture

ADDR = "http://127.0.0.1:8200"
TOKEN = "s." + "T" * 26  # token-shaped on purpose (redaction must catch it)
ROLE_ID = "role-" + "r" * 20
SECRET_ID = "secret-" + "x" * 20
VALUE = "gh-test-value-" + "v" * 20


@pytest.fixture(autouse=True)
def _reset():
    reset_provider_cache()
    yield
    reset_provider_cache()


@pytest.fixture
def bao(settings, memory_keyring):  # noqa: F811
    store = bootstrap_store(settings)
    store.set_approle(ROLE_ID, SECRET_ID)
    return OpenBaoProvider(settings, store)


def _healthy(sealed=False, initialized=True):
    return respx.get(f"{ADDR}/v1/sys/health").mock(return_value=httpx.Response(
        200, json={"initialized": initialized, "sealed": sealed, "standby": False, "version": "2.7.1"}))


CONTROLLER = ["aeos-personal", "aeos-platform", "aeos-projects", "aeos-workers", "default"]


def _login(lease=3600, renewable=True, token=TOKEN, policies=None):
    return respx.post(f"{ADDR}/v1/auth/approle/login").mock(return_value=httpx.Response(
        200, json={"auth": {"client_token": token, "lease_duration": lease, "renewable": renewable,
                            "policies": CONTROLLER if policies is None else policies}}))


@respx.mock
def test_get_uses_approle_token_and_registers_value(bao):
    _healthy()
    login = _login()
    read = respx.get(f"{ADDR}/v1/aeos/data/personal/github").mock(return_value=httpx.Response(200, json={
        "data": {"data": {"value": VALUE}, "metadata": {"created_time": "2026-10-18T01:00:00Z", "version": 1,
                                                          "custom_metadata": {"expires_at": ""}}}}))
    assert bao.get("personal/github", purpose="personal:broker.github", actor="broker") == VALUE
    body = json.loads(login.calls.last.request.content)
    assert body == {"role_id": ROLE_ID, "secret_id": SECRET_ID}
    assert read.calls.last.request.headers["X-Vault-Token"] == TOKEN
    assert redact_text(f"leak {VALUE} and {TOKEN}") == "leak [REDACTED] and [REDACTED]"
    # cached token: a second read does not log in again
    bao.get("personal/github", purpose="personal:broker.github", actor="broker")
    assert login.call_count == 1


@respx.mock
def test_put_status_list_and_delete_never_return_values(bao):
    _healthy()
    _login()
    write = respx.post(f"{ADDR}/v1/aeos/data/workers/openrouter").mock(
        return_value=httpx.Response(200, json={"data": {"version": 1}}))
    meta_write = respx.post(f"{ADDR}/v1/aeos/metadata/workers/openrouter").mock(return_value=httpx.Response(204))
    respx.get(f"{ADDR}/v1/aeos/metadata/workers/openrouter").mock(return_value=httpx.Response(200, json={"data": {
        "created_time": "2026-10-18T01:00:00Z", "updated_time": "2026-10-18T02:00:00Z", "current_version": 1,
        "custom_metadata": {"updated_by": "cli", "expires_at": "2027-01-31T00:00:00+00:00",
                            "scope": json.dumps({"routes": ["openrouter/*"]})}}}))
    st = bao.put("workers/openrouter", VALUE, actor="cli")
    assert json.loads(write.calls.last.request.content) == {"data": {"value": VALUE}}
    custom = json.loads(meta_write.calls.last.request.content)["custom_metadata"]
    assert custom["updated_by"] == "cli" and VALUE not in json.dumps(custom)
    assert st.exists and st.backend == "openbao" and st.updated_at.year == 2026 and st.expires_at.year == 2027
    assert st.scope == {"routes": ["openrouter/*"]} and VALUE not in repr(st)

    def listing(request):
        prefix = request.url.path.split("/metadata/", 1)[1]
        keys = {"workers/": ["openrouter"], "projects/": ["p1/"], "projects/p1/": ["github"]}.get(prefix)
        return httpx.Response(200, json={"data": {"keys": keys}}) if keys else httpx.Response(404)

    respx.get(url__regex=re.escape(ADDR) + r"/v1/aeos/metadata/[a-z0-9/]+/(\?.*)?$").mock(side_effect=listing)
    respx.get(f"{ADDR}/v1/aeos/metadata/projects/p1/github").mock(return_value=httpx.Response(200, json={"data": {
        "updated_time": "2026-10-18T03:00:00Z", "current_version": 2, "custom_metadata": {}}}))
    refs = [s.ref for s in bao.list_status()]
    assert refs == ["projects/p1/github", "workers/openrouter"]
    assert bao.denied_namespaces == ["office"]  # not bound to the controller: never even listed
    assert bao.token_view()["policies"] == sorted(CONTROLLER) and TOKEN not in repr(bao.token_view())
    delete = respx.delete(f"{ADDR}/v1/aeos/metadata/workers/openrouter").mock(return_value=httpx.Response(204))
    bao.delete("workers/openrouter", actor="cli")
    assert delete.called


@respx.mock
def test_list_status_survives_a_denied_namespace(bao):
    """Real OpenBao answers 403 for LIST on a namespace the token is not bound to (observed on
    2.7.1); the listing must skip it, not fail the whole status page (all refs UNKNOWN)."""
    _healthy()
    _login(policies=[])  # policies not reported -> the provider has to ask

    def listing(request):
        prefix = request.url.path.split("/metadata/", 1)[1]
        if prefix == "office/":
            return httpx.Response(403, json={"errors": ["permission denied"]})
        keys = {"personal/": ["github"]}.get(prefix)
        return httpx.Response(200, json={"data": {"keys": keys}}) if keys else httpx.Response(404)

    respx.get(url__regex=re.escape(ADDR) + r"/v1/aeos/metadata/[a-z0-9/]+/(\?.*)?$").mock(side_effect=listing)
    respx.get(f"{ADDR}/v1/aeos/metadata/personal/github").mock(return_value=httpx.Response(200, json={"data": {
        "updated_time": "2026-10-18T03:00:00Z", "current_version": 1, "custom_metadata": {}}}))
    assert [s.ref for s in bao.list_status()] == ["personal/github"]
    assert bao.denied_namespaces == ["office"]


@respx.mock
@pytest.mark.parametrize("health, needle", [
    ({"initialized": True, "sealed": True}, "sealed"),
    ({"initialized": False, "sealed": True}, "not initialised"),
])
def test_sealed_or_uninitialised_blocks(bao, health, needle):
    respx.get(f"{ADDR}/v1/sys/health").mock(return_value=httpx.Response(200, json=health))
    with pytest.raises(Blocked) as ei:
        bao.get("personal/github", purpose="personal:broker", actor="t")
    assert ei.value.code == "SECRET_STORE_UNAVAILABLE" and needle in ei.value.message


@respx.mock
def test_unreachable_blocks(bao):
    respx.get(f"{ADDR}/v1/sys/health").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(Blocked) as ei:
        bao.get("personal/github", purpose="personal:broker", actor="t")
    assert "unreachable" in ei.value.message


@respx.mock
def test_missing_expired_and_relogin_on_403(bao):
    _healthy()
    login = _login()
    respx.get(f"{ADDR}/v1/aeos/data/personal/none").mock(return_value=httpx.Response(404))
    with pytest.raises(Blocked) as ei:
        bao.get("personal/none", purpose="personal:x", actor="t")
    assert ei.value.code == "SECRET_MISSING"
    respx.get(f"{ADDR}/v1/aeos/data/personal/old").mock(return_value=httpx.Response(200, json={"data": {
        "data": {"value": VALUE}, "metadata": {"custom_metadata": {"expires_at": "2020-01-01T00:00:00Z"}}}}))
    with pytest.raises(Blocked) as ei:
        bao.get("personal/old", purpose="personal:x", actor="t")
    assert ei.value.code == "SECRET_EXPIRED"
    responses = iter([httpx.Response(403, json={"errors": ["permission denied"]}),
                      httpx.Response(200, json={"data": {"data": {"value": VALUE}, "metadata": {}}})])
    respx.get(f"{ADDR}/v1/aeos/data/personal/rotated").mock(side_effect=lambda req: next(responses))
    assert bao.get("personal/rotated", purpose="personal:x", actor="t") == VALUE
    assert login.call_count == 2  # revoked token -> one fresh AppRole login


@respx.mock
def test_policy_denial_after_relogin_is_policy_denied(bao):
    _healthy()
    _login()
    respx.get(f"{ADDR}/v1/aeos/data/personal/x").mock(return_value=httpx.Response(403, json={"errors": ["denied"]}))
    with pytest.raises(PolicyDenied):
        bao.get("personal/x", purpose="personal:x", actor="t")


@respx.mock
def test_token_renewed_before_expiry(bao):
    _healthy()
    _login(lease=20)
    renew = respx.post(f"{ADDR}/v1/auth/token/renew-self").mock(return_value=httpx.Response(200, json={
        "auth": {"client_token": TOKEN, "lease_duration": 3600, "renewable": True}}))
    respx.get(f"{ADDR}/v1/aeos/data/personal/a").mock(return_value=httpx.Response(200, json={
        "data": {"data": {"value": VALUE}, "metadata": {}}}))
    bao.get("personal/a", purpose="personal:x", actor="t")
    bao.get("personal/a", purpose="personal:x", actor="t")
    assert renew.called


@respx.mock
def test_missing_approle_credentials_blocks(settings, memory_keyring):  # noqa: F811
    _healthy()
    p = OpenBaoProvider(settings, KeyringBootstrapStore("empty-service"))
    with pytest.raises(Blocked) as ei:
        p.get("personal/github", purpose="personal:x", actor="t")
    assert "AppRole credentials are missing" in ei.value.message


def test_scope_enforcement():
    check_purpose("personal/github", "personal:broker.github")
    check_purpose("workers/openrouter", "workers.broker")
    check_purpose("projects/p1/github", "projects:p1:broker")
    for ref, purpose in (("office/atlassian", "personal:publisher"), ("personal/github", "office:publisher"),
                         ("personal/github", "broker"), ("projects/p1/github", "projects:p2:broker"),
                         ("workers/openrouter", "")):
        with pytest.raises(PolicyDenied):
            check_purpose(ref, purpose)
    for bad in ("personal/../x", "unknown/x", "personal/", "personal/a b"):
        with pytest.raises(ValidationFailed):
            check_purpose(bad, "personal:x")


def test_non_loopback_addr_refused():
    with pytest.raises(OpenBaoUnavailable):
        OpenBaoClient("http://10.0.0.5:8200")
    with pytest.raises(OpenBaoUnavailable):
        OpenBaoClient("http://vault.example.com:8200")


def _projects(db):
    from aeos.models.portfolio import Project

    personal = Project(project_key="sec-personal", name="Personal project", domain="PERSONAL")
    office = Project(project_key="sec-office", name="Office project", domain="OFFICE")
    db.add_all([personal, office])
    db.commit()
    return personal.id, office.id


@respx.mock
def test_project_secrets_only_for_registered_personal_projects(bao, db):
    """§23/§26: the controller is bound to aeos-projects but not aeos-office, so an OFFICE project's
    credential under projects/ would bypass OFFICE default deny; unknown projects fail closed too."""
    _healthy()
    _login()
    personal, office = _projects(db)
    respx.get(f"{ADDR}/v1/aeos/data/projects/{personal}/github").mock(return_value=httpx.Response(200, json={
        "data": {"data": {"value": VALUE}, "metadata": {}}}))
    calls = respx.route(url__regex=rf".*/projects/({office}|unknown-project)/.*").mock(
        return_value=httpx.Response(200, json={"data": {"data": {"value": VALUE}, "metadata": {}}}))
    assert bao.get(f"projects/{personal}/github", purpose=f"projects:{personal}:broker", actor="t") == VALUE
    for pid in (office, "unknown-project"):
        with pytest.raises(PolicyDenied):
            bao.get(f"projects/{pid}/github", purpose=f"projects:{pid}:broker", actor="t")
        with pytest.raises(PolicyDenied):
            bao.put(f"projects/{pid}/github", VALUE, actor="cli")
        with pytest.raises(PolicyDenied):
            bao.delete(f"projects/{pid}/github", actor="cli")
    assert not calls.called  # refused before any vault request


@respx.mock
def test_verify_roundtrip_reports_without_values(bao):
    _healthy()
    _login()
    stored: dict = {}

    def write(request):
        stored["value"] = json.loads(request.content)["data"]["value"]
        return httpx.Response(200, json={"data": {"version": 1}})

    respx.post(f"{ADDR}/v1/aeos/data/platform/aeos-healthcheck").mock(side_effect=write)
    respx.post(f"{ADDR}/v1/aeos/metadata/platform/aeos-healthcheck").mock(return_value=httpx.Response(204))
    respx.get(f"{ADDR}/v1/aeos/metadata/platform/aeos-healthcheck").mock(return_value=httpx.Response(
        200, json={"data": {"updated_time": "2026-10-18T03:00:00Z", "current_version": 1, "custom_metadata": {}}}))
    respx.get(f"{ADDR}/v1/aeos/data/platform/aeos-healthcheck").mock(side_effect=lambda req: httpx.Response(
        200, json={"data": {"data": {"value": stored["value"]}, "metadata": {}}}))
    delete = respx.delete(f"{ADDR}/v1/aeos/metadata/platform/aeos-healthcheck").mock(return_value=httpx.Response(204))
    res = bao.verify_roundtrip()
    assert res["status"] == "VERIFIED" and [s["id"] for s in res["steps"]] == ["write", "read", "delete"]
    assert delete.called and stored["value"] not in json.dumps(res)
    assert res["token"]["present"] and "aeos-office" not in res["token"]["policies"]


@respx.mock
def test_status_projection_marks_unbound_office_namespace_no_access(bao, settings, monkeypatch):
    """§26 status without values: office/* under the controller AppRole (office policy not bound) is
    NO_ACCESS - not 'missing' with a `secrets set` remedy that OpenBao would refuse - and the
    controller never even asks OpenBao for office paths."""
    from aeos.secrets import provider as provider_mod
    from aeos.secrets.health import secrets_status, store_health

    _healthy()
    _login()
    office_calls = respx.route(url__regex=r".*/v1/aeos/(data|metadata)/office/.*").mock(
        return_value=httpx.Response(403, json={"errors": ["permission denied"]}))
    respx.get(url__regex=r".*/v1/aeos/metadata/[a-z]+/(\?.*)?$").mock(return_value=httpx.Response(404))
    respx.get(url__regex=r".*/v1/aeos/metadata/(personal|workers)/[a-z-]+$").mock(return_value=httpx.Response(404))
    monkeypatch.setattr(provider_mod, "openbao_provider", lambda _s: bao)
    rows = {i["ref"]: i for i in secrets_status(settings, include_office=True)["items"]}
    assert rows["office/atlassian"]["state"] == "NO_ACCESS" and rows["office/atlassian"]["remedy"] is None
    assert rows["personal/github"]["state"] == "MISSING"
    assert rows["personal/github"]["remedy"] == "aeos secrets set personal/github"
    assert not office_calls.called
    h = store_health(settings)
    assert h["status"] == "CONNECTED" and h["functional_check"]["status"] == "NOT_TESTED"  # CONNECTED != VERIFIED
    assert h["openbao"]["token"]["present"] and TOKEN not in json.dumps(h)
    assert any(r["command"] == "aeos vault status --verify" for r in h["remedies"])
