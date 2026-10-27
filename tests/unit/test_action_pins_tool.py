"""scripts/action_pins.py: the monthly report of pinned actions and binaries, from fixture JSON (no network, no gh).

Every status has a violating fixture and a near-miss legitimate one: a pin behind is UPDATE with the tag, while a
newer release younger than 14 days is ignored (CURRENT); a pin without a version comment is UNKNOWN with its reason,
while the same pin with the comment is CURRENT."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from datetime import date

import pytest

from aeos.core.paths import repo_root

ROOT = repo_root()


def _load():
    spec = importlib.util.spec_from_file_location("aeos_script_action_pins", ROOT / "scripts" / "action_pins.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ap = _load()

TODAY = date(2026, 10, 27)  # the 14-day cutoff is 2026-10-13
OLD, NEW = "1" * 40, "2" * 40
SUM = "ab" * 32


def release(tag, published, **extra):
    return {"tag_name": tag, "published_at": published, "draft": False, "prerelease": False, **extra}


class FakeGh:
    """Runner replacement: maps the request path (without query) to a JSON value; unknown paths fail like gh."""

    def __init__(self, files):
        self.files = files
        self.calls = []

    def __call__(self, args):
        path = args[-1].split("?")[0]
        self.calls.append(path)
        if path not in self.files:
            raise ap.ci_status.CiError(f"gh api {path} failed: HTTP 404")
        return json.dumps(self.files[path])


def tag_files(repo, tag, sha):
    return {f"repos/{repo}/git/ref/tags/{tag}": {"object": {"type": "commit", "sha": sha}}}


def workflows(tmp_path, text, name="ci.yml"):
    d = tmp_path / "wf"
    d.mkdir(exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")
    return d


def report(wf, files, *extra):
    out, err = io.StringIO(), io.StringIO()
    rc = ap.run(["--report", "--workflows", str(wf), "--today", TODAY.isoformat(), *extra],
                runner=FakeGh(files), out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


def uses(repo="acme/tool", sha=OLD, comment="# v1.0.0"):
    return f"jobs:\n  a:\n    steps:\n      - uses: {repo}@{sha} {comment}\n".replace(" \n", "\n")


def rows(out):
    return {ln.split()[0]: ln for ln in out.splitlines() if "/" in ln.split(" ")[0]}


# ------------------------------------------------------------------ the four statuses
def test_a_pin_behind_is_an_update_row_with_the_tag(tmp_path):
    files = {"repos/acme/tool/releases": [release("v1.2.0", "2026-09-01T10:00:00Z"), release("v1.0.0", "2026-01-01T00:00:00Z")],
             **tag_files("acme/tool", "v1.2.0", NEW)}
    rc, out, err = report(workflows(tmp_path, uses()), files)
    assert rc == 0
    assert rows(out)["acme/tool"].endswith(f"{NEW}  UPDATE v1.2.0")  # the full sha of the new tag, to paste
    assert out.splitlines()[-1] == "ACTION_PINS: 1 pins, 1 updates available" and err == ""


def test_an_update_resolves_the_commit_of_the_new_tag_including_an_annotated_one(tmp_path):
    files = {"repos/acme/tool/releases": [release("v1.2.0", "2026-09-01T10:00:00Z")],
             "repos/acme/tool/git/ref/tags/v1.2.0": {"object": {"type": "tag", "sha": "c" * 40}},
             f"repos/acme/tool/git/tags/{'c' * 40}": {"object": {"type": "commit", "sha": NEW}}}
    _, out, _ = report(workflows(tmp_path, uses()), files, "--json")
    (pin,) = json.loads(out)["pins"]
    assert pin["newest_sha"] == NEW and pin["status_text"] == "UPDATE v1.2.0" and pin["reason"] == ""


def test_an_update_whose_tag_commit_cannot_be_found_stays_an_update_and_says_so(tmp_path):
    files = {"repos/acme/tool/releases": [release("v1.2.0", "2026-09-01T10:00:00Z")]}  # no git/ref/tags/v1.2.0
    _, out, _ = report(workflows(tmp_path, uses()), files, "--json")
    (pin,) = json.loads(out)["pins"]
    assert pin["status"] == "UPDATE" and pin["newest_sha"] == ""
    assert pin["status_text"].startswith("UPDATE v1.2.0 (commit of v1.2.0 not found: ")


def test_a_release_younger_than_14_days_is_ignored(tmp_path):
    files = {"repos/acme/tool/releases": [release("v1.1.0", "2026-10-20T10:00:00Z"),  # 7 days old: ignored
                                         release("v1.0.0", "2026-01-01T00:00:00Z")],
             **tag_files("acme/tool", "v1.0.0", OLD)}
    _, out, _ = report(workflows(tmp_path, uses()), files)
    assert rows(out)["acme/tool"].rstrip().endswith("CURRENT") and "v1.1.0" not in out
    assert out.splitlines()[-1] == "ACTION_PINS: 1 pins, 0 updates available"


def test_the_14_day_edge_is_inclusive_and_drafts_and_prereleases_never_count(tmp_path):
    files = {**tag_files("acme/tool", "v1.3.0", NEW),
             "repos/acme/tool/releases": [release("v1.3.0", "2026-10-13T23:59:59Z"),  # exactly 14 days: eligible
                                         release("v1.4.0", "2026-09-01T00:00:00Z", draft=True),
                                         release("v1.5.0", "2026-09-01T00:00:00Z", prerelease=True),
                                         release("nightly", "2026-09-01T00:00:00Z")]}
    _, out, _ = report(workflows(tmp_path, uses()), files)
    assert "UPDATE v1.3.0" in rows(out)["acme/tool"]
    younger = {"repos/acme/tool/releases": [release("v1.3.0", "2026-10-14T00:00:00Z")]}  # 13 days: not eligible
    _, out2, _ = report(workflows(tmp_path, uses()), younger)
    assert "UNKNOWN (no release of acme/tool is at least 14 days old)" in out2


def test_a_pin_without_a_version_comment_is_unknown_with_the_reason(tmp_path):
    files = {"repos/acme/tool/releases": [release("v1.0.0", "2026-01-01T00:00:00Z")], **tag_files("acme/tool", "v1.0.0", OLD)}
    _, out, _ = report(workflows(tmp_path, uses(comment="")), files)
    assert rows(out)["acme/tool"].endswith("UNKNOWN (no version comment (# vX.Y.Z) after the pin)")
    _, out2, _ = report(workflows(tmp_path, uses(comment="# pinned for stability")), files)
    assert "UNKNOWN (no version comment" in out2
    _, ok, _ = report(workflows(tmp_path, uses()), files)  # near miss: the same pin with its comment
    assert rows(ok)["acme/tool"].endswith("CURRENT")


def test_current_means_the_tag_commit_is_the_pinned_sha(tmp_path):
    releases = {"repos/acme/tool/releases": [release("v1.0.0", "2026-01-01T00:00:00Z")]}
    _, ok, _ = report(workflows(tmp_path, uses()), {**releases, **tag_files("acme/tool", "v1.0.0", OLD)})
    assert rows(ok)["acme/tool"].endswith("CURRENT")
    _, bad, _ = report(workflows(tmp_path, uses()), {**releases, **tag_files("acme/tool", "v1.0.0", NEW)})
    assert f"UNKNOWN (tag v1.0.0 points at {NEW[:10]}, not at the pinned sha)" in bad


def test_an_annotated_tag_is_followed_to_its_commit(tmp_path):
    files = {"repos/acme/tool/releases": [release("v1.0.0", "2026-01-01T00:00:00Z")],
             "repos/acme/tool/git/ref/tags/v1.0.0": {"object": {"type": "tag", "sha": "c" * 40}},
             f"repos/acme/tool/git/tags/{'c' * 40}": {"object": {"type": "commit", "sha": OLD}}}
    _, out, _ = report(workflows(tmp_path, uses()), files)
    assert rows(out)["acme/tool"].endswith("CURRENT")


def test_an_older_major_backport_never_beats_the_newest_major(tmp_path):
    files = {"repos/acme/tool/releases": [release("v3.9.9", "2026-10-01T00:00:00Z"),  # published last, older line
                                         release("v4.0.0", "2026-09-01T00:00:00Z")], **tag_files("acme/tool", "v4.0.0", OLD)}
    _, out, _ = report(workflows(tmp_path, uses(comment="# v4.0.0")), files)
    assert rows(out)["acme/tool"].endswith("CURRENT")


def test_a_lookup_failure_is_unknown_and_the_exit_stays_zero(tmp_path):
    rc, out, _ = report(workflows(tmp_path, uses()), {})
    assert rc == 0 and "UNKNOWN (gh api repos/acme/tool/releases failed: HTTP 404)" in out
    assert out.splitlines()[-1] == "ACTION_PINS: 1 pins, 0 updates available"


def test_a_tag_pinned_action_is_unknown_not_silently_skipped(tmp_path):
    _, out, _ = report(workflows(tmp_path, "      - uses: acme/tool@v1\n      - uses: ./local\n      - uses: docker://x\n"), {})
    assert rows(out)["acme/tool"].endswith("UNKNOWN (not pinned to a full commit sha)") and "local" not in out


# ------------------------------------------------------------------ binaries and parsing
def test_a_pinned_binary_is_judged_by_version_and_shows_the_sha256(tmp_path):
    text = ('      - name: x\n        env:\n          ACTIONLINT_VERSION: "1.7.12"\n'
            f'          ACTIONLINT_SHA256: "{SUM}"\n          NODE_VERSION: "22"\n')
    files = {"repos/rhysd/actionlint/releases": [release("v1.7.13", "2026-09-01T00:00:00Z"), release("v1.7.12", "2026-06-01T00:00:00Z")]}
    _, out, _ = report(workflows(tmp_path, text), files)
    line = rows(out)["rhysd/actionlint"]
    assert "v1.7.12" in line and SUM[:10] in line and "UPDATE v1.7.13" in line and "NODE" not in out  # no SHA256: not a pin
    assert line.split()[-3:] == ["-", "UPDATE", "v1.7.13"]  # a binary has no commit column: its new SHA-256 is in the checksums file
    current = {"repos/rhysd/actionlint/releases": [release("v1.7.12", "2026-06-01T00:00:00Z")]}
    _, out2, _ = report(workflows(tmp_path, text), current)
    assert rows(out2)["rhysd/actionlint"].endswith("CURRENT")
    unknown = text.replace("ACTIONLINT", "MYTOOL")
    _, out3, _ = report(workflows(tmp_path, unknown), {})
    assert "UNKNOWN (no release repository known for MYTOOL_VERSION" in out3


def test_one_row_per_distinct_pin_with_its_files(tmp_path):
    wf = workflows(tmp_path, uses())
    (wf / "other.yml").write_text(uses(), encoding="utf-8")
    pins = ap.collect_pins(wf)
    assert len(pins) == 1 and sorted(pins[0].files) == ["ci.yml", "other.yml"]
    files = {"repos/acme/tool/releases": [release("v1.0.0", "2026-01-01T00:00:00Z")], **tag_files("acme/tool", "v1.0.0", OLD)}
    assert report(wf, files)[1].splitlines()[-1] == "ACTION_PINS: 1 pins, 0 updates available"


def test_json_is_the_only_thing_on_stdout_and_the_verdict_line_goes_to_stderr(tmp_path):
    files = {"repos/acme/tool/releases": [release("v1.2.0", "2026-09-01T00:00:00Z")], **tag_files("acme/tool", "v1.2.0", NEW)}
    rc, out, err = report(workflows(tmp_path, uses()), files, "--json")
    data = json.loads(out)
    assert rc == 0 and data["summary"] == {"pins": 1, "updates": 1}
    (pin,) = data["pins"]
    assert pin["status"] == "UPDATE" and pin["newest_tag"] == "v1.2.0" and pin["newest_sha"] == NEW and pin["status_text"] == "UPDATE v1.2.0"
    assert err == "ACTION_PINS: 1 pins, 1 updates available\n"


def test_a_bad_date_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        ap.run(["--report", "--today", "27-10-2026"], runner=FakeGh({}), out=io.StringIO(), err=io.StringIO())
    assert exc.value.code == 2


def test_the_real_workflows_parse_into_pins():
    pins = ap.collect_pins(ROOT / ".github" / "workflows")
    names = {p.name for p in pins}
    assert {"actions/checkout", "actions/setup-python", "rhysd/actionlint"} <= names
    assert all(p.tag for p in pins), [p.name for p in pins if not p.tag]
