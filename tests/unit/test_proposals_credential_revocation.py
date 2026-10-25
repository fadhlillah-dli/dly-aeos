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


def test_the_review_never_revokes_anything(inventory, monkeypatch):
    """Building the plan is read-only for secrets: no delete/put is reachable from a review."""
    calls: list[str] = []
    monkeypatch.setattr(secrets, "put_secret", lambda *a, **k: calls.append("put"))
    from aeos.secrets import provider

    monkeypatch.setattr(provider, "provider_delete", lambda *a, **k: calls.append("delete"))
    review(inventory, create(inventory, kind="REMOVE", existing_component_ids=["openrouter"]))
    assert calls == []
