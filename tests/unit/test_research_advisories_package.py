"""Advisory matching narrows a multi-package repository to the candidate's package (research/advisories.py)."""

from __future__ import annotations

from aeos.proposals.review import repo_name
from aeos.research import advisories


def _up(*items: tuple[str, list[tuple[str | None, str]]]) -> dict:
    return {"advisories": {"status": "OK", "checked_at": "2026-10-26T00:00:00+00:00", "source": "test", "items": [
        {"ghsa_id": gid, "vulnerabilities": [{"package": pkg, "vulnerable_version_range": rng} for pkg, rng in vulns]}
        for gid, vulns in items]}}


MONOREPO = _up(
    ("GHSA-lg", [("langgraph-sdk", ">= 0.4.0, < 0.4.6"), ("langgraph", ">= 1.2.3, < 1.2.14")]),
    ("GHSA-cp", [("langgraph-checkpoint", "<= 4.1.0")]),
    ("GHSA-cli", [("langgraph-cli", ">= 0.4.15, <= 0.4.31")]),
)


def test_monorepo_advisories_of_other_packages_are_skipped():
    s = advisories.summary(MONOREPO, "1.2.14", package="langgraph")
    assert s["status"] == "NOT_AFFECTED" and s["package"] == "langgraph"
    assert set(s["other_packages_skipped"]) == {"GHSA-cp", "GHSA-cli"} and s["count"] == 1
    assert advisories.summary(MONOREPO, "1.2.13", package="langgraph")["affected"] == ["GHSA-lg"]


def test_a_package_named_in_the_pin_wins_over_the_repository_name():
    s = advisories.summary(MONOREPO, "langgraph-checkpoint==4.1.0", package="langgraph")
    assert s["status"] == "AFFECTED" and s["affected"] == ["GHSA-cp"]


def test_go_module_paths_match_on_their_last_segment():
    up = _up(("GHSA-th", [("github.com/stacklok/toolhive", ">= 0.41.0, < 0.56.0")]))
    assert advisories.summary(up, "v0.51.4", package="toolhive")["status"] == "AFFECTED"
    assert advisories.summary(up, "v0.56.0", package="toolhive")["status"] == "NOT_AFFECTED"


def test_unknown_naming_scheme_keeps_every_advisory():
    """Repository 'opencode' publishes npm 'opencode-ai': no advisory names the repository, so nothing is skipped."""
    up = _up(("GHSA-oc", [("opencode-ai", "< 1.2.0")]))
    s = advisories.summary(up, "1.1.0", package="opencode")
    assert s["status"] == "AFFECTED" and "other_packages_skipped" not in s


def test_a_vulnerability_without_a_package_name_still_counts():
    up = _up(("GHSA-a", [("tool", "< 2.0")]), ("GHSA-b", [(None, "< 3.0")]))
    s = advisories.summary(up, "2.5", package="tool")
    assert s["affected"] == ["GHSA-b"]


def test_without_a_package_the_old_conservative_rule_applies():
    s = advisories.summary(MONOREPO, "1.2.14")
    assert s["status"] == "AFFECTED" and "GHSA-cp" in s["affected"]


def test_split_pin_and_repo_name():
    assert advisories.split_pin("langgraph==1.2.14") == ("langgraph", "1.2.14")
    assert advisories.split_pin("@scope/pkg@1.2.3") == ("@scope/pkg", "1.2.3")
    assert advisories.split_pin("v0.56.0") == (None, "v0.56.0")
    assert advisories.split_pin("3b7254a54d3291c559b39b8ff7014b040d2650b0") == (None,
                                                                                "3b7254a54d3291c559b39b8ff7014b040d2650b0")
    assert repo_name("github:langchain-ai/langgraph") == "langgraph" and repo_name(None) is None
