"""§40 "Credential komponen lama dicabut setelah consumer sah dipindahkan" (W6-201), on the REAL InstallManifest.

The review package's revocation list is what the owner runs 'aeos secrets delete <ref>' on, so it must name exact
secret references only (never inventory prose such as "capability token per run" or wildcards such as
``workers/*``), and a reference that another component still uses (``workers/openrouter`` is used by
capability-broker, litellm and openrouter) is SHARED - never offered as safe to revoke. Revocation stays a
manual owner step: the review never deletes or rotates anything."""

from __future__ import annotations

import pytest

from aeos.inventory.seed import run as seed_inventory
from aeos.models.inventory import ComponentReference
from aeos.proposals import review as R
from aeos.secrets import facade as secrets
from tests.unit.proposal_fixtures import create, review


@pytest.fixture
def inventory(db):
    seed_inventory(db)  # the reviewed install-manifest/manifest.yaml, DESIGN lens
    db.commit()
    return db


def _identity(db, cid: str) -> str:
    return (db.get(ComponentReference, cid).access or {}).get("identity_ref")


def test_manifest_identity_fields_split_into_exact_refs_and_descriptions(inventory):
    db = inventory
    broker = R.credential_entries(_identity(db, "capability-broker"))  # "workers/openrouter, personal/github (...)"
    assert [e.get("ref") for e in broker] == ["workers/openrouter", "personal/github"]
    assert broker[1]["note"] == "diambil broker" and all(e["known"] for e in broker)
    assert R.credential_entries(_identity(db, "opencode")) == [{"description": "capability token per run"}]
    assert R.credential_entries(_identity(db, "litellm")) == [{"description": "workers/* (virtual keys)"}]
    assert [e.get("ref") for e in R.credential_entries(_identity(db, "openrouter"))] == ["workers/openrouter"]
    # a ';' inside a parenthesised note does not split the entry
    assert R.credential_entries("login native Claude (dikelola client resmi; tidak dipindah ke vault)") == [
        {"description": "login native Claude (dikelola client resmi; tidak dipindah ke vault)"}]
    # every reference offered to the owner is one 'aeos secrets delete' accepts
    for e in broker:
        assert secrets.parse_ref(e["ref"]) == e["ref"]
    assert secrets.parse_ref("workers/*") is None and secrets.parse_ref("capability token per run") is None


def test_a_ref_another_component_still_uses_is_shared_not_revocable(inventory):
    p = review(inventory, create(inventory, kind="REPLACE", existing_component_ids=["openrouter"]))
    creds = p.review_package["replacement_plan"]["credential_egress_changes"]
    assert creds["credential_refs_to_revoke_after_cutover"] == []
    shared = " ".join(creds["shared_credential_refs"])
    assert shared.startswith("workers/openrouter: SHARED") and "capability-broker" in shared and "litellm" in shared


def test_prose_identity_is_listed_as_not_a_secret_ref(inventory):
    p = review(inventory, create(inventory, kind="REPLACE", existing_component_ids=["opencode"]))
    creds = p.review_package["replacement_plan"]["credential_egress_changes"]
    assert creds["credential_refs_to_revoke_after_cutover"] == []
    assert "opencode: capability token per run (deskripsi, bukan secret ref)" in creds["not_secret_refs"]
    assert not any("capability token" in r for r in creds["credential_refs_to_revoke_after_cutover"])


def test_an_exclusive_ref_is_offered_and_an_unregistered_ref_is_flagged(inventory):
    """publisher holds personal/google-oauth (not a well-known ref: the real ones are -client/-token) together with
    the google-docs/drive components; a ref nobody else uses would be offered as revocable."""
    db = inventory
    users = R.credential_ref_users(db, create(db, kind="REPLACE", existing_component_ids=["publisher"]))
    p = review(db, create(db, kind="REPLACE", existing_component_ids=["publisher"]))
    creds = p.review_package["replacement_plan"]["credential_egress_changes"]
    held = {"personal/google-oauth", "personal/github", "office/atlassian"}
    assert set(creds["credential_refs_to_revoke_after_cutover"]) == {r for r in held if not users.get(r)}
    assert all(r.split(":")[0] in held for r in creds["shared_credential_refs"])
    assert "personal/google-oauth" in creds["refs_not_well_known"]
    # with no other user the ref becomes revocable (the publisher's own relations are not "other users")
    lone = ComponentReference(component_id="lone-tool", kind="cli-tool", name="Lone",
                              access={"credential_ref": "workers/gemini"})
    db.add(lone)
    db.commit()
    lone_p = review(db, create(db, kind="REPLACE", existing_component_ids=["lone-tool"]))
    assert lone_p.review_package["replacement_plan"]["credential_egress_changes"][
        "credential_refs_to_revoke_after_cutover"] == ["workers/gemini"]


def test_upgrade_keeps_the_ref_of_the_new_version_shared(inventory):
    p = review(inventory, create(inventory, kind="UPGRADE", existing_component_ids=["openrouter"]))
    shared = " ".join(p.review_package["replacement_plan"]["credential_egress_changes"]["shared_credential_refs"])
    assert "openrouter (kandidat)" in shared


def test_remove_closure_items_name_exact_refs_and_mark_shared_ones(inventory):
    p = review(inventory, create(inventory, kind="REMOVE", existing_component_ids=["capability-broker"]))
    items = {i["id"]: i for i in p.review_package["removal_plan"]["items"] if i["kind"] == "identity"}
    assert "identity:workers/openrouter, personal/github (diambil broker)" not in items  # no prose ids any more
    wo = items["identity:workers/openrouter"]
    assert wo["secret_ref"] == "workers/openrouter" and {"litellm", "openrouter"} <= set(wo["shared_with"])
    assert "SHARED" in wo["item"] and "aeos secrets delete workers/openrouter" in wo["item"]
    assert items["identity:personal/github"]["secret_ref"] == "personal/github"

    lite = review(inventory, create(inventory, kind="REMOVE", existing_component_ids=["litellm"]))
    lite_items = {i["id"]: i for i in lite.review_package["removal_plan"]["items"] if i["kind"] == "identity"}
    wild = lite_items["identity:workers/* (virtual keys)"]
    assert wild["secret_ref"] is None and "bukan secret ref" in wild["item"] and "secrets delete" in wild["item"]
    assert "capability-broker" in lite_items["identity:workers/openrouter"]["shared_with"]


def _lone(db, cid: str, ref: str, domains: list[str] | None = None) -> None:
    db.add(ComponentReference(component_id=cid, kind="cli-tool", name=cid.title(), access={"credential_ref": ref},
                              scope={"domains": domains} if domains else {}))
    db.commit()


def test_a_ref_the_other_domain_still_uses_is_shared_but_never_named(inventory):
    """One secret store per host: revoking workers/gemini for a PERSONAL replacement would break an OFFICE-only
    component that uses it too, so the ref is SHARED (fail closed) - but the OFFICE component is not named."""
    db = inventory
    _lone(db, "personal-tool", "workers/gemini", ["PERSONAL"])
    _lone(db, "office-secret-tool", "workers/gemini", ["OFFICE"])
    p = review(db, create(db, kind="REPLACE", existing_component_ids=["personal-tool"]))
    creds = p.review_package["replacement_plan"]["credential_egress_changes"]
    assert creds["credential_refs_to_revoke_after_cutover"] == []
    shared = " ".join(creds["shared_credential_refs"])
    assert shared.startswith("workers/gemini: SHARED") and R.OTHER_DOMAIN_USER in shared
    assert "office-secret-tool" not in str(p.review_package)  # no other-domain metadata in this review


def test_replace_tracks_revocation_per_ref_as_progress_after_acceptance_too(inventory):
    """W6-201: REPLACE/UPGRADE revocation is a tracked item per exact ref (credential:<ref>), recorded by the owner
    as CLOSED or WAIVED (with the reason); it is progress (never re-binds the candidate, never a gate) and the
    proposal view shows the status as it stands now. Descriptions are not items: nothing to 'secrets delete'."""
    from aeos.core.errors import ValidationFailed
    from aeos.proposals import plan
    from aeos.proposals import service as S

    db = inventory
    p = review(db, create(db, kind="REPLACE", existing_component_ids=["capability-broker"]))
    rev = p.review_package["credential_revocation"]
    items = {i["id"]: i for i in rev["items"]}
    assert set(items) == {"credential:workers/openrouter", "credential:personal/github"}
    assert items["credential:workers/openrouter"]["shared_with"] and "SHARED" in items["credential:workers/openrouter"]["item"]
    assert rev["open"] == sorted(items) and rev["status"] == "OPEN"
    before = p.candidate_hash
    out = plan.update(db, p, {"credentials": {
        "credential:personal/github": {"status": "CLOSED", "note": "dicabut di GitHub, secrets delete"},
        "credential:workers/openrouter": {"status": "WAIVED", "note": "masih dipakai litellm/openrouter"}}},
        actor_user_id="owner")
    db.commit()
    assert out["rebinds_candidate"] is False and p.candidate_hash == before
    view = S.proposal_view(db, p, detail=True)["review_package"]["credential_revocation"]
    assert {i["id"]: i["status"] for i in view["items"]} == {"credential:personal/github": "CLOSED",
                                                            "credential:workers/openrouter": "WAIVED"}
    assert view["open"] == [] and view["status"] == "CLOSED"
    with pytest.raises(ValidationFailed):  # a WAIVED item needs the owner's reason
        plan.update(db, p, {"credentials": {"credential:personal/github": {"status": "WAIVED", "note": ""}}},
                    actor_user_id="owner")
    with pytest.raises(ValidationFailed):  # only refs of the current plan
        plan.update(db, p, {"credentials": {"credential:workers/other": {"status": "CLOSED"}}}, actor_user_id="owner")
    # recordable after the outcome too: the right time to revoke can be after ACCEPTED
    p.status = "ACCEPTED"
    db.commit()
    plan.update(db, p, {"credentials": {"credential:personal/github": {"status": "OPEN", "note": ""}}},
                actor_user_id="owner")
    db.commit()
    assert S.proposal_view(db, p, detail=True)["review_package"]["credential_revocation"]["open"] == [
        "credential:personal/github"]
    opencode = review(db, create(db, kind="REPLACE", existing_component_ids=["opencode"]))
    assert opencode.review_package["credential_revocation"]["items"] == []  # "capability token per run": no ref
    remove = review(db, create(db, kind="REMOVE", existing_component_ids=["openrouter"]))
    assert remove.review_package["credential_revocation"] is None
    with pytest.raises(ValidationFailed):
        plan.update(db, remove, {"credentials": {"credential:workers/openrouter": {"status": "CLOSED"}}},
                    actor_user_id="owner")


def test_remove_closure_progress_shows_on_the_proposal_view(inventory):
    """Closure is recorded after the review (progress, no re-review): the proposal page must show the recorded
    status, not the OPEN snapshot of review time."""
    from aeos.proposals import plan
    from aeos.proposals import service as S

    db = inventory
    p = review(db, create(db, kind="REMOVE", existing_component_ids=["openrouter"]))
    item = next(i for i in p.review_package["removal_plan"]["items"] if i["id"] == "identity:workers/openrouter")
    assert item["status"] == "OPEN"
    plan.update(db, p, {"removal": {item["id"]: {"status": "WAIVED", "note": "SHARED: tetap dipakai broker"}}},
                actor_user_id="owner")
    db.commit()
    shown = {i["id"]: i for i in S.proposal_view(db, p, detail=True)["review_package"]["removal_plan"]["items"]}
    assert shown[item["id"]]["status"] == "WAIVED" and shown[item["id"]]["note"].startswith("SHARED")
    assert item["id"] not in S.proposal_view(db, p, detail=True)["review_package"]["removal_plan"]["open"]


def test_the_review_never_revokes_anything(inventory, monkeypatch):
    """Building the plan is read-only for secrets: no delete/put is reachable from a review."""
    calls: list[str] = []
    monkeypatch.setattr(secrets, "put_secret", lambda *a, **k: calls.append("put"))
    from aeos.secrets import provider

    monkeypatch.setattr(provider, "provider_delete", lambda *a, **k: calls.append("delete"))
    review(inventory, create(inventory, kind="REMOVE", existing_component_ids=["openrouter"]))
    assert calls == []
