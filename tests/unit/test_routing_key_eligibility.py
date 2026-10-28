"""A route whose worker key is not stored is not eligible (§26, ADR-0041; incident 2026-10-28).

The owner activated two OpenCode Zen TRIAL routes that name ``workers/opencode-zen``, which was never stored. Routing
checked only the namespace of the reference, so both stayed eligible: Doctor ``route.worker_key`` turned MISSING and
CAN_RUN_WORKER BLOCKED although the OpenRouter route had its key, and a dispatcher could pick a keyless route and fail
at call time. Hermetic: an in-memory secret store or a store that refuses (the SecretGuard hides the real ones);
no provider is called and no value is read.
"""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from aeos.core.enums import Capability
from aeos.core.errors import Blocked
from aeos.core.timeutil import utcnow
from aeos.doctor.capabilities import checks_for_capability, evaluate
from aeos.doctor.checks.model_route import route_registry, route_worker_key
from aeos.doctor.model import CheckContext
from aeos.models.audit import Event
from aeos.models.routing import ModelRoute
from aeos.routing import seed, service
from aeos.routing.facade import eligible_routes, unstored_credential_ref
from aeos.secrets.facade import SecretStatus
from tests.conftest import ADMIN_PW, AuthedClient, create_admin
from tests.unit.test_doctor_capabilities import ok_row
from tests.unit.test_inventory_fakes import base_runner
from tests.unit.test_routing_fakes import OWNER_CLIENT_TERMS, install_fakes, memory_keyring  # noqa: F401 - fixture
from tests.unit.test_routing_free_worker import FREE, _worker
from tests.unit.test_routing_service import DS, GLM, approve

ZEN_REF = "workers/opencode-zen"
NOT_STORED = f"credential {ZEN_REF} not stored: aeos secrets set {ZEN_REF}"


@pytest.fixture
def routes(db):
    seed.run(db)
    db.commit()
    return db


@pytest.fixture
def openrouter_key_only(secrets_memory):
    """The owner stored the OpenRouter worker key but not the OpenCode Zen one (fake value)."""
    secrets_memory.put("workers/openrouter", "fake-openrouter-key-value", actor="test")
    return secrets_memory


class CountingStore:
    """A secret store that answers status lookups (never a value) and counts them; ``down`` = sealed/unreachable."""

    name = "counting-test"

    def __init__(self, present: set[str], *, down: bool = False) -> None:
        self.present, self.down, self.calls = present, down, []  # type: ignore[var-annotated]

    def status(self, ref: str) -> SecretStatus:
        self.calls.append(ref)
        if self.down:
            raise Blocked("OpenBao is sealed", code="SECRET_STORE_UNAVAILABLE")
        return SecretStatus(ref=ref, exists=ref in self.present, backend=self.name)

    def get(self, ref: str, *, purpose: str, actor: str) -> str:  # pragma: no cover - eligibility never reads values
        raise AssertionError("eligibility must never read a secret value")


def _install_store(monkeypatch, store: Any) -> None:
    from aeos.secrets import provider

    monkeypatch.setattr(provider, "_override", store)


def _ctx(db, settings) -> CheckContext:
    return CheckContext(settings=settings, session=db, runner=base_runner(), repo=settings.data_dir, now=utcnow())


def _can_run_worker(db, monkeypatch, outcomes: dict[str, Any]) -> Any:
    """CAN_RUN_WORKER with every other prerequisite OK and the live sandbox READY: only the given checks decide."""
    import aeos.worker_runtime.facade as worker

    monkeypatch.setattr(worker, "sandbox_readiness", lambda s, *, domain: {
        "status": "READY", "kind": "container", "reasons": [], "remedies": [], "residual_risks": []})
    rows = {cid: ok_row(cid) for cid in checks_for_capability(Capability.CAN_RUN_WORKER)}
    for cid, out in outcomes.items():
        rows[cid] = ok_row(cid, status=out.status)
    return evaluate(Capability.CAN_RUN_WORKER, rows, now=utcnow(), fps={}, domain="PERSONAL", session=db)


# ------------------------------------------------------------------------------ (a) keyed + keyless TRIAL routes


def test_bite_a_keyless_trial_route_is_excluded_and_worker_readiness_stays_ready(routes, settings, monkeypatch,
                                                                                 openrouter_key_only):
    install_fakes(monkeypatch)
    _worker(routes)  # keyed: openrouter :free WORKER TRIAL (workers/openrouter stored)
    approve(routes, DS, kind="TRIAL")  # keyless: names workers/opencode-zen, never stored
    d = eligible_routes(routes, domain="PERSONAL", data_classification="PUBLIC")
    assert d.eligible == [FREE], d.rejected.get(DS)
    assert NOT_STORED in d.rejected[DS]
    assert unstored_credential_ref(NOT_STORED) == ZEN_REF and unstored_credential_ref("route DISABLED") is None

    key = route_worker_key(_ctx(routes, settings))
    assert key.status == "CONFIGURED", key.summary
    assert "tidak memblokir" in key.summary and DS in key.summary
    assert key.evidence["excluded_keyless"] == {DS: ZEN_REF}
    assert key.remedy is not None and f"secrets set {ZEN_REF}" in (key.remedy.command or "")
    assert f"routes disable {DS}" in key.remedy.operation  # both remedies: store the key, or disable the route
    reg = route_registry(_ctx(routes, settings))
    assert reg.status == "CONFIGURED" and "Dikecualikan: kunci belum disimpan" in reg.summary and DS in reg.summary
    assert reg.evidence["excluded_keyless"] == {DS: ZEN_REF}
    cap = _can_run_worker(routes, monkeypatch, {"route.worker_key": key, "route.registry": reg})
    assert cap.status == "READY", cap.reasons

    # near miss: storing the key is the only change the keyless route needs
    openrouter_key_only.put(ZEN_REF, "fake-zen-key-value", actor="test")
    assert DS in eligible_routes(routes, domain="PERSONAL", data_classification="PUBLIC").eligible
    assert "excluded_keyless" not in route_worker_key(_ctx(routes, settings)).evidence


def test_storing_a_worker_key_makes_the_registry_check_stale(routes, settings):
    from aeos.doctor.checks import REGISTRY, load_all

    load_all()
    for cid in ("route.registry", "route.worker_key"):
        assert {"secret:workers/*", "secret:personal/openrouter"} <= set(REGISTRY[cid].invalidators), cid


# ------------------------------------------------------------------------------ (b) every active route keyless


def test_bite_b_every_active_route_keyless_stays_missing(routes, settings, monkeypatch, secrets_memory):
    install_fakes(monkeypatch)
    approve(routes, DS, kind="TRIAL")
    assert eligible_routes(routes, domain="PERSONAL", data_classification="PUBLIC").eligible == []
    key = route_worker_key(_ctx(routes, settings))
    assert key.status == "MISSING" and key.error_code == "WORKER_KEY_MISSING", key.summary
    assert ZEN_REF in key.summary and key.evidence["excluded_keyless"] == {DS: ZEN_REF}
    assert key.remedy is not None and f"routes disable {DS}" in key.remedy.operation
    reg = route_registry(_ctx(routes, settings))
    assert reg.status == "MISSING" and "Dikecualikan: kunci belum disimpan" in reg.summary
    assert reg.error_code == "NO_ELIGIBLE_WORKER_ROUTE" and f"kunci belum disimpan: {ZEN_REF}" in reg.summary
    # the remedy is the key (or disabling the route), not a new and possibly paid activation (review 2026-10-28)
    assert reg.remedy is not None and reg.remedy.command is not None and f"secrets set {ZEN_REF}" in reg.remedy.command
    assert f"routes disable {DS}" in reg.remedy.operation and "routes activate" not in reg.remedy.command
    assert reg.requires_paid_write is False
    assert _can_run_worker(routes, monkeypatch, {"route.worker_key": key, "route.registry": reg}).status == "BLOCKED"


def test_near_miss_a_route_rejected_for_another_reason_keeps_the_activation_remedy(routes, settings, monkeypatch,
                                                                                    secrets_memory):
    install_fakes(monkeypatch)
    secrets_memory.put(ZEN_REF, "fake-zen-key-value", actor="test")
    approve(routes, DS, kind="TRIAL")
    service.get_route(routes, DS).availability = "MISSING"  # keyed, but gone from the provider catalog
    routes.flush()
    reg = route_registry(_ctx(routes, settings))
    assert reg.status == "MISSING" and reg.error_code == "NO_ELIGIBLE_WORKER_ROUTE", reg.summary
    assert "missing from the provider catalog" in reg.summary and reg.evidence["excluded_keyless"] == {}
    assert reg.remedy is not None and "routes activate" in (reg.remedy.command or "")


# ------------------------------------------------------------------------------ (c) store sealed/unreachable


@pytest.mark.parametrize("store", ["sealed", "absent"])
def test_bite_c_a_store_that_cannot_answer_makes_a_keyed_route_ineligible(routes, monkeypatch, store):
    install_fakes(monkeypatch)
    if store == "sealed":
        _install_store(monkeypatch, CountingStore({ZEN_REF}, down=True))
    # "absent": no store at all (the SecretGuard default: SECRET_STORE_UNAVAILABLE)
    approve(routes, DS, kind="TRIAL")
    d = eligible_routes(routes, domain="PERSONAL", data_classification="INTERNAL")
    assert d.eligible == []
    assert any(f"credential {ZEN_REF} cannot be checked: secret store unavailable" in x and "fail closed" in x
               for x in d.rejected[DS]), d.rejected[DS]


def test_bite_c_a_sealed_store_is_named_as_the_cause_in_route_registry(routes, settings, monkeypatch,
                                                                       openrouter_key_only):
    """Review 2026-10-28: with the store sealed, route.registry reported NO_ELIGIBLE_WORKER_ROUTE with an INTERNAL
    data-class example and the paid activate remedy; the cause (the store cannot answer) was not shown."""
    install_fakes(monkeypatch)
    _worker(routes)  # keyed: openrouter :free WORKER TRIAL; the key is stored, but the store is now sealed
    _install_store(monkeypatch, CountingStore({"workers/openrouter"}, down=True))
    reg = route_registry(_ctx(routes, settings))
    assert reg.status == "UNKNOWN" and reg.error_code == "SECRET_STORE_UNAVAILABLE", reg.summary
    assert "secret store tidak tersedia" in reg.summary and FREE in reg.summary and "fail closed" in reg.summary
    assert "data classification" not in reg.summary  # the example of another class is not the cause
    assert reg.evidence["excluded_store_unavailable"] == {FREE: "workers/openrouter"}
    assert reg.evidence["excluded_keyless"] == {}
    assert reg.requires_paid_write is False and reg.remedy is not None
    assert "vault unseal" in reg.remedy.operation and "routes activate" not in (reg.remedy.command or "")
    assert "routes activate" not in reg.remedy.operation
    key = route_worker_key(_ctx(routes, settings))
    assert key.status == "MISSING" and key.error_code == "SECRET_STORE_UNAVAILABLE", key.summary
    assert _can_run_worker(routes, monkeypatch, {"route.worker_key": key, "route.registry": reg}).status == "BLOCKED"

    # near miss: the same store unsealed, nothing else changed -> the route is eligible again
    _install_store(monkeypatch, CountingStore({"workers/openrouter"}))
    reg = route_registry(_ctx(routes, settings))
    assert reg.status == "CONFIGURED" and reg.evidence["excluded_store_unavailable"] == {}, reg.summary


def test_one_status_lookup_per_reference_per_eligibility_call(routes, monkeypatch):
    install_fakes(monkeypatch)
    store = CountingStore({ZEN_REF, "workers/openrouter"})
    _install_store(monkeypatch, store)
    _worker(routes)  # workers/openrouter
    approve(routes, DS, kind="TRIAL")
    approve(routes, GLM, kind="TRIAL")  # the same workers/opencode-zen reference as DS
    store.calls.clear()
    d = eligible_routes(routes, domain="PERSONAL", data_classification="PUBLIC")
    assert {DS, GLM, FREE} <= set(d.eligible), d.rejected
    assert sorted(store.calls) == ["workers/opencode-zen", "workers/openrouter"]  # memo inside the call; the
    # catalog rows nothing activated are never looked up
    store.calls.clear()
    store.down = True
    d = eligible_routes(routes, domain="PERSONAL", data_classification="PUBLIC")
    assert d.eligible == [] and len(store.calls) == 1  # the store failed once: no other reference is looked up


def test_near_miss_loopback_local_and_claude_native_need_no_stored_key(monkeypatch):
    _install_store(monkeypatch, CountingStore(set(), down=True))
    local = ModelRoute(route_key="local/x", pool="LOCAL", provider="local", model_id="m", mode_params={},
                       endpoint="http://127.0.0.1:8080/v1")
    loop = ModelRoute(route_key="l", pool="WORKER", provider="local-test", model_id="m", mode_params={},
                      endpoint="http://localhost:8099/v1")
    native = ModelRoute(route_key="claude-native/opus", pool="SUPERVISOR", provider="claude-native", model_id="m",
                        mode_params={})
    for r in (local, loop, native):
        assert service.credential_rejections(r, domain="PERSONAL") == [], r.route_key


# ------------------------------------------------------------------------------ (d) one emit point


def _events(db, kind: str, key: str) -> int:
    db.expire_all()
    return db.query(Event).filter_by(kind=kind, entity_id=key).count()


def test_bite_d_cli_activation_and_disable_emit_exactly_one_event(routes, monkeypatch, worker_keys):
    from aeos.cli import app
    from tests.unit.test_routing_cli import CLIENT_TERMS

    install_fakes(monkeypatch)
    create_admin(routes)
    r = service.get_route(routes, DS)
    r.pricing_checked_at = r.terms_checked_at = utcnow()
    routes.commit()
    runner = CliRunner()
    out = runner.invoke(app, ["routes", "activate", DS, "--as", "owner", "--mode", "TRIAL", "--rationale",
                              "pilot slice one", "--expected-hash", r.config_hash, *CLIENT_TERMS],
                        input=ADMIN_PW + "\n")
    assert out.exit_code == 0, out.output
    assert _events(routes, "route.activated", DS) == 1
    out = runner.invoke(app, ["routes", "disable", DS, "--reason", "kunci belum disimpan"])
    assert out.exit_code == 0, out.output
    assert _events(routes, "route.disabled", DS) == 1


def test_bite_d_api_activation_and_disable_emit_exactly_one_event(personal_client: AuthedClient, db, monkeypatch,
                                                                  worker_keys):
    install_fakes(monkeypatch)
    seed.run(db)
    r = service.get_route(db, DS)
    r.pricing_checked_at = r.terms_checked_at = utcnow()
    db.commit()
    res = personal_client.post(f"/api/routes/{DS}/activate", json={
        "mode": "TRIAL", "rationale": "pilot for slice 1", "expected_config_hash": r.config_hash,
        "client_terms": OWNER_CLIENT_TERMS})
    assert res.status_code == 200, res.text
    assert _events(db, "route.activated", DS) == 1
    res = personal_client.post(f"/api/routes/{DS}/disable", json={"reason": "kunci belum disimpan"})
    assert res.status_code == 200, res.text
    assert _events(db, "route.disabled", DS) == 1


# ------------------------------------------------------------------------------ round 2 (e) store state signal
# A route check taken while the store was down kept that result until its 3600 s TTL: nothing re-ran it when the
# vault was unsealed, started or stopped. Every such path signals ``secret:store`` (aeos.secrets.readiness, the one
# owner) and both route checks declare it.

ROUTE_CHECKS = ("route.registry", "route.worker_key")


def _route_rows(db) -> dict[str, Any]:
    from aeos.doctor.runner import latest_results

    db.expire_all()
    return latest_results(db, list(ROUTE_CHECKS))


def _store_down_results(routes, settings, monkeypatch) -> None:
    from aeos.doctor.runner import run_doctor

    install_fakes(monkeypatch, doctor=False, inventory=False)  # the real Doctor stores and invalidates the rows
    _worker(routes)  # keyed: openrouter :free WORKER TRIAL
    _install_store(monkeypatch, CountingStore({"workers/openrouter"}, down=True))
    run_doctor(settings, routes, checks=list(ROUTE_CHECKS), runner=base_runner())
    routes.commit()
    rows = _route_rows(routes)
    assert {cid: (rows[cid].status, rows[cid].error_code) for cid in ROUTE_CHECKS} == {
        "route.registry": ("UNKNOWN", "SECRET_STORE_UNAVAILABLE"),
        "route.worker_key": ("MISSING", "SECRET_STORE_UNAVAILABLE")}


@pytest.mark.usefixtures("memory_keyring")
def test_bite_e_an_unseal_makes_both_store_down_route_checks_stale(routes, settings, monkeypatch):
    import respx

    from aeos.secrets import unseal as unseal_mod
    from aeos.secrets.provider import bootstrap_store
    from tests.unit.test_vault_keyring_unseal import KEYS_B64, _vault

    _store_down_results(routes, settings, monkeypatch)
    bootstrap_store(settings).set_unseal_keys(KEYS_B64)
    with respx.mock(assert_all_called=False) as m:
        _vault(m, sealed=True)
        assert unseal_mod.unseal_from_keyring(settings, trigger="cli").status == unseal_mod.UNSEALED
    rows = _route_rows(routes)
    for cid in ROUTE_CHECKS:
        assert rows[cid].status == "STALE", f"{cid} kept its store-down result after the vault was unsealed"
        assert "secret-ref store" in rows[cid].observed_evidence["stale_reason"], cid
    assert routes.query(Event).filter(Event.kind == "doctor.check.stale",
                                      Event.entity_id.in_(ROUTE_CHECKS)).count() == 2


@pytest.mark.usefixtures("memory_keyring")
def test_near_miss_e_an_already_unsealed_vault_and_another_secret_signal_leave_the_route_checks(
        routes, settings, monkeypatch):
    import respx

    from aeos.secrets import unseal as unseal_mod
    from aeos.secrets.readiness import invalidate_secret_checks
    from tests.unit.test_vault_keyring_unseal import _vault

    _store_down_results(routes, settings, monkeypatch)
    with respx.mock(assert_all_called=False) as m:
        _vault(m, sealed=False)
        assert unseal_mod.unseal_from_keyring(settings, trigger="cli").status == unseal_mod.ALREADY_UNSEALED
    invalidate_secret_checks("platform/github", reason="another credential stored")
    rows = _route_rows(routes)
    assert {rows[cid].status for cid in ROUTE_CHECKS} == {"UNKNOWN", "MISSING"}  # nothing changed: not STALE


@pytest.mark.usefixtures("memory_keyring")
@pytest.mark.parametrize("path", ["start", "stop", "prompt-unseal"])
def test_bite_e_every_vault_state_path_signals_secret_store(settings, monkeypatch, tmp_path, path):
    """``vault start``, ``vault stop`` (OpenBao restarts sealed; there is no separate seal command) and the prompted
    ``vault unseal`` each signal ``secret:store`` exactly once; the keyring unseal is covered above."""
    from types import SimpleNamespace

    from aeos.cli import app
    from aeos.secrets import readiness, server

    signals: list[str] = []
    monkeypatch.setattr(readiness, "store_state_changed", lambda reason, **kw: signals.append(reason) or 0)
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
    else:
        import respx

        from aeos.secrets import vault_cli
        from tests.unit.test_vault_keyring_unseal import _vault

        typed = iter(["p1" * 16, "p2" * 16])
        monkeypatch.setattr(vault_cli.getpass, "getpass", lambda prompt="": next(typed))
        with respx.mock(assert_all_called=False) as m:
            _vault(m, sealed=True)
            r = CliRunner().invoke(app, ["vault", "unseal"])
        assert r.exit_code == 0, r.output
    assert len(signals) == 1, f"vault {path} sent {len(signals)} secret:store signal(s), expected exactly one"


def test_near_miss_e_a_stop_with_nothing_running_sends_no_signal(settings, monkeypatch):
    from aeos.secrets import readiness, server

    signals: list[str] = []
    monkeypatch.setattr(readiness, "store_state_changed", lambda reason, **kw: signals.append(reason) or 0)
    monkeypatch.setattr(server, "running", lambda s: None)
    assert server.stop(settings) is False and signals == []


def test_both_route_checks_declare_the_store_signal():
    from aeos.doctor.checks import REGISTRY, load_all
    from aeos.secrets.readiness import STORE_INVALIDATOR

    load_all()
    for cid in (*ROUTE_CHECKS, "secret.status"):
        assert STORE_INVALIDATOR in REGISTRY[cid].invalidators, cid

