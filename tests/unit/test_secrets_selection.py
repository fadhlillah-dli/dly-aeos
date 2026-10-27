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
from tests.conftest import AuthedClient, create_admin, create_viewer
from tests.unit.test_routing_fakes import human_decision, memory_keyring  # noqa: F401 - fixture

VALUE = "personal-value-" + "q" * 20


@pytest.fixture(autouse=True)
def _vault_down():
    reset_provider_cache()
    with respx.mock(assert_all_called=False) as mock:
        mock.route(url__startswith="http://127.0.0.1:8200").mock(side_effect=httpx.ConnectError("vault down"))
        yield mock
    reset_provider_cache()


def _owner_id(db) -> str:
    from aeos.models.identity import User

    existing = db.query(User).filter_by(username="owner").one_or_none()
    return existing.id if existing else create_admin(db).id


def _interim_decision(db, **kw):
    base = {"kind": "EXCEPTION", "target_type": "secret_store", "target_id": "interim-keyring",
            "domain": "PERSONAL", "details": {"secret_store_interim_keyring": True},
            "expires_at": utcnow() + timedelta(days=30), "actor_user_id": _owner_id(db)}
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
    assert "aeos vault install" in cmds and "aeos vault start" in cmds  # the manifest pins OpenBao (1.12)


@pytest.mark.parametrize("override", [
    {"details": {}},
    {"details": {"secret_store_interim_keyring": "yes"}},
    {"domain": "OFFICE"},
    {"expires_at": utcnow() - timedelta(days=1)},
    {"actor_kind": "AGENT"},
    {"kind": "APPROVAL"},
    {"target_id": "something-else"},  # bound to the exact target the CLI tells the owner to record
    {"target_type": "model_route"},
    {"actor_user_id": "not-a-user"},  # an EXCEPTION is an owner (active SUPER_ADMIN) decision
    {"actor_user_id": "VIEWER"},
])
def test_interim_requires_exact_active_human_personal_exception(settings, db, memory_keyring, override):  # noqa: F811
    if override.get("actor_user_id") == "VIEWER":
        override = {"actor_user_id": create_viewer(db).id}
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


def test_opencode_go_card_and_ref_are_separate_from_zen(personal_client: AuthedClient, secrets_memory, monkeypatch):
    """ADR-0041 amendment 2026-10-26 (owner decision A): the Go key is its own ref and card, so a stored Zen key never
    reads as a configured Go subscription (and the other way round)."""
    from aeos.claude_bridge import binary as binmod
    from aeos.secrets.refs import known
    from tests.unit.test_claude_bridge_run import _probe

    monkeypatch.setattr(binmod, "probe", lambda settings, refresh=False: _probe())
    ref = known("workers/opencode-go")
    assert ref is not None and ref.namespace == "workers" and ref.domain is None
    assert "OpenCode Go" in ref.purpose and ref.remedy == "aeos secrets set workers/opencode-go"
    secrets_memory.put("workers/opencode-zen", VALUE, actor="t")
    cards = {c["id"]: c for c in personal_client.get("/api/secrets/integrations").json()["items"]}
    go = cards["opencode-go"]
    assert go["status"] == "MISSING" and cards["opencode"]["status"] == "CONFIGURED"
    assert [r["ref"] for r in go["credential_refs"]] == ["workers/opencode-go"]
    assert any(x["command"] == "aeos secrets set workers/opencode-go" for x in go["remedies"])
    assert "Extra Usage" in go["warning"] and "OFF" in go["warning"]
    secrets_memory.put("workers/opencode-go", "go-" + VALUE, actor="t")
    r = personal_client.get("/api/secrets/integrations")
    assert VALUE not in r.text
    assert {c["id"]: c for c in r.json()["items"]}["opencode-go"]["status"] == "CONFIGURED"  # stored, not VERIFIED
    items = {i["ref"] for i in personal_client.get("/api/secrets/status").json()["items"]}
    assert "workers/opencode-go" in items


def test_google_card_reads_the_refs_google_connect_writes(personal_client: AuthedClient, secrets_memory, monkeypatch):
    """W1 seam: the card follows the refs `aeos publish google-connect` writes and the publisher reads
    (personal/google-oauth-client + personal/google-oauth-token); both are needed for CONFIGURED."""
    from aeos.claude_bridge import binary as binmod
    from tests.unit.test_claude_bridge_run import _probe

    monkeypatch.setattr(binmod, "probe", lambda settings, refresh=False: _probe())

    def google() -> dict:
        r = personal_client.get("/api/secrets/integrations")
        assert r.status_code == 200 and VALUE not in r.text
        return next(c for c in r.json()["items"] if c["id"] == "google")

    card = google()
    assert {x["ref"] for x in card["credential_refs"]} == {"personal/google-oauth-client", "personal/google-oauth-token"}
    assert card["status"] == "MISSING"
    secrets_memory.put("personal/google-oauth-client", '{"installed": {"client_id": "fake-id"}}', actor="t")
    assert google()["status"] == "MISSING"  # the client alone cannot publish: no consented token yet
    secrets_memory.put("personal/google-oauth-token", VALUE, actor="t")
    assert google()["status"] == "CONFIGURED"  # configuration, never VERIFIED


def _two_projects(db):
    from aeos.models.portfolio import Project

    personal = Project(project_key="vis-personal", name="Personal project", domain="PERSONAL")
    office = Project(project_key="vis-office", name="Office project", domain="OFFICE")
    db.add_all([personal, office])
    db.commit()
    return personal.id, office.id


def test_project_refs_never_cross_domains(settings, admin_client: AuthedClient, db, secrets_memory):
    """§35/§50 'Viewer mengakses ... Office': a projects/<office-id>/... ref (project id + the fact that
    it has a credential) never appears in a Personal session, and vice versa."""
    personal, office = _two_projects(db)
    secrets_memory.put(f"projects/{personal}/github", VALUE, actor="t")
    secrets_memory.put(f"projects/{office}/bitbucket", VALUE, actor="t")
    secrets_memory.put("projects/unregistered/x", VALUE, actor="t")
    admin_client.select_domain(Domain.PERSONAL)
    r = admin_client.get("/api/secrets/status")
    refs = {i["ref"] for i in r.json()["items"]}
    assert f"projects/{personal}/github" in refs and office not in r.text and "unregistered" not in r.text
    r = admin_client.get("/api/secrets/integrations")
    assert office not in r.text and VALUE not in r.text
    admin_client.select_domain(Domain.OFFICE)
    refs = {i["ref"] for i in admin_client.get("/api/secrets/status").json()["items"]}
    assert f"projects/{office}/bitbucket" in refs and f"projects/{personal}/github" not in refs
    # local CLI view (the owner): office project refs only on request
    cli = {i["ref"] for i in secrets_status(settings, include_office=False)["items"]}
    assert f"projects/{office}/bitbucket" not in cli and f"projects/{personal}/github" in cli


def test_interim_store_refuses_office_project_secrets(settings, db, memory_keyring):  # noqa: F811
    personal, office = _two_projects(db)
    _interim_decision(db)
    facade.put_secret(f"projects/{personal}/github", VALUE, actor="cli")
    assert facade.get_secret(f"projects/{personal}/github", purpose=f"projects:{personal}:broker", actor="t") == VALUE
    for pid in (office, "unknown"):
        with pytest.raises(PolicyDenied):
            facade.put_secret(f"projects/{pid}/github", VALUE, actor="cli")
    assert not any(k[1].startswith(f"projects/{office}") for k in memory_keyring.store)


def test_interim_mode_office_refs_are_no_access_without_a_bogus_remedy(settings, db, memory_keyring):  # noqa: F811
    """office/* is never served in interim mode: NO_ACCESS (neither present nor missing) and no
    `aeos secrets set office/...` command that would only be refused."""
    _interim_decision(db)
    rows = {i["ref"]: i for i in secrets_status(settings, include_office=True)["items"]}
    for ref in ("office/atlassian", "office/bitbucket"):
        assert rows[ref]["state"] == "NO_ACCESS" and rows[ref]["exists"] is None
        assert rows[ref]["remedy"] is None and "OFFICE" in rows[ref]["remedy_note"]
    assert rows["personal/github"]["state"] == "MISSING"
    assert rows["personal/github"]["remedy"] == "aeos secrets set personal/github"


def test_keyring_write_failure_fails_closed_without_echoing_the_value(settings, db, memory_keyring,  # noqa: F811
                                                                      monkeypatch):
    from aeos.core.errors import Blocked

    _interim_decision(db)

    def broken(service, username, password):
        raise RuntimeError(f"backend exploded while storing {password}")

    monkeypatch.setattr(memory_keyring, "set_password", broken)
    with pytest.raises(Blocked) as ei:
        facade.put_secret("personal/github", VALUE, actor="cli")
    assert ei.value.code == "SECRET_STORE_UNAVAILABLE" and VALUE not in ei.value.message
    assert not (settings.data_dir / "secrets" / "interim-index.json").exists()  # nothing recorded as present
