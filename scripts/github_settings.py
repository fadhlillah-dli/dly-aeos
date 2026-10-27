"""Repository settings as code: compare (or apply) the live GitHub settings with the files in .github/.

  python scripts/github_settings.py --check [--repo owner/name] [--root <checkout>]
  python scripts/github_settings.py --apply [--yes] [--repo owner/name] [--root <checkout>]

The files are the source of truth:

  .github/repository-settings.json   merge methods, merge commit title/message, delete branch on merge
                                     (the fields of PATCH repos/{owner}/{repo}; only the keys listed are managed)
  .github/rulesets/*.json            one file per ruleset (name, target, enforcement, conditions, bypass_actors, rules);
                                     a file never holds server-generated fields (id, node_id, created_at, _links...)

--check is read-only (GET calls only). It prints one line per difference between the live state and the files and ends
with the verdict. Lists of plain values compare as sets, ``rules`` are matched by ``type`` and ``required_status_checks``
by ``context``, so a difference names the setting (for example ``allowed_merge_methods``) or the check context. A live
ruleset that has no file is reported too (apply never deletes anything).

--apply prints what it will change. Without --yes it changes nothing and ends with DRIFT. With --yes it sends
PATCH repos/{owner}/{repo} (changed keys only), PUT rulesets/{id} for a ruleset that exists (matched by name) and POST
rulesets for one that does not, then reads everything again and prints the verdict. It never runs in CI: the required
status checks it would write are the merge gate, so an owner or the delegating session runs it by hand
(CONTRIBUTING.md "Merging").

The last line is always ``GITHUB_SETTINGS: OK``, ``GITHUB_SETTINGS: DRIFT`` or ``GITHUB_SETTINGS: ERROR``.
Exit 0 OK, 1 DRIFT, 2 ERROR (usage, files, gh or API failure). Stdlib and the gh CLI only, ASCII output. The API is a
small class (``GhApi``) so that tests inject a fake; no test reaches the network.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TextIO

OK, DRIFT, ERROR = "OK", "DRIFT", "ERROR"
EXIT_CODES = {OK: 0, DRIFT: 1, ERROR: 2}

SETTINGS_FILE = Path(".github") / "repository-settings.json"
RULESETS_DIR = Path(".github") / "rulesets"

# The repository fields this tool manages (PATCH repos/{owner}/{repo}); a key outside this set is a file error.
SETTING_KEYS = frozenset({
    "allow_merge_commit", "allow_squash_merge", "allow_rebase_merge", "delete_branch_on_merge",
    "merge_commit_title", "merge_commit_message", "squash_merge_commit_title", "squash_merge_commit_message",
})
# The fields of a ruleset file: exactly what POST/PUT rulesets accept that this repository uses. Server-generated
# fields (id, node_id, source, source_type, created_at, updated_at, current_user_can_bypass, _links) are refused.
RULESET_KEYS = frozenset({"name", "target", "enforcement", "conditions", "bypass_actors", "rules"})
# Lists of objects that are matched by one field instead of by position.
KEYED_LISTS = {"rules": "type", "required_status_checks": "context"}

Runner = Callable[[list[str], "str | None"], str]


class SettingsError(Exception):
    """Files, gh or API failure: the verdict is ERROR."""


# ------------------------------------------------------------------------------------------------ gh access
def gh_runner(args: list[str], stdin: str | None = None) -> str:
    """Run ``gh <args>`` (stdin optional) and return stdout; any failure becomes SettingsError."""
    try:
        proc = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120, check=False)
    except FileNotFoundError as exc:
        raise SettingsError("gh (GitHub CLI) is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise SettingsError("gh timed out") from exc
    if proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}")[:300]
        raise SettingsError(f"gh {' '.join(args[:4])} failed: {detail}")
    return proc.stdout


class Api(Protocol):
    def get(self, path: str) -> Any: ...

    def send(self, method: str, path: str, body: dict[str, Any]) -> Any: ...


class GhApi:
    """Repository-scoped calls through ``gh api``; ``path`` is relative to ``repos/{owner}/{repo}``."""

    def __init__(self, runner: Runner = gh_runner, repo: str | None = None) -> None:
        self.runner = runner
        # without --repo gh expands {owner}/{repo} from the current directory's remote itself
        self.base = f"repos/{repo}" if repo else "repos/{owner}/{repo}"

    def _decode(self, text: str, what: str) -> Any:
        try:
            return json.loads(text) if text.strip() else {}
        except ValueError as exc:
            raise SettingsError(f"gh returned invalid JSON for {what}: {exc}") from exc

    def get(self, path: str) -> Any:
        full = f"{self.base}/{path}" if path else self.base
        return self._decode(self.runner(["api", "-H", "Accept: application/vnd.github+json", full], None), full)

    def send(self, method: str, path: str, body: dict[str, Any]) -> Any:
        full = f"{self.base}/{path}" if path else self.base
        args = ["api", "--method", method, "-H", "Accept: application/vnd.github+json", full, "--input", "-"]
        return self._decode(self.runner(args, json.dumps(body)), full)


# ------------------------------------------------------------------------------------------------ files
@dataclass(frozen=True)
class Desired:
    settings: dict[str, Any]
    rulesets: list[dict[str, Any]]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SettingsError(f"{path.as_posix()}: cannot read ({exc.strerror or exc})") from exc
    except ValueError as exc:
        raise SettingsError(f"{path.as_posix()}: invalid JSON ({exc})") from exc


def load_desired(root: Path) -> Desired:
    settings = _read_json(root / SETTINGS_FILE)
    if not isinstance(settings, dict) or not settings:
        raise SettingsError(f"{SETTINGS_FILE.as_posix()}: expected a non-empty JSON object")
    unknown = sorted(set(settings) - SETTING_KEYS)
    if unknown:
        raise SettingsError(f"{SETTINGS_FILE.as_posix()}: unknown key(s) {', '.join(unknown)}")
    rulesets: list[dict[str, Any]] = []
    files = sorted((root / RULESETS_DIR).glob("*.json")) if (root / RULESETS_DIR).is_dir() else []
    if not files:
        raise SettingsError(f"{RULESETS_DIR.as_posix()}: no ruleset file")
    for path in files:
        rel = (RULESETS_DIR / path.name).as_posix()
        data = _read_json(path)
        if not isinstance(data, dict):
            raise SettingsError(f"{rel}: expected a JSON object")
        extra = sorted(set(data) - RULESET_KEYS)
        if extra:
            raise SettingsError(f"{rel}: not a writable ruleset field: {', '.join(extra)} (drop server-generated fields)")
        missing = sorted(RULESET_KEYS - set(data))
        if missing:
            raise SettingsError(f"{rel}: missing field(s) {', '.join(missing)}")
        if not isinstance(data["name"], str) or not data["name"]:
            raise SettingsError(f"{rel}: name must be a non-empty string")
        rulesets.append(data)
    names = [r["name"] for r in rulesets]
    for name in sorted({n for n in names if names.count(n) > 1}):
        raise SettingsError(f"{RULESETS_DIR.as_posix()}: two files name the ruleset {name!r}")
    return Desired(settings, rulesets)


# ------------------------------------------------------------------------------------------------ comparison
def _show(value: Any) -> str:
    return json.dumps(value, sort_keys=True)  # ensure_ascii: the output stays ASCII


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def diff(path: str, want: Any, have: Any, key: str = "") -> list[str]:
    """Differences between the file value *want* and the live value *have*, one line each, named by *path*."""
    out: list[str] = []
    if isinstance(want, dict) and isinstance(have, dict):
        for k in sorted(set(want) | set(have)):
            sub = f"{path}.{k}"
            if k not in have:
                out.append(f"{sub}: missing live (file has {_show(want[k])})")
            elif k not in want:
                out.append(f"{sub}: only live ({_show(have[k])})")
            else:
                out.extend(diff(sub, want[k], have[k], k))
    elif isinstance(want, list) and isinstance(have, list):
        ident = KEYED_LISTS.get(key)
        if ident and all(isinstance(x, dict) and ident in x for x in [*want, *have]):
            w = {x[ident]: x for x in want}
            h = {x[ident]: x for x in have}
            for name in sorted(set(w) | set(h), key=str):
                sub = f"{path}[{ident}={name}]"
                if name not in h:
                    out.append(f"{sub}: missing live")
                elif name not in w:
                    out.append(f"{sub}: only live")
                else:
                    out.extend(diff(sub, w[name], h[name]))
        else:
            wc, hc = sorted(_canon(x) for x in want), sorted(_canon(x) for x in have)
            if wc != hc:
                only_file = [c for c in wc if c not in hc]
                only_live = [c for c in hc if c not in wc]
                parts = ([f"only in file {', '.join(only_file)}"] if only_file else []) + (
                    [f"only live {', '.join(only_live)}"] if only_live else [])
                out.append(f"{path}: {'; '.join(parts) or 'different number of entries'}")
    elif want != have or type(want) is not type(have):
        out.append(f"{path}: live {_show(have)}, file {_show(want)}")
    return out


@dataclass(frozen=True)
class Action:
    method: str
    path: str  # relative to repos/{owner}/{repo}
    body: dict[str, Any]
    summary: str


def compute(api: Api, desired: Desired) -> tuple[list[str], list[Action]]:
    """Read the live state (GET only) and return the difference lines and the calls that would remove them."""
    lines: list[str] = []
    actions: list[Action] = []

    live_repo = api.get("")
    if not isinstance(live_repo, dict):
        raise SettingsError("repository response is not an object")
    changed = {}
    for k, want in desired.settings.items():
        problems = diff(f"repository.{k}", want, live_repo.get(k))
        if problems:
            lines.extend(problems)
            changed[k] = want
    if changed:
        actions.append(Action("PATCH", "", changed, f"PATCH repository: {', '.join(sorted(changed))}"))

    listed = api.get("rulesets?per_page=100")
    if not isinstance(listed, list):
        raise SettingsError("rulesets response is not a list")
    by_name: dict[str, list[dict[str, Any]]] = {}
    for item in listed:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            by_name.setdefault(item["name"], []).append(item)
    for name, items in sorted(by_name.items()):
        if len(items) > 1:
            raise SettingsError(f"two live rulesets are named {name!r}: apply cannot tell them apart")
    wanted = {r["name"] for r in desired.rulesets}
    for ruleset in desired.rulesets:
        name = ruleset["name"]
        found = by_name.get(name)
        if not found:
            lines.append(f"ruleset {name!r}: missing live (apply creates it)")
            actions.append(Action("POST", "rulesets", ruleset, f"POST ruleset {name!r} (create)"))
            continue
        rid = found[0].get("id")
        detail = api.get(f"rulesets/{rid}")
        if not isinstance(detail, dict):
            raise SettingsError(f"ruleset {rid} response is not an object")
        problems = diff(f"ruleset {name!r}", ruleset, {k: detail.get(k) for k in ruleset if k in detail})
        if problems:
            lines.extend(problems)
            actions.append(Action("PUT", f"rulesets/{rid}", ruleset, f"PUT ruleset {name!r} (id {rid})"))
    for name in sorted(set(by_name) - wanted):
        lines.append(f"ruleset {name!r}: only live, no file in {RULESETS_DIR.as_posix()} (apply never deletes)")
    return lines, actions


# ------------------------------------------------------------------------------------------------ main
def _write(out: TextIO, text: str) -> None:
    out.write(text.encode("ascii", "backslashreplace").decode("ascii") + "\n")


def main(argv: Sequence[str] | None = None, out: TextIO | None = None, api: Api | None = None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="github_settings.py", description="Compare or apply the repository settings "
                                     "stored in .github/ with the live GitHub repository.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="read-only: print every difference, exit 1 on DRIFT")
    mode.add_argument("--apply", action="store_true", help="print the changes; with --yes send them")
    parser.add_argument("--yes", action="store_true", help="with --apply: really change the repository")
    parser.add_argument("--repo", metavar="OWNER/NAME", help="repository (default: the remote of the current directory)")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent),
                        help="checkout that holds .github/ (default: this repository)")
    args = parser.parse_args(argv)
    if args.yes and not args.apply:
        parser.error("--yes belongs to --apply")

    def verdict(state: str) -> int:
        _write(out, f"GITHUB_SETTINGS: {state}")
        return EXIT_CODES[state]

    try:
        desired = load_desired(Path(args.root))
        client = api if api is not None else GhApi(repo=args.repo)
        lines, actions = compute(client, desired)
        for line in lines:
            _write(out, f"DIFF: {line}")
        if not lines:
            _write(out, "live settings and rulesets equal the files")
            return verdict(OK)
        if args.check:
            return verdict(DRIFT)
        for action in actions:
            _write(out, f"WILL {action.summary}")
        if not actions:
            _write(out, "nothing to send: the remaining differences need a manual change")
            return verdict(DRIFT)
        if not args.yes:
            _write(out, "NOT APPLIED: nothing was changed; add --yes to send the calls above")
            return verdict(DRIFT)
        for action in actions:
            client.send(action.method, action.path, action.body)
            _write(out, f"DONE {action.summary}")
        lines, _ = compute(client, desired)
        for line in lines:
            _write(out, f"DIFF: {line}")
        return verdict(DRIFT if lines else OK)
    except SettingsError as exc:
        _write(out, f"ERROR: {exc}")
        return verdict(ERROR)


if __name__ == "__main__":
    raise SystemExit(main())
