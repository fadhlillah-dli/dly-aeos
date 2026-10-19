"""OWASP ASVS trace (master §46): a pinned ASVS version, a risk-based control selection and a test id per
selected control. Unmapped controls are NOT_PROVEN — never presented as met."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
# A verification trace (contracts/, high-impact), not worker policy: policy/*.yaml is mandatory context in
# every worker context pack (aeos.knowledge.packs) and this table would only cost tokens there.
SELECTION = ROOT / "contracts" / "security" / "asvs-selection.yaml"
_ID = re.compile(r"^V\d{1,2}\.\d{1,2}\.\d{1,2}$")


def _load() -> dict:
    return yaml.safe_load(SELECTION.read_text(encoding="utf-8"))


def _test_exists(node_id: str) -> bool:
    path, _, name = node_id.partition("::")
    file = ROOT / path
    if not name or not file.is_file():
        return False
    func = name.split("[", 1)[0].split("::")[-1]
    return re.search(rf"^def {re.escape(func)}\(", file.read_text(encoding="utf-8"), re.MULTILINE) is not None


def test_asvs_version_is_pinned_and_ids_are_well_formed():
    data = _load()
    assert re.fullmatch(r"\d+\.\d+\.\d+", str(data["asvs_version"])), "an exact ASVS version is pinned"
    ids = [c["id"] for c in data["selected"]] + [c["id"] for c in data.get("not_selected") or []]
    assert all(_ID.match(i) for i in ids), ids
    assert len(ids) == len(set(ids)), "a control is either selected or not selected, once"
    assert all(c.get("reason") for c in data.get("not_selected") or []), "every exclusion states its reason"


def test_every_selected_control_traces_to_existing_tests_or_is_not_proven():
    missing: list[str] = []
    for control in _load()["selected"]:
        tests = control.get("tests") or []
        if control["status"] == "TESTED":
            assert tests, f"{control['id']} TESTED without test ids"
            missing += [f"{control['id']}: {t}" for t in tests if not _test_exists(t)]
        else:
            # honest status: a selected control without tests is NOT_PROVEN, with a note why
            assert control["status"] == "NOT_PROVEN" and not tests and control.get("note"), control["id"]
    assert not missing, missing


def test_the_trace_detects_a_stale_test_id():
    assert _test_exists("tests/unit/test_identity.py::test_login_throttling")
    assert not _test_exists("tests/unit/test_identity.py::test_this_test_does_not_exist")
    assert not _test_exists("tests/unit/no_such_file.py::test_x")
