"""Owner decision paths of the Policy & Decision Registry (§02 change control, §12, §34).

* ADR register: the owner accepts/rejects/supersedes an ADR as a Decision of kind ADR bound to the ADR content hash,
  only from a live SUPER_ADMIN session (decisions page) or ``aeos policy adr-decide`` with the password; a later
  edit of the file makes the owner status STALE and the register shows owner-recorded vs builder-written status.
* §12 paid extra usage: AEOS cannot read the claude.ai setting; the owner's ACTIVE POLICY attestation (preset
  ``claude_extra_usage_off``) is consumed by the Doctor check and the bridge view as CONFIGURED (E0), never VERIFIED.
* §34 per-pack mapping slots are exposed by ``GET /api/policy/mci`` (UNVERIFIED without a VERIFIED MCI + decision).
"""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from aeos.core.enums import Domain
from aeos.core.timeutil import utcnow
from aeos.models.decisions import ArchitectureDecisionRecord, Decision
from aeos.policy import adrs
from tests.conftest import ADMIN_PW, VIEWER_PW, AuthedClient, create_admin, create_viewer

EXPIRES = (utcnow() + timedelta(days=30)).isoformat()


@pytest.fixture
def adr_dir(tmp_path: Path, monkeypatch) -> Path:
    """A register of two real ADR files in a scratch decisions directory (edits never touch the repository)."""
    src = adrs.decisions_dir()
    d = tmp_path / "decisions"
    d.mkdir()
    for name in ("ADR-0001", "ADR-0002"):
        shutil.copyfile(next(src.glob(f"{name}-*.md")), d / next(src.glob(f"{name}-*.md")).name)
    monkeypatch.setattr(adrs, "decisions_dir", lambda: d)
    return d


def _load(db) -> None:
    adrs.load_adrs(db)
    db.commit()


def _items(c: AuthedClient) -> dict[str, dict]:
    body = c.get("/api/adrs").json()
    return {i["key"]: i for i in body["items"]}


def _decide(c: AuthedClient, key: str, **body):
    base = {"outcome": "ACCEPTED", "rationale": "ditinjau dan diterima pemilik"}
    return c.post(f"/api/adrs/{key}/decision", json={**base, **body})


# ---------------------------------------------------------------- §02 ADR change control


def test_owner_records_an_adr_decision_bound_to_its_content_hash(personal_client: AuthedClient, db, adr_dir):
    _load(db)
    before = _items(personal_client)["ADR-0001"]
    assert before["owner_status_state"] == "NOT_RECORDED" and before["status_source"] == "FILE (builder-written)"
    assert before["effective_status"] == before["file_status"] == before["status"]
    loaded = before["content_hash"]

    wrong = _decide(personal_client, "ADR-0001", content_hash="sha256:" + "0" * 64)
    assert wrong.status_code == 409 and wrong.json()["error"]["code"] == "ADR_CONTENT_CHANGED"
    r = _decide(personal_client, "ADR-0001", outcome="REJECTED", content_hash=loaded)
    assert r.status_code == 201, r.text
    d = r.json()["decision"]
    assert (d["kind"], d["target_type"], d["target_id"], d["target_hash"]) == ("ADR", "adr", "ADR-0001", loaded)
    assert d["actor_kind"] == "HUMAN" and d["details"]["outcome"] == "REJECTED"
    after = _items(personal_client)["ADR-0001"]
    assert (after["owner_status"], after["owner_status_state"], after["effective_status"], after["status_source"]) == (
        "REJECTED", "RECORDED", "REJECTED", "OWNER_DECISION")
    assert after["file_status"] == before["file_status"]  # the builder-written line is shown, not overwritten
    # a new owner decision supersedes the earlier one
    r2 = _decide(personal_client, "ADR-0001", content_hash=loaded)
    assert r2.status_code == 201
    db.expire_all()
    first = db.get(Decision, d["id"])
    assert first.status == "SUPERSEDED" and first.superseded_by == r2.json()["decision"]["id"]
    assert _items(personal_client)["ADR-0001"]["effective_status"] == "ACCEPTED"
    detail = personal_client.get("/api/adrs/ADR-0001").json()
    assert {x["kind"] for x in detail["linked_decisions"]} == {"ADR"}
    shapes = personal_client.get("/api/decisions/shapes").json()
    assert shapes["adr"]["outcomes"] == ["ACCEPTED", "REJECTED", "SUPERSEDED"]


def test_editing_the_adr_file_makes_the_owner_status_stale(personal_client: AuthedClient, db, adr_dir):
    _load(db)
    key = "ADR-0002"
    loaded = _items(personal_client)[key]["content_hash"]
    assert _decide(personal_client, key, content_hash=loaded).status_code == 201
    path = next(adr_dir.glob(f"{key}-*.md"))
    path.write_text(path.read_text(encoding="utf-8") + "\nEdited after the owner's decision.\n", encoding="utf-8")
    # the file on disk no longer is the content the register loaded: no decision on unreviewed content
    stale_file = _decide(personal_client, key, content_hash=loaded)
    assert stale_file.status_code == 409 and stale_file.json()["error"]["code"] == "ADR_FILE_CHANGED"
    _load(db)  # reload: the register now has the edited content
    item = _items(personal_client)[key]
    assert item["owner_status_state"] == "STALE" and item["status_source"] == "FILE (builder-written)"
    assert item["effective_status"] == item["file_status"] and item["owner_decision"]["target_hash"] == loaded
    counts = personal_client.get("/api/adrs").json()["owner_status_counts"]
    assert counts.get("STALE") == 1


def test_an_owner_rejection_is_not_undone_by_editing_the_adr_file(personal_client: AuthedClient, db, adr_dir):
    """Whoever can edit the file (the builder) cannot turn the owner's REJECTED back into the file's ACCEPTED: the
    stale rejection stays the effective status until the owner decides anew (fail closed)."""
    _load(db)
    key = "ADR-0001"
    item = _items(personal_client)[key]
    assert item["file_status"] == "ACCEPTED"
    assert _decide(personal_client, key, outcome="REJECTED", content_hash=item["content_hash"]).status_code == 201
    path = next(adr_dir.glob(f"{key}-*.md"))
    path.write_text(path.read_text(encoding="utf-8") + "\nRewritten after the owner's rejection.\n", encoding="utf-8")
    _load(db)
    item = _items(personal_client)[key]
    assert (item["owner_status_state"], item["owner_status"], item["file_status"]) == ("STALE", "REJECTED", "ACCEPTED")
    assert (item["effective_status"], item["status_source"]) == ("REJECTED", "OWNER_DECISION (STALE)")
    # the owner reviews the new content and accepts it: that decision binds the new hash and wins
    assert _decide(personal_client, key, content_hash=item["content_hash"]).status_code == 201
    item = _items(personal_client)[key]
    assert (item["owner_status_state"], item["effective_status"]) == ("RECORDED", "ACCEPTED")


def test_adr_memory_follows_the_owner_decision(personal_client: AuthedClient, db, adr_dir):
    """§24 long-term memory: an ADR becomes platform memory only while ACCEPTED as the owner decides, not as the file
    says; a decision past expires_at is no memory source either (EXPIRED before the expiry is written back)."""
    from aeos.models.portfolio import Project
    from tests.unit.test_portfolio_support import create_project

    _load(db)
    item = _items(personal_client)["ADR-0001"]
    assert item["file_status"] == "ACCEPTED"
    pid = create_project(personal_client, name="DLY AEOS platform")["id"]
    db.get(Project, pid).is_platform = True
    db.commit()

    def propose(content: str, source_type: str, source_id: str):
        return personal_client.post(f"/api/projects/{pid}/memory", json={
            "content": content, "source_type": source_type, "source_id": source_id})

    assert propose("ADR-0001 berlaku untuk platform", "adr", "ADR-0001").status_code == 200  # file ACCEPTED
    assert _decide(personal_client, "ADR-0001", outcome="REJECTED", content_hash=item["content_hash"]).status_code == 201
    r = propose("ADR-0001 tetap berlaku untuk platform", "adr", "ADR-0001")
    assert r.status_code == 403 and r.json()["error"]["code"] == "MEMORY_SOURCE_NOT_ACCEPTED"
    assert "REJECTED" in r.json()["error"]["message"]
    lapsed = Decision(kind="POLICY", actor_user_id="owner", actor_kind="HUMAN", target_type="project_convention",
                      target_id="lapsed", domain="PERSONAL", status="ACTIVE", rationale="lapsed convention",
                      expires_at=utcnow() - timedelta(minutes=5))
    db.add(lapsed)
    db.commit()
    r = propose("Konvensi yang sudah lewat masa berlaku", "decision", lapsed.id)
    assert r.status_code == 403 and "EXPIRED" in r.json()["error"]["message"]


def test_adr_decision_paths_are_owner_only_and_exact(client: AuthedClient, personal_client: AuthedClient, db, adr_dir):
    _load(db)
    loaded = _items(personal_client)["ADR-0001"]["content_hash"]
    bad = _decide(personal_client, "ADR-0001", outcome="SUPERSEDED", content_hash=loaded)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "ADR_SUCCESSOR_REQUIRED"
    ok = _decide(personal_client, "ADR-0001", outcome="SUPERSEDED", content_hash=loaded, superseded_by="ADR-0002")
    assert ok.status_code == 201 and ok.json()["adr"]["owner_decision"]["superseded_by"] == "ADR-0002"
    assert _decide(personal_client, "ADR-9999", content_hash=loaded).status_code == 404
    assert _decide(personal_client, "ADR-0001", outcome="APPROVED", content_hash=loaded).status_code == 422
    # never through the generic decision endpoint
    generic = personal_client.post("/api/decisions", json={
        "kind": "POLICY", "target_type": "adr", "target_id": "ADR-0001", "auth_scope": {"adr": "ADR-0001"},
        "rationale": "diterima lewat jalur umum"})
    assert generic.status_code == 422 and generic.json()["error"]["code"] == "DEDICATED_ENDPOINT_REQUIRED"
    # a VIEWER session is read-only
    create_viewer(db)
    personal_client.client.cookies.clear()
    client.login("viewer", VIEWER_PW)
    denied = _decide(client, "ADR-0002", content_hash=_items(client)["ADR-0002"]["content_hash"])
    assert denied.status_code == 403
    assert not list(db.scalars(Decision.__table__.select().where(Decision.target_id == "ADR-0002")))


def test_adr_decisions_need_a_super_admin_scoped_to_the_platform_domain(db, adr_dir):
    """§20: SUPER_ADMIN is not AUTH over every domain. ADRs are platform (PERSONAL) documents, so an admin scoped to
    OFFICE only, or an owner-recorded actor, cannot accept, reject or supersede them."""
    from aeos.core.errors import PolicyDenied

    _load(db)
    row = db.scalar(select(ArchitectureDecisionRecord).where(ArchitectureDecisionRecord.adr_key == "ADR-0001"))
    owner = create_admin(db)
    owner.domain_scope = ["OFFICE"]
    db.commit()
    for actor, code in ((owner.id, "DECISION_SCOPE_DENIED"), ("owner:ADR-0003", "DECISION_ACTOR_NOT_AUTHORISED")):
        with pytest.raises(PolicyDenied) as exc:
            adrs.record_adr_decision(db, adr_key="ADR-0001", outcome="ACCEPTED", actor_user_id=actor,
                                     rationale="diterima oleh admin di luar cakupan", content_hash=row.content_hash)
        assert exc.value.code == code
    owner.domain_scope = ["PERSONAL"]
    db.commit()
    d = adrs.record_adr_decision(db, adr_key="ADR-0001", outcome="ACCEPTED", actor_user_id=owner.id,
                                 rationale="diterima oleh pemilik platform", content_hash=row.content_hash)
    assert d.domain is None and d.actor_kind == "HUMAN"


def test_cli_adr_decide_needs_the_admin_password(settings, db, adr_dir):
    from aeos.cli import app

    create_admin(db)
    _load(db)
    runner = CliRunner()
    wrong = runner.invoke(app, ["policy", "adr-decide", "ADR-0002", "--outcome", "ACCEPTED", "--as", "owner",
                                "--rationale", "ditinjau pemilik lewat CLI"], input="not-it\n")
    assert wrong.exit_code != 0
    ok = runner.invoke(app, ["policy", "adr-decide", "ADR-0002", "--outcome", "ACCEPTED", "--as", "owner",
                             "--rationale", "ditinjau pemilik lewat CLI"], input=ADMIN_PW + "\n")
    assert ok.exit_code == 0, ok.output
    assert ok.output.isascii() and "Recorded ADR ADR-0002 ACCEPTED" in ok.output
    listed = runner.invoke(app, ["policy", "adrs"])
    assert listed.exit_code == 0 and "owner=ACCEPTED (RECORDED)" in listed.output and listed.output.isascii()


# ---------------------------------------------------------------- §12 paid extra usage attestation


def _extra_billing(settings, db):
    from aeos.doctor.checks import REGISTRY, load_all
    from aeos.doctor.runner import make_context, run_check
    from tests.unit.test_inventory_fakes import base_runner

    load_all()
    return run_check(REGISTRY["claude.extra_billing"], make_context(settings, db, runner=base_runner()))


def test_owner_attestation_makes_extra_billing_configured_never_verified(personal_client: AuthedClient, db, settings):
    from aeos.claude_bridge import bridge

    assert _extra_billing(settings, db).status == "UNKNOWN"
    assert bridge.extra_usage_view(db)["state"] == "UNKNOWN"
    shapes = personal_client.get("/api/decisions/shapes").json()["policy_presets"]["claude_extra_usage_off"]
    assert (shapes["target_type"], shapes["target_id"], shapes["expiry_required"]) == (
        "claude_extra_usage", "PERSONAL", True)
    body = {"kind": "POLICY", "policy_preset": "claude_extra_usage_off", "auth_scope": {"akun": "Max pribadi"},
            "rationale": "extra usage berbayar dimatikan di claude.ai"}
    no_expiry = personal_client.post("/api/decisions", json=body)
    assert no_expiry.status_code == 422 and no_expiry.json()["error"]["code"] == "POLICY_PRESET_EXPIRY_REQUIRED"
    r = personal_client.post("/api/decisions", json={**body, "expires_at": EXPIRES})
    assert r.status_code == 201, r.text
    d = r.json()
    assert (d["target_type"], d["target_id"], d["details"]["extra_usage_disabled"]) == (
        "claude_extra_usage", "PERSONAL", True)
    assert d["details"]["evidence_level"] == "E0"
    db.expire_all()
    res = _extra_billing(settings, db)
    assert res.status == "CONFIGURED" and res.observed_evidence["evidence_level"] == "E0"
    assert res.observed_evidence["decision_id"] == d["id"] and res.status != "VERIFIED"
    view = bridge.extra_usage_view(db)
    assert (view["state"], view["attestation"], view["decision_id"]) == ("CONFIGURED", "ATTESTED_OFF", d["id"])
    # the owner records that it is ON again: an observed failure of §12, not an unknown
    on = personal_client.post("/api/decisions", json={
        "kind": "POLICY", "target_type": "claude_extra_usage", "target_id": "PERSONAL",
        "details": {"extra_usage_disabled": False}, "auth_scope": {"akun": "Max pribadi"}, "expires_at": EXPIRES,
        "rationale": "extra usage berbayar diaktifkan sementara"})
    assert on.status_code == 201, on.text
    db.expire_all()
    assert db.get(Decision, d["id"]).status == "SUPERSEDED"
    res = _extra_billing(settings, db)
    assert res.status == "FAILED" and res.error_code == "EXTRA_USAGE_ATTESTED_ON"
    assert bridge.extra_usage_view(db)["state"] == "FAILED"
    # an expired attestation is no attestation (judged by expires_at, before the expiry is written back)
    row = db.get(Decision, on.json()["id"])
    row.expires_at = utcnow() - timedelta(minutes=1)
    db.commit()
    assert _extra_billing(settings, db).status == "UNKNOWN"


def test_attestation_shape_is_exact(db, settings):
    from aeos.core.errors import ValidationFailed
    from aeos.policy import recording

    owner = create_admin(db)
    base = {"actor_user_id": owner.id, "kind": "POLICY", "rationale": "atestasi pemilik extra usage",
            "auth_scope": {"akun": "Max"}, "expires_at": utcnow() + timedelta(days=7)}
    cases = (({"domain": Domain.OFFICE, "policy_preset": "claude_extra_usage_off"}, "POLICY_PRESET_DOMAIN_DENIED"),
             ({"domain": Domain.PERSONAL, "target_type": "claude_extra_usage", "target_id": "OFFICE"},
              "POLICY_PRESET_MISMATCH"),
             ({"domain": Domain.PERSONAL, "target_type": "project_convention", "target_id": "x",
               "details": {"extra_usage_disabled": True}}, "POLICY_PRESET_MISMATCH"),
             ({"domain": Domain.PERSONAL, "policy_preset": "claude_extra_usage_off",
               "details": {"extra_usage_disabled": "yes"}}, "POLICY_PRESET_DETAILS_INVALID"),
             ({"domain": Domain.PERSONAL, "kind": "EXCEPTION", "policy_preset": "claude_extra_usage_off"},
              "POLICY_PRESET_MISMATCH"))
    for extra, code in cases:
        with pytest.raises(ValidationFailed) as exc:
            recording.record_generic_decision(db, **{**base, **extra})
        assert exc.value.code == code, extra


def test_attestation_invalidates_the_stored_extra_billing_result(personal_client: AuthedClient, db):
    from aeos.policy.decisions import readiness_invalidators

    assert readiness_invalidators(Decision(kind="POLICY", target_type="claude_extra_usage", target_id="PERSONAL")) == [
        "config:policy", "config:claude_extra_usage"]
    from aeos.doctor.checks import REGISTRY, load_all

    load_all()
    assert "config:claude_extra_usage" in REGISTRY["claude.extra_billing"].invalidators


def test_cli_records_the_extra_usage_attestation_preset(settings, db):
    from aeos.cli import app
    from aeos.policy import facade as policy

    create_admin(db)
    runner = CliRunner()
    no_days = runner.invoke(app, ["policy", "decide", "--kind", "POLICY", "--as", "owner", "--domain", "PERSONAL",
                                  "--policy-preset", "claude_extra_usage_off",
                                  "--rationale", "extra usage berbayar dimatikan di claude.ai"], input=ADMIN_PW + "\n")
    assert no_days.exit_code != 0 and "POLICY_PRESET_EXPIRY_REQUIRED" in no_days.output
    ok = runner.invoke(app, ["policy", "decide", "--kind", "POLICY", "--as", "owner", "--domain", "PERSONAL",
                             "--policy-preset", "claude_extra_usage_off", "--days", "90",
                             "--rationale", "extra usage berbayar dimatikan di claude.ai"], input=ADMIN_PW + "\n")
    assert ok.exit_code == 0, ok.output
    assert ok.output.isascii() and "claude_extra_usage:PERSONAL" in ok.output
    db.expire_all()
    assert policy.claude_extra_usage_attestation(db)["state"] == "ATTESTED_OFF"


# ---------------------------------------------------------------- §34 per-pack slots through the API


def test_mci_api_exposes_every_pack_slot_and_hides_platform_ids_outside_personal(personal_client: AuthedClient,
                                                                                 monkeypatch, tmp_path):
    from aeos.policy import snapshot
    from tests.unit import mci_support as ms

    repo = ms.fake_repo(tmp_path, reference=ms.UNAVAILABLE.replace("packs: [ARCH, QA]\n", ""))
    (repo / "policy" / "mci-reference.yaml").write_text(
        ms.UNAVAILABLE.replace("packs: [ARCH, QA]\n", "packs: [" + ", ".join(snapshot.MASTER_PACKS) + "]\n"),
        encoding="utf-8")
    ms.isolate(monkeypatch, repo)
    mci = personal_client.get("/api/policy/mci").json()
    assert [p["key"] for p in mci["pack_mappings"]] == list(snapshot.MASTER_PACKS) and mci["packs_missing"] == []
    assert all(p["mapping_status"] == "UNVERIFIED" and p["evidence_contract_status"] == "UNDECIDED"
               and p["binding"]["target_type"] == "mci_pack_mapping" for p in mci["pack_mappings"])
    assert (mci["packs_mapping_status"], mci["compliance"], mci["source_placement"]) == (
        "UNVERIFIED", "NOT-PROVEN", "LOCAL_ONLY")
    assert mci["pack_mapping_file"] == "<data dir>/policy/canonical/mci-pack-mapping.yaml"
    assert mci["source"] == "policy/mci-reference.yaml" and mci["checked_at"]
