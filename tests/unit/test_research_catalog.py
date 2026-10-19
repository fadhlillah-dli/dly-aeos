"""Research registry seed vs the master itself (§45 catalog, §46 practices, §47 comparison, §52
S01-S64). §50 rows: "Nama repo ambigu atau sumber tidak terverifikasi -> UNVERIFIED dan blocked
admission; tidak mengarang URL/license/version", "Katalog blueprint ... -> tidak otomatis
INSTALLED/ENABLED/IN_USE"."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import func, select

from aeos.core.paths import repo_root
from aeos.models.inventory import ComponentReference
from aeos.models.research import ResearchEntry
from aeos.research import catalog
from aeos.research import service as research
from aeos.research.identity import IdentityError, canonical_identity, github_repo

MASTER = (repo_root() / "docs" / "architecture" / "DLY_AEOS_Master_Architecture_Final.md").read_text(encoding="utf-8")
URL_RE = re.compile(r"https?://[^\s;|)]+")


def _section(num: str) -> str:
    start = MASTER.index(f"\n## {num}.")
    nxt = MASTER.find("\n## ", start + 5)
    return MASTER[start:] if nxt == -1 else MASTER[start:nxt]


def _table_first_col(section: str) -> list[str]:
    rows = []
    for line in section.splitlines():
        if not line.startswith("| ") or line.startswith("|---"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows.append(cells[0])
    return rows[1:]  # drop header


def _clean(url: str) -> str:
    return url.rstrip(".,")


def test_every_section_45_row_is_in_the_catalog():
    rows = _table_first_col(_section("45"))
    groups = {e.group for e in catalog.ENTRIES}
    missing = [r for r in rows if r not in groups]
    assert not missing, f"§45 rows missing from the research catalog: {missing}"
    assert len(rows) >= 40


def test_every_section_45_url_is_seeded():
    urls = {_clean(u) for u in URL_RE.findall(_section("45"))}
    seeded = {u for e in catalog.ENTRIES for u in e.urls}
    assert urls and not (urls - seeded), urls - seeded


S47_ROWS = {
    "Agent Scaling, arXiv v3 dan repo penulis": "research:agent-scaling",
    "Anthropic Managed Agents, 8 April 2026": "research:managed-agents",
    "Claude Code Agent Teams docs": "tool:claude-native-teams",
    "Paperclip": "method:paperclip",
    "Agency Agents": "method:agency-agents",
    "gstack / Superpowers": "method:gstack",
    "MetaGPT / ChatDev / CrewAI": "method:metagpt",
    "Symphony": "method:symphony",
    "Ralph": "method:ralph",
    "Gas Town": "tool:gastown",
    "Ruflo": "tool:ruflo",
    "Forum": "research:forum-discussions",
}


def test_every_section_47_comparison_row_is_covered():
    sec = _section("47")
    table = sec[sec.index("| Sumber |"):]
    rows = _table_first_col(table.split("\n\n")[0])
    keys = {e.key for e in catalog.ENTRIES}
    assert set(rows) == set(S47_ROWS), f"§47 table changed: {set(rows) ^ set(S47_ROWS)}"
    for row, key in S47_ROWS.items():
        assert key in keys, row
        entry = next(e for e in catalog.ENTRIES if e.key == key)
        assert "§47" in entry.sections and entry.claims, key
    assert next(e for e in catalog.ENTRIES if e.key == "method:superpowers").claims
    assert next(e for e in catalog.ENTRIES if e.key == "method:chatdev").claims


def test_section_46_practices_are_present():
    keys = {e.key for e in catalog.ENTRIES}
    practices = ("practice:google-sre-slo", "practice:stripe-idempotency", "practice:owasp-asvs",
                 "practice:anthropic-long-running-harness", "practice:repo-organization")
    for k in practices:
        assert k in keys
    # §46 "Rujukan tidak berarti afiliasi, tenaga kerja asli, atau jaminan mutu setara perusahaan tersebut."
    for k in practices:
        e = next(x for x in catalog.ENTRIES if x.key == k)
        assert "Rujukan tidak berarti afiliasi, tenaga kerja asli, atau jaminan mutu setara" in e.limitations, k
    assert [e.key for e in catalog.ENTRIES if e.category == "practice"] == list(practices)


def test_9router_recheck_is_documented_without_adopting_the_claims():
    """§47 18 Oct re-check: OAuth/fallback documented; unlimited/never-hit-limits/savings not adopted."""
    e = next(x for x in catalog.ENTRIES if x.key == "tool:9router")
    assert any("OAuth dan fitur fallback" in c and "18 Oktober" in c for c in e.claims)
    assert "unlimited/never hit limits/savings TIDAK diadopsi" in e.limitations
    assert "ketentuan Anthropic dan pengujian batas tetap acuan" in e.limitations


def test_paper_and_community_version_limits_are_kept_and_never_predictions():
    """§47: arXiv v3, repo v2.2.x and dated community discussions stay statements, never predictions/results."""
    e = next(x for x in catalog.ENTRIES if x.key == "research:agent-scaling")
    text = " ".join(e.claims) + " " + e.limitations
    assert "arXiv v3" in text and "v2.2.x" in text and "tidak dijadikan prediksi" in text
    assert e.evidence == "E1" and e.lifecycle == "REFERENCE_ONLY"
    forum = next(x for x in catalog.ENTRIES if x.key == "research:forum-discussions")
    assert forum.evidence == "E0" and "tidak dipromosikan menjadi hasil controlled" in forum.limitations
    refs = catalog.REF_BY_ID
    assert (refs["S62"].source_date, refs["S63"].source_date, refs["S41"].source_date) == \
        ("2026-04-22", "2026-04-03", "2026-10-17")
    assert all(refs[s].evidence == "E0" for s in ("S41", "S42", "S43", "S61", "S62", "S63"))


def test_references_S01_to_S64_match_master_exactly():
    sec = _section("52")
    master_refs: dict[str, list[str]] = {}
    for line in sec.splitlines():
        m = re.match(r"^\| (S\d{2}) \| (.+) \|$", line)
        if m:
            master_refs[m.group(1)] = [_clean(u) for u in URL_RE.findall(m.group(2))]
    assert sorted(master_refs) == [f"S{i:02d}" for i in range(1, 65)]
    ours = {r.sid: list(r.urls) for r in catalog.REFERENCES}
    assert ours == master_refs


def test_forum_references_are_e0_official_e1():
    for r in catalog.REFERENCES:
        forum = "reddit.com" in r.urls[0] or "news.ycombinator.com" in r.urls[0]
        assert r.evidence == ("E0" if forum else "E1"), r.sid
        assert (r.source_kind == "FORUM") == forum


def test_no_catalog_url_is_invented():
    """Every URL in the catalog appears verbatim in the master (no guessed links)."""
    all_urls = {u for e in catalog.ENTRIES for u in e.urls} | {u for r in catalog.REFERENCES for u in r.urls}
    invented = [u for u in all_urls if u not in MASTER]
    assert not invented, invented


def test_unresolved_identities_have_no_url():
    for key in ("tool:ddorchestrator", "tool:codebase-memory-mcp", "tool:goose", "tool:pi", "tool:openhands",
                "tool:deep-agents", "tool:docker-mcp-gateway", "tool:infisical"):
        e = next(x for x in catalog.ENTRIES if x.key == key)
        assert e.identity_status == "UNVERIFIED" and not e.urls, key
    assert all(not e.urls for e in catalog.ENTRIES if e.identity_status == "UNVERIFIED")


def test_seed_is_idempotent_and_design_labelled(db):
    first = research.seed_registry(db, platform_repo_slug="fadhlillah-dli/dly-aeos")
    db.commit()
    total = db.scalar(select(func.count()).select_from(ResearchEntry))
    assert first["created"] == total == len(catalog.ENTRIES) + 64
    second = research.seed_registry(db, platform_repo_slug="fadhlillah-dli/dly-aeos")
    db.commit()
    assert second["created"] == 0 and db.scalar(select(func.count()).select_from(ResearchEntry)) == total
    rows = list(db.scalars(select(ResearchEntry)))
    assert all(r.mode == "DESIGN" and r.checked_at is None and r.upstream_status == "NOT_CHECKED" for r in rows)
    assert all(r.lifecycle != "ENABLED" for r in rows)  # §38/§47: research stage is not installation/use
    unverified = [r for r in rows if r.identity_status == "UNVERIFIED"]
    assert unverified and all(r.urls == [] for r in unverified)
    dd = next(r for r in rows if r.entry_key == "tool:ddorchestrator")
    assert dd.identity == "name:ddorchestrator" and dd.lifecycle == "DISCOVERED"
    refs = [r for r in rows if r.category == research.REFERENCE_CATEGORY]
    assert len(refs) == 64 and all(r.lifecycle == "REFERENCE_ONLY" for r in refs)
    assert all(r.research_date == "2026-10-17/18" for r in rows)
    s41 = next(r for r in refs if r.name == "S41")
    assert s41.evidence_level == "E0" and s41.release_date == "2026-10-17"
    platform = next(r for r in rows if r.entry_key == "tool:dly-aeos")
    assert platform.urls == ["https://github.com/fadhlillah-dli/dly-aeos"] and platform.identity_status == "CONFIGURED"
    oc = next(r for r in rows if r.entry_key == "tool:opencode")
    assert oc.lifecycle == "SOURCE_VERIFIED" and oc.identity == "github:anomalyco/opencode"
    assert next(r for r in rows if r.entry_key == "method:spec-kit").lifecycle == "REFERENCE_ONLY"


def test_seed_keeps_live_fields(db):
    research.seed_registry(db)
    e = db.scalar(select(ResearchEntry).where(ResearchEntry.entry_key == "tool:opencode"))
    e.lifecycle = "DISCOVERED"
    e.identity_status = "CHANGED"
    db.commit()
    research.seed_registry(db)
    db.commit()
    db.refresh(e)
    assert e.lifecycle == "DISCOVERED" and e.identity_status == "CHANGED"


def test_seed_links_inventory_components_when_names_match(db):
    db.add(ComponentReference(component_id="opencode", kind="worker-engine", name="OpenCode"))
    db.add(ComponentReference(component_id="vault", kind="secret", name="Something",
                              source_url="https://openbao.org/docs/auth/approle/"))
    db.commit()
    out = research.seed_registry(db)
    db.commit()
    assert out["linked_components"] >= 2
    oc = db.scalar(select(ResearchEntry).where(ResearchEntry.entry_key == "tool:opencode"))
    assert oc.component_id == "opencode"
    ob = db.scalar(select(ResearchEntry).where(ResearchEntry.entry_key == "tool:openbao"))
    assert ob.component_id == "vault"
    assert all(r.component_id is None for r in db.scalars(select(ResearchEntry).where(
        ResearchEntry.category == research.REFERENCE_CATEGORY)))


def test_list_filters(db):
    research.seed_registry(db)
    db.commit()
    refs = research.list_entries(db, domain="PERSONAL", kind="reference")
    assert [r["name"] for r in refs][:3] == ["S01", "S02", "S03"] and len(refs) == 64
    e0 = research.list_entries(db, domain="PERSONAL", evidence="E0", kind="reference")
    assert {r["name"] for r in e0} == {"S41", "S42", "S43", "S44", "S61", "S62", "S63", "S64"}
    q = research.list_entries(db, domain="PERSONAL", q="toolhive")
    assert any(r["entry_key"] == "tool:toolhive" for r in q)
    sv = research.list_entries(db, domain="PERSONAL", lifecycle="SOURCE_VERIFIED", kind="candidate")
    assert sv and all(r["lifecycle"] == "SOURCE_VERIFIED" for r in sv)
    assert all(r["freshness"] == "NOT_CHECKED" and r["installed_or_active"] == "NOT_IMPLIED" for r in sv)


@pytest.mark.parametrize("ref,expected", [
    ("https://github.com/Owner/Repo", "github:owner/repo"),
    ("https://github.com/owner/repo.git", "github:owner/repo"),
    ("git@github.com:Owner/Repo.git", "github:owner/repo"),
    ("ssh://git@github.com/owner/repo", "github:owner/repo"),
    ("github.com/owner/repo/tree/main/src", "github:owner/repo"),
    ("http://www.github.com/owner/repo/", "github:owner/repo"),
    ("https://github.com/owner/repo?tab=readme-ov-file#install", "github:owner/repo"),
    ("https://gitlab.com/group/sub/repo/-/tree/main", "gitlab:group/sub/repo"),
    ("https://bitbucket.org/team/app.git", "bitbucket:team/app"),
    ("https://docs.temporal.io/", "url:docs.temporal.io"),
    ("https://code.claude.com/docs/en/mcp", "url:code.claude.com/docs/en/mcp"),
    ("OpenCode", "name:opencode"),
    ("  Agent Orchestrator ", "name:agent-orchestrator"),
])
def test_canonical_identity_forms(ref, expected):
    assert canonical_identity(ref) == expected


@pytest.mark.parametrize("bad", ["https://user:pass@github.com/o/r", "", "ftp://example.com/x",
                                 "https://github.com/orgs/someorg"])
def test_canonical_identity_rejects(bad):
    with pytest.raises(IdentityError):
        canonical_identity(bad)


def test_github_repo_parse():
    assert github_repo("github:a/b") == ("a", "b")
    assert github_repo("github:a") is None and github_repo("url:x/y") is None
