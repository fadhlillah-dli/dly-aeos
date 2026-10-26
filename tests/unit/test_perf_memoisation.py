"""Memoised pure/parse work stays exact: a changed input is re-read and callers cannot mutate a cached object.

Covers the install-manifest openbao entry, the Claude bridge schema check, the worker-runtime scope matcher and the
policy version (hash of ``policy/*.yaml`` without a YAML parse).
"""

from __future__ import annotations

import itertools
import random

import pytest
from jsonschema.exceptions import SchemaError

from aeos.claude_bridge import parsing
from aeos.core.hashing import content_hash
from aeos.policy import snapshot
from aeos.routing import service as routing
from aeos.secrets import installer
from aeos.worker_runtime import scope


def _manifest(root, pin: str, *, extra: str = "") -> None:
    d = root / "install-manifest"
    d.mkdir(exist_ok=True)
    (d / "manifest.yaml").write_text(
        f"components:\n  - component_id: other\n    approved_pin: '9.9.9'\n"
        f"  - component_id: openbao\n    approved_pin: '{pin}'\n{extra}", encoding="utf-8")


# ------------------------------------------------------------------------------------------ install manifest
def test_manifest_entry_rereads_a_changed_file_even_at_the_same_size(tmp_path):
    _manifest(tmp_path, "2.7.1")
    assert installer.manifest_pin(tmp_path) == "2.7.1"
    _manifest(tmp_path, "2.7.2")  # same byte length, rewritten immediately: must not be served from the memo
    assert installer.manifest_pin(tmp_path) == "2.7.2"
    _manifest(tmp_path, "2.8.0", extra="# longer\n")
    assert installer.manifest_pin(tmp_path) == "2.8.0"


def test_manifest_entry_callers_cannot_mutate_the_cached_entry(tmp_path):
    _manifest(tmp_path, "2.7.1")
    first = installer._manifest_entry(tmp_path)
    assert first is not None
    first["approved_pin"] = "0.0.0"
    first["injected"] = True
    second = installer._manifest_entry(tmp_path)
    assert second is not None and second is not first
    assert second["approved_pin"] == "2.7.1" and "injected" not in second


def test_manifest_entry_failures_keep_the_old_behaviour(tmp_path):
    assert installer._manifest_entry(tmp_path) is None  # no file: OSError -> None
    d = tmp_path / "install-manifest"
    d.mkdir()
    (d / "manifest.yaml").write_text("components: [unclosed\n", encoding="utf-8")
    assert installer._manifest_entry(tmp_path) is None  # YAML error -> None
    (d / "manifest.yaml").write_text("components:\n  - component_id: other\n", encoding="utf-8")
    assert installer._manifest_entry(tmp_path) is None  # no openbao component
    _manifest(tmp_path, "2.7.1")
    assert installer.manifest_pin(tmp_path) == "2.7.1"  # and a repaired file is read again


# ------------------------------------------------------------------------------------------- policy version
def test_policy_file_hashes_match_load_policy_files_and_follow_edits(monkeypatch, tmp_path):
    (tmp_path / "a.yaml").write_text("x: 1\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("y: [unclosed\n", encoding="utf-8")  # unparsable: still hashed
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    monkeypatch.setattr(snapshot, "policy_dir", lambda: tmp_path)
    loaded = snapshot.load_policy_files()
    assert snapshot.policy_file_hashes() == {name: e["sha256"] for name, e in loaded.items()}
    assert list(snapshot.policy_file_hashes()) == ["a.yaml", "b.yaml"]
    before = routing.current_policy_version()
    assert before == "pol-" + content_hash({n: e["sha256"] for n, e in sorted(loaded.items())})[7:23]
    (tmp_path / "a.yaml").write_text("x: 2\n", encoding="utf-8")  # same size, new content
    assert routing.current_policy_version() != before


def test_policy_version_of_the_real_policy_equals_the_parsed_files_version():
    files = snapshot.load_policy_files()
    assert files, "the repository ships policy/*.yaml"
    expected = "pol-" + content_hash({name: entry.get("sha256") for name, entry in sorted(files.items())})[7:23]
    assert routing.current_policy_version() == expected


def test_policy_file_hashes_without_a_policy_dir_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(snapshot, "policy_dir", lambda: tmp_path / "missing")
    assert snapshot.policy_file_hashes() == {}


# --------------------------------------------------------------------------------------- bridge schema check
@pytest.fixture
def clean_schema_memo():
    parsing._VALID_SCHEMAS.clear()
    yield
    parsing._VALID_SCHEMAS.clear()


def test_a_valid_schema_is_checked_once_and_an_invalid_one_is_never_remembered(monkeypatch, clean_schema_memo):
    calls: list[object] = []
    real = parsing.Draft202012Validator.check_schema

    def counting(schema):
        calls.append(schema)
        real(schema)

    monkeypatch.setattr(parsing.Draft202012Validator, "check_schema", staticmethod(counting))
    ok = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    parsing.validate_schema(ok)
    parsing.validate_schema({"properties": {"ok": {"type": "boolean"}}, "type": "object"})  # same content, new dict
    assert len(calls) == 1
    bad = {"type": "nope"}
    for _ in range(2):
        with pytest.raises(SchemaError):
            parsing.validate_schema(bad)
    assert len(calls) == 3, "a failing schema is checked (and rejected) every time"
    parsing.validate_schema({"type": "object", "properties": {"ok": {"type": "string"}}})  # different content
    assert len(calls) == 4


def test_non_json_exact_schemas_are_never_served_from_the_memo(clean_schema_memo):
    parsing.validate_schema({"type": "string"})
    with pytest.raises(SchemaError):
        parsing.validate_schema({"type": {"string"}})  # a set is not a valid type, nor is it the string "{'string'}"
    with pytest.raises(SchemaError):
        parsing.validate_schema({"type": ("nope",)})  # tuple -> list on round trip: not exact, checked uncached
    assert {'{"type": "string"}'} == parsing._VALID_SCHEMAS


def test_the_schema_memo_is_bounded(clean_schema_memo):
    for i in range(parsing._MAX_VALID_SCHEMAS + 5):
        parsing.validate_schema({"type": "object", "title": f"t{i}"})
    assert len(parsing._VALID_SCHEMAS) <= parsing._MAX_VALID_SCHEMAS


# --------------------------------------------------------------------------------------------- scope matcher
def _old_matches_any(path, patterns, *, case_insensitive=False):
    for pat in patterns:
        if scope.matches(path, pat, case_insensitive=case_insensitive):
            return pat
    return None


PATHS = ["backend/aeos/x.py", "Policy/a.yaml", "policy/a.yaml", "docs/x.md", "docs", "a/b/c.ts", "src/a.ts", ".git/config",
         "README.md", "readme.md", "../etc/passwd", "/etc/passwd", "C:\\x", "~/y", "", "  ", "a//b", "./a/./b", "a\\b\\c",
         "dir/", "**", "x?y", "a[1]b"]
PATTERNS = ["backend/**", "policy/**", "docs", "docs/", "src/*.ts", "**/*.ts", "README.md", "a/b/", "..", "/abs", "C:/x",
            "~", "", "x?y", "a[1]b", "**", "dir/**", "Policy/", ".git/**"]


@pytest.mark.parametrize("ci", [False, True])
def test_matches_any_equals_the_per_pattern_loop(ci):
    rng = random.Random(7)
    for path in PATHS:
        for r in range(1, 5):
            for pats in (rng.sample(PATTERNS, r) for _ in range(12)):
                expected = _old_matches_any(path, pats, case_insensitive=ci)
                assert scope.matches_any(path, pats, case_insensitive=ci) == expected, (path, pats)
                assert scope.matches_any(path, tuple(pats), case_insensitive=ci) == expected
    for path, pat in itertools.product(PATHS, PATTERNS):
        assert scope.matches_any(path, [pat], case_insensitive=ci) == _old_matches_any(path, [pat], case_insensitive=ci)


def test_matches_any_follows_a_mutated_pattern_list_and_keeps_first_match_order():
    pats = ["docs/**"]
    assert scope.matches_any("src/a.py", pats) is None
    pats.append("src/**")  # the same list object, new content: the cache key is the content
    assert scope.matches_any("src/a.py", pats) == "src/**"
    assert scope.matches_any("src/a.py", ["**", "src/**"]) == "**"


def test_normalize_pattern_still_raises_for_bad_patterns_every_time():
    for _ in range(2):
        with pytest.raises(scope.ScopePathError):
            scope.normalize_pattern("../x")
    assert scope.normalize_pattern("dir/") == "dir/**" and scope.normalize_pattern(" a\\b ") == "a/b"
    assert scope.matches("a/b.py", "../x") is False
