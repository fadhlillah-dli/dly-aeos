"""Structured worker message aeos-report (§19 refs/findings/evidence/blocker/requested decision, §21 blocker)."""

from __future__ import annotations

import json

import pytest

from aeos.core.paths import repo_root
from aeos.worker_runtime import report, workspace

VALID = {"schema_version": "1", "status": "BLOCKED", "task_key": "T1", "run_id": "r1", "plan_version_hash": "sha256:p",
         "summary": "needs more scope", "findings": [{"title": "x", "detail": "y", "evidence_refs": ["a.py"]}],
         "evidence_refs": ["tests/test_a.py::test_x"], "blocker": {"code": "SCOPE_GROWTH", "detail": "models"},
         "requested_decision": {"kind": "POLICY", "title": "extend", "reason": "because",
                                "requested_operations": ["extend_scope"]}}


def test_published_schema_equals_the_code_schema():
    published = json.loads((repo_root() / "contracts" / "worker" / "aeos-report.schema.json").read_text(encoding="utf-8"))
    assert published == report.WORKER_REPORT_SCHEMA


def test_valid_report_carries_refs_findings_evidence_blocker_and_decision():
    assert report.validate(VALID) == []
    assert report.is_blocker(VALID)
    assert report.ref_mismatches(VALID, task_key="T1", run_id="r1", plan_version_hash="sha256:p") == []
    assert report.ref_mismatches(VALID, task_key="T2", run_id="r1", plan_version_hash="sha256:p")


@pytest.mark.parametrize("bad", [
    {**VALID, "approve": True},                                      # no extra authority fields
    {**VALID, "status": "DONE"},                                     # a worker never reports DONE
    {**VALID, "requested_decision": {"kind": "APPROVAL", "title": "x", "reason": "y"}},  # never a human-only kind
    {**VALID, "blocker": {"code": "SCOPE_GROWTH"}},
    {"status": "BLOCKED"},
    {**VALID, "summary": "x" * 5000},
])
def test_invalid_reports_are_rejected(bad):
    assert report.validate(bad)


def test_decision_fields_are_redacted_and_bounded():
    secret = {**VALID, "requested_decision": {"kind": "BUDGET", "title": "raise", "reason": "ghp_" + "a" * 36}}
    f = report.decision_request_fields(secret, task_key="T1")
    assert f["kind"] == "BUDGET" and "ghp_" not in f["reason"] and f["title"].startswith("T1: ")


def test_report_is_taken_out_of_the_candidate(tmp_path, settings):
    from aeos.core.enums import Domain
    from tests.unit.test_workforce_support import init_git_repo

    src = tmp_path / "src"
    init_git_repo(src, {"a.txt": "a\n"})
    ws = workspace.create_workspace(settings.layout, domain=Domain.PERSONAL, project_key="p", run_id="run-report-1",
                                    task_key="t", source_repo=src, base_commit=None, isolated=True)
    (ws.repo / ".aeos").mkdir()
    (ws.repo / ".aeos" / "report.json").write_text(json.dumps(VALID), encoding="utf-8")
    data, info = workspace.take_worker_report(ws)
    assert data == VALID and info["removed_from_candidate"] is True and not (ws.repo / ".aeos").exists()
    assert workspace.collect_changes(ws)["files"] == 0


def test_oversized_or_non_json_report_is_rejected(tmp_path, settings):
    from aeos.core.enums import Domain
    from tests.unit.test_workforce_support import init_git_repo

    src = tmp_path / "src"
    init_git_repo(src, {"a.txt": "a\n"})
    ws = workspace.create_workspace(settings.layout, domain=Domain.PERSONAL, project_key="p", run_id="run-report-2",
                                    task_key="t", source_repo=src, base_commit=None, isolated=True)
    (ws.repo / ".aeos").mkdir()
    (ws.repo / ".aeos" / "report.json").write_bytes(b"{" + b" " * (workspace.MAX_REPORT_BYTES + 10))
    data, info = workspace.take_worker_report(ws)
    assert data is None and "larger" in info["rejected"]
    (ws.repo / ".aeos").mkdir(exist_ok=True)
    (ws.repo / ".aeos" / "report.json").write_text("not json", encoding="utf-8")
    data, info = workspace.take_worker_report(ws)
    assert data is None and "JSON" in info["rejected"]
