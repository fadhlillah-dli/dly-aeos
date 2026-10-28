"""Secret and store changes reach the dashboard (R28 "always up to date", 2026-10-28; §35, §50).

Storing or deleting a secret, or starting, stopping, unsealing or initialising the vault, emitted no event, so an open
Models page never learned that a route became usable; and ``aeos vault init`` (which unseals the new vault) did not
send the ``secret:store`` signal at all, so the route checks kept a store-down result until their TTL.
``aeos.secrets.readiness`` is the single owner of both signals and of their events: ``secret.presence_changed``
(payload: the reference name, never a value; an ``office/`` name only into the OFFICE domain) and
``secret.store_changed`` (payload: the transition), source ``secrets``, exactly one per action.
Hermetic: the in-memory secret store, a memory keyring and a fake OpenBao client; no real store is reached.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from aeos.models.audit import Event
from aeos.secrets.readiness import PRESENCE_EVENT, STORE_EVENT, STORE_TRANSITIONS
from tests.unit.test_routing_fakes import memory_keyring  # noqa: F401 - fixture

FAKE_VALUE = "fake-value-never-in-an-event-7f3a"


def _secret_events(db, kind: str | None = None) -> list[Event]:
    db.expire_all()
    q = db.query(Event).filter(Event.source == "secrets")
    if kind is not None:
        q = q.filter(Event.kind == kind)
    return q.order_by(Event.seq).all()


def _row_text(e: Event) -> str:
    return json.dumps({c: getattr(e, c) for c in ("kind", "entity_type", "entity_id", "domain", "payload")},
                      default=str)


# ------------------------------------------------------------------------------ secret.presence_changed


def test_bite_one_presence_event_per_set_and_delete_and_no_value(db, secrets_memory):
    from aeos.secrets.facade import put_secret
    from aeos.secrets.provider import provider_delete, provider_put

    put_secret("workers/openrouter", FAKE_VALUE, actor="test")
    evs = _secret_events(db)
    assert [(e.kind, e.payload) for e in evs] == [(PRESENCE_EVENT, {"ref": "workers/openrouter"})], \
        "a stored secret must emit exactly one secret.presence_changed carrying only the reference name"
    provider_put("workers/openrouter", FAKE_VALUE + "-2", actor="test")
    provider_delete("workers/openrouter", actor="test")
    evs = _secret_events(db)
    assert len(evs) == 3, f"set, rotate and delete must emit one event each, got {len(evs)}"
    for e in evs:
        assert e.domain is None and e.entity_id == "workers/openrouter"
        assert FAKE_VALUE not in _row_text(e), "an event carried the secret value"


def test_near_miss_a_failed_store_emits_no_presence_event(db, secrets_memory, monkeypatch):
    from aeos.core.errors import Blocked
    from aeos.secrets.provider import provider_put

    def refuse(*a: Any, **kw: Any) -> None:
        raise Blocked("store refused", code="SECRET_STORE_UNAVAILABLE")

    monkeypatch.setattr(secrets_memory, "put", refuse)
    with pytest.raises(Blocked):
        provider_put("workers/openrouter", FAKE_VALUE, actor="test")
    assert _secret_events(db) == []


def test_bite_office_ref_event_only_in_the_office_domain(db, secrets_memory, personal_client):
    from aeos.events import service as events
    from aeos.secrets.facade import put_secret

    put_secret("office/atlassian", FAKE_VALUE, actor="test")
    (ev,) = _secret_events(db, PRESENCE_EVENT)
    assert ev.domain == "OFFICE" and ev.payload == {"ref": "office/atlassian"}
    assert FAKE_VALUE not in _row_text(ev)
    db.expire_all()
    personal = [v for v in events.replay(db, domains=["PERSONAL"]) if v.source == "secrets"]
    office = [v for v in events.replay(db, domains=["OFFICE"]) if v.source == "secrets"]
    assert personal == [], "an office/ reference reached the PERSONAL event stream"
    assert [v.payload for v in office] == [{"ref": "office/atlassian"}]
    # the PERSONAL session's API view (the stream uses the same scope rule)
    body = personal_client.get("/api/events").json()
    assert not [e for e in body["events"] if e["source"] == "secrets"], "PERSONAL /api/events showed an office event"
    assert "office/atlassian" not in json.dumps(body["events"])


def test_near_miss_platform_ref_event_is_visible_to_personal(db, secrets_memory, personal_client):
    from aeos.secrets.facade import put_secret

    put_secret("personal/openrouter", FAKE_VALUE, actor="test")
    body = personal_client.get("/api/events").json()
    assert [e["payload"] for e in body["events"] if e["kind"] == PRESENCE_EVENT] == [{"ref": "personal/openrouter"}]


def test_a_projects_ref_name_is_withheld_from_the_domainless_event(db, secrets_memory):
    from aeos.secrets.facade import put_secret

    put_secret("projects/p-123/github", FAKE_VALUE, actor="test")
    (ev,) = _secret_events(db, PRESENCE_EVENT)
    assert ev.domain is None and ev.payload == {"ref": None} and ev.entity_id == ""
    assert "p-123" not in _row_text(ev)


def test_no_database_no_event_and_no_side_effect(settings, secrets_memory):
    """Without the local database the signal neither creates it nor raises (CLI before the first migrate)."""
    from aeos.secrets.readiness import invalidate_secret_checks, store_state_changed

    db_path = settings.layout.db_path
    if db_path.is_file():
        db_path.unlink()
    assert invalidate_secret_checks("workers/openrouter", reason="x") == 0
    assert store_state_changed("x", transition="started") == 0
    assert not db_path.is_file()


# ------------------------------------------------------------------------------ secret.store_changed


class FakeBao:
    """An OpenBao client double for ``run_init``: uninitialised until ``init``; ``fail_at`` names a step that raises."""

    fail_at: str | None = None
    initialized = False

    def __init__(self, addr: str, token: str | None = None, http: Any = None) -> None:
        pass

    def _step(self, name: str) -> None:
        if FakeBao.fail_at == name:
            from aeos.secrets.openbao import OpenBaoError

            raise OpenBaoError(f"{name} failed")

    def health(self) -> dict[str, Any]:
        return {"initialized": FakeBao.initialized, "sealed": False, "standby": False}

    def init(self, shares: int, threshold: int) -> dict[str, Any]:
        FakeBao.initialized = True
        if FakeBao.fail_at == "init-empty":
            return {"keys_base64": [], "root_token": None}
        return {"keys_base64": ["fake-share-1"], "root_token": "fake-root"}

    def unseal(self, key: str) -> dict[str, Any]:
        return {"sealed": False}

    def enable_kv2(self, mount: str) -> None:
        self._step("kv")

    def write_policy(self, name: str, hcl: str) -> None:
        pass

    def enable_approle(self) -> None:
        pass

    def write_approle_role(self, *a: Any, **kw: Any) -> None:
        pass

    def approle_role_id(self, name: str) -> str:
        return "fake-role-id"

    def approle_secret_id(self, name: str) -> str:
        return "fake-secret-id"

    def approle_login(self, role_id: str, secret_id: str) -> dict[str, Any]:
        return {"client_token": "fake-client-token"}

    def lookup_self(self) -> dict[str, Any]:
        return {}

    def revoke_self(self) -> None:
        pass


@pytest.fixture
def fake_bao(monkeypatch):
    from aeos.secrets import setup

    monkeypatch.setattr(setup, "OpenBaoClient", FakeBao)
    monkeypatch.setattr(FakeBao, "fail_at", None)
    monkeypatch.setattr(FakeBao, "initialized", False)
    return FakeBao


def _run_init(settings) -> Any:
    from aeos.secrets.provider import bootstrap_store
    from aeos.secrets.setup import run_init

    return run_init(settings.openbao_addr, shares=1, threshold=1, bootstrap=bootstrap_store(settings))


@pytest.mark.usefixtures("memory_keyring")
@pytest.mark.parametrize("fail_at", [None, "kv"])
def test_bite_vault_init_sends_one_store_signal(settings, monkeypatch, fake_bao, fail_at):
    """``aeos vault init`` unseals the new vault: one ``secret:store`` signal (transition initialised), also when a
    step after the unseal failed (the store answers either way)."""
    from aeos.secrets import readiness

    signals: list[tuple[str, str]] = []
    monkeypatch.setattr(readiness, "store_state_changed",
                        lambda reason, **kw: signals.append((reason, kw["transition"])) or 0)
    fake_bao.fail_at = fail_at
    res = _run_init(settings)
    assert (res.error is None) is (fail_at is None)
    assert [t for _, t in signals] == ["initialised"], f"vault init sent {signals}, expected one 'initialised'"


@pytest.mark.usefixtures("memory_keyring")
def test_near_miss_an_init_that_never_unsealed_sends_no_signal(settings, monkeypatch, fake_bao):
    from aeos.secrets import readiness

    signals: list[str] = []
    monkeypatch.setattr(readiness, "store_state_changed", lambda reason, **kw: signals.append(reason) or 0)
    fake_bao.fail_at = "init-empty"
    res = _run_init(settings)
    assert res.error and signals == []


@pytest.mark.usefixtures("memory_keyring")
def test_bite_vault_init_makes_both_store_down_route_checks_stale(routes, settings, monkeypatch, fake_bao):
    from tests.unit.test_routing_key_eligibility import ROUTE_CHECKS, _route_rows, _store_down_results

    _store_down_results(routes, settings, monkeypatch)
    assert _run_init(settings).error is None
    rows = _route_rows(routes)
    for cid in ROUTE_CHECKS:
        assert rows[cid].status == "STALE", f"{cid} kept its store-down result after aeos vault init"
        assert "secret-ref store" in rows[cid].observed_evidence["stale_reason"], cid
    assert [e.payload for e in _secret_events(routes, STORE_EVENT)] == [{"transition": "initialised"}]


@pytest.fixture
def routes(db):
    from aeos.routing import seed

    seed.run(db)
    db.commit()
    return db


@pytest.mark.usefixtures("memory_keyring")
@pytest.mark.parametrize("path,transition", [("start", "started"), ("stop", "stopped"),
                                             ("keyring-unseal", "unsealed"), ("prompt-unseal", "unsealed"),
                                             ("init", "initialised")])
def test_bite_one_store_event_per_vault_action(db, settings, monkeypatch, tmp_path, fake_bao, path, transition):
    import respx

    from aeos.cli import app
    from aeos.secrets import server
    from aeos.secrets import unseal as unseal_mod
    from aeos.secrets.provider import bootstrap_store
    from tests.unit.test_vault_keyring_unseal import KEYS_B64, _vault

    if path == "start":
        monkeypatch.setattr(server, "running", lambda s: None)
        monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **kw: SimpleNamespace(pid=4242))
        monkeypatch.setattr(server.psutil, "Process", lambda pid: SimpleNamespace(create_time=lambda: 1.0))
        server.start(settings, tmp_path / "bao")
    elif path == "stop":
        monkeypatch.setattr(server, "running", lambda s: {"alive": True, "pid": 4242})
        monkeypatch.setattr(server.psutil, "Process", lambda pid: SimpleNamespace(terminate=lambda: None,
                                                                                 wait=lambda timeout: None))
        assert server.stop(settings) is True
    elif path == "keyring-unseal":
        bootstrap_store(settings).set_unseal_keys(KEYS_B64)
        with respx.mock(assert_all_called=False) as m:
            _vault(m, sealed=True)
            assert unseal_mod.unseal_from_keyring(settings, trigger="cli").status == unseal_mod.UNSEALED
    elif path == "prompt-unseal":
        from aeos.secrets import vault_cli

        typed = iter(["p1" * 16, "p2" * 16])
        monkeypatch.setattr(vault_cli.getpass, "getpass", lambda prompt="": next(typed))
        with respx.mock(assert_all_called=False) as m:
            _vault(m, sealed=True)
            r = CliRunner().invoke(app, ["vault", "unseal"])
        assert r.exit_code == 0, r.output
    else:
        assert _run_init(settings).error is None
    evs = _secret_events(db)
    assert [(e.kind, e.payload, e.domain) for e in evs] == [(STORE_EVENT, {"transition": transition}, None)], \
        f"vault {path} must emit exactly one secret.store_changed ({transition}), got {[_row_text(e) for e in evs]}"
    assert transition in STORE_TRANSITIONS


def test_near_miss_a_stop_with_nothing_running_emits_no_store_event(db, settings, monkeypatch):
    from aeos.secrets import server

    monkeypatch.setattr(server, "running", lambda s: None)
    assert server.stop(settings) is False and _secret_events(db) == []
