"""Decision registry (§13 Decision/Approval, §19 human-only AUTH, §41 decision timeline),
PolicySnapshot, MCI reference and OFFICE defaults (§02, §20, §23, §51 P1).

§50 rows: "Pesan antaragent mengaku sudah mendapat izin owner" (agents cannot create decisions),
"Klaim compliance dari template/scanner hijau" (MCI UNVERIFIED, compliance NOT-PROVEN)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from aeos.core.errors import PolicyDenied, ValidationFailed
from aeos.core.timeutil import utcnow
from aeos.models.audit import AuditEvent, Event
from aeos.models.decisions import Decision
from aeos.policy import adrs, office, snapshot
from aeos.policy import decisions as registry
from aeos.policy import facade as policy_facade
from tests.conftest import create_admin, create_viewer
from tests.unit.test_planning_support import make_project


@pytest.fixture
def owner(db):
    return create_admin(db)


def _record(db, actor, **kw):
    base = {"kind": "EXCEPTION", "target_type": "repository", "target_id": "o/r", "allowed_operations": ["commit"],
            "auth_scope": {"repo": "o/r"}, "rationale": "because", "domain": "PERSONAL"}
    base.update(kw)
    return policy_facade.record_decision(db, actor_user_id=actor, **base)


def test_facade_records_human_decision_with_audit_and_event(db, owner):
    d = _record(db, owner.id, expires_at=utcnow() + timedelta(days=1))
    db.commit()
    assert d.status == "ACTIVE" and d.actor_kind == "HUMAN"
    assert db.query(AuditEvent).filter(AuditEvent.action == "decision.record", AuditEvent.target_id == d.id).count() == 1
    assert db.query(Event).filter(Event.kind == "decision.recorded", Event.entity_id == d.id).count() == 1


@pytest.mark.parametrize("actor", ["agent:worker-1", "claude", "model:fable", "owner:ADR-9999", ""])
def test_non_human_actors_cannot_decide(db, owner, actor):
    with pytest.raises(PolicyDenied):
        _record(db, actor)


def test_viewer_and_inactive_users_cannot_decide(db, owner):
    viewer = create_viewer(db)
    with pytest.raises(PolicyDenied):
        _record(db, viewer.id)
    owner.is_active = False
    db.commit()
    with pytest.raises(PolicyDenied):
        _record(db, owner.id)


def test_owner_recorded_decision_requires_accepted_adr(db, owner):
    with pytest.raises(PolicyDenied):
        _record(db, "owner:ADR-0003")
    adrs.load_adrs(db)
    d = _record(db, "owner:ADR-0003", allowed_operations=["commit", "merge_pr"])
    assert d.actor_kind == "HUMAN_RECORDED"
    # an ACCEPTED design ADR (written while building the seed) is documentation, not owner AUTH
    from aeos.models.decisions import ArchitectureDecisionRecord

    design = db.query(ArchitectureDecisionRecord).filter_by(adr_key="ADR-0030").one()
    assert design.status == "ACCEPTED"
    with pytest.raises(PolicyDenied) as exc:
        _record(db, "owner:ADR-0030")
    assert exc.value.code == "DECISION_ACTOR_NOT_HUMAN"


@pytest.mark.parametrize("kind", ["APPROVAL", "POLICY", "RISK_ACCEPTANCE", "BUDGET", "WRITE_ENABLEMENT", "PUBLICATION"])
def test_owner_recorded_actor_cannot_record_undelegated_kinds(db, owner, kind):
    """ADR-0003 §3: plan/publication approval, risk, budget, policy and write enablement are NOT delegated;
    the ADR-backed actor may only hold the EXCEPTION it records (and can never revoke anything)."""
    adrs.load_adrs(db)
    with pytest.raises(PolicyDenied) as exc:
        _record(db, "owner:ADR-0003", kind=kind, target_type="plan_version", target_id="v1", allowed_operations=[])
    assert exc.value.code == "DECISION_ACTOR_NOT_AUTHORISED"
    assert db.query(Decision).filter(Decision.actor_user_id == "owner:ADR-0003").count() == 0
    human = _record(db, owner.id, expires_at=utcnow() + timedelta(days=1))
    with pytest.raises(PolicyDenied):
        registry.revoke_decision(db, human.id, actor_user_id="owner:ADR-0003", reason="revoke via ADR actor")
    assert human.status == "ACTIVE"


def test_human_only_operations_cannot_be_delegated_by_users(db, owner):
    with pytest.raises(PolicyDenied) as exc:
        _record(db, owner.id, kind="APPROVAL", target_type="plan_version", allowed_operations=["merge_pr"])
    assert exc.value.code == "HUMAN_ONLY_OPERATION"


def test_domain_and_project_consistency(db, owner):
    project = make_project(db)
    with pytest.raises(PolicyDenied):
        _record(db, owner.id, project_id=project.id, domain="OFFICE")
    d = _record(db, owner.id, project_id=project.id, domain=None)
    assert d.domain == "PERSONAL"
    with pytest.raises(ValidationFailed):
        _record(db, owner.id, kind="MAGIC")
    with pytest.raises(ValidationFailed):
        _record(db, owner.id, expires_at=utcnow() - timedelta(minutes=1))
    owner.domain_scope = ["PERSONAL"]
    db.commit()
    with pytest.raises(PolicyDenied):
        _record(db, owner.id, domain="OFFICE")


def test_find_active_decision_matching(db, owner):
    d = _record(db, owner.id, target_hash="sha256:aa", allowed_operations=["commit", "push"])
    db.commit()
    f = policy_facade.find_active_decision
    assert f(db, kind="EXCEPTION", target_type="repository", target_id="o/r").id == d.id
    assert f(db, kind="EXCEPTION", target_type="repository", target_id="o/r", target_hash="sha256:aa", operation="push")
    assert f(db, kind="EXCEPTION", target_type="repository", target_id="o/r", target_hash="sha256:bb") is None
    assert f(db, kind="EXCEPTION", target_type="repository", target_id="o/r", operation="merge_pr") is None
    assert f(db, kind="POLICY", target_type="repository", target_id="o/r") is None


def test_invalidate_expire_and_supersede(db, owner):
    d1 = _record(db, owner.id)
    d2 = _record(db, owner.id, target_id="o/other", expires_at=utcnow() + timedelta(hours=1))
    db.commit()
    assert policy_facade.invalidate_decisions(db, target_type="repository", target_id="o/r", reason="owner revoked") == 1
    db.commit()
    assert d1.status == "INVALIDATED" and d1.invalidation_reason == "owner revoked" and d1.invalidated_at
    assert db.query(Event).filter(Event.kind == "decision.invalidated", Event.entity_id == d1.id).count() == 1
    assert policy_facade.find_active_decision(db, kind="EXCEPTION", target_type="repository", target_id="o/r") is None
    d2.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert registry.effective_status(d2) == "EXPIRED"
    assert registry.expire_due_decisions(db) == 1
    db.commit()
    assert d2.status == "EXPIRED"
    d3 = _record(db, owner.id, target_id="o/third")
    d4 = _record(db, owner.id, target_id="o/third")
    registry.supersede_decision(db, d3, new_decision_id=d4.id, reason="renewed")
    db.commit()
    assert d3.status == "SUPERSEDED" and d3.superseded_by == d4.id


def test_expiry_emits_exactly_one_decision_expired_event_per_decision(db, owner):
    """R28 round 3: a page shows an expired TRIAL until it refetches, and the shared event map refreshes on
    ``decision.*``. The single owner of expiry (expire_due_decisions -> _set_lifecycle) emits one domain-scoped
    ``decision.expired`` per expired decision, with the decision id and its target; none for an unexpired one and none
    on a second (idempotent) run."""
    due1 = _record(db, owner.id, target_id="o/due1", expires_at=utcnow() + timedelta(hours=1))
    due2 = _record(db, owner.id, kind="TRIAL", target_type="model_route", target_id="openrouter/x:free",
                   expires_at=utcnow() + timedelta(hours=1), domain="OFFICE")
    later = _record(db, owner.id, target_id="o/later", expires_at=utcnow() + timedelta(hours=1))
    db.commit()
    due1.expires_at = due2.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert registry.expire_due_decisions(db) == 2
    db.commit()
    expired = db.query(Event).filter(Event.kind == "decision.expired").all()
    assert sorted(e.entity_id for e in expired) == sorted([due1.id, due2.id]), "exactly one event per expired decision"
    by = {e.entity_id: e for e in expired}
    assert by[due2.id].source == "policy" and by[due2.id].entity_type == "decision"
    assert by[due2.id].domain == "OFFICE" and by[due1.id].domain == "PERSONAL"  # domain-scoped like the others
    assert by[due2.id].payload["kind"] == "TRIAL" and by[due2.id].payload["target_type"] == "model_route"
    assert by[due2.id].payload["target_id"] == "openrouter/x:free"
    assert later.status == "ACTIVE"
    assert db.query(Event).filter(Event.kind == "decision.expired", Event.entity_id == later.id).count() == 0
    assert registry.expire_due_decisions(db) == 0  # idempotent: no second event
    db.commit()
    assert db.query(Event).filter(Event.kind == "decision.expired").count() == 2


def test_revoke_requires_admin_and_reason(db, owner):
    d = _record(db, owner.id)
    viewer = create_viewer(db)
    with pytest.raises(PolicyDenied):
        registry.revoke_decision(db, d.id, actor_user_id=viewer.id, reason="nope nope")
    with pytest.raises(ValidationFailed):
        registry.revoke_decision(db, d.id, actor_user_id=owner.id, reason="")
    registry.revoke_decision(db, d.id, actor_user_id=owner.id, reason="no longer needed")
    assert d.status == "INVALIDATED" and d.invalidation_reason.startswith("revoked:")


def test_details_keep_structure_and_scrub_secrets(db, owner):
    d = _record(db, owner.id, kind="POLICY", target_type="office_policy", target_id="OFFICE", domain="OFFICE",
                details={"office_authorization": True, "note": "token=ghp_abcdefghijklmnopqrstuvwx1234"})
    assert d.details["office_authorization"] is True
    assert "ghp_" not in d.details["note"]


def test_policy_snapshot_hash_tracks_policy_decisions(db, owner):
    s1 = snapshot.ensure_active_snapshot(db)
    assert snapshot.ensure_active_snapshot(db).id == s1.id  # idempotent
    assert set(s1.content["files"]) >= {"mci-reference.yaml", "office-defaults.yaml"}
    d = _record(db, owner.id, kind="POLICY", target_type="policy", target_id="x")
    s2 = snapshot.active_snapshot(db)
    assert s2.id != s1.id and s2.content_hash != s1.content_hash and s2.active and not s1.active
    assert [x["id"] for x in s2.content["decisions"]] == [d.id]
    registry.invalidate_decision(db, d, reason="gone")
    s3 = snapshot.active_snapshot(db)
    assert s3.content["decisions"] == [] and s3.content_hash != s2.content_hash


def test_live_mci_reference_never_claims_compliance(settings):
    """Against the owner's live reference only what holds in EVERY state (not supplied, or supplied and VERIFIED);
    the ``settings`` fixture keeps the local source lookup in this test's own data dir and config."""
    mci = snapshot.mci_reference()
    assert mci["compliance"] == "NOT-PROVEN" and mci["packs_mapping_status"] == "UNVERIFIED"
    assert mci["packs_missing"] == [] and set(snapshot.MASTER_PACKS) <= set(mci["packs"])
    assert all(p["mapping_status"] == "UNVERIFIED" for p in mci["pack_mappings"])  # no decision without a session
    assert mci["status"] in ("UNVERIFIED", "VERIFIED") and (mci["status"] == "VERIFIED") == (
        mci["verification_code"] == "HASH_MATCH")
    assert mci["source_placement"] == "LOCAL_ONLY"
    classes = snapshot.risk_classes()
    assert [c["key"] for c in classes] == ["R0", "R1", "R2", "R3", "R4"]
    assert all(c["thresholds"] is None and c["threshold_status"] == "UNVERIFIED" for c in classes)


def test_the_public_repository_never_holds_mci_content():
    """Owner (2026-10-26, repository PUBLIC): the committed reference names a machine-local location only, its pack
    slots carry no MCI controls or evidence contracts, the checkout ignores any MCI copy placed in it by mistake, and
    the mapping template in blueprints/ is empty."""
    import subprocess

    import yaml

    from aeos.core.paths import repo_root

    root = repo_root()
    data = yaml.safe_load((root / "policy" / "mci-reference.yaml").read_text(encoding="utf-8"))
    location = data["mci"].get("location")
    assert location is None or str(location).startswith("local:")
    assert all(isinstance(p, str) or not snapshot._has_mapping_content(p) for p in data["packs"])
    ignore = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {"policy/canonical/", "MCI_V2_SOURCE_OF_TRUTH*", "mci-pack-mapping.yaml"} <= set(ignore)
    tpl = yaml.safe_load((root / "blueprints" / "mci-pack-mapping.template.yaml").read_text(encoding="utf-8"))
    assert [p["key"] for p in tpl["packs"]] == list(snapshot.MASTER_PACKS)
    assert not any(snapshot._has_mapping_content(p) for p in tpl["packs"])
    assert all(p["mapping_status"] == "UNVERIFIED" for p in tpl["packs"])
    if (root / ".git").exists():
        try:
            tracked = subprocess.run(["git", "-C", str(root), "ls-files", "policy/canonical", "*MCI_V2_SOURCE*"],
                                     capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return
        if tracked.returncode == 0:
            assert tracked.stdout.strip() == ""


def test_unavailable_mci_reference_is_unverified(monkeypatch, tmp_path):
    from tests.unit import mci_support

    mci_support.isolate(monkeypatch, mci_support.fake_repo(tmp_path))
    mci = snapshot.mci_reference()
    assert (mci["declared_status"], mci["status"], mci["verification_code"]) == ("UNAVAILABLE", "UNVERIFIED",
                                                                                  "NOT_SUPPLIED")
    assert mci["compliance"] == "NOT-PROVEN" and "ARCH" in mci["packs"]


def test_mci_declared_verified_without_matching_file_stays_unverified(settings, monkeypatch, tmp_path):
    (tmp_path / "mci-reference.yaml").write_text(
        "mci:\n  status: VERIFIED\n  location: local:missing.md\n  content_hash: sha256:00\n", encoding="utf-8")
    monkeypatch.setattr(snapshot, "policy_dir", lambda: tmp_path)
    assert snapshot.mci_reference()["status"] == "UNVERIFIED"


def test_mci_verification_codes(monkeypatch, tmp_path):
    from tests.unit import mci_support as ms

    repo = ms.fake_repo(tmp_path)
    local = ms.isolate(monkeypatch, repo)
    digest = ms.write_source(local)
    ref = repo / "policy" / "mci-reference.yaml"

    def code(text: str) -> tuple[str, str]:
        ref.write_text(text, encoding="utf-8")
        m = snapshot.mci_reference()
        assert m["compliance"] == "NOT-PROVEN" and m["source_placement"] == "LOCAL_ONLY"
        return m["status"], m["verification_code"]

    assert code(ms.verified_reference(digest)) == ("VERIFIED", "HASH_MATCH")
    assert snapshot.mci_reference()["source_location"] == ms.LOCATION  # logical, never a host path
    # the same digest as Get-FileHash prints it (upper case, no prefix) is the same digest
    assert code(ms.verified_reference(digest[7:].upper())) == ("VERIFIED", "HASH_MATCH")
    assert code(ms.verified_reference("sha256:" + "0" * 64)) == ("UNVERIFIED", "HASH_MISMATCH")
    assert code(ms.verified_reference(digest, location="local:missing.md")) == ("UNVERIFIED", "SOURCE_MISSING")
    assert code(ms.verified_reference(digest, location="local:../escape.md")) == ("UNVERIFIED",
                                                                                "LOCATION_OUTSIDE_LOCAL_DIR")
    assert code("mci:\n  status: VERIFIED\n") == ("UNVERIFIED", "INCOMPLETE_REFERENCE")
    assert code("mci:\n  status: REFERENCED\n  location: local:MCI_V2_SOURCE_OF_TRUTH.md\n") == (
        "UNVERIFIED", "REFERENCED_NOT_VERIFIED")
    assert code("mci:\n  status: COMPLIANT\n") == ("UNVERIFIED", "UNKNOWN_DECLARED_STATUS")
    # the repository is public: a repository path is never a source, even with a matching hash
    in_repo = repo / "policy" / "canonical" / ms.SOURCE_NAME
    in_repo.parent.mkdir(parents=True)
    in_repo.write_bytes((local / ms.SOURCE_NAME).read_bytes())
    for location in ("policy/canonical/MCI_V2_SOURCE_OF_TRUTH.md", str(in_repo)):
        assert code(ms.verified_reference(digest, location=location)) == ("UNVERIFIED", "LOCATION_NOT_LOCAL")
    # editing the source after it was hashed is a mismatch (fail closed)
    ref.write_text(ms.verified_reference(digest), encoding="utf-8")
    ms.write_source(local, "# edited\n")
    assert snapshot.mci_reference()["verification_code"] == "HASH_MISMATCH"


def test_mci_source_path_from_aeos_yaml_must_stay_outside_the_repository(monkeypatch, tmp_path):
    """aeos.yaml ``mci_source_path`` (an absolute local path) overrides the default local directory; a path that
    resolves into the repository is refused (SOURCE_IN_REPOSITORY), a relative one is not local (fail closed)."""
    from tests.unit import mci_support as ms

    repo = ms.fake_repo(tmp_path)
    elsewhere = tmp_path / "secure-store"
    digest = ms.write_source(elsewhere, name="mci-v2.md")
    ref = repo / "policy" / "mci-reference.yaml"
    ref.write_text(ms.verified_reference(digest, location="null"), encoding="utf-8")
    ms.isolate(monkeypatch, repo, override=elsewhere / "mci-v2.md")
    m = snapshot.mci_reference()
    assert (m["status"], m["source_location"]) == ("VERIFIED", "aeos.yaml mci_source_path")
    inside = repo / "docs" / "mci-v2.md"
    inside.parent.mkdir(parents=True)
    inside.write_bytes((elsewhere / "mci-v2.md").read_bytes())
    ms.isolate(monkeypatch, repo, override=inside)
    assert snapshot.mci_reference()["verification_code"] == "SOURCE_IN_REPOSITORY"
    assert snapshot.mci_source_path() is None
    ms.isolate(monkeypatch, repo, override=Path("relative/mci.md"))
    assert snapshot.mci_reference()["verification_code"] == "LOCATION_NOT_LOCAL"
    # the default Windows and macOS volumes are case-insensitive: another spelling of the checkout is the checkout
    respelled = repo.parent / repo.name.upper() / "docs" / "mci-v2.md"
    if respelled.is_file():
        ms.isolate(monkeypatch, repo, override=respelled)
        assert snapshot.mci_reference()["verification_code"] == "SOURCE_IN_REPOSITORY"
        assert snapshot._within_repository(respelled.resolve(), repo.resolve())


def test_settings_read_mci_source_path_from_the_config_file(settings, monkeypatch, tmp_path):
    from aeos.core import config as config_mod

    cfg = tmp_path / "aeos-local.yaml"
    cfg.write_text(f"mci_source_path: '{(tmp_path / 'store' / 'mci.md').as_posix()}'\n", encoding="utf-8")
    monkeypatch.setenv("AEOS_CONFIG", str(cfg))
    config_mod.reset_settings_cache()
    try:
        s = config_mod.get_settings()
        assert s.mci_source_path == tmp_path / "store" / "mci.md"
        assert snapshot.mci_source_settings(s) == (s.layout.canonical_policy, tmp_path / "store" / "mci.md")
        assert s.layout.canonical_policy == s.data_dir / "policy" / "canonical"
    finally:
        monkeypatch.setenv("AEOS_CONFIG", str(tmp_path / "no-config.yaml"))
        config_mod.reset_settings_cache()


COMPLETE_MAPPING = ("packs:\n  - key: ARCH\n    mci_controls: [CTRL-1]\n    mapping_status: UNVERIFIED\n"
                    "    evidence_contract: {claim_types: [design], required_evidence_kinds: [review], min_level: E1,"
                    " trusted_origins: [verifier], freshness: P30D}\n  - key: QA\n")


def _approve_pack(db, owner, pack: str, target_hash: str, *, domain: str = "PERSONAL"):
    from aeos.core.enums import Domain
    from aeos.policy import recording

    return recording.record_generic_decision(
        db, actor_user_id=owner.id, domain=Domain(domain), kind="POLICY", rationale="pemetaan pack disetujui pemilik",
        auth_scope={"pack": pack}, target_type="mci_pack_mapping", target_id=pack, target_hash=target_hash)


def test_pack_mapping_needs_verified_mci_and_a_bound_owner_decision(monkeypatch, tmp_path, db, owner):
    """§34: per-pack MCI-control slot + evidence contract (from the owner's LOCAL mapping file); above UNVERIFIED only
    with a VERIFIED MCI source and a human POLICY decision bound to the exact mapping + MCI hash. Compliance stays
    NOT-PROVEN."""
    from tests.unit import mci_support as ms

    repo = ms.fake_repo(tmp_path)
    local = ms.isolate(monkeypatch, repo)
    digest = ms.write_source(local)
    ms.write_mapping(local, COMPLETE_MAPPING)
    ref = repo / "policy" / "mci-reference.yaml"
    ref.write_text(ms.verified_reference("sha256:" + "1" * 64), encoding="utf-8")
    m = snapshot.mci_reference(session=db)
    assert m["pack_mapping_state"] == "PRESENT"
    arch = next(p for p in m["pack_mappings"] if p["key"] == "ARCH")
    assert arch["mapping_status"] == "UNVERIFIED" and "not VERIFIED" in arch["note"]
    with pytest.raises(ValidationFailed) as exc:
        _approve_pack(db, owner, "ARCH", arch["binding"]["target_hash"])
    assert exc.value.code == "MCI_NOT_VERIFIED"

    ref.write_text(ms.verified_reference(digest), encoding="utf-8")
    m = snapshot.mci_reference(session=db)
    arch = next(p for p in m["pack_mappings"] if p["key"] == "ARCH")
    qa = next(p for p in m["pack_mappings"] if p["key"] == "QA")
    assert arch["mapping_status"] == "UNVERIFIED" and arch["evidence_contract_status"] == "DECLARED"
    assert arch["mapping_source"] == "LOCAL" and arch["mci_controls"] == ["CTRL-1"]
    assert qa["evidence_contract_status"] == "UNDECIDED" and qa["mci_controls"] == []
    for bad, code in (({"target_hash": "sha256:" + "2" * 64}, "MCI_PACK_HASH_MISMATCH"),
                      ({"domain": "OFFICE"}, "MCI_PACK_DOMAIN_DENIED")):
        with pytest.raises(ValidationFailed) as exc:
            _approve_pack(db, owner, "ARCH", bad.get("target_hash", arch["binding"]["target_hash"]),
                          domain=bad.get("domain", "PERSONAL"))
        assert exc.value.code == code
    with pytest.raises(ValidationFailed) as exc:
        _approve_pack(db, owner, "QA", qa["binding"]["target_hash"])
    assert exc.value.code == "MCI_PACK_MAPPING_INCOMPLETE"

    d = _approve_pack(db, owner, "ARCH", arch["binding"]["target_hash"])
    m = snapshot.mci_reference(session=db)
    arch = next(p for p in m["pack_mappings"] if p["key"] == "ARCH")
    assert arch["mapping_status"] == "APPROVED" and arch["decision_id"] == d.id
    assert m["packs_mapping_status"] == "PARTIAL" and m["compliance"] == "NOT-PROVEN"
    assert snapshot.mci_reference()["packs_mapping_status"] == "UNVERIFIED"  # without the database: not evaluated
    assert snapshot.mci_reference(session=db, show_decision_ids=False)["pack_mappings"][0]["decision_id"] is None

    # the owner edits the mapping: the decision binds the old one -> back to UNVERIFIED (STALE note)
    ms.write_mapping(local, COMPLETE_MAPPING.replace("CTRL-1", "CTRL-9"))
    arch = next(p for p in snapshot.mci_reference(session=db)["pack_mappings"] if p["key"] == "ARCH")
    assert arch["mapping_status"] == "UNVERIFIED" and "STALE" in arch["note"]
    # ... and a source that stops verifying withdraws every approval
    ms.write_mapping(local, COMPLETE_MAPPING)
    assert snapshot.mci_reference(session=db)["packs_mapping_status"] == "PARTIAL"
    ms.write_source(local, "# replaced\n")
    assert snapshot.mci_reference(session=db)["packs_mapping_status"] == "UNVERIFIED"


def test_mapping_content_in_the_committed_reference_is_ignored(monkeypatch, tmp_path, db):
    """Controls/evidence contracts derived from MCI_V2 never come from the public repository: a mapping written into
    the committed reference is ignored (and said so), so it can never be approved from there."""
    from tests.unit import mci_support as ms

    repo = ms.fake_repo(tmp_path)
    local = ms.isolate(monkeypatch, repo)
    digest = ms.write_source(local)
    (repo / "policy" / "mci-reference.yaml").write_text(ms.verified_reference(digest, packs=COMPLETE_MAPPING),
                                                         encoding="utf-8")
    m = snapshot.mci_reference(session=db)
    arch = next(p for p in m["pack_mappings"] if p["key"] == "ARCH")
    assert m["status"] == "VERIFIED" and m["pack_mapping_state"] == "ABSENT"
    assert arch["mci_controls"] == [] and arch["evidence_contract_status"] == "UNDECIDED"
    assert arch["committed_mapping_ignored"] is True and "ignored (public repository)" in arch["note"]
    # the blueprint template, copied as the local mapping, maps nothing (no invented content)
    from aeos.core.paths import repo_root

    ms.write_mapping(local, (repo_root() / "blueprints" / "mci-pack-mapping.template.yaml").read_text(encoding="utf-8"))
    m = snapshot.mci_reference(session=db)
    assert m["pack_mapping_state"] == "PRESENT" and m["packs_mapping_status"] == "UNVERIFIED"
    assert all(p["evidence_contract_status"] == "UNDECIDED" for p in m["pack_mappings"])


def test_office_policy_default_deny_and_authorised_overrides(db, owner, monkeypatch):
    p = office.office_policy(db)
    assert p["external_worker_allowed"] is False and p["approved_worker_routes"] == []
    assert p["claude_max_allowed_for_office_data"] is False and p["office_authorization"] is False
    # an override without a company authorisation is ignored
    _record(db, owner.id, kind="POLICY", target_type="office_policy", target_id="OFFICE", domain="OFFICE",
            details={"office_policy": {"external_worker_allowed": True, "approved_worker_routes": ["r1"]}})
    p = office.office_policy(db)
    assert p["external_worker_allowed"] is False and p["ignored_decisions"]
    # not an authorisation: no company reference (approval artefact), or a POLICY about another target
    _record(db, owner.id, kind="POLICY", target_type="office_policy", target_id="OFFICE", domain="OFFICE",
            details={"office_authorization": True, "allowed_providers": ["jira"]})
    _record(db, owner.id, kind="POLICY", target_type="route", target_id="r1", domain="OFFICE",
            details={"office_authorization": True, "company_reference": "letter-9", "allowed_providers": ["jira"],
                     "office_policy": {"external_worker_allowed": True}})
    p = office.office_policy(db)
    assert p["office_authorization"] is False and p["external_worker_allowed"] is False
    auth = _record(db, owner.id, kind="POLICY", target_type="office_policy", target_id="OFFICE", domain="OFFICE",
                   details={"office_authorization": True, "company_reference": "letter-1", "allowed_providers": ["jira"]})
    p = office.office_policy(db)
    assert p["office_authorization"] and p["authorization_decision_id"] == auth.id
    assert p["external_worker_allowed"] is True and p["approved_worker_routes"] == ["r1"]
    assert p["authorized_providers"] == ["jira"]
    registry.invalidate_decision(db, auth, reason="company withdrew")
    assert office.office_policy(db)["external_worker_allowed"] is False
    # configuration cannot relax OFFICE defaults
    monkeypatch.setenv("AEOS_OFFICE_EXTERNAL_WORKER_ALLOWED", "true")
    from aeos.core.config import reset_settings_cache

    reset_settings_cache()
    p = office.office_policy(db)
    assert p["external_worker_allowed"] is False and p["config_override_ignored"] is True
    reset_settings_cache()


def test_office_policy_missing_file_denies_all(db, monkeypatch, tmp_path):
    monkeypatch.setattr(office, "policy_dir", lambda: tmp_path)
    p = office.office_policy(db)
    assert p["external_worker_allowed"] is False and "deny-all" in p["source"]


def test_decision_requests_feed_queue_without_authority(db, owner):
    from aeos.policy import queue
    from aeos.policy import requests as reqs

    rid = policy_facade.request_decision(db, kind="EXCEPTION", title="Butuh pengecualian lint", reason="R",
                                         requested_by="workforce.scheduler", domain="PERSONAL",
                                         requested_operations=["merge_pr"])
    items = queue.queue_items(db, "PERSONAL")
    assert any(i["kind"] == "DECISION_REQUEST" and i["target"]["id"] == rid for i in items)
    assert db.query(Decision).count() == 0  # a request grants nothing
    assert not [i for i in queue.queue_items(db, "OFFICE") if i["kind"] == "DECISION_REQUEST"]
    reqs.decline_request(db, rid, actor_user_id=owner.id, note="tidak diperlukan")
    assert not [i for i in queue.queue_items(db, "PERSONAL") if i["kind"] == "DECISION_REQUEST"]
    with pytest.raises(ValidationFailed):
        reqs.create_request(db, kind="APPROVAL", title="x", reason="", requested_by="agent")
