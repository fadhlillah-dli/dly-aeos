"""Secret store selection and status (U-13, §26, §35; §50 'Worker membaca vault', 'Viewer mengakses
... Office', 'Trace berisi secret'): fail closed, interim OS keyring only with an owner EXCEPTION
decision, status/health never contain values, no web write endpoint."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import respx

from aeos.core.enums import Domain
from aeos.core.errors import Blocked, PolicyDenied
from aeos.core.timeutil import utcnow
from aeos.secrets import facade
from aeos.secrets.health import secrets_status, store_health
from aeos.secrets.provider import InterimKeyringProvider, get_provider, reset_provider_cache, select_backend
from tests.conftest import AuthedClient
from tests.unit.test_routing_fakes import human_decision, memory_keyring  # noqa: F401 - fixture

VALUE = "personal-value-" + "q" * 20


@pytest.fixture(autouse=True)
def _vault_down():
    reset_provider_cache()
    with respx.mock(assert_all_called=False) as mock:
        mock.route(url__startswith="http://127.0.0.1:8200").mock(side_effect=httpx.ConnectError("vault down"))
        yield mock
    reset_provider_cache()


def _interim_decision(db, **kw):
    base = {"kind": "EXCEPTION", "target_type": "secret_store", "target_id": "interim-keyring",
            "domain": "PERSONAL", "details": {"secret_store_interim_keyring": True},
            "expires_at": utcnow() + timedelta(days=30)}
    d = human_decision(db, **{**base, **kw})
    db.commit()
    return d


def test_fail_closed_without_any_store(settings, memory_keyring):  # noqa: F811
    with pytest.raises(Blocked) as ei:
        get_provider()
    assert ei.value.code == "SECRET_STORE_UNAVAILABLE"
    reasons = " ".join(ei.value.details["reasons"])
    assert "unreachable" in reasons and "no ACTIVE owner EXCEPTION" in reasons
    with pytest.raises(Blocked):
        facade.get_secret("personal/github", purpose="personal:broker", actor="t")
    h = store_health(settings)
    assert h["status"] == "BLOCKED" and h["backend"] == "none"
    cmds = [r["command"] for r in h["remedies"]]
    assert "aeos vault install" in cmds and "aeos vault start" in cmds


@pytest.mark.parametrize("override", [
    {"details": {}},
    {"details": {"secret_store_interim_keyring": "yes"}},
    {"domain": "OFFICE"},
    {"expires_at": utcnow() - timedelta(days=1)},
    {"actor_kind": "AGENT"},
    {"kind": "APPROVAL"},
])
def test_interim_requires_exact_active_human_personal_exception(settings, db, memory_keyring, override):  # noqa: F811
    _interim_decision(db, **override)
    assert select_backend(settings).backend == "none"


def test_interim_keyring_with_decision(settings, db, memory_keyring):  # noqa: F811
    d = _interim_decision(db)
    sel = select_backend(settings)
    assert sel.backend == "os-keyring-interim" and isinstance(sel.provider, InterimKeyringProvider)
    assert sel.interim["decision_id"] == d.id
    facade.put_secret("personal/github", VALUE, actor="cli")
    assert facade.get_secret("personal/github", purpose="personal:broker", actor="t") == VALUE
    with pytest.raises(PolicyDenied):
        facade.get_secret("personal/github", purpose="office:publisher", actor="t")
    with pytest.raises(PolicyDenied):
        facade.put_secret("office/atlassian", VALUE, actor="cli")  # OFFICE never in the interim store
    index = (settings.data_dir / "secrets" / "interim-index.json").read_text(encoding="utf-8")
    assert "personal/github" in index and VALUE not in index
    st = facade.secret_status("personal/github")
    assert st.exists and st.backend == "os-keyring-interim" and VALUE not in repr(st)
    h = store_health(settings)
    assert h["status"] == "DEGRADED" and h["interim"]["active"] and "Mode sementara" in h["interim"]["residual_risk"]
    assert VALUE not in str(h)


def test_status_unknown_when_store_unavailable_never_missing(settings, memory_keyring):  # noqa: F811
    out = secrets_status(settings, include_office=False)
    assert out["items"] and all(i["state"] == "UNKNOWN" and i["exists"] is None for i in out["items"])
    assert not any(i["ref"].startswith("office/") for i in out["items"])
    assert any(i["ref"].startswith("office/") for i in secrets_status(settings, include_office=True)["items"])


def test_api_status_and_health_never_show_values(personal_client: AuthedClient, secrets_memory):
    secrets_memory.put("personal/github", VALUE, actor="t")
    secrets_memory.put("office/atlassian", VALUE + "-office", actor="t")
    r = personal_client.get("/api/secrets/status")
    assert r.status_code == 200 and VALUE not in r.text
    items = {i["ref"]: i for i in r.json()["items"]}
    assert items["personal/github"]["state"] == "PRESENT" and items["personal/github"]["remedy"]
    assert "office/atlassian" not in items and r.json()["office_hidden"] is True
    h = personal_client.get("/api/secrets/health")
    assert h.status_code == 200 and VALUE not in h.text


def test_office_refs_visible_only_in_office_session(admin_client: AuthedClient, secrets_memory):
    secrets_memory.put("office/atlassian", VALUE, actor="t")
    admin_client.select_domain(Domain.OFFICE)
    items = {i["ref"] for i in admin_client.get("/api/secrets/status").json()["items"]}
    assert "office/atlassian" in items


def test_no_secret_write_endpoint(personal_client: AuthedClient):
    for method in ("post", "put", "patch"):
        r = getattr(personal_client, method)("/api/secrets/personal/github", json={"value": "x"})
        assert r.status_code in (404, 405)
    assert personal_client.delete("/api/secrets/personal/github").status_code in (404, 405)
    assert personal_client.post("/api/secrets/status", json={}).status_code in (404, 405)


def test_integrations_overview_cards(personal_client: AuthedClient, secrets_memory, monkeypatch):
    from aeos.claude_bridge import binary as binmod
    from tests.unit.test_claude_bridge_run import _probe

    monkeypatch.setattr(binmod, "probe", lambda settings, refresh=False: _probe())
    secrets_memory.put("personal/github", VALUE, actor="t")
    r = personal_client.get("/api/secrets/integrations")
    assert r.status_code == 200 and VALUE not in r.text
    cards = {c["id"]: c for c in r.json()["items"]}
    assert set(cards) >= {"claude-native", "secret-store", "github", "google", "openrouter", "atlassian"}
    assert cards["github"]["status"] == "CONFIGURED"  # stored credential is configuration, not VERIFIED
    assert cards["google"]["status"] == "MISSING"
    assert any("aeos publish google-connect" in x["command"] for x in cards["google"]["remedies"])
    assert any("aeos secrets set workers/openrouter" in x["command"] for x in cards["openrouter"]["remedies"])
    assert cards["atlassian"]["status"] == "BLOCKED"
    assert all(ref["state"] == "HIDDEN" for ref in cards["atlassian"]["credential_refs"])
    assert cards["claude-native"]["status"] == "NOT_TESTED"
    assert "VERIFIED" not in {c["status"] for c in cards.values()}
