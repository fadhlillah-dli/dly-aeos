"""OWASP ASVS trace (master §46): a pinned ASVS version, a risk-based control selection and a test id per
selected control. Unmapped controls are NOT_PROVEN — never presented as met. The selection is complete: every
requirement of the pinned 4.0.3 catalog is either selected or not selected with a reason, chapter by chapter
(review W6-130: whole chapters used to be dropped without the reason the file promised)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
# A verification trace (contracts/, high-impact), not worker policy: policy/*.yaml is mandatory context in
# every worker context pack (aeos.knowledge.packs) and this table would only cost tokens there.
SELECTION = ROOT / "contracts" / "security" / "asvs-selection.yaml"
CATALOG = ROOT / "contracts" / "security" / "asvs-4.0.3-catalog.yaml"
_ID = re.compile(r"^V\d{1,2}\.\d{1,2}\.\d{1,2}$")
CHAPTERS = [f"V{n}" for n in range(1, 15)]


def _load() -> dict:
    return yaml.safe_load(SELECTION.read_text(encoding="utf-8"))


def _catalog() -> dict:
    return yaml.safe_load(CATALOG.read_text(encoding="utf-8"))


def _catalog_ids() -> dict[str, str]:
    return {rid: chapter for chapter, body in _catalog()["chapters"].items() for rid in body["requirements"]}


def _not_selected_ids(data: dict) -> list[str]:
    out: list[str] = []
    for entry in data.get("not_selected") or []:
        out += list(entry.get("ids") or []) + ([entry["id"]] if entry.get("id") else [])
    return out


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
    assert str(_catalog()["asvs_version"]) == str(data["asvs_version"]), "selection and catalog pin one version"
    ids = [c["id"] for c in data["selected"]] + _not_selected_ids(data)
    assert all(_ID.match(i) for i in ids), [i for i in ids if not _ID.match(i)]
    assert len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})
    assert all(c.get("reason") for c in data.get("not_selected") or []), "every exclusion states its reason"


def test_the_catalog_is_the_complete_4_0_3_requirement_list():
    """14 chapters and 286 requirement ids (deleted ones keep their id and are flagged)."""
    cat = _catalog()
    assert list(cat["chapters"]) == CHAPTERS
    ids = _catalog_ids()
    assert len(ids) == 286 and all(_ID.match(i) for i in ids)
    for rid in ids:  # every id sits in the chapter its number names
        assert rid.split(".")[0] == ids[rid], rid
    deleted = cat["deleted"]
    assert set(deleted) <= set(ids) and all(cat["chapters"][ids[d]]["requirements"][d].startswith("[DELETED]")
                                            for d in deleted)


def test_every_requirement_is_selected_or_excluded_with_a_reason_chapter_by_chapter():
    data = _load()
    catalog, deleted = _catalog_ids(), set(_catalog()["deleted"])
    selected = [c["id"] for c in data["selected"]]
    excluded = _not_selected_ids(data)
    unknown = sorted(set(selected + excluded) - set(catalog))
    assert not unknown, f"ids not in the ASVS 4.0.3 catalog: {unknown}"
    assert not (set(selected) & deleted), "a requirement deleted by ASVS itself cannot be selected"
    missing = sorted(set(catalog) - deleted - set(selected) - set(excluded))
    assert not missing, f"silently dropped (neither selected nor not_selected with a reason): {missing}"
    for chapter in CHAPTERS:  # every chapter is covered: its controls are selected, or excluded with a reason
        in_chapter = [i for i, c in catalog.items() if c == chapter and i not in deleted]
        assert in_chapter and all(i in selected or i in excluded for i in in_chapter), chapter


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


def test_the_selection_claims_no_per_project_coverage():
    """Applications AEOS builds get no ASVS level until the owner decides one per classification/risk: the file
    says so instead of implying coverage (evidence honesty)."""
    text = SELECTION.read_text(encoding="utf-8")
    assert _load()["application"] == "dly-aeos-control-plane"
    assert "UNDECIDED" in text and "no per-project selection is" in text
