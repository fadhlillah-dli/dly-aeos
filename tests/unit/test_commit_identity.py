"""Code-host commit identities (owner rule 2026-10-26, AGENTS.md invariant 11): GitHub activity uses the owner's
GitHub identity, Bitbucket activity the owner's company identity (different addresses, both only in aeos.yaml).
Identity selection per provider, the github-only legacy fallback, fail-closed validation, the commit checks the
broker runs before a push, and how the runtime marks a candidate (not) pushable. Placeholder addresses only."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from aeos.core import commit_identity as cid
from aeos.core.config import Settings
from aeos.core.enums import Domain
from aeos.core.paths import DataLayout
from aeos.models.portfolio import Project, RepositoryReference
from aeos.models.workforce import WorkerRun
from aeos.worker_runtime import executor, workspace
from tests.unit.test_workforce_support import init_git_repo

GH = {"name": "Owner Name", "email": "12345+owner@users.noreply.github.com"}
BB = {"name": "Owner Name", "email": "owner@company.example.com"}


def _settings(settings: Settings, **update) -> Settings:
    return settings.model_copy(update={"commit_identities": {}, "commit_author_name": None,
                                       "commit_author_email": None, **update})


def test_each_provider_gets_its_own_identity(settings):
    s = _settings(settings, commit_identities={"github": GH, "Bitbucket": BB})
    gh, bb = cid.resolve(s, "github"), cid.resolve(s, "bitbucket")
    assert gh.configured and gh.email == GH["email"] and gh.source == "commit_identities.github"
    assert bb.configured and bb.email == BB["email"] and bb.source == "commit_identities.bitbucket"
    assert GH["email"] not in repr(gh) and BB["email"] not in repr(bb)  # never in a log line by accident


def test_legacy_keys_are_a_fallback_for_github_only(settings):
    s = _settings(settings, commit_author_name=GH["name"], commit_author_email=GH["email"])
    assert cid.resolve(s, "github").configured
    assert cid.resolve(s, "github").source == cid.LEGACY_KEYS
    bb = cid.resolve(s, "bitbucket")
    assert bb.status == cid.MISSING and not bb.configured  # the company identity never falls back
    # commit_identities.github wins over the legacy keys
    both = _settings(settings, commit_identities={"github": BB}, commit_author_name="x", commit_author_email=GH["email"])
    assert cid.resolve(both, "github").email == BB["email"]


@pytest.mark.parametrize(("entry", "fragment"), [
    ("owner@example.com", "mapping"),
    ({"name": "Owner"}, "email is missing"),
    ({"email": "owner@example.com"}, "name is missing"),
    ({"name": "Owner", "email": "not-an-address"}, "not an e-mail"),
    ({"name": "Claude", "email": "noreply@anthropic.com"}, "no AI or bot"),
    ({"name": "Owner <x>", "email": "owner@example.com"}, "plain name"),
])
def test_an_invalid_identity_is_invalid_never_replaced(settings, entry, fragment):
    s = _settings(settings, commit_identities={"github": entry}, commit_author_name=GH["name"],
                  commit_author_email=GH["email"])
    ident = cid.resolve(s, "github")
    assert ident.status == cid.INVALID and not ident.configured and fragment in ident.problem
    assert "commit_identities.github" in cid.missing_message(ident)


def test_missing_message_names_the_key_never_an_address(settings):
    msg = cid.missing_message(cid.resolve(_settings(settings), "bitbucket"))
    assert "commit_identities.bitbucket" in msg and "@" not in msg


@pytest.mark.parametrize(("domain", "repo", "expected"), [
    ("PERSONAL", None, "github"), ("PERSONAL", "GITHUB", "github"), ("PERSONAL", "BITBUCKET", "bitbucket"),
    ("OFFICE", None, "bitbucket"), ("OFFICE", "GITHUB", "bitbucket"),
])
def test_provider_follows_the_domain_and_the_registered_repository(domain, repo, expected):
    assert cid.provider_for(domain, repo) == expected


def test_runtime_selects_the_identity_of_the_projects_code_host(settings, db):
    s = _settings(settings, commit_identities={"github": GH, "bitbucket": BB})
    personal = Project(project_key="p-gh", name="gh", domain="PERSONAL")
    office = Project(project_key="o-bb", name="bb", domain="OFFICE")
    bb_personal = Project(project_key="p-bb", name="bbp", domain="PERSONAL")
    db.add_all([personal, office, bb_personal])
    db.flush()
    db.add(RepositoryReference(project_id=bb_personal.id, provider="BITBUCKET", full_name="ws/repo",
                               local_path_ref="C:/nowhere", default_branch="main"))
    db.flush()
    assert executor.code_host_provider(db, personal) == "github"
    assert executor.code_host_provider(db, office) == "bitbucket"
    assert executor.code_host_provider(db, bb_personal) == "bitbucket"
    rec = executor._commit_identity_record(db, s, office)
    assert (rec.pop("_name"), rec.pop("_email")) == (BB["name"], BB["email"])
    assert rec["provider"] == "bitbucket" and rec["pushable"] is True and BB["email"] not in str(rec)
    rec = executor._commit_identity_record(db, s, personal)
    assert rec.pop("_email") == GH["email"] and rec["config_key"] == "commit_identities.github"
    # no company identity configured: the OFFICE candidate is committed locally by the runtime, never pushable
    only_gh = _settings(settings, commit_identities={"github": GH})
    rec = executor._commit_identity_record(db, only_gh, office)
    assert rec.pop("_email") == workspace.RUNTIME_IDENTITY[1]
    assert rec["pushable"] is False and "commit_identities.bitbucket" in rec["not_pushable_reason"]


# ----------------------------------------------------------------------------- commit objects + checks
RAW = ("tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904\n"
       "parent 1111111111111111111111111111111111111111\n"
       "author Owner Name <owner@example.com> 1761450000 +0700\n"
       "committer Owner Name <owner@example.com> 1761450000 +0700\n"
       "gpgsig -----BEGIN PGP SIGNATURE-----\n"
       " author Forged <forged@example.com> 1 +0000\n"
       " -----END PGP SIGNATURE-----\n"
       "\n"
       "fix: x\n\nbody\n")


def test_commit_objects_are_parsed_from_headers_only():
    c = cid.parse_commit_object("a" * 40, RAW)
    assert (c.author_email, c.committer_email) == ("owner@example.com", "owner@example.com")
    assert c.message == "fix: x\n\nbody\n"


def test_cat_file_batch_honours_object_sizes():
    forged = "fix\n\nb" * 3 + "\n" + ("b" * 40) + " commit 10\n"  # content that looks like a batch header
    obj = RAW.replace("fix: x\n\nbody\n", forged).encode()
    data = f"{'a' * 40} commit {len(obj)}\n".encode() + obj + b"\n"
    data += f"{'c' * 40} commit {len(RAW.encode())}\n".encode() + RAW.encode() + b"\n"
    commits = cid.parse_cat_file_batch(data)
    assert [c.sha for c in commits] == ["a" * 40, "c" * 40]
    with pytest.raises(ValueError):
        cid.parse_cat_file_batch(f"{'a' * 40} missing\n".encode())


def _rec(sha="a" * 40, *, author="owner@example.com", committer="owner@example.com", name="Owner", msg="fix: x\n"):
    return cid.CommitRecord(sha, name, author, name, committer, msg)


def test_commit_problems_check_author_committer_identity_and_message():
    want = "owner@example.com"
    assert cid.commit_problems([_rec()], provider="github", expected_email=want.upper()) == []
    (p,) = cid.commit_problems([_rec(committer="other@example.com")], provider="github", expected_email=want)
    assert p["code"] == cid.CODE_IDENTITY_MISMATCH and p["detail"].startswith("committer e-mail")
    assert "other@example.com" not in str(p) and "commit_identities.github" in p["detail"]
    probs = cid.commit_problems([_rec(msg="fix\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n")],
                                provider="github", expected_email=want)
    assert [x["code"] for x in probs] == [cid.CODE_ATTRIBUTION] and cid.refusal_code(probs) == cid.CODE_ATTRIBUTION
    probs = cid.commit_problems([_rec(name="Claude", author="noreply@anthropic.com")], provider="bitbucket",
                                expected_email=want)
    assert {x["code"] for x in probs} == {cid.CODE_IDENTITY_MISMATCH, cid.CODE_ATTRIBUTION}
    assert cid.commit_problems([_rec()], provider="github", expected_email="")[0]["code"] == cid.CODE_IDENTITY_MISMATCH
    assert cid.commit_problems([_rec(msg="fix(claude_bridge): argv\n")], provider="github", expected_email=want) == []


# ----------------------------------------------------------------------------- the candidate in the workspace
def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def ws(tmp_path):
    src = tmp_path / "repo"
    init_git_repo(src, {"src/a.py": "a = 1\n"})
    w = workspace.create_workspace(DataLayout(tmp_path / "data"), domain=Domain.PERSONAL, project_key="proj",
                                   run_id="run-0002-bbbb", task_key="Task B", source_repo=src, base_commit=None,
                                   isolated=True)
    yield w
    workspace.cleanup_workspace(w)


def test_runtime_commit_message_with_attribution_is_refused(ws):
    (ws.repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
    workspace.collect_changes(ws)
    with pytest.raises(workspace.WorkspaceError, match="AI attribution"):
        workspace.commit_candidate(ws, "aeos: candidate", identity=("Owner", "owner@example.com"),
                                   trailers={"AEOS-Route": "worker.test-free"})
    head = workspace.commit_candidate(ws, "aeos: candidate", identity=("Owner", "owner@example.com"))
    assert head and _git(ws.repo, "log", "-1", "--format=%ae|%ce") == "owner@example.com|owner@example.com"


def _prep(ws, email="owner@example.com"):
    return SimpleNamespace(ws=ws, commit_identity=("Owner", email))


def _run(pushable=True):
    return WorkerRun(project_id="p", domain="PERSONAL", caps={"commit_identity": {
        "provider": "github", "pushable": pushable, "not_pushable_reason": None}})


def test_a_worker_made_commit_with_an_ai_identity_marks_the_candidate_not_pushable(ws):
    (ws.repo / "notes.md").write_text("n\n", encoding="utf-8")
    _git(ws.repo, "add", "-A")
    subprocess.run(["git", "-c", "user.name=Claude", "-c", "user.email=noreply@anthropic.com", "-c",
                    "commit.gpgsign=false", "commit", "-q", "-m", "wip"], cwd=ws.repo, check=True)
    (ws.repo / "src" / "a.py").write_text("a = 3\n", encoding="utf-8")
    workspace.collect_changes(ws)
    head = workspace.commit_candidate(ws, "aeos: candidate", identity=("Owner", "owner@example.com"))
    commits = workspace.candidate_commits(ws, head)
    assert len(commits) == 2 and commits[0].sha == head
    run = _run()
    executor._mark_pushable(run, _prep(ws), head)
    rec = run.caps["commit_identity"]
    assert rec["pushable"] is False and rec["not_pushable_reason"].startswith(cid.CODE_ATTRIBUTION)
    assert "noreply@anthropic.com" not in str(rec)


def test_the_runtime_commit_alone_stays_pushable(ws):
    (ws.repo / "src" / "a.py").write_text("a = 4\n", encoding="utf-8")
    workspace.collect_changes(ws)
    head = workspace.commit_candidate(ws, "aeos: candidate", identity=("Owner", "owner@example.com"))
    run = _run()
    executor._mark_pushable(run, _prep(ws), head)
    assert run.caps["commit_identity"]["pushable"] is True
    other = _run()
    executor._mark_pushable(other, _prep(ws, email="someone-else@example.com"), head)
    assert other.caps["commit_identity"]["pushable"] is False
    assert other.caps["commit_identity"]["not_pushable_reason"].startswith(cid.CODE_IDENTITY_MISMATCH)
