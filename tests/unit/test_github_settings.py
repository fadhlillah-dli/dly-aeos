"""scripts/github_settings.py: repository settings as code, compared with a fake API (no network, no gh).

Incident class: the merge policy lived only in the GitHub web settings, so nobody could review a change to it and a
silent drift (squash allowed again, a required check removed) went unnoticed. Every verdict below has a violating
fixture (red, with the text that names the setting) and a near-miss legitimate one (green)."""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from aeos.core.paths import repo_root

ROOT = repo_root()


def _load():
    spec = importlib.util.spec_from_file_location("aeos_script_github_settings", ROOT / "scripts" / "github_settings.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


gs = _load()

MAIN_NAME = "main: PR + CI gates, no force-push or deletion"
TAGS_NAME = "release tags: no deletion, no force update"
SERVER_FIELDS = {"node_id": "RRS_x", "source_type": "Repository", "source": "octo/demo", "created_at": "2026-10-26",
                 "updated_at": "2026-10-26", "current_user_can_bypass": "never", "_links": {"self": {"href": "x"}}}


def _file(rel: str) -> dict[str, Any]:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def _files() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    return (_file(".github/repository-settings.json"), _file(".github/rulesets/main.json"),
            _file(".github/rulesets/release-tags.json"))


class FakeApi:
    """The live repository: GET reads the state, send() changes it like GitHub would (and is recorded)."""

    def __init__(self, repo: dict[str, Any], rulesets: list[dict[str, Any]]) -> None:
        self.repo = {"id": 1, "full_name": "octo/demo", "private": False, **repo}
        self.rulesets = {100 + i: {"id": 100 + i, **r, **SERVER_FIELDS} for i, r in enumerate(rulesets)}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.gets: list[str] = []

    def get(self, path: str) -> Any:
        self.gets.append(path)
        if path == "":
            return copy.deepcopy(self.repo)
        if path.startswith("rulesets?"):
            return [{"id": i, "name": r["name"], "target": r["target"], "enforcement": r["enforcement"]}
                    for i, r in self.rulesets.items()]
        return copy.deepcopy(self.rulesets[int(path.rsplit("/", 1)[1])])

    def send(self, method: str, path: str, body: dict[str, Any]) -> Any:
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == "PATCH" and path == "":
            self.repo.update(body)
        elif method == "PUT":
            rid = int(path.rsplit("/", 1)[1])
            self.rulesets[rid] = {"id": rid, **copy.deepcopy(body), **SERVER_FIELDS}
        elif method == "POST" and path == "rulesets":
            rid = max(self.rulesets, default=99) + 1
            self.rulesets[rid] = {"id": rid, **copy.deepcopy(body), **SERVER_FIELDS}
        else:  # pragma: no cover - a call the tool must never make
            raise AssertionError((method, path))
        return {}


def in_sync() -> FakeApi:
    settings, main, tags = _files()
    return FakeApi(settings, [main, tags])


def before_state() -> FakeApi:
    """The live repository before this change: three merge methods, 8 required checks, no tag ruleset."""
    settings, main, _ = _files()
    main = copy.deepcopy(main)
    rule = {r["type"]: r for r in main["rules"]}
    rule["pull_request"]["parameters"]["allowed_merge_methods"] = ["merge", "squash", "rebase"]
    checks = rule["required_status_checks"]["parameters"]["required_status_checks"]
    checks[:] = [c for c in checks if c["context"] not in ("commit subjects [G1]", "pr title [G1]")]
    return FakeApi({**settings, "allow_squash_merge": True, "allow_rebase_merge": True, "delete_branch_on_merge": False},
                   [main])


def run(api: FakeApi, *argv: str, root: Path | None = None) -> tuple[int, list[str]]:
    out = io.StringIO()
    args = list(argv) + (["--root", str(root)] if root else [])
    code = gs.main(args, out=out, api=api)
    text = out.getvalue()
    assert text.isascii()
    return code, text.splitlines()


def mutate_main(api: FakeApi, edit) -> FakeApi:
    ruleset = next(r for r in api.rulesets.values() if r["name"] == MAIN_NAME)
    edit({r["type"]: r for r in ruleset["rules"]}, ruleset)
    return api


# ------------------------------------------------------------------ --check
def test_identical_live_state_is_ok_and_check_never_writes():
    api = in_sync()
    code, lines = run(api, "--check")
    assert (code, lines[-1]) == (0, "GITHUB_SETTINGS: OK")
    assert api.calls == []


def test_reordered_lists_are_a_near_miss_and_server_fields_are_ignored():
    api = in_sync()

    def edit(rules, ruleset):
        rules["pull_request"]["parameters"]["allowed_merge_methods"] = ["merge"]
        rules["required_status_checks"]["parameters"]["required_status_checks"].reverse()
        ruleset["rules"].reverse()

    code, lines = run(mutate_main(api, edit), "--check")
    assert (code, lines[-1]) == (0, "GITHUB_SETTINGS: OK")


def test_a_live_ruleset_that_allows_squash_is_drift_naming_allowed_merge_methods():
    api = in_sync()
    mutate_main(api, lambda rules, _: rules["pull_request"]["parameters"].update(
        allowed_merge_methods=["merge", "squash"]))
    code, lines = run(api, "--check")
    assert code == 1 and lines[-1] == "GITHUB_SETTINGS: DRIFT"
    hit = [ln for ln in lines if "allowed_merge_methods" in ln]
    assert len(hit) == 1 and "only live" in hit[0] and "squash" in hit[0], lines
    assert api.calls == []


def test_a_missing_required_context_is_drift_naming_the_context():
    for context in ("pr title [G1]", "commit subjects [G1]", "frontend [G1,G3]"):
        api = in_sync()
        def drop(rules, _, c=context):
            params = rules["required_status_checks"]["parameters"]
            params["required_status_checks"] = [x for x in params["required_status_checks"] if x["context"] != c]

        mutate_main(api, drop)
        code, lines = run(api, "--check")
        assert code == 1 and lines[-1] == "GITHUB_SETTINGS: DRIFT"
        assert any(f"[context={context}]: missing live" in ln for ln in lines), (context, lines)
        assert len([ln for ln in lines if ln.startswith("DIFF:")]) == 1  # only that context, nothing else


def test_an_extra_live_context_strict_flag_and_integration_id_are_drift():
    api = in_sync()

    def edit(rules, _):
        params = rules["required_status_checks"]["parameters"]
        params["required_status_checks"].append({"context": "lint", "integration_id": 15368})
        params["strict_required_status_checks_policy"] = True
        params["required_status_checks"][0]["integration_id"] = 1

    code, lines = run(mutate_main(api, edit), "--check")
    text = "\n".join(lines)
    assert code == 1
    assert "[context=lint]: only live" in text
    assert "strict_required_status_checks_policy: live true, file false" in text
    assert "integration_id: live 1, file 15368" in text


def test_repository_setting_drift_names_each_key():
    api = in_sync()
    api.repo.update(allow_squash_merge=True, delete_branch_on_merge=False)
    code, lines = run(api, "--check")
    text = "\n".join(lines)
    assert code == 1
    assert "repository.allow_squash_merge: live true, file false" in text
    assert "repository.delete_branch_on_merge: live false, file true" in text
    assert "allow_rebase_merge" not in text  # only the differing keys


def test_a_missing_tag_ruleset_and_an_unmanaged_live_ruleset_are_both_reported():
    settings, main, _ = _files()
    api = FakeApi(settings, [main, {**main, "name": "hand made", "target": "tag"}])
    code, lines = run(api, "--check")
    text = "\n".join(lines)
    assert code == 1
    assert f"ruleset '{TAGS_NAME}': missing live" in text
    assert "ruleset 'hand made': only live" in text and "apply never deletes" in text


# ------------------------------------------------------------------ --apply
def test_apply_without_yes_changes_nothing_and_says_so():
    api = before_state()
    snapshot = copy.deepcopy((api.repo, api.rulesets))
    code, lines = run(api, "--apply")
    assert code == 1 and lines[-1] == "GITHUB_SETTINGS: DRIFT"
    assert api.calls == [] and (api.repo, api.rulesets) == snapshot
    text = "\n".join(lines)
    assert "NOT APPLIED" in text and "WILL PATCH repository" in text and "WILL PUT ruleset" in text
    assert f"WILL POST ruleset '{TAGS_NAME}'" in text


def test_apply_with_yes_brings_the_before_state_to_the_files_and_a_second_check_is_ok():
    api = before_state()
    code, lines = run(api, "--apply", "--yes")
    assert (code, lines[-1]) == (0, "GITHUB_SETTINGS: OK"), lines
    assert [(m, p) for m, p, _ in api.calls] == [("PATCH", ""), ("PUT", "rulesets/100"), ("POST", "rulesets")]
    patch = api.calls[0][2]
    assert patch == {"allow_squash_merge": False, "allow_rebase_merge": False, "delete_branch_on_merge": True}
    _, main, tags = _files()
    assert api.calls[1][2] == main and api.calls[2][2] == tags
    again = len(api.calls)
    code, lines = run(api, "--check")
    assert (code, lines[-1]) == (0, "GITHUB_SETTINGS: OK") and len(api.calls) == again
    code, lines = run(api, "--apply", "--yes")  # idempotent: nothing left to send
    assert (code, lines[-1]) == (0, "GITHUB_SETTINGS: OK") and len(api.calls) == again


def test_apply_leaves_an_unmanaged_ruleset_alone_and_stays_drift():
    settings, main, tags = _files()
    api = FakeApi(settings, [main, tags, {**main, "name": "hand made"}])
    code, lines = run(api, "--apply", "--yes")
    assert code == 1 and lines[-1] == "GITHUB_SETTINGS: DRIFT"
    assert api.calls == [] and any("nothing to send" in ln for ln in lines)


def test_yes_without_apply_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        gs.main(["--check", "--yes"], out=io.StringIO(), api=in_sync())
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        gs.main([], out=io.StringIO(), api=in_sync())


# ------------------------------------------------------------------ files
def _copy_files(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / ".github" / "rulesets", tmp_path / ".github" / "rulesets")
    shutil.copy(ROOT / ".github" / "repository-settings.json", tmp_path / ".github" / "repository-settings.json")
    return tmp_path


def test_the_files_in_a_copy_are_ok_and_a_server_generated_field_is_an_error(tmp_path):
    root = _copy_files(tmp_path)
    code, lines = run(in_sync(), "--check", root=root)
    assert (code, lines[-1]) == (0, "GITHUB_SETTINGS: OK")
    path = root / ".github" / "rulesets" / "main.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["id"] = 25832910
    path.write_text(json.dumps(data), encoding="utf-8")
    code, lines = run(in_sync(), "--check", root=root)
    assert code == 2 and lines[-1] == "GITHUB_SETTINGS: ERROR"
    assert "main.json: not a writable ruleset field: id" in "\n".join(lines)


def test_an_unknown_setting_key_a_broken_file_and_a_duplicate_name_are_errors(tmp_path):
    root = _copy_files(tmp_path)
    settings = root / ".github" / "repository-settings.json"
    good = settings.read_text(encoding="utf-8")
    settings.write_text(json.dumps({**json.loads(good), "private": True}), encoding="utf-8")
    code, lines = run(in_sync(), "--check", root=root)
    assert code == 2 and "unknown key(s) private" in "\n".join(lines)
    settings.write_text("{not json", encoding="utf-8")
    code, lines = run(in_sync(), "--check", root=root)
    assert code == 2 and "invalid JSON" in "\n".join(lines)
    settings.write_text(good, encoding="utf-8")
    shutil.copy(root / ".github" / "rulesets" / "main.json", root / ".github" / "rulesets" / "copy.json")
    code, lines = run(in_sync(), "--check", root=root)
    assert code == 2 and "two files name the ruleset" in "\n".join(lines)


def test_two_live_rulesets_with_one_name_are_an_error_not_a_guess():
    settings, main, tags = _files()
    code, lines = run(FakeApi(settings, [main, main, tags]), "--check")
    assert code == 2 and lines[-1] == "GITHUB_SETTINGS: ERROR" and "two live rulesets" in "\n".join(lines)


# ------------------------------------------------------------------ the real files pin the owner decision (D1)
def test_the_checked_in_files_hold_the_merge_policy():
    settings, main, tags = _files()
    assert settings == {
        "allow_merge_commit": True, "allow_squash_merge": False, "allow_rebase_merge": False,
        "delete_branch_on_merge": True, "merge_commit_title": "MERGE_MESSAGE", "merge_commit_message": "PR_TITLE",
        "squash_merge_commit_title": "COMMIT_OR_PR_TITLE", "squash_merge_commit_message": "COMMIT_MESSAGES"}
    rules = {r["type"]: r.get("parameters") for r in main["rules"]}
    assert set(rules) == {"deletion", "non_fast_forward", "pull_request", "required_status_checks"}
    assert rules["pull_request"]["allowed_merge_methods"] == ["merge"]
    assert rules["pull_request"]["required_approving_review_count"] == 0  # a solo owner: unchanged from before
    assert rules["required_status_checks"]["strict_required_status_checks_policy"] is False
    assert main["bypass_actors"] == [] and main["enforcement"] == "active" and main["target"] == "branch"
    assert main["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]
    assert tags["target"] == "tag" and tags["enforcement"] == "active" and tags["bypass_actors"] == []
    assert tags["conditions"]["ref_name"] == {"include": ["refs/tags/v*"], "exclude": []}
    assert sorted(r["type"] for r in tags["rules"]) == ["deletion", "non_fast_forward", "update"]
    assert {"type": "update"} in tags["rules"]  # a fast-forward move of a release tag is an update, not a force push
    assert (main["name"], tags["name"]) == (MAIN_NAME, TAGS_NAME)


# ------------------------------------------------------------------ gh wrapper
def test_gh_api_builds_read_and_write_commands_with_the_body_on_stdin():
    seen: list[tuple[list[str], str | None]] = []

    def runner(args, stdin):
        seen.append((args, stdin))
        return '{"ok": true}' if args[1] != "-H" else "[]"

    api = gs.GhApi(runner, repo="octo/demo")
    assert api.get("rulesets?per_page=100") == []
    assert seen[0][0][-1] == "repos/octo/demo/rulesets?per_page=100" and seen[0][1] is None
    api.send("PUT", "rulesets/7", {"name": "x"})
    args, stdin = seen[1]
    assert args[:3] == ["api", "--method", "PUT"] and args[-3:] == ["repos/octo/demo/rulesets/7", "--input", "-"]
    assert json.loads(stdin) == {"name": "x"}
    api.send("PATCH", "", {"delete_branch_on_merge": True})
    assert seen[2][0][-3] == "repos/octo/demo"
    assert gs.GhApi(runner).base == "repos/{owner}/{repo}"  # gh expands the placeholders from the remote


def test_a_gh_failure_is_an_error_verdict_not_a_traceback():
    def failing(args, stdin):
        raise gs.SettingsError("gh api failed: HTTP 403")

    out = io.StringIO()
    assert gs.main(["--check"], out=out, api=gs.GhApi(failing)) == 2
    assert out.getvalue().splitlines()[-1] == "GITHUB_SETTINGS: ERROR"
