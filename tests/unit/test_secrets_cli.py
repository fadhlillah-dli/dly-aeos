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
from aeos.secrets import installer, server
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
    m.get(f"{installer.GITHUB_API}/latest").mock(return_value=httpx.Response(200, json={
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
        r = runner.invoke(app, ["vault", "install"], input="n\n")
    assert r.exit_code == 1 and "Install plan" in r.output and "Aborted - nothing installed" in r.output
    for word in ("source", "asset", "checksums_url", "privilege", "cost", "rollback", "not_verified"):
        assert word in r.output
    assert not (settings.data_dir / "bin").exists()


def test_vault_install_verifies_sha256_user_level(settings):
    with respx.mock(assert_all_called=False) as m:
        _, binary = _mock_release(m)
        r = runner.invoke(app, ["vault", "install", "--yes"])
    assert r.exit_code == 0, r.output
    target = settings.data_dir / "bin" / installer.binary_name()
    assert target.read_bytes() == binary and "Signature (GPG/sigstore) NOT verified" in r.output


def test_vault_install_checksum_mismatch_installs_nothing(settings):
    with respx.mock(assert_all_called=False) as m:
        _mock_release(m, tamper=True)
        r = runner.invoke(app, ["vault", "install", "--yes"])
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


def test_vault_init_full_flow(settings, memory_keyring):  # noqa: F811
    with respx.mock(assert_all_called=False) as m:
        calls = _mock_init(m)
        r = runner.invoke(app, ["vault", "init", "--shares", "3", "--threshold", "2", "--yes"])
    assert r.exit_code == 0, r.output
    assert "SHOWN ONCE" in r.output and "b1b1" in r.output and "Root token revoked" in r.output
    assert calls["policies"].call_count == 5 and calls["revoke"].called
    role_body = json.loads(calls["role"].calls.last.request.content)
    assert "aeos-office" not in role_body["token_policies"] and role_body["token_ttl"] == "1h"
    assert "aeos-personal" in role_body["token_policies"]
    office_policy = next(c.request for c in calls["policies"].calls if c.request.url.path.endswith("aeos-office"))
    assert "aeos/data/office/*" in json.loads(office_policy.content)["policy"]
    assert bootstrap_store(settings).approle() == ("role-" + "i" * 20, "sid-" + "j" * 20)
    assert bootstrap_store(settings).unseal_keys() == []  # only with --store-unseal-in-keyring


def test_vault_init_failure_keeps_root_and_shows_keys(settings, memory_keyring):  # noqa: F811
    with respx.mock(assert_all_called=False) as m:
        calls = _mock_init(m, fail_approle=True)
        r = runner.invoke(app, ["vault", "init", "--shares", "3", "--threshold", "2", "--yes"])
    assert r.exit_code == 1
    assert "SHOWN ONCE" in r.output and "b1b1" in r.output and "NOT revoked" in r.output
    assert not calls["revoke"].called


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


def test_generated_server_config_is_loopback_only(settings):
    text = server.config_text(settings)
    assert 'address         = "127.0.0.1:8200"' in text and "tls_disable     = true" in text and "ui = false" in text
    assert 'storage "raft"' in text and 'cluster_addr = "https://127.0.0.1:8201"' in text
    assert "disable_mlock" not in text and 'storage "file"' not in text  # unsupported in OpenBao 2.7.1
    assert "token" not in text.lower() and str(settings.data_dir.as_posix()) in text
    bad = settings.model_copy(update={"openbao_addr": "http://0.0.0.0:8200"})
    with pytest.raises(Exception):
        server.config_text(bad)
