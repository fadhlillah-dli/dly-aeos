"""Trusted acceptance suite for the DLY AEOS platform (§09, §19, §21, §50).

This directory is a **protected path**: workers may propose changes, but the patch is quarantined
for special review. The independent verifier copies this directory (plus ``tests/__init__.py`` and
``tests/conftest.py``, whose fixtures are reused here: ``settings``, ``db``, ``client``,
``admin_client``, ``personal_client``, ``secrets_memory``, ``make_app``, ``create_admin``,
``create_viewer``) from the *trusted controller checkout* and runs it against candidate code.

Rules for tests in this suite:

* deterministic, no network, no real external tools, **no skips** (a skipped trusted test makes the
  verification NOT-PROVEN, never PASS);
* parametrize only over static data — collected test ids must not depend on candidate code;
* exercise public surfaces (HTTP API, facades, CLI, DB guards), not private helpers;
* import nothing from ``tests/unit`` or ``tests/integration`` (only this directory is trusted).

``MATRIX.md`` maps every master §50 acceptance row to a test id here, to module tests, or to an
explicit NOT_IMPLEMENTED/BLOCKED status with the owning module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        try:
            inside = _HERE in Path(str(item.path)).resolve().parents
        except (OSError, ValueError):
            inside = False
        if inside:
            item.add_marker(pytest.mark.acceptance)
