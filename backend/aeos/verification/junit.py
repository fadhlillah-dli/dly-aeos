"""JUnit XML parsing for verifier-collected test reports (§34 TEST_REPORT evidence).

Counts are derived from the ``<testcase>`` elements themselves (not from the summary attributes),
so a report whose attributes disagree with its cases is flagged instead of trusted.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET  # expat does not resolve external entities
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MAX_JUNIT_BYTES = 20 * 1024 * 1024
MESSAGE_LIMIT = 2000


class JUnitParseError(ValueError):
    pass


@dataclass
class TestCaseResult:
    __test__ = False  # not a pytest test class

    key: str  # "<dotted module[.Class]>::<name>" — comparable with collect-only node ids
    classname: str
    name: str
    outcome: str  # passed | failed | error | skipped
    time: float = 0.0
    message: str = ""


@dataclass
class JUnitReport:
    tests: int = 0
    passed: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    time: float = 0.0
    cases: list[TestCaseResult] = field(default_factory=list)
    attribute_mismatch: bool = False

    def to_dict(self, *, include_cases: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            "tests": self.tests, "passed": self.passed, "failures": self.failures, "errors": self.errors,
            "skipped": self.skipped, "time": round(self.time, 3), "attribute_mismatch": self.attribute_mismatch,
        }
        if include_cases:
            d["cases"] = [asdict(c) for c in self.cases]
        return d

    @property
    def failing_keys(self) -> list[str]:
        return [c.key for c in self.cases if c.outcome in ("failed", "error")]

    @property
    def skipped_keys(self) -> list[str]:
        return [c.key for c in self.cases if c.outcome == "skipped"]


def nodeid_to_key(nodeid: str) -> str:
    """``tests/acceptance/test_x.py::TestA::test_y[p]`` -> ``tests.acceptance.test_x.TestA::test_y[p]``.

    The parametrize suffix (``[...]``) may itself contain ``::`` or ``/`` and is kept verbatim.
    """
    bracket = nodeid.find("[")
    param = nodeid[bracket:] if bracket != -1 else ""
    parts = (nodeid[:bracket] if bracket != -1 else nodeid).split("::")
    parts[-1] = parts[-1] + param
    module = parts[0]
    if module.endswith(".py"):
        module = module[:-3]
    module = module.replace("\\", "/").replace("/", ".")
    if len(parts) == 1:
        return module
    classes = parts[1:-1]
    cls = ".".join([module, *classes]) if classes else module
    return f"{cls}::{parts[-1]}"


def _float(v: str | None) -> float:
    try:
        return float(v) if v is not None else 0.0
    except ValueError:
        return 0.0


def _int(v: str | None) -> int | None:
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def parse_junit(source: Path | bytes | str) -> JUnitReport:
    if isinstance(source, Path):
        if not source.is_file():
            raise JUnitParseError(f"junit report not found: {source.name}")
        if source.stat().st_size > MAX_JUNIT_BYTES:
            raise JUnitParseError("junit report exceeds size limit")
        data = source.read_bytes()
    elif isinstance(source, str):
        data = source.encode("utf-8")
    else:
        data = source
    if len(data) > MAX_JUNIT_BYTES:
        raise JUnitParseError("junit report exceeds size limit")
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise JUnitParseError("junit report must not contain DTD/entity declarations")
    try:
        root = ET.fromstring(data)  # noqa: S314 - size-bounded, DTD rejected above
    except ET.ParseError as exc:
        raise JUnitParseError(f"malformed junit xml: {exc}") from exc

    if root.tag == "testsuites":
        suites = list(root.iter("testsuite"))
    elif root.tag == "testsuite":
        suites = [root, *[s for s in root.iter("testsuite") if s is not root]]
    else:
        raise JUnitParseError(f"unexpected junit root element <{root.tag}>")

    report = JUnitReport()
    attr_tests = attr_fail = attr_err = attr_skip = 0
    attrs_present = False
    for suite in suites:
        t = _int(suite.get("tests"))
        if t is not None:
            attrs_present = True
            attr_tests += t
            attr_fail += _int(suite.get("failures")) or 0
            attr_err += _int(suite.get("errors")) or 0
            attr_skip += _int(suite.get("skipped")) or 0
        for case in suite.findall("testcase"):
            classname = case.get("classname") or ""
            name = case.get("name") or ""
            outcome = "passed"
            message = ""
            for tag in ("error", "failure", "skipped"):
                el = case.find(tag)
                if el is not None:
                    outcome = {"error": "error", "failure": "failed", "skipped": "skipped"}[tag]
                    message = (el.get("message") or el.text or "")[:MESSAGE_LIMIT]
                    break
            key = f"{classname}::{name}" if classname else name
            report.cases.append(TestCaseResult(key=key, classname=classname, name=name, outcome=outcome,
                                               time=_float(case.get("time")), message=message))
            report.time += _float(case.get("time"))

    report.tests = len(report.cases)
    report.failures = sum(1 for c in report.cases if c.outcome == "failed")
    report.errors = sum(1 for c in report.cases if c.outcome == "error")
    report.skipped = sum(1 for c in report.cases if c.outcome == "skipped")
    report.passed = report.tests - report.failures - report.errors - report.skipped
    if attrs_present and (attr_tests, attr_fail, attr_err, attr_skip) != (
        report.tests, report.failures, report.errors, report.skipped
    ):
        # pytest records collection errors as suite-level errors without a testcase; count them.
        extra_errors = max(0, attr_err - report.errors)
        report.errors += extra_errors
        report.attribute_mismatch = (attr_tests, attr_fail, attr_skip) != (
            report.tests, report.failures, report.skipped
        )
    return report
