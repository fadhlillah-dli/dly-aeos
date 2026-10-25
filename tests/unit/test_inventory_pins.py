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


def test_the_reviewed_manifest_records_no_invented_pins():
    """Owner step (W6-020/W6-042/W6-166): nothing is pinned until the owner records it in a reviewed PR."""
    from aeos.inventory import facade as inventory

    for cid in ("openbao", "claude-code", "opencode"):
        assert inventory.approved_pin(cid) is None, cid
        assert inventory.approved_digest(cid)["status"] == "NOT_RECORDED", cid
    assert inventory.approved_signer("claude-code", os_name="windows") is None
    assert inventory.approved_signer("claude-code", os_name="darwin") is None
