"""A dependency's accepted change must be in the source checkout's HEAD before a dependent starts (LESSONS L32).

On 2026-10-29 a review of the first revised plans found that a dependent task became READY at its dependency's
acceptance and started from the controller checkout's HEAD, before the dependency's pull request was merged, so it
would build on a base without the code it depends on. These tests cover the git side on a real repository."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from aeos.worker_runtime import executor, facade, workspace

IDENTITY = ["-c", "user.email=test@example.invalid", "-c", "user.name=test"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *IDENTITY, *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture(name="repo")
def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    return repo


def _accepted_on_task_branch(repo: Path) -> str:
    _git(repo, "checkout", "-q", "-b", "task")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "accepted change")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return head


def test_the_checkout_head_contains_an_accepted_change_only_after_its_merge(repo):
    accepted = _accepted_on_task_branch(repo)
    assert workspace.head_contains(repo, accepted) is False  # accepted, pull request not merged yet
    assert workspace.head_contains(repo, "0" * 40) is False  # unknown to the checkout (not fetched)
    assert workspace.head_contains(repo, "not-a-commit") is False
    _git(repo, "merge", "-q", "--no-ff", "task", "-m", "Merge pull request")
    assert workspace.head_contains(repo, accepted) is True


def test_the_facade_answers_for_the_projects_checkout_and_fails_closed(repo, monkeypatch):
    session = SimpleNamespace(get=lambda model, project_id: object() if project_id == "p" else None)
    monkeypatch.setattr(executor, "resolve_repository", lambda s, project: (repo, True))
    head = _git(repo, "rev-parse", "HEAD")
    assert facade.source_head_contains(session, "p", head) == (True, "")
    ok, why = facade.source_head_contains(session, "p", _accepted_on_task_branch(repo))
    assert not ok and "is not in the source checkout HEAD yet: merge its pull request" in why
    assert facade.source_head_contains(session, "p", None) == (False, "no accepted head commit is recorded")
    assert facade.source_head_contains(session, "missing", head) == (False, "project not found")

    def no_checkout(s, project):
        raise executor.StartError("NO_LOCAL_REPOSITORY", "project has no local repository reference")

    monkeypatch.setattr(executor, "resolve_repository", no_checkout)
    ok, why = facade.source_head_contains(session, "p", head)
    assert not ok and why.startswith("the source checkout cannot be checked")
