"""The route view carries a credential block, and a keyless route is not counted as active (R28 round 3).

Incident 2026-10-28: a TRIAL route with an ACTIVE decision but no stored key was rejected by the router (Doctor
CAN_RUN_WORKER BLOCKED) while the Models page drew it green "Aktif · TRIAL", counted it in "Aktif: N" and sorted it
first. The view now states {state, ref, reason} from the router's own credential rules; ``counts.approved`` requires
PRESENT or NOT_NEEDED. Hermetic: an in-memory store or a counting fake (the SecretGuard hides the real ones); no value
is read and no provider is called.
"""

from __future__ import annotations

from aeos.core.enums import Domain
from aeos.core.timeutil import utcnow
from aeos.routing import seed, service
from tests.conftest import AuthedClient
from tests.unit.test_routing_fakes import install_fakes
from tests.unit.test_routing_key_eligibility import CountingStore, _install_store
from tests.unit.test_routing_service import DS, approve

ZEN_REF = "workers/opencode-zen"
LOCAL = "local/qwen3.8-flash-next"
NATIVE = "claude-native/opus"


def _seed(db) -> None:
    seed.run(db)
    for r in db.query(service.ModelRoute).all():
        r.pricing_checked_at = r.terms_checked_at = utcnow()
        r.availability, r.availability_checked_at = "AVAILABLE", utcnow()
    db.commit()


def _list(client: AuthedClient) -> tuple[dict, dict[str, dict]]:
    r = client.get("/api/routes")
    assert r.status_code == 200, r.text
    body = r.json()
    return body, {i["route_key"]: i for i in body["items"]}


def test_bite_a_keyless_trial_route_is_missing_and_not_counted(personal_client: AuthedClient, db, monkeypatch,
                                                                 secrets_memory):
    install_fakes(monkeypatch)
    _seed(db)
    approve(db, DS, kind="TRIAL")
    db.commit()
    body, by = _list(personal_client)
    cred = by[DS]["credential"]
    assert by[DS]["status"] == "TRIAL" and by[DS]["active_decision"], by[DS]
    assert cred["state"] == "MISSING" and cred["ref"] == ZEN_REF and cred["domain"] == "PERSONAL", cred
    assert f"not stored: aeos secrets set {ZEN_REF}" in cred["reason"]
    assert body["counts"]["approved"] == 0, "a keyless TRIAL route must not be counted as active"
    detail = personal_client.get(f"/api/routes/{DS}").json()
    assert detail["credential"]["state"] == "MISSING"

    # near miss: storing the key is the only change; the route is then PRESENT and counted
    secrets_memory.put(ZEN_REF, "fake-zen-key-value", actor="test")
    body, by = _list(personal_client)
    assert by[DS]["credential"] == {"state": "PRESENT", "ref": ZEN_REF, "reason": None, "domain": "PERSONAL"}
    assert body["counts"]["approved"] == 1


def test_local_and_native_routes_need_no_key_and_count_as_active(personal_client: AuthedClient, db, monkeypatch):
    install_fakes(monkeypatch)
    store = CountingStore(set(), down=True)  # even a sealed store: no lookup for a route that needs no key
    _install_store(monkeypatch, store)
    _seed(db)
    approve(db, LOCAL, kind="TRIAL")
    db.commit()
    body, by = _list(personal_client)
    for key in (LOCAL, NATIVE):
        assert by[key]["credential"] == {"state": "NOT_NEEDED", "ref": None, "reason": None, "domain": "PERSONAL"}, key
    assert by[LOCAL]["active_decision"] and body["counts"]["approved"] == 1


def test_bite_sealed_store_is_unavailable_and_looked_up_once(personal_client: AuthedClient, db, monkeypatch):
    install_fakes(monkeypatch)
    _seed(db)
    approve(db, DS, kind="TRIAL")
    db.commit()
    store = CountingStore({ZEN_REF}, down=True)
    _install_store(monkeypatch, store)
    body, by = _list(personal_client)
    cred = by[DS]["credential"]
    assert cred["state"] == "UNAVAILABLE" and cred["ref"] == ZEN_REF, cred
    assert "cannot be checked: secret store unavailable" in cred["reason"]
    assert body["counts"]["approved"] == 0
    assert len(store.calls) == 1, store.calls  # the first failure short-circuits every other reference

    # near miss: the store answers again and the key is stored
    store.down = False
    store.calls.clear()
    body, by = _list(personal_client)
    assert by[DS]["credential"]["state"] == "PRESENT" and body["counts"]["approved"] == 1
    assert len(store.calls) == len(set(store.calls)), f"one lookup per reference per list call: {store.calls}"


def test_office_session_never_looks_up_personal_references(admin_client: AuthedClient, db, monkeypatch):
    """L12: the credential block is evaluated for the session's domain; an OFFICE view does not reveal whether a
    PERSONAL/worker key is stored (no lookup at all): a PERSONAL-only route is NOT_ELIGIBLE_IN_DOMAIN for OFFICE."""
    install_fakes(monkeypatch)
    _seed(db)
    approve(db, DS, kind="TRIAL")
    db.commit()
    store = CountingStore({ZEN_REF, "workers/openrouter"})
    _install_store(monkeypatch, store)
    admin_client.select_domain(Domain.OFFICE)
    body, by = _list(admin_client)
    assert by[DS]["credential"]["state"] == "NOT_ELIGIBLE_IN_DOMAIN" and by[DS]["credential"]["domain"] == "OFFICE"
    assert by[DS]["credential"]["ref"] is None
    assert not [c for c in store.calls if not c.startswith("office/")], store.calls
    assert body["counts"]["approved"] == 0
    admin_client.select_domain(Domain.PERSONAL)
    _, by = _list(admin_client)
    assert by[DS]["credential"]["state"] == "PRESENT"


# Review round 3 (findings 1 and 2): the credential block is evaluated only in a domain the route serves, and a
# reference name private to another domain or to a project the viewer cannot see never reaches the payload.
OFFICE_REF = "office/acme-corp-openai"


def _office_only(db, *, domains: list[str], ref: str = OFFICE_REF) -> None:
    r = service.get_route(db, DS)
    r.eligible_domains = domains
    r.mode_params = {**(r.mode_params or {}), "credential_ref": ref}
    approve(db, DS, kind="TRIAL")
    db.commit()


def test_bite_office_only_route_in_personal_session_is_not_eligible_and_never_looked_up(
        admin_client: AuthedClient, db, monkeypatch):
    """Finding 2: an activated OFFICE-only route with a stored key is not 'MISSING' in a PERSONAL session (the router
    never serves it there); finding 1: its office/ name and its stored state never reach the PERSONAL payload."""
    install_fakes(monkeypatch)
    _seed(db)
    _office_only(db, domains=["OFFICE"])
    store = CountingStore({OFFICE_REF})
    _install_store(monkeypatch, store)
    admin_client.select_domain(Domain.PERSONAL)
    r = admin_client.get("/api/routes")
    assert r.status_code == 200, r.text
    assert OFFICE_REF not in r.text and "acme-corp" not in r.text, "an office/ reference name reached a PERSONAL view"
    cred = {i["route_key"]: i for i in r.json()["items"]}[DS]["credential"]
    assert cred["state"] == "NOT_ELIGIBLE_IN_DOMAIN" and cred["ref"] is None and cred["domain"] == "PERSONAL", cred
    assert "route not eligible in PERSONAL" in cred["reason"]
    assert not [c for c in store.calls if c.startswith("office/")], f"PERSONAL looked up an office/ ref: {store.calls}"
    detail = admin_client.get(f"/api/routes/{DS}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["credential"]["state"] == "NOT_ELIGIBLE_IN_DOMAIN"
    assert OFFICE_REF not in str(detail.json()["credential"])

    # near miss: the same route in an OFFICE session is PRESENT, quoted and counted
    admin_client.select_domain(Domain.OFFICE)
    body, by = _list(admin_client)
    assert by[DS]["credential"] == {"state": "PRESENT", "ref": OFFICE_REF, "reason": None, "domain": "OFFICE"}
    assert body["counts"]["approved"] == 1


def test_bite_office_ref_on_a_two_domain_route_is_redacted_in_personal(admin_client: AuthedClient, db, monkeypatch):
    """Finding 1: the namespace check fails in PERSONAL; the router's reason is kept with the name replaced."""
    install_fakes(monkeypatch)
    _seed(db)
    _office_only(db, domains=["PERSONAL", "OFFICE"])
    store = CountingStore({OFFICE_REF})
    _install_store(monkeypatch, store)
    admin_client.select_domain(Domain.PERSONAL)
    r = admin_client.get("/api/routes")
    assert OFFICE_REF not in r.text and "acme-corp" not in r.text, "an office/ reference name reached a PERSONAL view"
    cred = {i["route_key"]: i for i in r.json()["items"]}[DS]["credential"]
    assert cred["state"] == "MISSING" and cred["ref"] is None, cred
    assert cred["reason"] == ("credential_ref <office/ reference, name withheld>: office/ credentials are never used "
                              "outside OFFICE"), cred
    assert not [c for c in store.calls if c.startswith("office/")], f"PERSONAL looked up an office/ ref: {store.calls}"
    # near miss: a platform-level workers/ reference that fails nowhere is still quoted in PERSONAL
    _office_only(db, domains=["PERSONAL", "OFFICE"], ref=ZEN_REF)
    _, by = _list(admin_client)
    assert by[DS]["credential"]["ref"] == ZEN_REF


def test_bite_project_ref_is_withheld_unless_the_viewer_sees_that_project(db, monkeypatch):
    """Finding 1: a projects/<id>/ reference passes the PERSONAL namespace check (the id comes from the ref itself);
    neither its name nor its stored state is shown unless the project exists in this domain and is in the viewer's
    project scope."""
    from types import SimpleNamespace

    from aeos.models.portfolio import Project
    from aeos.routing.router import route_view

    install_fakes(monkeypatch)
    _seed(db)
    ref = "projects/acme/openai"
    _office_only(db, domains=["PERSONAL"], ref=ref)
    store = CountingStore({ref})
    _install_store(monkeypatch, store)
    db.add(Project(project_key="acme", name="Acme", domain="PERSONAL"))
    db.flush()

    def viewer(scope: list[str] | None):
        def can_access_project(pid: str, domain: str) -> bool:
            return domain == "PERSONAL" and (scope is None or pid in scope)
        return SimpleNamespace(active_domain=Domain.PERSONAL, can_access_project=can_access_project)

    route = service.get_route(db, DS)
    outsider = route_view(db, route, viewer([]), credential_states={})["credential"]
    assert outsider["state"] == "WITHHELD" and outsider["ref"] is None and "acme" not in str(outsider), outsider
    assert not store.calls, f"a WITHHELD reference was looked up: {store.calls}"

    # near miss: a viewer whose scope holds the project sees the name and the stored state
    project_id = db.query(Project).filter_by(project_key="acme").one().id
    member = route_view(db, route, viewer([project_id]), credential_states={})["credential"]
    assert member == {"state": "PRESENT", "ref": ref, "reason": None, "domain": "PERSONAL"}, member


# Review round 4 (L12 strike 2): the credential block withheld the name, but GET /api/routes/{key} still sent
# config.mode_params.credential_ref and /api/routes/eligibility quoted it in rejection reasons. Every route payload a
# session can read is scanned as full JSON text, not one key.
OFFICE_NAME = OFFICE_REF.partition("/")[2]
WITHHELD_OFFICE = "<office/ reference, name withheld>"
ROUTE_ENDPOINTS = ("/api/routes", f"/api/routes/{DS}",
                   "/api/routes/eligibility?domain=PERSONAL&classification=PUBLIC")


def _bodies(client: AuthedClient, paths: tuple[str, ...] = ROUTE_ENDPOINTS) -> dict[str, str]:
    out = {}
    for path in paths:
        r = client.get(path)
        assert r.status_code == 200, f"{path}: {r.status_code} {r.text}"
        out[path] = r.text
    return out


def test_bite_office_ref_name_reaches_no_route_endpoint_of_a_personal_session(personal_client: AuthedClient, db,
                                                                               monkeypatch):
    install_fakes(monkeypatch)
    _seed(db)
    _office_only(db, domains=["OFFICE"])
    _install_store(monkeypatch, CountingStore({OFFICE_REF}))
    leaks = [path for path, text in _bodies(personal_client).items() if OFFICE_NAME in text]
    assert not leaks, f"an office/ reference name reached a PERSONAL session through {leaks}"
    detail = personal_client.get(f"/api/routes/{DS}").json()
    assert detail["config"]["mode_params"]["credential_ref"] == WITHHELD_OFFICE, detail["config"]
    # the config hash is still the hash of the real configuration (activation binds to it)
    assert detail["config_hash"] == service.compute_config_hash(service.get_route(db, DS))
    elig = personal_client.get("/api/routes/eligibility?domain=PERSONAL&classification=PUBLIC").json()
    assert f"credential_ref {WITHHELD_OFFICE}: office/ credentials are never used outside OFFICE" in elig["rejected"][DS]

    # near miss: an OFFICE session sees the name in the detail config
    personal_client.select_domain(Domain.OFFICE)
    detail = personal_client.get(f"/api/routes/{DS}").json()
    assert detail["config"]["mode_params"]["credential_ref"] == OFFICE_REF


def test_bite_personal_session_querying_office_eligibility_sees_no_office_project_name(personal_client: AuthedClient,
                                                                                      db, monkeypatch):
    """The session's selected domain decides what may be quoted, not the queried domain: OFFICE eligibility rejects a
    projects/ reference by name, and that OFFICE project's name never reaches a PERSONAL session."""
    from aeos.models.portfolio import Project

    install_fakes(monkeypatch)
    _seed(db)
    ref = f"projects/{OFFICE_NAME}/openai"
    _office_only(db, domains=["OFFICE"], ref=ref)
    db.add(Project(project_key=OFFICE_NAME, name="Acme", domain="OFFICE"))
    db.commit()
    path = "/api/routes/eligibility?domain=OFFICE&classification=PUBLIC"
    text = _bodies(personal_client, (path,))[path]
    assert OFFICE_NAME not in text, "an OFFICE project reference reached a PERSONAL session through OFFICE eligibility"
    assert "<projects/ reference, name withheld>" in text

    # near miss: an OFFICE session that sees the project gets the router's reason with the name
    personal_client.select_domain(Domain.OFFICE)
    assert ref in _bodies(personal_client, (path,))[path]


def test_workers_ref_is_still_quoted_to_a_personal_session(personal_client: AuthedClient, db, monkeypatch):
    """Near miss: a platform-level workers/ reference is not private; withholding never touches it."""
    install_fakes(monkeypatch)
    _seed(db)
    _office_only(db, domains=["OFFICE"], ref=ZEN_REF)
    bodies = _bodies(personal_client)
    assert ZEN_REF in bodies[f"/api/routes/{DS}"]
    assert personal_client.get(f"/api/routes/{DS}").json()["config"]["mode_params"]["credential_ref"] == ZEN_REF
    assert not any("name withheld" in t for t in bodies.values()), "a workers/ reference was withheld"


def test_withhold_refs_replaces_a_private_project_name_at_any_depth_only_when_not_visible():
    route = service.ModelRoute(route_key="x", mode_params={"credential_ref": "projects/acme/openai"})
    payload = {"config": {"mode_params": {"credential_ref": "projects/acme/openai"}},
               "rejected": {"x": ["credential projects/acme/openai not stored: aeos secrets set projects/acme/openai"]},
               "projects/acme/openai": [("projects/acme/openai",)], "n": 3}
    hidden = service.withheld_refs([route], domain="PERSONAL", project_visible=lambda p: False)
    assert hidden == {"projects/acme/openai": "<projects/ reference, name withheld>"}
    assert "acme" not in str(service.withhold_refs(payload, hidden))
    # near miss: a member of the project sees the payload unchanged
    shown = service.withheld_refs([route], domain="PERSONAL", project_visible=lambda p: p == "acme")
    assert shown == {} and service.withhold_refs(payload, shown) == payload


# Review round 5: the Doctor row route.registry has no domain, so every session reads it; it copied the routing
# rejection reasons (evidence.rejected_decided) and the first one into its summary, quoting the office/ name.
DOCTOR_ENDPOINTS = ("/api/doctor/checks", "/api/doctor/checks/route.registry/history", "/api/doctor/capabilities")


def _run_route_registry(settings, db, monkeypatch, ref: str, domains: list[str], *, stored: bool = True):
    from aeos.doctor.checks.model_route import route_registry
    from aeos.doctor.model import CheckContext
    from aeos.doctor.runner import run_doctor
    from tests.unit.test_inventory_fakes import base_runner

    install_fakes(monkeypatch, doctor=False, inventory=False)  # the real Doctor stores the row
    _seed(db)
    _office_only(db, domains=domains, ref=ref)
    _install_store(monkeypatch, CountingStore({ref} if stored else set()))
    run_doctor(settings, db, checks=["route.registry"], runner=base_runner())
    db.commit()
    return route_registry(CheckContext(settings=settings, session=db, runner=base_runner(), repo=settings.data_dir,
                                       now=utcnow()))


def test_bite_office_ref_name_reaches_no_doctor_route_registry_payload(personal_client: AuthedClient, settings, db,
                                                                         monkeypatch):
    outcome = _run_route_registry(settings, db, monkeypatch, OFFICE_REF, ["OFFICE"])
    assert OFFICE_NAME not in str(outcome), f"route.registry quoted an office/ reference name: {outcome}"
    reasons = outcome.evidence["rejected_decided"][DS]
    assert f"credential_ref {WITHHELD_OFFICE}: office/ credentials are never used outside OFFICE" in reasons, reasons
    leaks = [path for path, text in _bodies(personal_client, DOCTOR_ENDPOINTS).items() if OFFICE_NAME in text]
    assert not leaks, f"an office/ reference name reached a PERSONAL session through {leaks}"


def test_bite_two_domain_route_registry_summary_withholds_the_office_name(settings, db, monkeypatch):
    """On a ['PERSONAL', 'OFFICE'] route the credential reason comes first, so the summary example quoted the name."""
    outcome = _run_route_registry(settings, db, monkeypatch, OFFICE_REF, ["PERSONAL", "OFFICE"])
    assert f"{DS}: credential_ref {WITHHELD_OFFICE}" in outcome.summary, outcome.summary
    assert OFFICE_NAME not in str(outcome), f"route.registry quoted an office/ reference name: {outcome}"


def test_workers_ref_is_still_quoted_in_doctor_route_registry(personal_client: AuthedClient, settings, db,
                                                               monkeypatch):
    """Near miss: a platform-level workers/ reference is not private; the Doctor row keeps quoting it (here: the
    PERSONAL route is kept out because that key is not stored)."""
    outcome = _run_route_registry(settings, db, monkeypatch, ZEN_REF, ["PERSONAL", "OFFICE"], stored=False)
    assert ZEN_REF in str(outcome.evidence["rejected_decided"]), outcome.evidence
    assert outcome.evidence["excluded_keyless"] == {DS: ZEN_REF} and ZEN_REF in outcome.summary, outcome.summary
    assert "name withheld" not in str(outcome), "a workers/ reference was withheld"
    assert ZEN_REF in _bodies(personal_client, ("/api/doctor/checks",))["/api/doctor/checks"]
