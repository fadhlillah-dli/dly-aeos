"""Module group H: CSRF on every write endpoint and the read-only CLI commands (master §35 "Semua
write mengikuti existing session/RBAC/CSRF/interlock", §50 "VIEWER memanggil write endpoint
langsung -> Ditolak backend"; AGENTS.md "CLI output and help strings are ASCII-only")."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from aeos.core.enums import Domain
from aeos.cost import facade as cost
from aeos.evaluation import service as evals
from aeos.proposals import service as proposals
from aeos.research import service as research
from tests.conftest import AuthedClient

WRITES = [
    ("/api/budgets", {"scope_type": "GLOBAL", "limit_usd": 5, "rationale": "csrf probe"}),
    ("/api/costs/native-estimate", None),
    ("/api/proposals", {"kind": "COMPARE", "sources": ["https://github.com/a/b"]}),
    ("/api/proposals/x/review", {}),
    ("/api/proposals/x/decision", {"action": "REJECT", "baseline_hash": "sha256:aaaaaaaa",
                                   "candidate_hash": "sha256:bbbbbbbb", "rationale": "csrf probe"}),
    ("/api/proposals/x/evidence", {"evidence_ids": ["e1"]}),
    ("/api/proposals/x/outcome", {"status": "FAILED"}),
    ("/api/proposals/x/links", {"task_ids": ["t1"]}),
    ("/api/proposals/x/trial/launch", {}),
    ("/api/proposals/x/trial/stop", {"reason": "csrf probe"}),
    ("/api/proposals/x/checkpoint", {}),
    ("/api/proposals/suggestions", {"source_kind": "hermes", "source_id": "r1"}),
    ("/api/research", {"url": "https://github.com/a/b"}),
    ("/api/research/x/check-upstream", None),
    ("/api/research/x/confirm-source", None),
    ("/api/research/x/mark-reviewed", {"note": "csrf probe note"}),
    ("/api/research/x/dependency-diff", {"base_ref": "a", "head_ref": "b"}),
    ("/api/eval/runs", {"suite_key": "eval.small-bugfix", "configuration": {}, "limits": {}}),
    ("/api/eval/scorecard", {"role": "feature-implementer"}),
]


@pytest.mark.parametrize("path,body", WRITES)
def test_module_h_writes_require_csrf(personal_client: AuthedClient, path, body):
    raw = personal_client.client  # session cookie present, CSRF header deliberately missing
    r = raw.post(path, json=body) if body is not None else raw.post(path)
    assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_FAILED", (path, r.text)
    r2 = personal_client.post(path, headers={"X-CSRF-Token": "forged"}, json=body)
    assert r2.status_code == 403 and r2.json()["error"]["code"] == "CSRF_FAILED", path


@pytest.mark.parametrize("path,body", [("/api/proposals/x/plan", {"operations_owner": "x"}),
                                       ("/api/research/x", {"maintainer": "x"})])
def test_module_h_patch_writes_require_csrf(personal_client: AuthedClient, path, body):
    r = personal_client.client.patch(path, json=body)
    assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_FAILED", (path, r.text)


def _cli(*args: str, ascii_only: bool = True):
    from aeos.cli import app

    res = CliRunner().invoke(app, list(args))
    if ascii_only:  # module output; usage errors are rendered by the shared typer/rich framework
        assert res.output.isascii(), res.output
    return res


def test_cli_read_only_commands_are_ascii_and_domain_explicit(db):
    research.seed_registry(db)
    evals.seed_suites(db)
    cost.record_usage(db, provider="claude-native", category="NATIVE_CLAUDE", source="bridge", cost_usd=1.0,
                      domain="PERSONAL")
    p = proposals.create_proposal(db, actor_user_id="owner", domain="PERSONAL", kind="COMPARE", title="Serena review",
                                  sources=["https://github.com/oraios/serena"])
    db.commit()
    pid = p.id
    db.close()

    out = _cli("cost", "summary", "--domain", "PERSONAL")
    assert out.exit_code == 0, out.output
    assert "not summed together" in out.output and "remaining quota UNKNOWN" in out.output
    assert "NATIVE_CLAUDE" in out.output and "no dollar conversion" in out.output
    out = _cli("cost", "budgets", "--domain", "PERSONAL")
    assert out.exit_code == 0 and "GLOBAL:*:MONTH" in out.output and "$0.0000" in out.output

    out = _cli("research", "list", "--domain", "PERSONAL", "--kind", "reference")
    assert out.exit_code == 0 and "64 entries" in out.output
    out = _cli("research", "list", "--domain", "PERSONAL", "--q", "ddorchestrator")
    assert "(no URL - identity UNVERIFIED)" in out.output

    out = _cli("eval", "suites")
    assert out.exit_code == 0 and out.output.count("eval.") == 8
    out = _cli("eval", "experiments")
    assert out.exit_code == 0 and len(out.output.strip().splitlines()) == 4
    out = _cli("eval", "leaderboard", "--role", "feature-implementer")
    assert out.exit_code == 0 and "BELUM DIUJI" in out.output and "NONE" in out.output

    out = _cli("proposal", "list", "--domain", "PERSONAL")
    assert out.exit_code == 0 and "Serena review" in out.output and "1 proposals." in out.output
    out = _cli("proposal", "list", "--domain", "OFFICE")
    assert out.exit_code == 0 and "0 proposals." in out.output  # Office never sees Personal rows
    out = _cli("proposal", "show", pid[:8], "--domain", "PERSONAL")
    assert out.exit_code == 0 and "not reviewed yet" in out.output
    out = _cli("proposal", "show", pid[:8], "--domain", "OFFICE")
    assert out.exit_code == 2

    out = _cli("cost", "summary", ascii_only=False)  # domain is never defaulted
    assert out.exit_code == 2 and "--domain" in out.output


def test_cli_upstream_check_denied_for_office(db):
    research.seed_registry(db)
    db.commit()
    entry = next(e for e in research.list_entries(db, domain=Domain.PERSONAL.value, q="anomalyco")
                 if e["entry_key"] == "tool:opencode")
    db.close()
    out = _cli("research", "check", entry["id"], "--domain", "OFFICE")
    assert out.exit_code == 1 and "OFFICE_RESEARCH_DENIED" in out.output
