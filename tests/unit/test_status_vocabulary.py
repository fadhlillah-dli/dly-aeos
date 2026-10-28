"""The status values the backend can return, kept in sync with the frontend StatusBadge enumeration test.

R28 (2026-10-28): the Control Center showed activated items as not active, partly because StatusBadge had no spec for
TRIAL, UNTESTED and TRIAL_APPROVED and drew them as a grey raw code with a question mark, like a candidate. The
frontend test (frontend/src/components/StatusBadge.enumeration.test.tsx) reads the fixture below and fails with
"StatusBadge has no spec for <X>"; THIS test fails when the fixture no longer equals what the backend enums define, so
a new backend value cannot slip past the frontend test by not being in the fixture.

To refresh the fixture after adding a backend value: run this test, copy the "expected" JSON it prints into
frontend/src/components/__fixtures__/backend-status-values.json, then add the missing StatusBadge spec.
"""

from __future__ import annotations

import json
from pathlib import Path

from aeos.core import enums
from aeos.routing import free_tier

FIXTURE = Path(__file__).resolve().parents[2] / "frontend/src/components/__fixtures__/backend-status-values.json"


def backend_status_values() -> dict[str, list[str]]:
    def vals(e: type) -> list[str]:
        return [m.value for m in e]  # type: ignore[attr-defined]

    return {
        "route.status": vals(enums.RouteStatus),
        "route.availability": vals(enums.RouteAvailability),
        "inventory.admission": vals(enums.AdmissionStatus),
        "inventory.installation": vals(enums.InstallationStatus),
        "inventory.configuration": vals(enums.ConfigurationStatus),
        "inventory.connectivity": vals(enums.ConnectivityStatus),
        "inventory.functional": vals(enums.FunctionalStatus),
        "inventory.enablement": vals(enums.Enablement),
        "inventory.usage": vals(enums.UsageStatus),
        "inventory.lifecycle_final": vals(enums.LifecycleFinal),
        "inventory.design_label": vals(enums.DesignLabel),
        "data_mode": vals(enums.DataMode),
        "free_tier.state": [free_tier.STATE_OK, free_tier.STATE_LIMIT_REACHED],
        "free_tier.decision_c": [free_tier.DECISION_C_INELIGIBLE, free_tier.DECISION_C_ELIGIBLE],
    }


def test_fixture_equals_the_backend_enums():
    expected = backend_status_values()
    actual = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert actual == expected, (
        f"{FIXTURE.name} is out of sync with the backend enums; expected:\n"
        + json.dumps(expected, indent=2, ensure_ascii=True))


def test_every_seeded_and_activated_route_uses_a_listed_status(db):
    """The route code writes status literals (no enum at the write sites); the enum must list every value it can hold."""
    from aeos.models.routing import ModelRoute
    from aeos.routing import seed

    seed.run(db)
    db.commit()
    rows = db.query(ModelRoute).all()
    assert rows
    assert {r.status for r in rows} <= {m.value for m in enums.RouteStatus}
    assert {r.availability for r in rows} <= {m.value for m in enums.RouteAvailability}


def test_a_value_missing_from_the_fixture_is_detected():
    """Bite: the near miss (fixture equal) is green above; a fixture that lost TRIAL is red with the diff message."""
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["route.status"].remove("TRIAL")
    assert fixture != backend_status_values()
