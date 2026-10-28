"""Per-role route preference ranking for the category agents (ADR-0126, stdlib only, ASCII output).

The category agents of ``aeos.workforce.catalog`` carry ``route_policy.preferred_routes``: an ordered list of route
keys that only RE-ORDERS routes that are already eligible (ADR-0124 consumes it; it never makes a route eligible).
This script derives that order from LiveBench, one release per category, and writes the reviewed data file
``backend/aeos/workforce/role_route_preferences.json``. Nothing here reads the network or the database.

  python scripts/role_route_ranking.py --snapshot --livebench-dir DIR     rebuild the committed input snapshot
  python scripts/role_route_ranking.py                                    rebuild the data file from the snapshot
  python scripts/role_route_ranking.py --check                            exit 1 when the data file differs

``--snapshot`` reads a LiveBench download (``DIR/20261025/{table.csv,cost.csv,categories.json}`` and
``DIR/v1.csv`` plus ``DIR/v1cat.json``, the 2026-06-25 v1 table and its categories) and keeps only the rows of the
models mapped below, so the repository holds the evidence the ranking uses and not the whole download.

Method (critic H7 of the 2026-10-28 review, binding):

* One release per category: LiveBench 2.0 release 2026-10-25 for Long Agentic, Coding, Design, Reasoning, Research
  and Data Analysis; the frozen v1 release 2026-06-25 for Instruction Following. Scores of other releases are never
  mixed in (a model without a score in that release is unscored there).
* CI-aware buckets: the noise band of a category is the half-width of a 95% interval of a mean score in [0, 100]
  over ``nq`` questions in the worst case (p = 0.5): ``1.96 * 50 / sqrt(nq)``. Scores are sorted high to low; a
  bucket starts at the best score not yet placed and takes every model whose score is within the band of that
  first score. v1 publishes no question count in the table: ``V1_ASSUMED_NQ`` is used and stated (NOT-PROVEN).
* Inside a bucket: larger monthly allowance first (a bigger allowance carries more work before its window stops),
  then the higher score, then the route key. Unscored routes follow the scored ones: larger allowance, then key.
* OpenCode Go routes come first (only the servable chat-completions ones named in ``GO_ALLOWANCE``), then the free
  OpenRouter routes with tools as the PUBLIC fallback, ranked the same way. Eligibility still filters every entry
  by data class: a ``:free`` route is never eligible for INTERNAL work, whatever its position here.
* Never listed: Gemini 4 Argon and Qwen 3.8 Max (Go serves them on the google / anthropic API families, which the
  broker does not support; owner answer 2 of 2026-10-28) and DeepSeek (PUBLIC-only data terms).

Exit codes: 0 ok, 1 the data file differs (--check), 2 usage or input error.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / "docs" / "evidence" / "livebench-2026-10-28" / "role-ranking-inputs.json"
DATA = ROOT / "backend" / "aeos" / "workforce" / "role_route_preferences.json"
FETCHED = "2026-10-28"

R2_RELEASE, R2_DIR = "2026-10-25", "20261025"
V1_RELEASE = "2026-06-25"
R2_URL = f"https://livebench.ai/data/{R2_DIR}/table.csv"
R2_NQ_URL = f"https://livebench.ai/data/{R2_DIR}/cost.csv"
V1_URL = "https://livebench.ai/v1/table_2026_06_25.csv"
V1_ASSUMED_NQ = 50  # not in the v1 table; a small count widens the band (fewer distinctions), NOT-PROVEN

# agent key -> (release, LiveBench category). One category per agent.
AGENT_CATEGORIES: dict[str, tuple[str, str]] = {
    "agentic-coder": ("2.0", "Long Agentic"),
    "coder": ("2.0", "Coding"),
    "designer": ("2.0", "Design"),
    "reasoner": ("2.0", "Reasoning"),
    "researcher": ("2.0", "Research"),
    "writer": ("v1", "IF"),
    "analyst": ("2.0", "Data Analysis"),
}
V1_CATEGORY_LABEL = {"IF": "Instruction Following"}

# Servable OpenCode Go routes (chat-completions family, tool_call true per models.dev read 2026-10-28) -> monthly
# allowance USD. Source: the opencode.ai/go page bundle read 2026-10-28; the seeded ones equal
# aeos.routing.seed GO_WORKERS / GO_WATCH (tests/unit/test_workforce_role_agents.py keeps them equal).
GO_ALLOWANCE: dict[str, float] = {
    "glm-5.2": 60, "glm-5.3": 15, "glm-5.3-flash": 60, "kimi-k2.7-code": 60, "kimi-k3": 15, "kimi-k3.1-pro": 15,
    "mimo-v2.6-pro": 15, "minimax-m3.1-flash": 20, "step-5-preview": 15,
}
# Free OpenRouter models whose catalog entry lists tool support (OpenRouter catalog read 2026-10-28, E1 declared).
OPENROUTER_FREE_TOOLS: tuple[str, ...] = (
    "cohere/north-mini-code", "dots-studio/dots-3-note-preview", "google/gemma-4-26b-a4b-it", "google/gemma-4-31b-it",
    "liquid/lfm-2.5-2.6b", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning", "nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/nemotron-3.5-lightning", "nvidia/nemotron-3.5-super-vl", "poolside/laguna-s-2.1", "poolside/laguna-xs-2.1",
    "recursive/flint-v1.0", "thinkingmachines/inkling", "thinkingmachines/inkling-small",
    "xingchen-agi/xing4.0-29b-a4b",
)
NEVER_LISTED = ("argon", "qwen3.8-max", "qwen", "deepseek")

# (release, LiveBench model id) -> route key. Exact ids only, plus the aliases stated with their reason.
ALIASES: dict[tuple[str, str], tuple[str, str]] = {
    ("2.0", "gemma-4-31b"): ("openrouter/google/gemma-4-31b-it:free",
                             "LiveBench lists the instruction-tuned release without the -it suffix"),
}
NOT_MAPPED: dict[str, str] = {
    "openrouter/nvidia/nemotron-3.5-super-vl:free": "LiveBench 2026-10-25 has two builds (full precision and nvfp4) "
                                                     "and the free endpoint does not say which one it serves",
}


def go_key(model: str) -> str:
    return f"opencode-go/{model}"


def free_key(model: str) -> str:
    return f"openrouter/{model}:free"


def route_for(release: str, lb_id: str) -> str | None:
    if (release, lb_id) in ALIASES:
        return ALIASES[(release, lb_id)][0]
    if lb_id in GO_ALLOWANCE:
        return go_key(lb_id)
    for model in OPENROUTER_FREE_TOOLS:
        if model.split("/", 1)[1] == lb_id:
            key = free_key(model)
            return None if key in NOT_MAPPED else key
    return None


def noise_band(nq: int) -> float:
    return round(1.96 * 50 / math.sqrt(nq), 2)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ----------------------------------------------------------------------------- snapshot (download -> inputs)
def build_snapshot(livebench_dir: Path) -> dict[str, Any]:
    r2 = livebench_dir / R2_DIR
    table, cost, cats = r2 / "table.csv", r2 / "cost.csv", r2 / "categories.json"
    v1, v1cats = livebench_dir / "v1.csv", livebench_dir / "v1cat.json"
    for p in (table, cost, cats, v1, v1cats):
        if not p.is_file():
            raise FileNotFoundError(f"missing LiveBench input {p}")
    categories = json.loads(cats.read_text(encoding="utf-8"))
    v1_categories = json.loads(v1cats.read_text(encoding="utf-8"))
    nq_rows = {r["model"]: r for r in csv.DictReader(cost.open(encoding="utf-8"))}
    r2_rows: list[dict[str, Any]] = []
    for row in csv.DictReader(table.open(encoding="utf-8")):
        if route_for("2.0", row["model"]) is None:
            continue
        cost_row = nq_rows.get(row["model"]) or {}
        out: dict[str, Any] = {"model": row["model"], "scores": {}, "nq": {}}
        for _agent, (rel, cat) in sorted(AGENT_CATEGORIES.items()):
            if rel != "2.0":
                continue
            (column,) = categories[cat]
            if row.get(column) not in (None, ""):
                out["scores"][cat] = float(row[column])
                out["nq"][cat] = int(cost_row[f"nq_{column}"])
        r2_rows.append(out)
    v1_rows: list[dict[str, Any]] = []
    tasks = sorted({t for rel, cat in AGENT_CATEGORIES.values() if rel == "v1" for t in v1_categories[cat]})
    for row in csv.DictReader(v1.open(encoding="utf-8")):
        if route_for("v1", row["model"]) is None:
            continue
        v1_rows.append({"model": row["model"], "tasks": {t: float(row[t]) for t in tasks if row.get(t) not in (None, "")}})
    return {
        "schema_version": "1",
        "fetched": FETCHED,
        "note": "Only the rows of the models the ranking maps; generated by scripts/role_route_ranking.py --snapshot",
        "sources": {
            "2.0": {"release": R2_RELEASE, "url": R2_URL, "nq_url": R2_NQ_URL,
                    "sha256": {"table.csv": _sha256(table), "cost.csv": _sha256(cost),
                               "categories.json": _sha256(cats)},
                    "categories": {cat: categories[cat] for rel, cat in sorted(set(AGENT_CATEGORIES.values()))
                                   if rel == "2.0"}},
            "v1": {"release": V1_RELEASE, "url": V1_URL, "nq": None,
                   "sha256": {"table_2026_06_25.csv": _sha256(v1), "categories_2026_06_25.json": _sha256(v1cats)},
                   "categories": {cat: v1_categories[cat] for rel, cat in sorted(set(AGENT_CATEGORIES.values()))
                                  if rel == "v1"}},
        },
        "rows": {"2.0": sorted(r2_rows, key=lambda r: r["model"]), "v1": sorted(v1_rows, key=lambda r: r["model"])},
    }


# ----------------------------------------------------------------------------- ranking (inputs -> data file)
def _scores(inputs: dict[str, Any], release: str, category: str) -> tuple[dict[str, float], int | None]:
    """{route_key: score} and the question count of one category in one release."""
    out: dict[str, float] = {}
    nq: int | None = None
    if release == "2.0":
        for row in inputs["rows"]["2.0"]:
            if category in row["scores"]:
                key = route_for("2.0", row["model"])
                if key:
                    out[key] = float(row["scores"][category])
                    nq = int(row["nq"][category])
    else:
        tasks = inputs["sources"]["v1"]["categories"][category]
        for row in inputs["rows"]["v1"]:
            vals = [row["tasks"][t] for t in tasks if t in row["tasks"]]
            key = route_for("v1", row["model"])
            if key and len(vals) == len(tasks):
                out[key] = round(sum(vals) / len(vals), 2)
    return out, nq


def _allowance(key: str) -> float:
    return float(GO_ALLOWANCE.get(key.removeprefix("opencode-go/"), 0)) if key.startswith("opencode-go/") else 0.0


def _rank(keys: list[str], scores: dict[str, float], band: float) -> list[tuple[str, int | None]]:
    scored = sorted((k for k in keys if k in scores), key=lambda k: (-scores[k], k))
    out: list[tuple[str, int | None]] = []
    bucket = 0
    while scored:
        bucket += 1
        top = scores[scored[0]]
        members = [k for k in scored if top - scores[k] <= band + 1e-9]
        scored = [k for k in scored if k not in members]
        members.sort(key=lambda k: (-_allowance(k), -scores[k], k))
        out += [(k, bucket) for k in members]
    unscored = sorted((k for k in keys if k not in scores), key=lambda k: (-_allowance(k), k))
    return out + [(k, None) for k in unscored]


def build_data(inputs: dict[str, Any], inputs_sha256: str) -> dict[str, Any]:
    go_keys = sorted(go_key(m) for m in GO_ALLOWANCE)
    free_keys = sorted(free_key(m) for m in OPENROUTER_FREE_TOOLS)
    agents: dict[str, Any] = {}
    for agent, (release, category) in sorted(AGENT_CATEGORIES.items()):
        scores, nq = _scores(inputs, release, category)
        nq_used = nq if nq is not None else V1_ASSUMED_NQ
        band = noise_band(nq_used)
        src = inputs["sources"][release]
        label = V1_CATEGORY_LABEL.get(category, category)
        entries: list[dict[str, Any]] = []
        for tier, keys in (("OPENCODE_GO", go_keys), ("OPENROUTER_FREE", free_keys)):
            for key, bucket in _rank(keys, scores, band):
                entry: dict[str, Any] = {"route_key": key, "tier": tier, "bucket": bucket,
                                         "score": scores.get(key), "allowance_usd_month": _allowance(key) or None}
                if key in scores:
                    entry["source_refs"] = [{"url": src["url"], "release": src["release"], "category": label,
                                             "score": scores[key], "nq": nq, "date": inputs["fetched"]}]
                else:
                    entry["source_refs"] = []
                    entry["note"] = NOT_MAPPED.get(key, f"no score in LiveBench {release} release {src['release']}")
                entries.append(entry)
        agents[agent] = {
            "category": label, "release": src["release"], "livebench_version": release,
            "nq": nq, "nq_used_for_band": nq_used, "noise_band": band,
            "preferred_routes": [e["route_key"] for e in entries], "entries": entries,
        }
    return {
        "schema_version": "1",
        "generated_by": "scripts/role_route_ranking.py",
        "inputs": INPUTS.relative_to(ROOT).as_posix(),
        "inputs_sha256": inputs_sha256,
        "decision": "owner decision R31, 2026-10-28 (ADR-0126): best models first, matched to each role's needs",
        "method": {
            "release_per_category": {"2.0": R2_RELEASE, "v1": V1_RELEASE},
            "noise_band": "1.96 * 50 / sqrt(nq) points (95% half-width, worst case p=0.5)",
            "v1_nq": f"not published in the v1 table; {V1_ASSUMED_NQ} assumed (NOT-PROVEN)",
            "bucket": "a bucket takes every model within the noise band of its best remaining score",
            "inside_bucket": "larger monthly allowance first, then higher score, then route key",
            "unscored": "after the scored routes of the tier: larger allowance first, then route key",
            "tiers": "OPENCODE_GO first, then OPENROUTER_FREE (PUBLIC only: eligibility filters it for INTERNAL)",
            "never_listed": "Gemini 4 Argon, Qwen 3.8 Max (API family the broker does not support), DeepSeek",
            "effect": "re-orders routes that are already eligible; never makes a route eligible",
        },
        "aliases": {f"{rel}:{lb}": {"route_key": key, "reason": why} for (rel, lb), (key, why) in sorted(ALIASES.items())},
        "agents": agents,
    }


def render(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=1, sort_keys=False, ensure_ascii=True) + "\n"


def expected_text(inputs_path: Path = INPUTS) -> str:
    raw = inputs_path.read_bytes()
    return render(build_data(json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()))


def check(data_path: Path = DATA, inputs_path: Path = INPUTS) -> list[str]:
    """Findings (empty = the data file is exactly the generator output for the committed inputs)."""
    if not data_path.is_file():
        return [f"{data_path.name} is missing: run python scripts/role_route_ranking.py"]
    want = expected_text(inputs_path)
    have = data_path.read_text(encoding="utf-8")
    if have == want:
        return []
    for i, (a, b) in enumerate(zip(have.splitlines(), want.splitlines(), strict=False), start=1):
        if a != b:
            return [f"{data_path.name} differs from the generator output for {inputs_path.name} at line {i}: "
                    "the ranking is generated, never edited by hand (python scripts/role_route_ranking.py)"]
    return [f"{data_path.name} differs from the generator output for {inputs_path.name} in length: the ranking is "
            "generated, never edited by hand (python scripts/role_route_ranking.py)"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--snapshot", action="store_true", help="rebuild the input snapshot from a LiveBench download")
    ap.add_argument("--livebench-dir", type=Path, help="LiveBench download directory (with --snapshot)")
    ap.add_argument("--check", action="store_true", help="exit 1 when the data file differs from the generator output")
    args = ap.parse_args(argv)
    try:
        if args.snapshot:
            if args.livebench_dir is None:
                print("--snapshot needs --livebench-dir", file=sys.stderr)
                return 2
            snap = build_snapshot(args.livebench_dir)
            INPUTS.parent.mkdir(parents=True, exist_ok=True)
            INPUTS.write_text(json.dumps(snap, indent=1, ensure_ascii=True) + "\n", encoding="utf-8")
            print(f"wrote {INPUTS.relative_to(ROOT).as_posix()}")
            return 0
        if args.check:
            findings = check()
            for f in findings:
                print(f"FAIL: {f}")
            if not findings:
                print(f"OK: {DATA.relative_to(ROOT).as_posix()} matches the generator output")
            return 1 if findings else 0
        DATA.write_text(expected_text(), encoding="utf-8")
        print(f"wrote {DATA.relative_to(ROOT).as_posix()}")
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
