"""Trusted acceptance suite for the DLY AEOS platform (§09, §19, §21, §50).

This directory is a **protected path**: workers may propose changes, but the patch is quarantined
for special review. The independent verifier copies this directory (plus ``tests/__init__.py`` and
``tests/conftest.py``, whose fixtures are reused here: ``settings``, ``db``, ``client``,
``admin_client``, ``personal_client``, ``secrets_memory``, ``make_app``, ``create_admin``,
``create_viewer``) from the *trusted controller checkout* and runs it against candidate code.

Rules for tests in this suite:

* deterministic, no network, no real external tools, **no skips in the verifier selection** (a
  skipped trusted test makes the verification NOT-PROVEN, never PASS). A §50 row that needs an
  external capability this host does not have (real OpenBao, Docker containment, Google/Atlassian
  tenant, GitHub merge enforcement, macOS) is split: everything checkable locally is a normal test;
  the remainder is a separate test marked ``integration`` (deselected by the verifier's
  ``-m "not integration and not network"``) that skips with an explicit ``BLOCKED-<reason>``;
* a §50 row that fails because of a known product bug stays as ``xfail(strict=True, reason="BUG: ...")``
  — the verifier counts it as skipped, so verification stays NOT-PROVEN until the bug is fixed and
  the marker is removed (human-reviewed change of this protected path);
* parametrize only over static data — collected test ids must not depend on candidate code;
* exercise public surfaces (HTTP API, facades, CLI, DB guards), not private helpers;
* every DLY AEOS module runs for real; only EXTERNAL tools/services are faked (``fakes.py``);
* import nothing from ``tests/unit`` or ``tests/integration`` (only this directory is trusted).

``MATRIX.md`` maps every master §50 acceptance row to the test ids here and their status.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.acceptance.fakes import ContractSecretStore, FakeHost, base_host

_HERE = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        try:
            inside = _HERE in Path(str(item.path)).resolve().parents
        except (OSError, ValueError):
            inside = False
        if inside:
            item.add_marker(pytest.mark.acceptance)


@pytest.fixture(autouse=True)
def _no_real_external_binaries(request: pytest.FixtureRequest, tmp_path: Path) -> None:
    """The owner's real ``claude``/``opencode`` binaries are never reachable from this suite: the
    configured binaries point at paths that do not exist (tests that need the official client use
    ``FakeClaude`` at the process boundary)."""
    if "settings" not in request.fixturenames:
        return
    settings = request.getfixturevalue("settings")
    absent = tmp_path / "absent-binaries"
    settings.claude_binary = str(absent / "claude.exe")
    settings.opencode_binary = str(absent / "opencode.exe")


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeHost]:
    """Fake host at the probe boundary: git only; the control-plane port is free and bindable."""
    from aeos.doctor.checks import host as host_checks
    from aeos.inventory.probes import set_runner_override

    h = base_host()
    set_runner_override(h)
    monkeypatch.setattr(host_checks, "tcp_in_use", lambda *_a, **_k: False)
    monkeypatch.setattr(host_checks, "can_bind", lambda *_a, **_k: True)
    yield h
    set_runner_override(None)


@pytest.fixture
def vault() -> Iterator[ContractSecretStore]:
    """Secret store with the real provider caller contract (namespace-scoped purposes)."""
    from aeos.secrets.provider import set_provider_override

    store = ContractSecretStore()
    set_provider_override(store)
    yield store
    set_provider_override(None)
