"""OpenBao unseal key in the OS keyring (ADR-0123): init stores and never prints, auto-unseal only when sealed and
only from the keyring, one-time import from a hidden prompt, owner-only recovery copy, Doctor line.

Fakes only (L20): respx answers for OpenBao, the in-memory keyring of tests/unit/test_routing_fakes.py, a
monkeypatched getpass. Nothing reaches the real keyring, vault or Keychain. Every key and token below is a fake.
"""

from __future__ import annotations

import json
import logging
import stat
import sys
import uuid
from types import SimpleNamespace

import httpx
import keyring
import pytest
import respx
from sqlalchemy import select
from typer.testing import CliRunner

from aeos.cli import app
from aeos.core.db import session_factory
from aeos.core.paths import repo_root
from aeos.models.audit import AuditEvent
from aeos.secrets import unseal as unseal_mod
from aeos.secrets import vault_cli
from aeos.secrets.provider import UNSEAL_KEYRING_RISK, bootstrap_store, reset_provider_cache
from tests.unit.test_routing_fakes import memory_keyring  # noqa: F401 - fixture
from tests.unit.test_secrets_cli import ADDR, ROOT, _mock_init

runner = CliRunner()
KEYS_B64 = ["b1" * 16, "b2" * 16, "b3" * 16]  # what _mock_init's sys/init returns as keys_base64
KEYS_HEX = ["k1" * 16, "k2" * 16, "k3" * 16]
INIT = ["vault", "init", "--shares", "3", "--threshold", "2", "--yes"]  # default: keys into the OS keyring
FAKE_KEY = "Fk" + "q" * 40 + "=="  # an unseal key share as the owner would type it (fake)
ROLE_ID, SECRET_ID = "role-" + "i" * 20, "sid-" + "j" * 20  # what _mock_init's AppRole endpoints return (fake)


@pytest.fixture(autouse=True)
def _reset():
    reset_provider_cache()
    yield
    reset_provider_cache()


@pytest.fixture
def posix_export(monkeypatch):
    """The POSIX flow on every host (also on the Windows CI runner): export-recovery is supported."""
    monkeypatch.setattr(unseal_mod, "recovery_export_supported", lambda: True)


@pytest.fixture
def windows_export(monkeypatch):
    """The Windows flow on every host: export-recovery refuses, so vault init shows the recovery material once."""
    monkeypatch.setattr(unseal_mod, "recovery_export_supported", lambda: False)


def assert_no_secret(where: dict[str, str], secrets: list[str]) -> None:
    """Red with the place of the leak, never with the value itself."""
    for name, text in where.items():
        for i, value in enumerate(secrets):
            if value and value in text:
                raise AssertionError(f"secret value #{i} leaked into {name}")


def _outputs(r, caplog) -> dict[str, str]:
    return {"stdout": r.stdout, "stderr": r.stderr, "log records": "\n".join(m.getMessage() for m in caplog.records)}


def _audit_rows(action: str) -> list[AuditEvent]:
    with session_factory()() as s:
        return list(s.scalars(select(AuditEvent).where(AuditEvent.action == action)))


# ------------------------------------------------------------------------------ init


def test_init_stores_the_keys_in_the_keyring_and_never_prints_them(settings, memory_keyring, caplog,  # noqa: F811
                                                                   posix_export):
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as m:
        calls = _mock_init(m)
        r = runner.invoke(app, INIT)  # no terminal needed: nothing is shown
    assert r.exit_code == 0, r.output
    assert_no_secret(_outputs(r, caplog), [*KEYS_B64, *KEYS_HEX, ROOT, ROLE_ID, SECRET_ID])
    store = bootstrap_store(settings)
    assert store.unseal_keys() == KEYS_B64  # every share, so a recovery copy is complete
    assert store.root_token() is None and calls["revoke"].called  # revoked, then removed from the keyring
    assert "3 stored in the OS keyring" in r.stdout and "never printed" in r.stdout
    assert "aeos vault export-recovery <file>" in r.stdout and UNSEAL_KEYRING_RISK in r.stdout
    assert vault_cli.ALT_SCREEN_ON not in r.output and "SHOWN ONCE" not in r.output


def test_init_incomplete_keeps_the_root_token_in_the_keyring_only(settings, memory_keyring, caplog,  # noqa: F811
                                                                  posix_export):
    with respx.mock(assert_all_called=False) as m:
        calls = _mock_init(m, fail_approle=True)
        r = runner.invoke(app, INIT)
    assert r.exit_code == 1 and not calls["revoke"].called
    assert_no_secret(_outputs(r, caplog), [*KEYS_B64, ROOT])
    assert bootstrap_store(settings).root_token() == ROOT and bootstrap_store(settings).unseal_keys() == KEYS_B64
    assert "NOT revoked" in r.stderr and "kept in the OS keyring only" in r.stderr and "export-recovery" in r.stderr


def test_init_falls_back_to_showing_once_when_the_keyring_refuses(settings, memory_keyring, monkeypatch,  # noqa: F811
                                                                  posix_export):
    real_set = keyring.set_password

    def refuse_unseal(service, name, value):
        if name.startswith("openbao-unseal-key-"):
            raise keyring.errors.PasswordSetError("refused")
        real_set(service, name, value)

    monkeypatch.setattr(keyring, "set_password", refuse_unseal)
    with respx.mock(assert_all_called=False) as m:
        _mock_init(m)
        lost = runner.invoke(app, INIT)
    assert lost.exit_code == 1 and "cannot be shown and is lost" in lost.stderr and "aeos vault init" in lost.stderr
    assert_no_secret({"output": lost.output}, KEYS_B64)


def test_init_keyring_refusal_with_plain_output_shows_the_material(settings, memory_keyring, monkeypatch,  # noqa: F811
                                                                   posix_export):
    monkeypatch.setattr(keyring, "set_password", lambda *a: (_ for _ in ()).throw(keyring.errors.PasswordSetError("x")))
    with respx.mock(assert_all_called=False) as m:
        _mock_init(m)
        r = runner.invoke(app, [*INIT, "--plain-output"])
    assert r.exit_code == 1 and "did not take the unseal material" in r.stderr
    assert "SHOWN ONCE" in r.stdout and KEYS_B64[0] in r.stdout and ROOT in r.stdout  # never lose the material


def test_bite_windows_init_outside_a_terminal_is_refused_before_anything_changes(settings, memory_keyring,  # noqa: F811
                                                                                 windows_export):
    with respx.mock(assert_all_called=False) as m:
        calls = _mock_init(m)
        r = runner.invoke(app, INIT)
        init_called = any(c.request.url.path.endswith("/sys/init") for c in m.calls)
    assert r.exit_code == 1 and "On Windows vault init also shows the recovery material" in r.stderr
    assert "Nothing was changed" in r.stderr and not init_called and not calls["revoke"].called
    assert bootstrap_store(settings).unseal_key_count() == 0


def test_windows_init_keeps_the_keyring_and_shows_the_recovery_material_once(settings, memory_keyring,  # noqa: F811
                                                                              windows_export, monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(vault_cli, "_vt_terminal", lambda: True)
    monkeypatch.setattr(vault_cli, "_show_on_temporary_screen", lambda lines, what: seen.append(lines) or True)
    with respx.mock(assert_all_called=False) as m:
        calls = _mock_init(m)
        r = runner.invoke(app, INIT)
    assert r.exit_code == 0, r.output
    assert bootstrap_store(settings).unseal_keys() == KEYS_B64 and calls["revoke"].called  # auto-unseal still works
    screen = "\n".join(seen[0])
    assert all(k in screen for k in KEYS_B64) and f"AppRole role_id  : {ROLE_ID}" in screen
    assert f"AppRole secret_id: {SECRET_ID}" in screen and ROOT not in screen  # revoked: useless, not shown
    assert "import-unseal-key --shares 3 --with-approle" in " ".join(screen.split())
    assert_no_secret({"output": r.output}, [*KEYS_B64, ROOT, ROLE_ID, SECRET_ID])  # only on the temporary screen
    assert "export-recovery <file>" not in r.output and "is your offline" in r.stdout


def test_windows_init_screen_closed_early_names_the_reset_and_fails(settings, memory_keyring,  # noqa: F811
                                                                     windows_export, monkeypatch):
    monkeypatch.setattr(vault_cli, "_vt_terminal", lambda: True)
    monkeypatch.setattr(vault_cli, "_show_on_temporary_screen", lambda lines, what: False)
    with respx.mock(assert_all_called=False) as m:
        _mock_init(m)
        r = runner.invoke(app, INIT)
    assert r.exit_code == 1 and "now holds the only copy" in r.stderr and "reset the still empty vault" in r.stderr
    assert bootstrap_store(settings).unseal_keys() == KEYS_B64


def test_windows_init_plain_output_is_the_near_miss(settings, memory_keyring, windows_export):  # noqa: F811
    with respx.mock(assert_all_called=False) as m:
        _mock_init(m)
        r = runner.invoke(app, [*INIT, "--plain-output"])
    assert r.exit_code == 0, r.output
    assert "SHOWN ONCE" in r.stdout and KEYS_B64[0] in r.stdout and SECRET_ID in r.stdout and ROOT not in r.stdout
    assert bootstrap_store(settings).unseal_keys() == KEYS_B64


# ------------------------------------------------------------------------------ unseal / auto-unseal


class CountingKeyring(keyring.backend.KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}
        self.reads = 0

    def get_password(self, service, username):
        self.reads += 1
        return self.data.get((service, username))

    def set_password(self, service, username, password):
        self.data[(service, username)] = password

    def delete_password(self, service, username):
        self.data.pop((service, username), None)


@pytest.fixture
def counting_keyring():
    previous = keyring.get_keyring()
    kr = CountingKeyring()
    keyring.set_keyring(kr)
    try:
        yield kr
    finally:
        keyring.set_keyring(previous)


def _vault(m, *, sealed: bool, initialized: bool = True, t: int = 2):
    got: list[str] = []

    def do_unseal(req):
        got.append(json.loads(req.content)["key"])
        return httpx.Response(200, json={"sealed": len(got) < t, "t": t, "progress": len(got)})

    m.get(f"{ADDR}/v1/sys/seal-status").mock(return_value=httpx.Response(
        200, json={"initialized": initialized, "sealed": sealed, "t": t, "n": 3, "progress": 0}))
    m.put(f"{ADDR}/v1/sys/unseal").mock(side_effect=do_unseal)
    return got


def test_auto_unseal_only_when_sealed_and_only_from_the_keyring(settings, counting_keyring, caplog):
    bootstrap_store(settings).set_unseal_keys(KEYS_B64)
    counting_keyring.reads = 0
    with respx.mock(assert_all_called=False) as m:
        got = _vault(m, sealed=False)
        assert unseal_mod.unseal_at_start(settings) is None
    assert got == [] and counting_keyring.reads == 0  # unsealed: the keyring is not even read
    with respx.mock(assert_all_called=False) as m:
        got = _vault(m, sealed=True)
        note = unseal_mod.unseal_at_start(settings)
    assert got == KEYS_B64[:2] and note and "unsealed with the key from the OS keyring" in note
    assert_no_secret({"note": note, "log records": "\n".join(x.getMessage() for x in caplog.records)}, KEYS_B64)
    rows = _audit_rows("openbao.unseal")
    assert len(rows) == 1 and rows[0].result == "SUCCESS"
    assert rows[0].details["source"] == "os-keyring" and rows[0].details["trigger"] == "serve"
    assert_no_secret({"audit row": json.dumps(rows[0].details)}, KEYS_B64)


def test_auto_unseal_without_a_stored_key_keeps_the_former_message(settings, memory_keyring):  # noqa: F811
    with respx.mock(assert_all_called=False) as m:
        got = _vault(m, sealed=True)
        note = unseal_mod.unseal_at_start(settings)
    assert got == [] and note == "OpenBao is sealed. Next: aeos vault unseal"
    assert _audit_rows("openbao.unseal") == []  # no attempt, nothing to audit


def test_auto_unseal_unreachable_or_uninitialised_is_silent(settings, memory_keyring):  # noqa: F811
    bootstrap_store(settings).set_unseal_keys(KEYS_B64)
    with respx.mock(assert_all_called=False) as m:
        m.get(f"{ADDR}/v1/sys/seal-status").mock(side_effect=httpx.ConnectError("refused"))
        assert unseal_mod.unseal_at_start(settings) is None
    with respx.mock(assert_all_called=False) as m:
        got = _vault(m, sealed=True, initialized=False)
        assert unseal_mod.unseal_at_start(settings) is None
    assert got == []


def test_serve_runs_the_auto_unseal_before_starting(settings, monkeypatch):
    seen = []
    monkeypatch.setattr(unseal_mod, "unseal_at_start", lambda s: seen.append(s) or "UNSEAL-NOTE-LINE")
    r = runner.invoke(app, ["serve", "--release", "active", "--port", "1"])  # stops right after: no active release
    assert seen and "UNSEAL-NOTE-LINE" in r.output


def test_unseal_command_uses_the_keyring_when_sealed(settings, memory_keyring, monkeypatch):  # noqa: F811
    bootstrap_store(settings).set_unseal_keys(KEYS_B64)
    monkeypatch.setattr(vault_cli.getpass, "getpass", lambda prompt="": pytest.fail("prompted despite a stored key"))
    with respx.mock(assert_all_called=False) as m:
        got = _vault(m, sealed=True)
        r = runner.invoke(app, ["vault", "unseal"])
    assert r.exit_code == 0 and "Unsealed with the key from the OS keyring" in r.stdout and got == KEYS_B64[:2]
    assert_no_secret({"output": r.output}, KEYS_B64)
    assert _audit_rows("openbao.unseal")[0].details["trigger"] == "cli"


def test_unseal_command_without_a_stored_key_prompts_as_before(settings, memory_keyring, monkeypatch):  # noqa: F811
    prompts = []
    typed = iter(["p1" * 16, "p2" * 16])
    monkeypatch.setattr(vault_cli.getpass, "getpass", lambda prompt="": prompts.append(prompt) or next(typed))
    with respx.mock(assert_all_called=False) as m:
        got = _vault(m, sealed=True)
        r = runner.invoke(app, ["vault", "unseal"])
    assert r.exit_code == 0 and "Unsealed." in r.stdout and got == ["p1" * 16, "p2" * 16]
    assert prompts == ["Unseal key 1/2 (hidden): ", "Unseal key 2/2 (hidden): "]
    with respx.mock(assert_all_called=False) as m:
        _vault(m, sealed=True)
        strict = runner.invoke(app, ["vault", "unseal", "--from-keyring"])
    assert strict.exit_code == 1 and "no unseal keys stored in the OS keyring" in strict.stderr


def test_unseal_command_with_a_wrong_stored_key_names_the_fix(settings, memory_keyring):  # noqa: F811
    bootstrap_store(settings).set_unseal_keys([FAKE_KEY])
    with respx.mock(assert_all_called=False) as m:
        m.get(f"{ADDR}/v1/sys/seal-status").mock(return_value=httpx.Response(
            200, json={"initialized": True, "sealed": True, "t": 1, "progress": 0}))
        m.put(f"{ADDR}/v1/sys/unseal").mock(return_value=httpx.Response(400, json={"errors": ["invalid key"]}))
        r = runner.invoke(app, ["vault", "unseal"])
    assert r.exit_code == 1 and "import-unseal-key --replace" in r.stderr
    assert_no_secret({"output": r.output}, [FAKE_KEY])
    assert _audit_rows("openbao.unseal")[0].result == "FAILED"


# ------------------------------------------------------------------------------ import from a hidden prompt


def _typed(monkeypatch, values: list[str]) -> list[str]:
    prompts: list[str] = []
    it = iter(values)
    monkeypatch.setattr(vault_cli.getpass, "getpass", lambda prompt="": prompts.append(prompt) or next(it))
    return prompts


def test_import_reads_a_hidden_prompt_and_never_echoes(settings, memory_keyring, monkeypatch, caplog,  # noqa: F811
                                                       posix_export):
    caplog.set_level(logging.DEBUG)
    prompts = _typed(monkeypatch, [FAKE_KEY, FAKE_KEY])
    r = runner.invoke(app, ["vault", "import-unseal-key"])
    assert r.exit_code == 0, r.output
    assert prompts == ["Unseal key 1/1 (hidden): ", "Repeat unseal key 1/1 (hidden): "]
    assert bootstrap_store(settings).unseal_keys() == [FAKE_KEY]
    assert_no_secret(_outputs(r, caplog), [FAKE_KEY])
    assert "Stored 1 unseal key share(s) in the OS keyring" in r.stdout and "export-recovery" in r.stdout
    row = _audit_rows("openbao.unseal_key_import")[0]
    assert row.details == {"unseal_key_shares": 1, "replaced": False}


def test_import_on_windows_names_the_refused_export_instead_of_suggesting_it(settings, memory_keyring,  # noqa: F811
                                                                                monkeypatch, windows_export):
    _typed(monkeypatch, [FAKE_KEY, FAKE_KEY])
    r = runner.invoke(app, ["vault", "import-unseal-key"])
    assert r.exit_code == 0 and "export-recovery is refused on Windows" in r.stdout
    assert "Next: aeos vault export-recovery" not in r.stdout


def test_import_with_approle_restores_a_lost_keyring_from_hidden_prompts(settings, memory_keyring,  # noqa: F811
                                                                         monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    prompts = _typed(monkeypatch, [FAKE_KEY, FAKE_KEY, ROLE_ID, ROLE_ID, SECRET_ID, SECRET_ID])
    r = runner.invoke(app, ["vault", "import-unseal-key", "--with-approle"])
    assert r.exit_code == 0, r.output
    assert prompts[2:] == ["AppRole role_id (hidden): ", "Repeat AppRole role_id (hidden): ",
                           "AppRole secret_id (hidden): ", "Repeat AppRole secret_id (hidden): "]
    store = bootstrap_store(settings)
    assert store.unseal_keys() == [FAKE_KEY] and store.approle() == (ROLE_ID, SECRET_ID)
    assert "and the AppRole credentials" in r.stdout and "aeos vault status --verify" in r.stdout
    assert_no_secret(_outputs(r, caplog), [FAKE_KEY, ROLE_ID, SECRET_ID])
    row = _audit_rows("openbao.unseal_key_import")[0]
    assert row.details == {"unseal_key_shares": 1, "approle_restored": True, "replaced": False}


@pytest.mark.parametrize(("typed", "message"), [
    ([FAKE_KEY, FAKE_KEY, "not an id!", "not an id!"], "That is not an AppRole role_id"),
    ([FAKE_KEY, FAKE_KEY, ROLE_ID, ROLE_ID + "x"], "The two entries differ"),
    ([FAKE_KEY, FAKE_KEY, ROLE_ID, ROLE_ID, ROLE_ID, ROLE_ID], "two different values"),
])
def test_bite_import_with_approle_refuses_bad_input_and_stores_nothing(settings, memory_keyring,  # noqa: F811
                                                                      monkeypatch, typed, message):
    _typed(monkeypatch, typed)
    r = runner.invoke(app, ["vault", "import-unseal-key", "--with-approle"])
    assert r.exit_code == 1 and message in r.stderr and "Nothing stored" in r.stderr
    store = bootstrap_store(settings)
    assert store.unseal_key_count() == 0 and store.approle() is None
    assert_no_secret({"output": r.output}, [FAKE_KEY, ROLE_ID])


def test_bite_import_with_approle_refuses_to_replace_unless_asked(settings, memory_keyring, monkeypatch):  # noqa: F811
    bootstrap_store(settings).set_approle("role-old-0000", "sid-old-0000")
    _typed(monkeypatch, [FAKE_KEY, FAKE_KEY, ROLE_ID, ROLE_ID, SECRET_ID, SECRET_ID])
    r = runner.invoke(app, ["vault", "import-unseal-key", "--with-approle"])
    assert r.exit_code == 1 and "AppRole credentials are already stored" in r.stderr and "pass --replace" in r.stderr
    assert bootstrap_store(settings).approle() == ("role-old-0000", "sid-old-0000")
    _typed(monkeypatch, [FAKE_KEY, FAKE_KEY, ROLE_ID, ROLE_ID, SECRET_ID, SECRET_ID])
    near = runner.invoke(app, ["vault", "import-unseal-key", "--with-approle", "--replace"])
    assert near.exit_code == 0 and bootstrap_store(settings).approle() == (ROLE_ID, SECRET_ID)


def test_import_never_takes_the_key_from_argv(settings, memory_keyring):  # noqa: F811
    r = runner.invoke(app, ["vault", "import-unseal-key", FAKE_KEY])
    assert r.exit_code == 2 and bootstrap_store(settings).unseal_key_count() == 0


@pytest.mark.parametrize(("typed", "message"), [
    ([FAKE_KEY, FAKE_KEY[:-3] + "x=="], "The two entries differ"),
    (["not a key at all", "not a key at all"], "is not an unseal key share"),
    (["short", "short"], "is not an unseal key share"),
])
def test_bite_import_refuses_bad_input_and_stores_nothing(settings, memory_keyring, monkeypatch, typed,  # noqa: F811
                                                          message):
    _typed(monkeypatch, typed)
    r = runner.invoke(app, ["vault", "import-unseal-key"])
    assert r.exit_code == 1 and message in r.stderr and "Nothing stored" in r.stderr
    assert bootstrap_store(settings).unseal_key_count() == 0
    assert_no_secret({"output": r.output}, [t for t in typed if len(t) >= 16])


def test_import_refuses_to_replace_unless_asked(settings, memory_keyring, monkeypatch):  # noqa: F811
    bootstrap_store(settings).set_unseal_keys(KEYS_B64)
    _typed(monkeypatch, [FAKE_KEY, FAKE_KEY])
    r = runner.invoke(app, ["vault", "import-unseal-key"])
    assert r.exit_code == 1 and "pass --replace" in r.stderr and bootstrap_store(settings).unseal_keys() == KEYS_B64
    _typed(monkeypatch, [FAKE_KEY, FAKE_KEY])
    r = runner.invoke(app, ["vault", "import-unseal-key", "--replace"])
    assert r.exit_code == 0 and bootstrap_store(settings).unseal_keys() == [FAKE_KEY]  # old shares 2 and 3 removed
    assert keyring.get_password(bootstrap_store(settings).service, "openbao-unseal-key-1") is None


# ------------------------------------------------------------------------------ recovery copy

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="Windows is refused (owner-only ACL NOT-PROVEN)")


@posix_only
def test_export_writes_a_new_owner_only_file_and_prints_only_the_path(settings, memory_keyring, tmp_path,  # noqa: F811
                                                                     caplog):
    bootstrap_store(settings).set_unseal_keys(KEYS_B64)
    target = tmp_path / "offline" / "aeos-recovery.txt"
    target.parent.mkdir()
    r = runner.invoke(app, ["vault", "export-recovery", str(target)])
    assert r.exit_code == 0, r.output
    assert r.stdout == f"{target.resolve()}\n" and r.stderr == ""
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    body = target.read_text(encoding="utf-8")
    assert all(f"unseal_key_{i}: {k}" in body for i, k in enumerate(KEYS_B64, 1)) and "root_token" not in body
    assert "approle: NOT in the OS keyring" in body  # said, not silently missing
    assert_no_secret(_outputs(r, caplog), KEYS_B64)
    row = _audit_rows("openbao.recovery_export")[0]
    assert row.details["unseal_key_shares"] == 3 and row.details["root_token_included"] is False
    assert row.details["approle_included"] is False


@posix_only
def test_recovery_copy_restores_a_lost_keyring_unseal_and_approle(settings, memory_keyring, tmp_path,  # noqa: F811
                                                                   monkeypatch, caplog):
    store = bootstrap_store(settings)
    store.set_unseal_keys(KEYS_B64)
    store.set_approle(ROLE_ID, SECRET_ID)
    target = tmp_path / "r.txt"
    r = runner.invoke(app, ["vault", "export-recovery", str(target)])
    assert r.exit_code == 0 and r.stdout == f"{target.resolve()}\n"
    assert_no_secret(_outputs(r, caplog), [*KEYS_B64, ROLE_ID, SECRET_ID])
    assert _audit_rows("openbao.recovery_export")[0].details["approle_included"] is True
    fields = dict(line.split(": ", 1) for line in target.read_text(encoding="utf-8").splitlines() if ": " in line)
    assert "--shares 3 --with-approle" in target.read_text(encoding="utf-8")
    # the keyring is lost (profile reset / new machine): every entry of the bootstrap service is gone
    store.clear_unseal_keys()
    keyring.delete_password(store.service, store.ROLE_ID)
    keyring.delete_password(store.service, store.SECRET_ID)
    assert store.unseal_keys() == [] and store.approle() is None
    shares = [fields[f"unseal_key_{i}"] for i in (1, 2, 3)]
    _typed(monkeypatch, [v for x in [*shares, fields["approle_role_id"], fields["approle_secret_id"]] for v in (x, x)])
    back = runner.invoke(app, ["vault", "import-unseal-key", "--shares", "3", "--with-approle"])
    assert back.exit_code == 0, back.output
    assert store.unseal_keys() == KEYS_B64 and store.approle() == (ROLE_ID, SECRET_ID)


@posix_only
def test_export_includes_a_root_token_kept_by_an_incomplete_init(settings, memory_keyring, tmp_path):  # noqa: F811
    bootstrap_store(settings).set_unseal_keys(KEYS_B64[:1])
    bootstrap_store(settings).set_root_token(ROOT)
    r = runner.invoke(app, ["vault", "export-recovery", str(tmp_path / "r.txt")])
    assert r.exit_code == 0 and f"root_token: {ROOT}" in (tmp_path / "r.txt").read_text(encoding="utf-8")
    assert ROOT not in r.output


@posix_only
def test_bite_export_refuses_to_overwrite_and_near_miss_new_name_is_written(settings, memory_keyring,  # noqa: F811
                                                                          tmp_path):
    bootstrap_store(settings).set_unseal_keys(KEYS_B64)
    existing = tmp_path / "taken.txt"
    existing.write_text("keep me", encoding="utf-8")
    r = runner.invoke(app, ["vault", "export-recovery", str(existing)])
    assert r.exit_code == 1 and "refusing to overwrite" in r.stderr
    assert existing.read_text(encoding="utf-8") == "keep me"
    link = tmp_path / "dangling-link.txt"
    try:
        link.symlink_to(tmp_path / "nowhere.txt")
    except OSError:
        pass
    else:
        r = runner.invoke(app, ["vault", "export-recovery", str(link)])
        assert r.exit_code == 1 and "refusing to overwrite" in r.stderr and not (tmp_path / "nowhere.txt").exists()
    near = runner.invoke(app, ["vault", "export-recovery", str(tmp_path / "taken-2.txt")])
    assert near.exit_code == 0 and (tmp_path / "taken-2.txt").exists()


@posix_only
def test_bite_export_refuses_repository_and_data_dir_paths(settings, memory_keyring, tmp_path):  # noqa: F811
    bootstrap_store(settings).set_unseal_keys(KEYS_B64)
    in_repo = repo_root() / "docs" / f"recovery-{uuid.uuid4().hex}.txt"
    try:
        r = runner.invoke(app, ["vault", "export-recovery", str(in_repo)])
        assert r.exit_code == 1 and "inside a git repository" in r.stderr and not in_repo.exists()
    finally:
        in_repo.unlink(missing_ok=True)
    other_repo = tmp_path / "project"
    (other_repo / ".git").mkdir(parents=True)
    (other_repo / "sub").mkdir()
    r = runner.invoke(app, ["vault", "export-recovery", str(other_repo / "sub" / "r.txt")])
    assert r.exit_code == 1 and "inside a git repository" in r.stderr and not (other_repo / "sub" / "r.txt").exists()
    data = settings.layout.root / "r.txt"
    r = runner.invoke(app, ["vault", "export-recovery", str(data)])
    assert r.exit_code == 1 and "inside the AEOS data directory" in r.stderr and not data.exists()
    near = runner.invoke(app, ["vault", "export-recovery", str(tmp_path / "beside-the-project.txt")])
    assert near.exit_code == 0, near.output  # a sibling of a repository is fine


def test_export_refuses_without_a_stored_key_and_on_windows(settings, memory_keyring, tmp_path,  # noqa: F811
                                                            monkeypatch):
    r = runner.invoke(app, ["vault", "export-recovery", str(tmp_path / "r.txt")])
    if sys.platform != "win32":
        assert r.exit_code == 1 and "No unseal key is stored" in r.stderr and not (tmp_path / "r.txt").exists()
    bootstrap_store(settings).set_unseal_keys(KEYS_B64)
    monkeypatch.setattr(unseal_mod, "sys", SimpleNamespace(platform="win32"))
    assert unseal_mod.recovery_export_supported() is False  # the one switch vault init and the export share
    r = runner.invoke(app, ["vault", "export-recovery", str(tmp_path / "w.txt")])
    assert r.exit_code == 1 and "NOT-PROVEN" in r.stderr and not (tmp_path / "w.txt").exists()


# ------------------------------------------------------------------------------ status and Doctor


def test_vault_status_names_where_the_unseal_key_lives(settings, memory_keyring, monkeypatch):  # noqa: F811
    from aeos.secrets import server

    monkeypatch.setattr(server, "bin_path", lambda s: None)
    r = runner.invoke(app, ["vault", "status"])
    assert "unseal key       not stored" in r.stdout
    bootstrap_store(settings).set_unseal_keys(KEYS_B64)
    r = runner.invoke(app, ["vault", "status"])
    assert "unseal key       3 share(s) in the OS keyring" in r.stdout
    assert_no_secret({"output": r.output}, KEYS_B64)


def test_doctor_openbao_health_states_the_unseal_key_location_and_risk(settings, db, memory_keyring):  # noqa: F811
    from aeos.doctor.checks import REGISTRY, load_all
    from aeos.doctor.runner import make_context, run_check
    from aeos.inventory.probes import HttpResult
    from tests.unit.test_inventory_fakes import base_runner

    load_all()
    url = "http://127.0.0.1:8200/v1/sys/health"
    r = base_runner()
    r.add_binary("bao")
    r.http[url] = HttpResult(200, {"initialized": True, "sealed": False, "version": "2.7.1"})
    res = run_check(REGISTRY["secret.openbao_health"], make_context(settings, db, runner=r))
    assert res.status == "CONNECTED" and res.observed_evidence["unseal_key_location"] == "not-stored"
    assert "Kunci unseal: tidak disimpan di OS keyring" in res.summary
    bootstrap_store(settings).set_unseal_keys(KEYS_B64)
    r.http[url] = HttpResult(503, {"initialized": True, "sealed": True})
    res = run_check(REGISTRY["secret.openbao_health"], make_context(settings, db, runner=r))
    assert res.error_code == "OPENBAO_SEALED" and res.observed_evidence["unseal_key_location"] == "os-keyring"
    assert "Kunci unseal: OS keyring" in res.summary and "Risiko sisa" in res.summary
    assert_no_secret({"summary": res.summary, "evidence": json.dumps(res.observed_evidence)}, KEYS_B64)


def test_bite_assert_no_secret_is_red_with_the_place_and_green_on_a_near_miss():
    with pytest.raises(AssertionError, match=r"secret value #0 leaked into stdout") as exc:
        assert_no_secret({"stdout": f"Unseal key 1: {KEYS_B64[0]}"}, KEYS_B64)
    assert KEYS_B64[0] not in str(exc.value)  # the failure message never repeats the value
    assert_no_secret({"stdout": "3 stored in the OS keyring (macOS Keychain), service dly-aeos-bootstrap; "
                                f"prefix {KEYS_B64[0][:8]}"}, KEYS_B64)


# ------------------------------------------------------------------------------ review fixes (ADR-0123)


def _health(m, *, initialized: bool) -> None:
    m.get(f"{ADDR}/v1/sys/health").mock(return_value=httpx.Response(
        200, json={"initialized": initialized, "sealed": False, "standby": False, "version": "2.7.1"}))


def test_bite_missing_approle_names_the_lost_keyring_path_on_an_initialised_vault(settings, memory_keyring):  # noqa: F811
    from aeos.core.errors import Blocked
    from aeos.secrets.health import _remedies
    from aeos.secrets.provider import KeyringBootstrapStore, OpenBaoProvider

    lost = _remedies({"reachable": True, "initialized": True, "sealed": False}, True, False)
    assert [r["command"] for r in lost] == ["aeos vault import-unseal-key --with-approle"]
    assert "Lost keyring" in lost[0]["why"] and "openbao-unseal-recovery.md" in lost[0]["why"]
    fresh = _remedies({"reachable": True, "initialized": False, "sealed": True}, True, False)
    assert [r["command"] for r in fresh] == ["aeos vault init"]  # near miss: not initialised, init is right
    for initialized, want, never in ((True, "`aeos vault import-unseal-key --with-approle`", "vault init"),
                                     (False, "run `aeos vault init`", "import-unseal-key")):
        with respx.mock(assert_all_called=False) as m:
            _health(m, initialized=initialized)
            with pytest.raises(Blocked) as ei:
                OpenBaoProvider(settings, KeyringBootstrapStore("empty-service"))._login()
        assert want in ei.value.message and never not in ei.value.message, ei.value.message


@posix_only
def test_bite_recovery_copy_without_approle_gives_the_command_without_it(settings, memory_keyring,  # noqa: F811
                                                                          tmp_path):
    bootstrap_store(settings).set_unseal_keys(KEYS_B64)
    r = runner.invoke(app, ["vault", "export-recovery", str(tmp_path / "r.txt")])
    assert r.exit_code == 0, r.output
    body = (tmp_path / "r.txt").read_text(encoding="utf-8")
    assert "--with-approle" not in body and "aeos vault import-unseal-key --shares 3 (hidden prompt" in body
    assert "AppRole credentials are NOT included in this copy" in body
    # near miss (the AppRole is in the copy): test_recovery_copy_restores_a_lost_keyring_unseal_and_approle


def test_bite_windows_screen_names_with_approle_only_when_the_copy_holds_it(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(vault_cli, "_show_on_temporary_screen", lambda lines, what: seen.append(lines) or True)
    res = SimpleNamespace(keys=KEYS_B64, root_token=ROOT, root_revoked=True, error=None)
    for approle in (None, (ROLE_ID, SECRET_ID)):
        vault_cli._show_material_once(res, threshold=2, shares=3, vt=True, kept_in="OS keyring", approle=approle)
    without, with_ = (" ".join(" ".join(lines).split()) for lines in seen)
    assert "--with-approle" not in without and "import-unseal-key --shares 3 (hidden prompt" in without
    assert "AppRole credentials are NOT included" in without
    assert "import-unseal-key --shares 3 --with-approle" in with_ and "NOT included" not in with_


def test_bite_show_once_init_removes_shares_of_an_earlier_vault(settings, memory_keyring):  # noqa: F811
    store = bootstrap_store(settings)
    store.set_unseal_keys([FAKE_KEY])  # left by an earlier vault at this address
    with respx.mock(assert_all_called=False) as m:
        _mock_init(m)
        r = runner.invoke(app, [*INIT, "--show-unseal-once", "--plain-output"])
    assert r.exit_code == 0, r.output
    assert store.unseal_key_count() == 0 and store.unseal_keys() == []  # auto-unseal no longer applies stale shares
    assert "1 unseal key share(s) of an earlier vault removed from the OS keyring" in r.stdout
    assert_no_secret({"output": r.output}, [FAKE_KEY])
    with respx.mock(assert_all_called=False) as m:  # near miss: nothing stored before, nothing to remove
        _mock_init(m)
        clean = runner.invoke(app, [*INIT, "--show-unseal-once", "--plain-output"])
    assert clean.exit_code == 0 and "earlier vault" not in clean.output and store.unseal_key_count() == 0


def test_show_once_init_with_a_stuck_earlier_share_says_so_and_completes(settings, memory_keyring,  # noqa: F811
                                                                          monkeypatch):
    bootstrap_store(settings).set_unseal_keys([FAKE_KEY])
    monkeypatch.setattr(keyring, "delete_password",
                        lambda *a: (_ for _ in ()).throw(keyring.errors.KeyringError("locked")))
    with respx.mock(assert_all_called=False) as m:
        calls = _mock_init(m)
        r = runner.invoke(app, [*INIT, "--show-unseal-once", "--plain-output"])
    assert r.exit_code == 0 and calls["revoke"].called
    assert "earlier vault NOT removed from the OS keyring (KeyringError)" in r.stdout


def _no_migration(monkeypatch) -> list[str]:
    import aeos.cli
    import aeos.core.migrate

    calls: list[str] = []
    monkeypatch.setattr(aeos.cli, "ensure_database", lambda: calls.append("ensure_database"))
    monkeypatch.setattr(aeos.core.migrate, "upgrade_database", lambda: calls.append("upgrade_database"))
    return calls


def test_bite_serve_auto_unseal_never_migrates_and_skips_the_row_when_the_db_is_not_ready(  # noqa: F811
        settings, memory_keyring, monkeypatch, caplog):  # noqa: F811
    from aeos.core import db as db_mod
    from aeos.core.migrate import migration_status

    calls = _no_migration(monkeypatch)
    bootstrap_store(settings).set_unseal_keys(KEYS_B64)
    with db_mod.get_engine().begin() as conn:  # a database one migration behind (an upgrade is pending)
        conn.exec_driver_sql("UPDATE alembic_version SET version_num = 'older-revision'")
    caplog.set_level(logging.WARNING, logger="aeos.secrets")
    with respx.mock(assert_all_called=False) as m:
        _vault(m, sealed=True)
        note = unseal_mod.unseal_at_start(settings)
    assert note and "unsealed with the key from the OS keyring" in note  # the unseal itself still happens
    assert calls == [] and migration_status()["current"] == "older-revision"  # no migration as a side effect
    assert _audit_rows("openbao.unseal") == []
    assert "openbao audit row openbao.unseal skipped: database not at the migration head" in caplog.text
    assert_no_secret({"log": caplog.text}, KEYS_B64)


def test_serve_auto_unseal_without_a_database_file_creates_none(settings, memory_keyring, monkeypatch,  # noqa: F811
                                                               caplog):
    from aeos.core import db as db_mod

    calls = _no_migration(monkeypatch)
    bootstrap_store(settings).set_unseal_keys(KEYS_B64)
    moved = settings.layout.db_path.with_suffix(".moved")
    db_mod.get_engine().dispose()
    monkeypatch.setattr(db_mod, "_engine", None)
    settings.layout.db_path.rename(moved)
    try:
        caplog.set_level(logging.WARNING, logger="aeos.secrets")
        with respx.mock(assert_all_called=False) as m:
            _vault(m, sealed=True)
            assert unseal_mod.unseal_at_start(settings)
        assert calls == [] and not settings.layout.db_path.exists()
        assert "skipped: database not created yet" in caplog.text
    finally:
        moved.rename(settings.layout.db_path)


def test_serve_auto_unseal_records_the_row_in_a_ready_db_without_preparing_it(settings, memory_keyring,  # noqa: F811
                                                                             monkeypatch):
    calls = _no_migration(monkeypatch)
    bootstrap_store(settings).set_unseal_keys(KEYS_B64)
    with respx.mock(assert_all_called=False) as m:
        _vault(m, sealed=True)
        unseal_mod.unseal_at_start(settings)
    assert calls == [] and [r.details["trigger"] for r in _audit_rows("openbao.unseal")] == ["serve"]


def test_bite_non_interactive_keyring_init_stops_before_any_change_when_the_keyring_refuses_writes(  # noqa: F811
        settings, memory_keyring, monkeypatch, posix_export):  # noqa: F811
    monkeypatch.setattr(keyring, "set_password", lambda *a: (_ for _ in ()).throw(keyring.errors.PasswordSetError("x")))
    with respx.mock(assert_all_called=False) as m:
        calls = _mock_init(m)
        r = runner.invoke(app, INIT)  # --yes, no terminal, no --plain-output
        init_called = any(c.request.url.path.endswith("/sys/init") for c in m.calls)
    assert r.exit_code == 1 and "did not take a test write (PasswordSetError)" in r.stderr
    assert "Nothing was changed" in r.stderr and not init_called and not calls["revoke"].called
    # near misses: a keyring that takes writes (test_init_stores_the_keys_in_the_keyring_and_never_prints_them) and
    # --plain-output (test_init_keyring_refusal_with_plain_output_shows_the_material) still initialise


def test_keyring_write_probe_leaves_no_entry(settings, memory_keyring):  # noqa: F811
    store = bootstrap_store(settings)
    assert store.write_probe() is None
    assert keyring.get_password(store.service, store.WRITE_PROBE) is None


def test_bite_a_failed_probe_delete_does_not_claim_the_keyring_refused_a_write(settings, memory_keyring,  # noqa: F811
                                                                              monkeypatch):
    from keyring.errors import PasswordDeleteError

    def refuse_delete(*_a, **_k):
        raise PasswordDeleteError("backend refuses deletes")

    monkeypatch.setattr(keyring, "delete_password", refuse_delete)
    store = bootstrap_store(settings)
    assert store.write_probe() is None  # the write and the read back worked: init may go on
    monkeypatch.setattr(keyring, "set_password", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no write")))
    assert store.write_probe() == "RuntimeError"  # near miss: a refused write is still reported
