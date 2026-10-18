"""JUnit parsing for TEST_REPORT evidence (§34): counts come from test cases, malformed input is refused."""

from __future__ import annotations

from pathlib import Path

import pytest

from aeos.verification.junit import JUnitParseError, nodeid_to_key, parse_junit

PYTEST_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests"><testsuite name="pytest" errors="1" failures="1" skipped="1" tests="5" time="0.5">
<testcase classname="tests.acceptance.test_auth" name="test_viewer_denied" time="0.1"/>
<testcase classname="tests.acceptance.test_auth" name="test_csrf[post]" time="0.1"/>
<testcase classname="tests.acceptance.test_auth.TestX" name="test_fail" time="0.1"><failure message="assert 1 == 2">trace</failure></testcase>
<testcase classname="tests.acceptance.test_auth" name="test_err" time="0.1"><error message="fixture boom"/></testcase>
<testcase classname="tests.acceptance.test_auth" name="test_skip" time="0.1"><skipped message="no tool"/></testcase>
</testsuite></testsuites>"""


def test_counts_and_outcomes():
    r = parse_junit(PYTEST_XML)
    assert (r.tests, r.passed, r.failures, r.errors, r.skipped) == (5, 2, 1, 1, 1)
    assert r.failing_keys == ["tests.acceptance.test_auth.TestX::test_fail", "tests.acceptance.test_auth::test_err"]
    assert r.skipped_keys == ["tests.acceptance.test_auth::test_skip"]
    assert not r.attribute_mismatch
    d = r.to_dict()
    assert d["cases"][2]["message"] == "assert 1 == 2"


def test_single_testsuite_root_and_file(tmp_path: Path):
    p = tmp_path / "r.xml"
    p.write_text('<testsuite tests="1"><testcase classname="a.b" name="t"/></testsuite>', encoding="utf-8")
    r = parse_junit(p)
    assert r.passed == 1 and r.cases[0].key == "a.b::t"


def test_attribute_mismatch_is_flagged():
    xml = '<testsuite tests="9" failures="0" errors="0" skipped="0"><testcase classname="a" name="t"/></testsuite>'
    assert parse_junit(xml).attribute_mismatch is True


def test_suite_level_errors_counted():
    xml = '<testsuite tests="1" errors="2" failures="0" skipped="0"><testcase classname="a" name="t"/></testsuite>'
    assert parse_junit(xml).errors == 2


@pytest.mark.parametrize("bad", ["<not-xml", "<html/>",
                                 '<!DOCTYPE x [<!ENTITY a "b">]><testsuite tests="0"/>'])
def test_malformed_rejected(bad):
    with pytest.raises(JUnitParseError):
        parse_junit(bad)


def test_missing_file(tmp_path: Path):
    with pytest.raises(JUnitParseError):
        parse_junit(tmp_path / "missing.xml")


def test_nodeid_keys_match_junit_keys():
    assert nodeid_to_key("tests/acceptance/test_auth.py::test_viewer_denied") == \
        "tests.acceptance.test_auth::test_viewer_denied"
    assert nodeid_to_key("tests/acceptance/test_auth.py::TestX::test_fail[a/b]") == \
        "tests.acceptance.test_auth.TestX::test_fail[a/b]"
    # parametrize ids may contain '::' (e.g. an IPv6 host) — must not be split
    assert nodeid_to_key("tests/acceptance/test_rt.py::test_bind[::]") == "tests.acceptance.test_rt::test_bind[::]"
    xml = '<testsuite tests="1"><testcase classname="tests.acceptance.test_rt" name="test_bind[::]"/></testsuite>'
    assert parse_junit(xml).cases[0].key == "tests.acceptance.test_rt::test_bind[::]"
