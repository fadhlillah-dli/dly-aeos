"""Monthly report of pinned GitHub Actions and pinned release binaries (informational, never fails).

Why: the workflows pin every third-party action to a full commit SHA (a moving tag could inject code into the trusted
pipeline) and one release binary to a version plus SHA-256. A pin never moves by itself, and ADR-0115 allows no automatic
Dependabot pull requests (they would not carry the owner identity). So this report says which pins are behind.

  python scripts/action_pins.py --report [--json] [--today YYYY-MM-DD] [--workflows DIR]

It reads every ``uses: owner/repo@<40-hex sha> # vX`` of ``.github/workflows/*.yml`` and every ``<NAME>_VERSION`` +
``<NAME>_SHA256`` pair of a workflow ``env:`` (``ACTIONLINT_*`` in ci.yml; the release repository of a binary is listed in
``BINARY_REPOS``). For each distinct pin it asks GitHub (the ``gh`` CLI, through ``scripts/ci_status.py``'s access code)
for the newest release that was published at least 14 days before today and for the commit of that release's tag.

One row per distinct pin: name, pinned tag, pinned sha (short; the SHA-256 for a binary), newest eligible tag, the
full commit sha of that newest tag (40 hex, ready to paste into the workflow; also JSON ``newest_sha``; ``-`` for a
binary, whose new SHA-256 comes from the release's checksums file, and for a pin that is not an update), status.

  CURRENT          the pin is the newest eligible release (or newer: a release younger than 14 days is ignored), and
                   for an action the commit of the pinned tag is the pinned sha
  UPDATE <tag>     a newer eligible release exists; for an action its tag commit is looked up too (when that lookup
                   fails the row stays UPDATE and says so in brackets, with an empty newest_sha)
  UNKNOWN (why)    no version comment, a ref that is not a full sha, no eligible release, the tag does not point at the
                   pinned sha, or the GitHub lookup failed (the reason is printed)

"Newest" is the highest version number among non-draft, non-prerelease releases whose tag reads as vX[.Y[.Z]]; a
backport release of an older major line never beats a newer major. The pinned-tag/sha cross-check runs only for a pin
that would be CURRENT, so an UPDATE is never hidden by it. Exit 0 always, except 2 for a usage error (a bad option or
date). The last line is ``ACTION_PINS: <n> pins, <k> updates available``; with ``--json`` the JSON is the only thing on
stdout and that line goes to stderr, written after the JSON is flushed.

Updating is a normal pull request: the new full SHA of the tag and the version comment; for a binary also the new
SHA-256 taken from that release's checksums file. ASCII output. Stdlib plus the ``gh`` CLI; the API is injected in tests.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_status  # noqa: E402  the one owner of gh access (L10, L17): runner, paging and error type

MIN_AGE_DAYS = 14
SHORT = 10
CURRENT, UPDATE, UNKNOWN = "CURRENT", "UPDATE", "UNKNOWN"
# <NAME>_VERSION + <NAME>_SHA256 in a workflow env: NAME -> GitHub repository that publishes the release.
BINARY_REPOS = {"ACTIONLINT": "rhysd/actionlint"}

USES = re.compile(r"""^\s*(?:-\s+)?uses:\s*["']?(?P<spec>[^\s"'#]+)["']?\s*(?:#\s*(?P<comment>.*?))?\s*$""")
ACTION_REF = re.compile(r"^(?P<repo>[\w.-]+/[\w.-]+)(?:/[^@]*)?@(?P<ref>.+)$")
ENV_VERSION = re.compile(r"""^\s*(?P<name>[A-Z][A-Z0-9_]*)_VERSION:\s*["']?(?P<value>[^"'\s#]+)["']?""")
ENV_SHA256 = re.compile(r"""^\s*(?P<name>[A-Z][A-Z0-9_]*)_SHA256:\s*["']?(?P<value>[0-9a-fA-F]{64})["']?""")
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
VERSION = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")


@dataclass
class Pin:
    kind: str  # "action" or "binary"
    name: str  # owner/repo, or the binary's release repository
    repo: str  # repository to ask for releases ("" when unknown)
    ref: str  # full commit sha (action) or SHA-256 (binary); may be a non-sha ref for a broken action pin
    tag: str  # pinned version tag ("" when there is no version comment)
    files: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.kind, self.name, self.ref, self.tag)


@dataclass
class Row:
    kind: str
    name: str
    pinned_tag: str
    pinned_sha: str
    newest_tag: str
    status: str
    reason: str = ""
    files: list[str] = field(default_factory=list)
    newest_sha: str = ""  # full commit sha of newest_tag (an UPDATE of an action only; "" otherwise)

    def status_text(self) -> str:
        if self.status == UPDATE:
            return f"UPDATE {self.newest_tag}" + (f" ({self.reason})" if self.reason else "")
        if self.status == UNKNOWN:
            return f"UNKNOWN ({self.reason})"
        return self.status


# ------------------------------------------------------------------------------------------------ reading pins
def parse_version(tag: str) -> tuple[int, int, int] | None:
    m = VERSION.match(tag.strip())
    if not m:
        return None
    major, minor, patch = (int(g) if g else 0 for g in m.groups())
    return (major, minor, patch)


def _version_comment(comment: str | None) -> str:
    token = (comment or "").split(None, 1)[0] if (comment or "").strip() else ""
    return token if parse_version(token) else ""


def parse_workflow(text: str, filename: str) -> list[Pin]:
    """Pins of one workflow file, read line by line (the script is stdlib only, so no YAML parser)."""
    pins: list[Pin] = []
    versions: dict[str, str] = {}
    sums: dict[str, str] = {}
    for line in text.splitlines():
        if (m := USES.match(line)) is not None:
            spec = m.group("spec")
            if spec.startswith("./") or spec.startswith("docker://"):
                continue  # a local action or an image: not a release pin
            a = ACTION_REF.match(spec)
            if a is None:
                pins.append(Pin("action", spec, "", "", "", [filename]))
                continue
            pins.append(Pin("action", a.group("repo"), a.group("repo"), a.group("ref"),
                            _version_comment(m.group("comment")), [filename]))
        elif (m := ENV_VERSION.match(line)) is not None:
            versions.setdefault(m.group("name"), m.group("value"))
        elif (m := ENV_SHA256.match(line)) is not None:
            sums.setdefault(m.group("name"), m.group("value").lower())
    for name in sorted(set(versions) & set(sums)):
        repo = BINARY_REPOS.get(name, "")
        tag = versions[name] if versions[name].startswith("v") else f"v{versions[name]}"
        pins.append(Pin("binary", repo or name, repo, sums[name], tag, [filename]))
    return pins


def collect_pins(workflows: Path) -> list[Pin]:
    """Distinct pins of every workflow file, each with the files that use it."""
    merged: dict[tuple[str, str, str, str], Pin] = {}
    for path in sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")]):
        for pin in parse_workflow(path.read_text(encoding="utf-8"), path.name):
            seen = merged.setdefault(pin.key, pin)
            if path.name not in seen.files:
                seen.files.append(path.name)
    return list(merged.values())


# ------------------------------------------------------------------------------------------------ GitHub lookups
class Lookups:
    """Releases and tag commits per repository, cached for one report run."""

    def __init__(self, runner: ci_status.Runner) -> None:
        self.runner = runner
        self._apis: dict[str, ci_status.Api] = {}
        self._releases: dict[str, list[dict[str, Any]]] = {}
        self._tag_sha: dict[tuple[str, str], str] = {}

    def _api(self, repo: str) -> ci_status.Api:
        return self._apis.setdefault(repo, ci_status.Api(self.runner, repo))

    def releases(self, repo: str) -> list[dict[str, Any]]:
        if repo not in self._releases:
            pages = self._api(repo).get("releases?per_page=100")
            items = [r for page in pages for r in (page if isinstance(page, list) else [page]) if isinstance(r, dict)]
            self._releases[repo] = items
        return self._releases[repo]

    def tag_commit(self, repo: str, tag: str) -> str:
        """Commit sha a tag points at (an annotated tag is followed to its commit)."""
        key = (repo, tag)
        if key not in self._tag_sha:
            api = self._api(repo)
            obj = api.one(f"git/ref/tags/{tag}").get("object") or {}
            for _ in range(3):
                if obj.get("type") != "tag":
                    break
                obj = api.one(f"git/tags/{obj.get('sha')}").get("object") or {}
            sha = obj.get("sha")
            if obj.get("type") != "commit" or not isinstance(sha, str) or not FULL_SHA.match(sha):
                raise ci_status.CiError(f"tag {tag} does not resolve to a commit")
            self._tag_sha[key] = sha
        return self._tag_sha[key]


def newest_eligible(releases: Sequence[dict[str, Any]], today: date) -> str:
    """Tag of the highest-version release published at least MIN_AGE_DAYS before *today* ("" when none)."""
    cutoff = today - timedelta(days=MIN_AGE_DAYS)
    best: tuple[tuple[int, int, int], str, str] | None = None
    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        tag, published = rel.get("tag_name"), rel.get("published_at")
        if not isinstance(tag, str) or not isinstance(published, str):
            continue
        version = parse_version(tag)
        try:
            when = datetime.fromisoformat(published.replace("Z", "+00:00")).astimezone(UTC).date()
        except ValueError:
            continue
        if version is None or when > cutoff:
            continue
        if best is None or (version, published) > (best[0], best[1]):
            best = (version, published, tag)
    return best[2] if best else ""


# ------------------------------------------------------------------------------------------------ the report
def short(ref: str) -> str:
    return ref[:SHORT] if ref else "-"


def judge(pin: Pin, lookups: Lookups, today: date) -> Row:
    row = Row(pin.kind, pin.name, pin.tag or "-", short(pin.ref), "-", UNKNOWN, files=sorted(pin.files))
    if pin.kind == "action" and not FULL_SHA.match(pin.ref):
        row.reason = "not pinned to a full commit sha"
        return row
    if not pin.tag:
        row.reason = "no version comment (# vX.Y.Z) after the pin"
        return row
    if not pin.repo:
        row.reason = f"no release repository known for {pin.name}_VERSION (add it to BINARY_REPOS)"
        return row
    try:
        newest = newest_eligible(lookups.releases(pin.repo), today)
        if not newest:
            row.reason = f"no release of {pin.repo} is at least {MIN_AGE_DAYS} days old"
            return row
        row.newest_tag = newest
        pinned_v, newest_v = parse_version(pin.tag), parse_version(newest)
        if pinned_v is not None and newest_v is not None and newest_v > pinned_v:
            row.status = UPDATE
            if pin.kind == "action":  # the owner pastes this sha into the workflow: never resolve it by hand
                try:
                    row.newest_sha = lookups.tag_commit(pin.repo, newest)
                except ci_status.CiError as exc:
                    row.reason = f"commit of {newest} not found: {str(exc)[:120]}"
            return row
        if pin.kind == "action":
            tag_sha = lookups.tag_commit(pin.repo, pin.tag)
            if tag_sha != pin.ref:
                row.reason = f"tag {pin.tag} points at {short(tag_sha)}, not at the pinned sha"
                return row
        row.status = CURRENT
    except ci_status.CiError as exc:
        row.reason = str(exc)[:160]
        row.status = UNKNOWN
    return row


def build_rows(pins: Sequence[Pin], runner: ci_status.Runner, today: date) -> list[Row]:
    lookups = Lookups(runner)
    return [judge(p, lookups, today) for p in sorted(pins, key=lambda p: (p.kind, p.name, p.tag, p.ref))]


def summary_line(rows: Sequence[Row]) -> str:
    return f"ACTION_PINS: {len(rows)} pins, {sum(r.status == UPDATE for r in rows)} updates available"


def render_table(rows: Sequence[Row]) -> list[str]:
    head = ("NAME", "PINNED", "SHA", "NEWEST", "NEWEST SHA", "STATUS")
    body = [(r.name, r.pinned_tag, r.pinned_sha, r.newest_tag, r.newest_sha or "-", r.status_text()) for r in rows]
    widths = [max(len(x[i]) for x in [head, *body]) for i in range(5)]
    return ["  ".join([*(x.ljust(w) for x, w in zip(row[:5], widths, strict=True)), row[5]]).rstrip()
            for row in [head, *body]]


def run(argv: Sequence[str] | None = None, *, runner: ci_status.Runner = ci_status.gh_runner,
        today_default: Callable[[], date] = lambda: datetime.now(UTC).date(),
        out: TextIO | None = None, err: TextIO | None = None) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    args = build_parser().parse_args(argv)
    today = args.today or today_default()
    rows = build_rows(collect_pins(Path(args.workflows)), runner, today)
    if args.json:
        payload = {"today": today.isoformat(), "min_age_days": MIN_AGE_DAYS, "pins": [asdict(r) | {
            "status_text": r.status_text()} for r in rows],
            "summary": {"pins": len(rows), "updates": sum(r.status == UPDATE for r in rows)}}
        out.write(json.dumps(payload, indent=2) + "\n")
        out.flush()
        err.write(summary_line(rows) + "\n")
    else:
        out.write("\n".join([*render_table(rows), "", summary_line(rows)]) + "\n")
    return 0


def date_arg(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a date YYYY-MM-DD: {value!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="action_pins.py", description="Report pinned actions and binaries that are behind.")
    p.add_argument("--report", action="store_true", required=True, help="print the report (the only mode)")
    p.add_argument("--json", action="store_true", help="JSON on stdout; the ACTION_PINS line goes to stderr")
    p.add_argument("--today", type=date_arg, default=None, metavar="YYYY-MM-DD",
                   help="treat this as today when applying the 14-day rule (default: the real date, UTC)")
    p.add_argument("--workflows", default=str(ROOT / ".github" / "workflows"), metavar="DIR",
                   help="directory of workflow files (default: .github/workflows)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
