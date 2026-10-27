"""Per-platform approved digests and per-OS signer pins of the reviewed InstallManifest (master §07 "installer
dipin ke versi/digest dan diverifikasi", §12 official binary, §25 tool admission; audit W6-020, W6-042).

The owner uses Windows AND macOS: the executable (and the publisher identity the OS reports) differs per
platform, so one scalar pin refused the install / raised drift on the other platform. A platform without an
entry is NOT_RECORDED there - never a mismatch, never another platform's value."""

from __future__ import annotations

import copy
import hashlib

import pytest
import respx
from typer.testing import CliRunner

from aeos.cli import app
from aeos.inventory import drift as drift_mod
from aeos.inventory import pins, views
from aeos.inventory.observations import record_observation
from aeos.inventory.seed import run as seed_run
from aeos.models.inventory import ComponentReference
from aeos.secrets import installer
from tests.unit.test_inventory_c4 import _manifest_data
from tests.unit.test_secrets_cli import _mock_release

WIN = "sha256:" + "a" * 64
MAC = "sha256:" + "b" * 64
GOOD = "sha256:" + hashlib.sha256(b"fake-bao-binary").hexdigest()  # the binary _mock_release ships
runner = CliRunner()


def _host(monkeypatch, os_name: str, arch: str) -> None:
    monkeypatch.setattr(pins, "host_os", lambda: os_name)
    monkeypatch.setattr(pins, "host_arch", lambda: arch)


# ------------------------------------------------------------------------------------------- pure selection
def test_select_digest_keeps_the_scalar_form_and_selects_the_platform_entry():
    assert pins.select_digest(WIN.upper().replace("SHA256", "sha256"), "darwin_arm64")["digest"] == WIN
    by_platform = {"windows_amd64": WIN, "darwin_arm64": MAC}
    assert pins.select_digest(by_platform, "windows_amd64")["digest"] == WIN
    assert pins.select_digest(by_platform, "darwin_arm64")["digest"] == MAC
    other = pins.select_digest(by_platform, "darwin_amd64")
    assert other["status"] == "NOT_RECORDED" and other["digest"] is None, "never another platform's digest"
    assert other["recorded_platforms"] == ["darwin_arm64", "windows_amd64"]
    assert pins.select_digest(None, "windows_amd64")["status"] == "NOT_RECORDED"
    assert pins.select_digest({"windows": WIN}, "windows_amd64")["status"] == "INVALID"
    assert pins.select_digest({"windows_amd64": "abc"}, "windows_amd64")["status"] == "INVALID"


def test_select_signer_per_os_mapping_or_one_identity_for_all():
    assert pins.select_signer("Anthropic, PBC", "darwin") == "Anthropic, PBC"
    m = {"windows": "Anthropic, PBC", "darwin": "TEAMID1234"}
    assert pins.select_signer(m, "windows") == "Anthropic, PBC"
    assert pins.select_signer(m, "darwin") == "TEAMID1234"
    assert pins.select_signer({"windows": "Anthropic, PBC"}, "darwin") is None, "not pinned on macOS"
    assert pins.select_signer(None, "windows") is None and pins.select_signer("  ", "windows") is None


# ------------------------------------------------------------------------------------------- OpenBao installer
def _openbao(monkeypatch, digest) -> None:
    monkeypatch.setattr(installer, "_manifest_entry",
                        lambda root=None: {"component_id": "openbao", "approved_pin": "2.7.1", "approved_digest": digest})


@pytest.mark.parametrize(("target", "expected"), [(("windows", "amd64"), WIN), (("darwin", "arm64"), MAC)])
def test_installer_selects_this_platforms_digest(monkeypatch, target, expected):
    _openbao(monkeypatch, {"windows_amd64": WIN, "darwin_arm64": MAC})
    monkeypatch.setattr(installer, "platform_target", lambda: target)
    assert installer.manifest_digest() == expected
    req = installer.digest_requirement("2.7.1")
    assert req["digest_status"] == "REQUIRED" and req["approved_digest"] == expected
    assert req["digest_platform"] == "_".join(target)


def test_a_platform_without_an_entry_is_not_recorded_never_refused(monkeypatch):
    _openbao(monkeypatch, {"windows_amd64": WIN})
    monkeypatch.setattr(installer, "platform_target", lambda: ("darwin", "arm64"))
    assert installer.manifest_digest() is None
    req = installer.digest_requirement("2.7.1")
    assert req["digest_status"] == "NOT_RECORDED" and req["approved_digest"] is None
    assert "darwin_arm64" in req["digest_note"]
    _openbao(monkeypatch, {"windows_amd64": "sha256:nothex"})
    monkeypatch.setattr(installer, "platform_target", lambda: ("windows", "amd64"))
    with pytest.raises(installer.InstallError):  # a malformed reviewed digest fails closed
        installer.manifest_digest()


def test_install_on_both_platforms_from_one_reviewed_manifest(settings, monkeypatch):
    """Windows has the owner's reviewed digest (the real binary): it must match. macOS has no entry yet: the install
    is not refused and says NOT_RECORDED. A wrong macOS entry is refused before anything is written."""
    bin_path = settings.data_dir / "bin" / installer.binary_name()
    for target, digest, code, needle in (
            (("windows", "amd64"), {"windows_amd64": GOOD}, 0, "matched the install-manifest approved_digest"),
            (("darwin", "arm64"), {"windows_amd64": GOOD}, 0, "NOT_RECORDED"),
            (("darwin", "arm64"), {"windows_amd64": GOOD, "darwin_arm64": MAC}, 1, "differs from the install-manifest")):
        if bin_path.exists():
            bin_path.unlink()
        _openbao(monkeypatch, digest)
        monkeypatch.setattr(installer, "platform_target", lambda t=target: t)
        with respx.mock(assert_all_called=False) as m:
            _mock_release(m)
            r = runner.invoke(app, ["vault", "install", "--yes"])
        assert r.exit_code == code and needle in r.output, (target, r.output)
        assert bin_path.exists() is (code == 0)
        if code == 0 and needle == "NOT_RECORDED":
            assert "darwin_arm64" in r.output  # the owner is told which platform entry to record


def test_install_records_signature_authenticity_as_not_verified(settings, monkeypatch):
    """W6-075 (owner/tool part): OpenBao publishes a GPG signature of checksums.txt, but neither gpg nor an
    owner-pinned release-key fingerprint is part of the reviewed toolchain - the observation says so explicitly."""
    from aeos.core.db import session_scope
    from aeos.models.inventory import ComponentObservation

    _openbao(monkeypatch, {"windows_amd64": GOOD})
    monkeypatch.setattr(installer, "platform_target", lambda: ("windows", "amd64"))
    with respx.mock(assert_all_called=False) as m:
        _mock_release(m)
        r = runner.invoke(app, ["vault", "install", "--yes"])
    assert r.exit_code == 0, r.output
    with session_scope() as s:
        obs = [o for o in s.query(ComponentObservation).filter_by(component_id="openbao", dimension="installation")
               if o.status == "INSTALLED"]
        assert obs and obs[-1].detail["signature"]["result"] == "NOT_VERIFIED"
        assert obs[-1].detail["digest_platform"] == "windows_amd64" and obs[-1].detail["digest_status"] == "MATCH"


# ------------------------------------------------------------------------------------------- inventory drift
@pytest.fixture
def seeded(db):
    seed_run(db)
    db.commit()
    return db


def _installed(db, digest: str) -> None:
    comp = db.get(ComponentReference, "openbao")
    comp.approved_pin = "2.7.1"
    db.commit()
    record_observation(db, component_id="openbao", dimension="installation", status="INSTALLED",
                       observer="test", observed_version="2.7.1", observed_identity=digest)
    db.commit()


def test_drift_compares_only_this_platforms_digest(seeded, monkeypatch):
    comp = seeded.get(ComponentReference, "openbao")
    comp.details = {**comp.details, "approved_digest": {"windows_amd64": WIN, "darwin_arm64": MAC}}
    seeded.commit()
    _installed(seeded, MAC)  # the macOS executable, observed on the Mac
    _host(monkeypatch, "darwin", "arm64")
    items = drift_mod.compute_drift(seeded, evaluated={"openbao"})
    assert not [i for i in items if i.key == "digest"], "the Mac's own reviewed digest matches"
    detail = views.component_detail(seeded, "openbao", domain="PERSONAL")["versions"]
    assert detail["digest_status"] == "MATCH" and detail["approved_digest_platform"] == "darwin_arm64"
    _host(monkeypatch, "darwin", "amd64")  # an Intel Mac: nothing recorded -> not a mismatch
    assert not [i for i in drift_mod.compute_drift(seeded, evaluated={"openbao"}) if i.key == "digest"]
    assert views.component_detail(seeded, "openbao", domain="PERSONAL")["versions"]["digest_status"] == \
        "NO_APPROVED_DIGEST"
    _host(monkeypatch, "windows", "amd64")  # the Windows entry differs from what is observed here -> drift
    digest_items = [i for i in drift_mod.compute_drift(seeded, evaluated={"openbao"}) if i.key == "digest"]
    assert len(digest_items) == 1 and digest_items[0].detail["approved_digest"] == WIN
    assert digest_items[0].detail["platform"] == "windows_amd64"


def test_a_malformed_approved_digest_fails_closed_in_drift_and_the_view(seeded, monkeypatch):
    """A reviewed digest that cannot be read is never treated as 'nothing to compare' (it used to be compared, and
    differed, as a raw string): drift is raised and the component view says INVALID_APPROVED_DIGEST."""
    comp = seeded.get(ComponentReference, "openbao")
    comp.details = {**comp.details, "approved_digest": {"windows_amd64": "sha256:nothex"}}
    seeded.commit()
    _installed(seeded, WIN)
    _host(monkeypatch, "windows", "amd64")
    digest_items = [i for i in drift_mod.compute_drift(seeded, evaluated={"openbao"}) if i.key == "digest"]
    assert len(digest_items) == 1 and digest_items[0].detail["approved_digest_status"] == "INVALID"
    detail = views.component_detail(seeded, "openbao", domain="PERSONAL")["versions"]
    assert detail["digest_status"] == "INVALID_APPROVED_DIGEST" and detail["approved_digest_reason"]


# ------------------------------------------------------------------------------------------- manifest schema
def test_manifest_accepts_per_platform_digests_and_per_os_signers():
    from aeos.inventory.manifest import validate_manifest

    data, master = _manifest_data()
    comp = next(c for c in data["components"] if c["component_id"] == "openbao")
    comp["approved_pin"] = "2.7.1"
    comp["approved_digest"] = {"windows_amd64": WIN, "darwin_arm64": MAC}
    claude = next(c for c in data["components"] if c["component_id"] == "claude-code")
    claude["signature"] = {**claude["signature"], "approved_signer": {"windows": "Anthropic, PBC", "darwin": "TEAMID"}}
    errors, _ = validate_manifest(data, master_text=master)
    assert not errors, errors

    bad = copy.deepcopy(data)
    ob = next(c for c in bad["components"] if c["component_id"] == "openbao")
    ob["approved_digest"] = {"windows": WIN, "darwin_arm64": "sha256:short"}
    cc = next(c for c in bad["components"] if c["component_id"] == "claude-code")
    cc["signature"]["approved_signer"] = {"linux": "x"}
    errors, _ = validate_manifest(bad, master_text=master)
    joined = "\n".join(errors)
    assert "approved_digest" in joined and "approved_signer" in joined, joined

    narrow = copy.deepcopy(data)
    ob = next(c for c in narrow["components"] if c["component_id"] == "openbao")
    ob["compatibility"] = {**ob["compatibility"], "os": ["windows"]}
    errors, _ = validate_manifest(narrow, master_text=master)
    assert any("outside compatibility.os" in e for e in errors), errors


# ------------------------------------------------------------------------------------------- OWNER_ACTIONS 1.12
# Owner decision of 2026-10-27, applied as D13: pin what protects without blocking a routine update.
# Version drift that BLOCKS a capability: drift from the opencode pin makes worker.opencode FAILED
# (OPENCODE_PIN_DRIFT), a critical check of CAN_RUN_WORKER. Other version/digest drift is only an inventory finding.
VERSION_DRIFT_BLOCKS = {"opencode": ("worker.opencode", "CAN_RUN_WORKER")}
SELF_UPDATING = ("claude-code", "opencode")  # update themselves: a version/digest pin drifts on every update
HOST_VERSIONED = ("git", "nodejs-runtime", "python-runtime")  # OS/package-manager builds differ per platform
RECORDED_PLATFORMS = frozenset({"darwin_arm64"})  # windows_amd64 / darwin_amd64: NOT_RECORDED until those machines
OBSERVED_BAO = "sha256:50c3588e64271171cc2147bddeace8ec2b6742f94fd3fe53baa60f6d938cda0c"  # owner's M4, 2026-10-27


def _pin_policy_violations(data: dict) -> list[str]:
    """The 1.12 rule as findings (empty = the manifest follows it)."""
    out: list[str] = []
    for c in data["components"]:
        cid = c["component_id"]
        pinned = bool(c.get("approved_pin") or c.get("approved_digest"))
        if pinned and cid in VERSION_DRIFT_BLOCKS:
            check_id, cap = VERSION_DRIFT_BLOCKS[cid]
            out.append(f"{cid}: version/digest pin, but drift from it fails {check_id} and BLOCKS {cap}")
        elif pinned and cid in SELF_UPDATING:
            out.append(f"{cid}: version/digest pin on a self-updating tool drifts on every routine update")
        elif pinned and cid in HOST_VERSIONED:
            out.append(f"{cid}: approved_pin applies on every platform, but this tool's version differs per platform")
        raw = c.get("approved_digest")
        if isinstance(raw, str):
            out.append(f"{cid}: a scalar approved_digest applies on every platform; record it per <os>_<arch>")
        elif isinstance(raw, dict) and set(raw) - RECORDED_PLATFORMS:
            out.append(f"{cid}: approved_digest records {sorted(set(raw) - RECORDED_PLATFORMS)}, "
                       "which are NOT_RECORDED until those machines")
    claude = next(c for c in data["components"] if c["component_id"] == "claude-code")
    signer = (claude.get("signature") or {}).get("approved_signer")
    if not (isinstance(signer, dict) and signer.get("darwin")):
        out.append("claude-code: no darwin publisher pin (signature.approved_signer.darwin = TeamIdentifier)")
    return out


def test_the_rule_matches_the_capability_gates():
    """The components listed as BLOCKING really are: their check is a critical check of the capability (FAILED there
    -> BLOCKED), and the claude-code publisher pin is applied by claude.binary, which planning requires."""
    from aeos.core.enums import Capability
    from aeos.doctor.capabilities import RULES
    from aeos.doctor.checks import REGISTRY, load_all

    load_all()
    for cid, (check_id, cap) in VERSION_DRIFT_BLOCKS.items():
        assert REGISTRY[check_id].component_id == cid
        assert check_id in RULES[Capability(cap)].critical, (check_id, cap)
    assert "claude.binary" in RULES[Capability.CAN_PLAN_PERSONAL].required


def test_the_reviewed_manifest_follows_the_1_12_pin_rule():
    """OWNER_ACTIONS 1.12, darwin_arm64 (2026-10-27): claude-code gets only the macOS publisher pin (a foreign- or
    un-signed binary is BLOCKED; a version pin would drift on every self-update); opencode gets no version/digest pin
    (drift BLOCKS CAN_RUN_WORKER and it updates itself); openbao gets its version (installed only by `aeos vault
    install`, never self-updating, drift is a finding, not a block) but no digest yet, because a digest pin makes every
    openbao proposal need a candidate digest the trusted acceptance suite does not supply; git, Node.js and Python stay
    unpinned (approved_pin has no per-platform form). Other platforms stay NOT_RECORDED."""
    from aeos.inventory import facade as inventory
    from aeos.inventory.manifest import load_manifest

    data = load_manifest().data
    assert _pin_policy_violations(data) == []
    assert data["reviewed_by"] == "Fadhlillah"
    assert inventory.approved_signer("claude-code", os_name="darwin") == "Q6L2SF6YDW"
    assert inventory.approved_signer("claude-code", os_name="windows") is None
    for cid in ("claude-code", "opencode"):
        assert inventory.approved_pin(cid) is None, cid
        assert inventory.approved_digest(cid, platform="darwin_arm64")["status"] == "NOT_RECORDED", cid
    assert inventory.approved_pin("openbao") == "2.7.1"
    for platform in ("darwin_arm64", "windows_amd64", "darwin_amd64"):
        assert inventory.approved_digest("openbao", platform=platform)["status"] == "NOT_RECORDED", platform


@pytest.mark.parametrize(("cid", "field", "value", "finding"), [
    ("opencode", "approved_pin", "1.19.3", "drift from it fails worker.opencode and BLOCKS CAN_RUN_WORKER"),
    ("claude-code", "approved_pin", "2.1.303", "self-updating tool drifts on every routine update"),
    ("git", "approved_pin", "2.50.1", "this tool's version differs per platform"),
    ("openbao", "approved_digest", {"darwin_arm64": OBSERVED_BAO, "windows_amd64": WIN},
     "['windows_amd64'], which are NOT_RECORDED"),
    ("openbao", "approved_digest", OBSERVED_BAO, "scalar approved_digest applies on every platform"),
    ("claude-code", "signature", {"method": "os_codesign", "result": "UNKNOWN", "approved_signer": None},
     "claude-code: no darwin publisher pin"),
])
def test_the_pin_rule_bites(cid, field, value, finding):
    """Violating fixtures are red for the right reason; the near-miss (the reviewed manifest, which pins openbao's
    version and the claude-code darwin publisher) is green above, and so is a darwin_arm64-only digest below."""
    from aeos.inventory.manifest import load_manifest

    data = copy.deepcopy(load_manifest().data)
    comp = next(c for c in data["components"] if c["component_id"] == cid)
    comp[field] = value
    found = _pin_policy_violations(data)
    assert any(finding in f for f in found), found


def test_a_darwin_arm64_only_digest_is_allowed_by_the_rule():
    """Near-miss: the follow-up digest of openbao for darwin_arm64 alone is not a violation."""
    from aeos.inventory.manifest import load_manifest

    data = copy.deepcopy(load_manifest().data)
    next(c for c in data["components"] if c["component_id"] == "openbao")["approved_digest"] = {
        "darwin_arm64": OBSERVED_BAO}
    assert _pin_policy_violations(data) == []
