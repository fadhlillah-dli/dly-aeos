"""`aeos secrets` / `aeos vault` CLI (U-13, §07 order 3, §08 remedy 'tidak melakukan auto-install',
§26). respx for GitHub releases and the OpenBao API; in-memory OS keyring; nothing touches the
real machine, network or credential store."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import zipfile

import httpx
import pytest
import respx
from typer.testing import CliRunner

from aeos.cli import app
from aeos.core.db import session_factory
from aeos.models.audit import AuditEvent
from aeos.models.decisions import Decision
from aeos.secrets import installer, server, vault_cli
from aeos.secrets.provider import bootstrap_store, reset_provider_cache
from tests.unit.test_routing_fakes import memory_keyring  # noqa: F401 - fixture

runner = CliRunner()
ADDR = "http://127.0.0.1:8200"
VALUE = "cli-value-" + "w" * 24


@pytest.fixture(autouse=True)
def _reset():
    reset_provider_cache()
    yield
    reset_provider_cache()


def test_secrets_set_stdin_never_echoes_and_audits(settings, secrets_memory):
    r = runner.invoke(app, ["secrets", "set", "personal/github", "--stdin"], input=VALUE + "\r\n")
    assert r.exit_code == 0, r.output
    assert VALUE not in r.output and "value not shown" in r.output
    assert secrets_memory.get("personal/github", purpose="personal:t", actor="t") == VALUE  # newline stripped
    s = session_factory()()
    try:
        ev = s.query(AuditEvent).filter_by(action="secret.put", target_id="personal/github").one()
        assert VALUE not in json.dumps(ev.details)
    finally:
        s.close()


def test_secrets_set_rejects_bad_ref_and_empty(settings, secrets_memory):
    assert runner.invoke(app, ["secrets", "set", "nope/x", "--stdin"], input="v").exit_code == 2
    assert runner.invoke(app, ["secrets", "set", "personal/github", "--stdin"], input="\n").exit_code == 2


def test_secrets_set_fails_closed_without_store(settings, memory_keyring):  # noqa: F811
    with respx.mock(assert_all_called=False) as m:
        m.route(url__startswith=ADDR).mock(side_effect=httpx.ConnectError("down"))
        r = runner.invoke(app, ["secrets", "set", "personal/github", "--stdin"], input=VALUE)
    assert r.exit_code == 1 and "SECRET_STORE_UNAVAILABLE" in r.output and VALUE not in r.output


def test_secrets_status_and_delete(settings, secrets_memory):
    secrets_memory.put("workers/openrouter", VALUE, actor="t")
    r = runner.invoke(app, ["secrets", "status"])
    assert r.exit_code == 0 and "workers/openrouter" in r.output and "PRESENT" in r.output and VALUE not in r.output
    assert "office/atlassian" not in r.output
    r = runner.invoke(app, ["secrets", "delete", "workers/openrouter", "--yes"])
    assert r.exit_code == 0 and not secrets_memory.status("workers/openrouter").exists


def test_interim_keyring_explains_and_records_nothing(settings):
    r = runner.invoke(app, ["secrets", "interim-keyring"])
    assert r.exit_code == 0 and "secret_store_interim_keyring" in r.output and "records NOTHING" in r.output
    s = session_factory()()
    try:
        assert s.query(Decision).count() == 0
    finally:
        s.close()


def _archive(name: str, binary: bytes) -> bytes:
    buf = io.BytesIO()
    member = "bao.exe" if name.endswith(".zip") else "bao"
    if name.endswith(".zip"):
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("LICENSE", "MPL-2.0")
            zf.writestr(member, binary)
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo(member)
            info.size = len(binary)
            tf.addfile(info, io.BytesIO(binary))
    return buf.getvalue()


def _mock_release(m, *, tamper: bool = False) -> tuple[str, bytes]:
    os_name, arch = installer.platform_target()
    name = installer.asset_name("2.7.1", os_name, arch)
    binary = b"fake-bao-binary"
    archive = _archive(name, binary)
    digest = hashlib.sha256(archive).hexdigest()
    base = "https://github.com/openbao/openbao/releases/download/v2.7.1"
    m.get(f"{installer.GITHUB_API}/tags/v2.7.1").mock(return_value=httpx.Response(200, json={
        "tag_name": "v2.7.1", "html_url": "https://github.com/openbao/openbao/releases/tag/v2.7.1",
        "published_at": "2026-10-01T14:03:19Z",
        "assets": [{"name": name, "browser_download_url": f"{base}/{name}"},
                   {"name": "checksums.txt", "browser_download_url": f"{base}/checksums.txt"}]}))
    m.get(f"{base}/checksums.txt").mock(return_value=httpx.Response(
        200, text=f"{'0' * 64 if tamper else digest}  {name}\n{'1' * 64}  other.tar.gz\n"))
    m.get(f"{base}/{name}").mock(return_value=httpx.Response(200, content=archive))
    return name, binary


def test_vault_install_shows_plan_and_needs_confirmation(settings):
    with respx.mock(assert_all_called=False) as m:
        _mock_release(m)
        r = runner.invoke(app, ["vault", "install", "--version", "2.7.1"], input="n\n")
    assert r.exit_code == 1 and "Install plan" in r.output and "Aborted - nothing installed" in r.output
    for word in ("source", "asset", "checksums_url", "privilege", "cost", "rollback", "not_verified"):
        assert word in r.output
    assert not (settings.data_dir / "bin").exists()


def test_vault_install_verifies_sha256_user_level(settings):
    with respx.mock(assert_all_called=False) as m:
        _, binary = _mock_release(m)
        r = runner.invoke(app, ["vault", "install", "--version", "2.7.1", "--yes"])
    assert r.exit_code == 0, r.output
    target = settings.data_dir / "bin" / installer.binary_name()
    assert target.read_bytes() == binary and "Signature (GPG/sigstore) NOT verified" in r.output


def test_vault_install_checksum_mismatch_installs_nothing(settings):
    with respx.mock(assert_all_called=False) as m:
        _mock_release(m, tamper=True)
        r = runner.invoke(app, ["vault", "install", "--version", "2.7.1", "--yes"])
    assert r.exit_code == 1 and "SHA256 mismatch" in r.output
    assert not (settings.data_dir / "bin" / installer.binary_name()).exists()


def test_installer_refuses_unofficial_hosts_and_bad_versions(settings):
    with pytest.raises(installer.InstallError):
        installer._check_host("https://evil.example/bao.zip")
    with pytest.raises(installer.InstallError):
        installer._check_host("http://github.com/openbao/x")
    with httpx.Client() as c, pytest.raises(installer.InstallError):
        installer.resolve_release(c, "latest; rm -rf /")


def _mock_init(m, *, fail_approle: bool = False):
    state = {"initialized": False, "sealed": True}

    def health(_req):
        return httpx.Response(200, json={**state, "standby": False, "version": "2.7.1"})

    def do_init(req):
        body = json.loads(req.content)
        assert body == {"secret_shares": 3, "secret_threshold": 2}
        state["initialized"] = True
        return httpx.Response(200, json={"keys": ["k1" * 16, "k2" * 16, "k3" * 16],
                                         "keys_base64": ["b1" * 16, "b2" * 16, "b3" * 16],
                                         "root_token": "s." + "R" * 26})

    unseals = []

    def do_unseal(req):
        unseals.append(json.loads(req.content)["key"])
        if len(unseals) >= 2:
            state["sealed"] = False
        return httpx.Response(200, json={"sealed": state["sealed"], "t": 2, "n": 3, "progress": len(unseals)})

    m.get(f"{ADDR}/v1/sys/health").mock(side_effect=health)
    m.put(f"{ADDR}/v1/sys/init").mock(side_effect=do_init)
    m.put(f"{ADDR}/v1/sys/unseal").mock(side_effect=do_unseal)
    m.post(f"{ADDR}/v1/sys/mounts/aeos").mock(return_value=httpx.Response(204))
    policies = m.put(url__regex=r".*/v1/sys/policies/acl/aeos-[a-z]+$").mock(return_value=httpx.Response(204))
    m.post(f"{ADDR}/v1/sys/auth/approle").mock(
        return_value=httpx.Response(500, json={"errors": ["boom"]}) if fail_approle else httpx.Response(204))
    role = m.post(f"{ADDR}/v1/auth/approle/role/aeos-controller").mock(return_value=httpx.Response(204))
    m.get(f"{ADDR}/v1/auth/approle/role/aeos-controller/role-id").mock(
        return_value=httpx.Response(200, json={"data": {"role_id": "role-" + "i" * 20}}))
    m.post(f"{ADDR}/v1/auth/approle/role/aeos-controller/secret-id").mock(
        return_value=httpx.Response(200, json={"data": {"secret_id": "sid-" + "j" * 20}}))
    m.post(f"{ADDR}/v1/auth/approle/login").mock(return_value=httpx.Response(200, json={
        "auth": {"client_token": "s." + "C" * 26, "lease_duration": 3600, "renewable": True}}))
    m.get(f"{ADDR}/v1/auth/token/lookup-self").mock(return_value=httpx.Response(200, json={"data": {"ttl": 3600}}))
    revoke = m.post(f"{ADDR}/v1/auth/token/revoke-self").mock(return_value=httpx.Response(204))
    return {"policies": policies, "role": role, "revoke": revoke, "unseals": unseals}


INIT = ["vault", "init", "--shares", "3", "--threshold", "2", "--yes"]
ROOT = "s." + "R" * 26


def test_vault_init_full_flow(settings, memory_keyring):  # noqa: F811
    with respx.mock(assert_all_called=False) as m:
        calls = _mock_init(m)
        r = runner.invoke(app, [*INIT, "--plain-output"])
    assert r.exit_code == 0, r.output
    assert "SHOWN ONCE" in r.output and "b1b1" in r.output and "Root token revoked" in r.output
    assert ROOT not in r.output  # revoked root token is never shown
    assert "AI assistant" in r.output and "never copy this output" in r.output
    assert vault_cli.ALT_SCREEN_ON not in r.output  # not a terminal: plain output
    assert calls["policies"].call_count == 5 and calls["revoke"].called
    role_body = json.loads(calls["role"].calls.last.request.content)
    assert "aeos-office" not in role_body["token_policies"] and role_body["token_ttl"] == "1h"
    assert "aeos-personal" in role_body["token_policies"]
    office_policy = next(c.request for c in calls["policies"].calls if c.request.url.path.endswith("aeos-office"))
    assert "aeos/data/office/*" in json.loads(office_policy.content)["policy"]
    assert bootstrap_store(settings).approle() == ("role-" + "i" * 20, "sid-" + "j" * 20)
    assert bootstrap_store(settings).unseal_keys() == []  # only with --store-unseal-in-keyring


def test_vault_init_outside_a_terminal_is_refused_before_anything_changes(settings, memory_keyring):  # noqa: F811
    with respx.mock(assert_all_called=False) as m:
        _mock_init(m)
        r = runner.invoke(app, INIT)
    assert r.exit_code == 1 and "--plain-output" in r.output and "Nothing was changed" in r.output
    assert not m.calls  # not even a health probe: OpenBao is untouched


def test_vault_init_failure_keeps_root_and_shows_keys(settings, memory_keyring):  # noqa: F811
    with respx.mock(assert_all_called=False) as m:
        calls = _mock_init(m, fail_approle=True)
        r = runner.invoke(app, [*INIT, "--plain-output"])
    assert r.exit_code == 1
    assert "SHOWN ONCE" in r.output and "b1b1" in r.output and "NOT revoked" in r.output
    assert ROOT in r.output and "SETUP INCOMPLETE" in r.output  # still needed to finish the setup
    assert not calls["revoke"].called


def _screen(output: str) -> tuple[str, str]:
    """(what was written on the temporary screen, what came after it)."""
    on, off = output.index(vault_cli.ALT_SCREEN_ON), output.index(vault_cli.ALT_SCREEN_OFF)
    return output[on:off], output[off:]


def test_vault_init_shows_keys_only_on_the_temporary_screen(settings, memory_keyring, monkeypatch):  # noqa: F811
    monkeypatch.setattr(vault_cli, "_vt_terminal", lambda: True)
    with respx.mock(assert_all_called=False) as m:
        _mock_init(m)
        r = runner.invoke(app, INIT, input="ok\nstored\n")
    assert r.exit_code == 0, r.output
    screen, after = _screen(r.output)
    assert "b1b1" in screen and "b1b1" not in after and "SHOWN ONCE" not in after and ROOT not in r.output
    assert screen.count(f"Type '{vault_cli.STORED_WORD}'") == 2  # asked again until the owner confirms
    assert screen.count(vault_cli.ASK_AGAIN) == 1  # on the same row: nothing scrolls off the top
    # iTerm2 copies an erased alternate screen into the scrollback: no erase-in-display at all, rows are wiped
    assert "\x1b[2J" not in r.output and "\x1b[J" not in r.output and "\x1b[0J" not in r.output
    assert screen.rindex("\x1b[1;1H\x1b[2K") > screen.rindex(f"Type '{vault_cli.STORED_WORD}'")  # rows wiped
    assert "not written to this terminal's scrollback" in after and "Root token revoked" in after


def test_vault_init_temporary_screen_closed_early_warns_and_fails(settings, memory_keyring, monkeypatch):  # noqa: F811
    monkeypatch.setattr(vault_cli, "_vt_terminal", lambda: True)
    with respx.mock(assert_all_called=False) as m:
        calls = _mock_init(m)
        r = runner.invoke(app, INIT, input="")
    assert r.exit_code == 1
    _, after = _screen(r.output)
    assert "b1b1" not in after and "before you typed 'stored'" in after and "aeos vault init" in after
    assert calls["revoke"].called  # the setup itself completed


def test_vault_init_incomplete_setup_on_the_temporary_screen_asks_for_the_root_token(
        settings, memory_keyring, monkeypatch):  # noqa: F811
    monkeypatch.setattr(vault_cli, "_vt_terminal", lambda: True)
    with respx.mock(assert_all_called=False) as m:
        calls = _mock_init(m, fail_approle=True)
        r = runner.invoke(app, INIT, input="stored\n")
    assert r.exit_code == 1 and not calls["revoke"].called
    screen, after = _screen(r.output)
    assert ROOT in screen and "SETUP INCOMPLETE" in screen and "and the root token offline" in screen
    assert ROOT not in after and "If you stored it" in after and "aeos vault init" in after


def test_vault_unseal_from_keyring(settings, memory_keyring):  # noqa: F811
    bootstrap_store(settings).set_unseal_keys(["u1" * 16, "u2" * 16])
    got = []

    def unseal(req):
        got.append(json.loads(req.content)["key"])
        return httpx.Response(200, json={"sealed": len(got) < 2, "t": 2, "progress": len(got)})

    with respx.mock(assert_all_called=False) as m:
        m.get(f"{ADDR}/v1/sys/seal-status").mock(return_value=httpx.Response(
            200, json={"initialized": True, "sealed": True, "t": 2, "n": 3, "progress": 0}))
        m.put(f"{ADDR}/v1/sys/unseal").mock(side_effect=unseal)
        r = runner.invoke(app, ["vault", "unseal", "--from-keyring"])
    assert r.exit_code == 0 and "Unsealed." in r.output and got == ["u1" * 16, "u2" * 16]
    assert "u1u1" not in r.output


def test_vault_status_and_start_without_binary(settings, memory_keyring, monkeypatch):  # noqa: F811
    monkeypatch.setattr(server, "bin_path", lambda s: None)
    with respx.mock(assert_all_called=False) as m:
        m.route(url__startswith=ADDR).mock(side_effect=httpx.ConnectError("down"))
        r = runner.invoke(app, ["vault", "status"])
        assert r.exit_code == 0 and "BLOCKED" in r.output and "aeos vault install" in r.output
        r = runner.invoke(app, ["vault", "start"])
    assert r.exit_code == 1 and "aeos vault install" in r.output


def test_vault_start_with_an_unusable_openbao_binary_setting_does_not_advise_install(settings, tmp_path,
                                                                                    monkeypatch):
    """The settings override wins over <data>/bin, so `aeos vault install` would change nothing."""
    a_directory = tmp_path / "bao-dir"
    a_directory.mkdir()
    monkeypatch.setattr(settings, "openbao_binary", str(a_directory))
    r = runner.invoke(app, ["vault", "start"])
    assert r.exit_code == 1
    assert f"settings openbao_binary={a_directory} is not an executable file; fix the setting or remove it" in r.output
    assert "aeos vault install" not in r.output


def test_generated_server_config_is_loopback_only(settings):
    text = server.config_text(settings)
    assert 'address         = "127.0.0.1:8200"' in text and "tls_disable     = true" in text and "ui = false" in text
    assert 'storage "raft"' in text and 'cluster_addr = "https://127.0.0.1:8201"' in text
    assert "disable_mlock" not in text and 'storage "file"' not in text  # unsupported in OpenBao 2.7.1
    assert "token" not in text.lower() and str(settings.data_dir.as_posix()) in text
    bad = settings.model_copy(update={"openbao_addr": "http://0.0.0.0:8200"})
    with pytest.raises(Exception):
        server.config_text(bad)


def test_vault_status_verify_blocked_without_store(settings, memory_keyring, monkeypatch):  # noqa: F811
    """`--verify` never claims VERIFIED without a reachable OpenBao (CONNECTED != VERIFIED)."""
    monkeypatch.setattr(server, "bin_path", lambda s: None)
    with respx.mock(assert_all_called=False) as m:
        m.route(url__startswith=ADDR).mock(side_effect=httpx.ConnectError("down"))
        r = runner.invoke(app, ["vault", "status", "--verify"])
    assert r.exit_code == 1 and "not the active store" in r.output
    from aeos.secrets.provider import last_verification

    assert last_verification(settings)["status"] == "BLOCKED"


def test_vault_status_verify_roundtrip(settings, memory_keyring):  # noqa: F811
    bootstrap_store(settings).set_approle("role-" + "i" * 20, "sid-" + "j" * 20)
    stored: dict = {}

    def write(req):
        stored["v"] = json.loads(req.content)["data"]["value"]
        return httpx.Response(200, json={"data": {"version": 1}})

    with respx.mock(assert_all_called=False) as m:
        m.get(f"{ADDR}/v1/sys/health").mock(return_value=httpx.Response(
            200, json={"initialized": True, "sealed": False, "standby": False, "version": "2.7.1"}))
        m.post(f"{ADDR}/v1/auth/approle/login").mock(return_value=httpx.Response(200, json={
            "auth": {"client_token": "s." + "C" * 26, "lease_duration": 3600, "renewable": True,
                     "token_policies": ["aeos-personal", "aeos-platform", "aeos-projects", "aeos-workers"]}}))
        m.post(f"{ADDR}/v1/aeos/data/platform/aeos-healthcheck").mock(side_effect=write)
        m.post(f"{ADDR}/v1/aeos/metadata/platform/aeos-healthcheck").mock(return_value=httpx.Response(204))
        m.get(f"{ADDR}/v1/aeos/metadata/platform/aeos-healthcheck").mock(return_value=httpx.Response(
            200, json={"data": {"updated_time": "2026-10-18T03:00:00Z", "current_version": 1, "custom_metadata": {}}}))
        m.get(f"{ADDR}/v1/aeos/data/platform/aeos-healthcheck").mock(side_effect=lambda req: httpx.Response(
            200, json={"data": {"data": {"value": stored["v"]}, "metadata": {}}}))
        m.delete(f"{ADDR}/v1/aeos/metadata/platform/aeos-healthcheck").mock(return_value=httpx.Response(204))
        m.get(url__regex=r".*/v1/aeos/metadata/[a-z]+/\?list=true$").mock(return_value=httpx.Response(404))
        r = runner.invoke(app, ["vault", "status", "--verify"])
    assert r.exit_code == 0, r.output
    assert "functional check VERIFIED" in r.output and stored["v"] not in r.output
    assert "policies=aeos-personal,aeos-platform,aeos-projects,aeos-workers" in r.output
    from aeos.secrets.health import store_health
    from aeos.secrets.provider import last_verification

    assert last_verification(settings)["status"] == "VERIFIED"
    with respx.mock(assert_all_called=False) as m:
        m.route(url__startswith=ADDR).mock(side_effect=httpx.ConnectError("down"))
        reset_provider_cache()
        h = store_health(settings)
    assert h["functional_check"]["status"] == "VERIFIED" and h["status"] == "BLOCKED"  # history, not live state
    assert h["functional_check"]["stale"] is True  # never shown as a current proof


def test_vault_install_defaults_to_the_manifest_pin_and_never_to_latest(settings, monkeypatch):
    """§07: installers are pinned; with no approved pin the owner must name an exact version."""
    monkeypatch.setattr(installer, "manifest_pin", lambda root=None: None)
    r = runner.invoke(app, ["vault", "install", "--yes"])
    assert r.exit_code == 1 and "no approved_pin for openbao" in r.output
    r = runner.invoke(app, ["vault", "install", "--version", "latest", "--yes"])
    assert r.exit_code == 1 and "'latest' is not a pin" in r.output
    monkeypatch.setattr(installer, "manifest_pin", lambda root=None: "2.7.1")
    with respx.mock(assert_all_called=False) as m:
        _mock_release(m)
        latest = m.get(f"{installer.GITHUB_API}/latest")
        r = runner.invoke(app, ["vault", "install"], input="n\n")
    assert "install-manifest approved_pin" in r.output and "2.7.1" in r.output
    assert not latest.called


def test_manifest_pin_reads_the_reviewed_manifest(tmp_path):
    (tmp_path / "install-manifest").mkdir()
    (tmp_path / "install-manifest" / "manifest.yaml").write_text(
        "components:\n  - component_id: openbao\n    approved_pin: v2.7.1\n  - component_id: x\n    approved_pin: 9.9.9\n",
        encoding="utf-8")
    assert installer.manifest_pin(tmp_path) == "2.7.1"
    (tmp_path / "install-manifest" / "manifest.yaml").write_text(
        "components:\n  - component_id: openbao\n    approved_pin: null\n", encoding="utf-8")
    assert installer.manifest_pin(tmp_path) is None
    assert installer.manifest_pin() == "2.7.1"  # the repository manifest pin (OWNER_ACTIONS 1.12, 2026-10-27)


def _manifest_digest(monkeypatch, pin: str | None, digest: str | None) -> None:
    monkeypatch.setattr(installer, "manifest_pin", lambda root=None: pin)
    monkeypatch.setattr(installer, "manifest_digest", lambda root=None: digest)


def test_install_checks_the_manifest_approved_digest_before_installing(settings, monkeypatch):
    """§07 (re-audit): the installer used to compare the archive only with checksums.txt of the same release;
    the owner's approved_digest was checked only by post-install drift. A wrong binary is now refused before it
    is written, and the installed binary's digest is recorded for the drift check."""
    good = "sha256:" + hashlib.sha256(b"fake-bao-binary").hexdigest()
    _manifest_digest(monkeypatch, "2.7.1", "sha256:" + "f" * 64)  # the reviewed digest is NOT this release's binary
    with respx.mock(assert_all_called=False) as m:
        _mock_release(m)
        r = runner.invoke(app, ["vault", "install", "--yes"])
    assert r.exit_code == 1 and "differs from the install-manifest approved_digest" in r.output, r.output
    assert "REQUIRED" in r.output and not (settings.data_dir / "bin" / installer.binary_name()).exists()

    _manifest_digest(monkeypatch, "2.7.1", good)
    with respx.mock(assert_all_called=False) as m:
        _mock_release(m)
        r = runner.invoke(app, ["vault", "install", "--yes"])
    assert r.exit_code == 0, r.output
    assert "matched the install-manifest approved_digest" in r.output
    from aeos.core.db import session_scope
    from aeos.models.inventory import ComponentObservation

    with session_scope() as s:
        obs = s.query(ComponentObservation).filter_by(component_id="openbao", dimension="installation").all()
        assert any(o.observed_identity == good and o.status == "INSTALLED" for o in obs)


def test_install_states_when_no_reviewed_digest_applies(settings, monkeypatch):
    _manifest_digest(monkeypatch, "2.7.1", None)  # pin recorded, digest not (owner step)
    with respx.mock(assert_all_called=False) as m:
        _mock_release(m)
        r = runner.invoke(app, ["vault", "install", "--yes"])
    assert r.exit_code == 0 and "NOT_RECORDED" in r.output and "NOT checked against a reviewed digest" in r.output
    _manifest_digest(monkeypatch, "2.6.0", "sha256:" + "f" * 64)  # explicit other version: the digest is not its own
    plan = installer.digest_requirement("2.7.1")
    assert plan["digest_status"] == "NOT_APPLICABLE" and plan["approved_digest"] is None


def test_manifest_digest_is_read_and_a_malformed_one_fails_closed(tmp_path):
    (tmp_path / "install-manifest").mkdir()
    path = tmp_path / "install-manifest" / "manifest.yaml"
    path.write_text("components:\n  - component_id: openbao\n    approved_pin: 2.7.1\n"
                    f"    approved_digest: SHA256:{'A' * 64}\n", encoding="utf-8")
    assert installer.manifest_digest(tmp_path) == "sha256:" + "a" * 64
    assert installer.digest_requirement("2.7.1", tmp_path)["digest_status"] == "REQUIRED"
    path.write_text("components:\n  - component_id: openbao\n    approved_pin: 2.7.1\n    approved_digest: abc\n",
                    encoding="utf-8")
    with pytest.raises(installer.InstallError):
        installer.manifest_digest(tmp_path)
    # the repository manifest pins openbao's version but records no digest yet (OWNER_ACTIONS 1.12)
    assert installer.manifest_digest() is None


def test_install_remedies_never_suggest_a_command_the_cli_refuses(settings, monkeypatch):
    """Review finding: with no approved pin `aeos vault install` is refused, so the Doctor/health remedies must
    name the exact-version form; with a pin the plain command is offered."""
    from aeos.doctor.checks.secret import vault_remedy
    from aeos.secrets.health import _remedies

    monkeypatch.setattr(installer, "manifest_pin", lambda root=None: None)
    assert vault_remedy("install").command.endswith("vault install --version <X.Y.Z>")
    assert "approved_pin" in vault_remedy("install").operation
    assert _remedies({}, False, None)[0]["command"] == "aeos vault install --version <X.Y.Z>"
    r = runner.invoke(app, ["vault", "install", "--yes"])
    assert r.exit_code == 1  # the plain form really is refused without a pin
    monkeypatch.setattr(installer, "manifest_pin", lambda root=None: "2.7.1")
    assert vault_remedy("install").command.endswith("vault install")
    assert _remedies({}, False, None)[0]["command"] == "aeos vault install"


def test_vault_health_does_not_advise_install_when_the_binary_setting_is_unusable(settings, tmp_path):
    """The openbao_binary setting wins over <data>/bin, so an install would not help: say to fix the setting."""
    from aeos.secrets.health import _remedies, store_health

    bad = tmp_path / "not-a-binary"
    bad.mkdir()
    settings.openbao_binary = str(bad)
    remedies = store_health(settings)["remedies"]
    commands = [r["command"] for r in remedies]
    assert "aeos vault install" not in commands and not any(c.startswith("aeos vault install ") for c in commands)
    assert any(str(bad) in r["why"] and "openbao_binary" in r["why"] for r in remedies)
    near_miss = _remedies({}, False, None, None)
    assert near_miss[0]["command"].startswith("aeos vault install")
