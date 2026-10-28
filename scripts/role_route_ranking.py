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

Method (critic H7 of the 2026-10-28 review and the owner's words of 2026-10-28, binding): "model-model terbaik
saat ini yang berjalan, selanjutnya model lainnya" (the best models first, the others after) and "jangan sampai
model terbaik yang bisa kita gunakan malah tidak kita gunakan" (the best usable model must never go unused).

* One release per category: LiveBench 2.0 release 2026-10-25 for Long Agentic, Coding, Design, Reasoning, Research
  and Data Analysis; the frozen v1 release 2026-06-25 for Instruction Following. Scores of other releases are never
  mixed in (a model without a score in that release is unscored there), with one stated exception (owner decision
  R34, ADR-0129): a route whose served build only an earlier release scored (``SERVED_BUILDS``: Go
  deepseek-v4.1-flash serves the original build, which only release 2026-10-14 scored) takes that release's score in
  a category only where that release is comparable, i.e. every model both releases score has the same score there
  (proven from the committed rows; Long Agentic and Browser were re-run on 2026-10-25, so not there).
* Best first: the scored routes of a tier are ordered by the category score, highest first; an equal score goes to
  the larger monthly allowance, then the route key.
* The noise band is metadata only (``source_refs[].band``, for later load spreading among near-equal routes): the
  band of a category is the half-width of a 95% interval of a mean score in [0, 100] over ``nq`` questions in the
  worst case (p = 0.5), ``1.96 * 50 / sqrt(nq)``; a band starts at the best score not yet placed and takes every
  model within that half-width of it. It never re-orders against the score. v1 publishes no question count in the
  table: ``V1_ASSUMED_NQ`` is used and stated (NOT-PROVEN).
* Never omitted: every servable Go route is in every list. Routes without a score in the category's release follow
  the scored ones of their tier, ordered by their best other evidence: the LiveBench v1 overall (the mean of the v1
  category means, release 2026-06-25) where the model has one, highest first, then the larger monthly allowance,
  then the route key. The v1 overall only orders unscored routes; it is never a category score.
* OpenCode Go routes come first (every chat-completions WORKER route of the registry, ``GO_ALLOWANCE``, including
  the Console-translation candidates Gemini 4 Argon, the Qwen models and MiniMax M3 since owner answer R34; the
  superseded MiMo V2.5 pair last, overflow only), then the free OpenRouter routes with tools as the PUBLIC fallback,
  then the paid OpenRouter routes (``OPENROUTER_PAID``, owner R34: used only as an escalation after two failed
  $0-marginal attempts or as overflow when every $0-marginal route of the role is at capacity, never for PUBLIC
  tasks; listing them last keeps a PUBLIC task on the free fallback), ranked the same way. Eligibility still decides
  whether a listed route serves: a ``:free`` route is never eligible for INTERNAL work, a paid route never without the
  owner's BUDGET, a translation candidate never before its probes pass, and DeepSeek on Go (PUBLIC-only data class,
  needs the Global region of the Go workspace) only when its own eligibility and probes say so.
* Never listed (``aeos.core.model_policy``, the one AGENTS.md invariant 6 check; the generator refuses its own output
  and ``aeos.workforce.role_agents.catalog_problems`` the catalog): any Claude-family model and meta-routers.

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
sys.path.insert(0, str(ROOT / "backend"))

from aeos.core.model_policy import model_family_refusal  # noqa: E402  (stdlib only)

INPUTS = ROOT / "docs" / "evidence" / "livebench-2026-10-28" / "role-ranking-inputs.json"
DATA = ROOT / "backend" / "aeos" / "workforce" / "role_route_preferences.json"
FETCHED = "2026-10-28"

R2_RELEASE, R2_DIR = "2026-10-25", "20261025"
R1014, R1014_RELEASE, R1014_DIR = "2.0-1014", "2026-10-14", "20261014"
R1014_URL = f"https://livebench.ai/data/{R1014_DIR}/table.csv"
R1014_NQ_URL = f"https://livebench.ai/data/{R1014_DIR}/cost.csv"
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

# Servable OpenCode Go routes (the chat-completions WORKER-capable routes of the registry, aeos.routing.seed after
# ADR-0125) -> monthly allowance USD. Source: the opencode.ai/go page bundle read 2026-10-28; the seeded ones equal
# aeos.routing.seed (tests/unit/test_workforce_role_agents.py keeps them equal).
GO_ALLOWANCE: dict[str, float] = {
    "deepseek-v4.1-flash": 80, "glm-5.2": 60, "glm-5.3": 15, "glm-5.3-flash": 60, "hy3": 60, "kimi-k2.7-code": 60,
    "kimi-k3": 15, "kimi-k3.1-pro": 15, "longcat-2.0": 60, "mimo-v2.6-flash": 60, "mimo-v2.6-pro": 15,
    "minimax-m3.1-flash": 20, "step-5-preview": 15,
    # owner decision R34 (ADR-0129): the remaining chat-completions models and the Console-translation candidates
    "deepseek-v4-pro": 15, "hy4-preview": 30, "mimo-v2.5": 60, "mimo-v2.5-pro": 15,
    "gemini-4-argon": 30, "qwen3.8-max": 15, "qwen3.8-flash": 30, "qwen3.7-plus": 60, "minimax-m3": 60,
}
# Go serves these on another API family; seeded on chat-completions (aeos.routing.seed.GO_TRANSLATION_STATEMENT)
GO_TRANSLATION: tuple[str, ...] = ("gemini-4-argon", "minimax-m3", "qwen3.7-plus", "qwen3.8-flash", "qwen3.8-max")
TRANSLATION_STATEMENT = ("served through OpenCode Console API translation - NOT PROVEN until the plain and --tools "
                         "probes pass")
# superseded Go models -> successor: listed after every other Go route (overflow only, owner R34)
GO_SUPERSEDED: dict[str, str] = {"mimo-v2.5": "mimo-v2.6-flash", "mimo-v2.5-pro": "mimo-v2.6-pro"}
# Paid OpenRouter routes (PUBLIC + INTERNAL through ZDR hosts; seeded by their own change, ADR-0129) -> how they serve
OPENROUTER_PAID: dict[str, str] = {
    "openrouter/deepseek/deepseek-v4.1-flash-1017": (
        "paid per token (owner R34: the credit goes to this route): only as an escalation after two failed "
        "$0-marginal attempts of the task, or as overflow when every $0-marginal route of the role is at capacity, "
        "in allowance fit or in cool-down; never for PUBLIC tasks (they have the free fallback); never without the "
        "owner's BUDGET decision"),
}
# Free OpenRouter models whose catalog entry lists tool support (OpenRouter catalog read 2026-10-28, E1 declared).
OPENROUTER_FREE_TOOLS: tuple[str, ...] = (
    "cohere/north-mini-code", "dots-studio/dots-3-note-preview", "google/gemma-4-26b-a4b-it", "google/gemma-4-31b-it",
    "liquid/lfm-2.5-2.6b", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning", "nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/nemotron-3.5-lightning", "nvidia/nemotron-3.5-super-vl", "poolside/laguna-s-2.1", "poolside/laguna-xs-2.1",
    "recursive/flint-v1.0", "thinkingmachines/inkling", "thinkingmachines/inkling-small",
    "xingchen-agi/xing4.0-29b-a4b",
)
# (release, LiveBench model id) -> route key. Exact ids only, plus the aliases stated with their reason.
ALIASES: dict[tuple[str, str], tuple[str, str]] = {
    ("2.0", "deepseek-v4.1-flash-1017"): ("openrouter/deepseek/deepseek-v4.1-flash-1017",
                                          "the OpenRouter id names the 1017 build exactly (canonical slug "
                                          "deepseek/deepseek-v4.1-flash-20261017, catalog read 2026-10-28); Go "
                                          "serves deepseek-v4.1-flash without a date, and OpenRouter's undated id "
                                          "dates 2026-09-10 like models.dev dates the Go model, so the 1017 score is "
                                          "not given to the Go route (SERVED_BUILDS). The v1 build "
                                          "deepseek-v4.1-flash-max is an effort variant and is not mapped"),
    ("2.0", "gemma-4-31b"): ("openrouter/google/gemma-4-31b-it:free",
                             "LiveBench lists the instruction-tuned release without the -it suffix"),
}
# route key -> (LiveBench id in release 2026-10-14, reason): the build the route serves, scored only by that release
SERVED_BUILDS: dict[str, tuple[str, str]] = {
    "opencode-go/deepseek-v4.1-flash": (
        "deepseek-v4.1-flash",
        "Go serves deepseek-v4.1-flash without a date (models.dev release date 2026-09-10, the original build; that "
        "Go serves exactly that build is NOT-PROVEN); release 2026-10-14 scored the original build, release 2026-10-25 "
        "only the 1017 update (owner R34, ADR-0129)"),
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
    if release == R1014:  # only the served builds take a score of this release
        return next((key for key, (lb, _why) in SERVED_BUILDS.items() if lb == lb_id), None)
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
def _release_rows(table: Path, cost: Path, categories: dict[str, Any], keep: Any) -> list[dict[str, Any]]:
    """The rows of a 2.0-format release whose model *keep* accepts: the category scores and question counts."""
    nq_rows = {r["model"]: r for r in csv.DictReader(cost.open(encoding="utf-8"))}
    out: list[dict[str, Any]] = []
    for row in csv.DictReader(table.open(encoding="utf-8")):
        if not keep(row["model"]):
            continue
        cost_row = nq_rows.get(row["model"]) or {}
        entry: dict[str, Any] = {"model": row["model"], "scores": {}, "nq": {}}
        for _agent, (rel, cat) in sorted(AGENT_CATEGORIES.items()):
            if rel != "2.0":
                continue
            (column,) = categories[cat]
            if row.get(column) not in (None, ""):
                entry["scores"][cat] = float(row[column])
                entry["nq"][cat] = int(cost_row[f"nq_{column}"])
        out.append(entry)
    return sorted(out, key=lambda r: r["model"])


def build_snapshot(livebench_dir: Path) -> dict[str, Any]:
    r2 = livebench_dir / R2_DIR
    table, cost, cats = r2 / "table.csv", r2 / "cost.csv", r2 / "categories.json"
    r14 = livebench_dir / R1014_DIR
    table14, cost14, cats14 = r14 / "table.csv", r14 / "cost.csv", r14 / "categories.json"
    v1, v1cats = livebench_dir / "v1.csv", livebench_dir / "v1cat.json"
    for p in (table, cost, cats, table14, cost14, cats14, v1, v1cats):
        if not p.is_file():
            raise FileNotFoundError(f"missing LiveBench input {p}")
    categories = json.loads(cats.read_text(encoding="utf-8"))
    categories14 = json.loads(cats14.read_text(encoding="utf-8"))
    v1_categories = json.loads(v1cats.read_text(encoding="utf-8"))
    r2_rows = _release_rows(table, cost, categories, lambda m: route_for("2.0", m) is not None)
    # 2026-10-14: the served builds, plus every model of the 2.0 rows as the comparability evidence per category
    mapped = {r["model"] for r in r2_rows}
    r14_rows = _release_rows(table14, cost14, categories14,
                             lambda m: route_for(R1014, m) is not None or m in mapped)
    v1_rows: list[dict[str, Any]] = []
    tasks = sorted({t for cat_tasks in v1_categories.values() for t in cat_tasks})  # all: the v1 overall needs them
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
            R1014: {"release": R1014_RELEASE, "url": R1014_URL, "nq_url": R1014_NQ_URL,
                    "sha256": {"table.csv": _sha256(table14), "cost.csv": _sha256(cost14),
                               "categories.json": _sha256(cats14)},
                    "use": "the served builds of SERVED_BUILDS, plus the rows of the 2.0 models as the per-category "
                           "comparability evidence"},
            "v1": {"release": V1_RELEASE, "url": V1_URL, "nq": None,
                   "sha256": {"table_2026_06_25.csv": _sha256(v1), "categories_2026_06_25.json": _sha256(v1cats)},
                   "categories": {cat: v1_categories[cat] for cat in sorted(v1_categories)}},
        },
        "rows": {"2.0": r2_rows, R1014: r14_rows, "v1": sorted(v1_rows, key=lambda r: r["model"])},
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


def v1_overall(inputs: dict[str, Any]) -> dict[str, float]:
    """{route_key: LiveBench v1 overall}: the mean of the v1 category means (only models with every v1 task)."""
    cats: dict[str, list[str]] = inputs["sources"]["v1"]["categories"]
    out: dict[str, float] = {}
    for row in inputs["rows"]["v1"]:
        key = route_for("v1", row["model"])
        if not key or not all(t in row["tasks"] for tasks in cats.values() for t in tasks):
            continue
        means = [sum(row["tasks"][t] for t in tasks) / len(tasks) for tasks in cats.values()]
        out[key] = round(sum(means) / len(means), 2)
    return out


def _allowance(key: str) -> float:
    return float(GO_ALLOWANCE.get(key.removeprefix("opencode-go/"), 0)) if key.startswith("opencode-go/") else 0.0


def _rank(keys: list[str], scores: dict[str, float], band: float,
          overall: dict[str, float] | None = None) -> list[tuple[str, int | None]]:
    """[(route_key, band number or None)]: scored routes by score, best first (an equal score: larger allowance,
    then key), each tagged with its noise band; then the unscored routes by v1 overall where present, best first,
    then larger allowance, then key. The band never re-orders against the score."""
    overall = overall or {}
    scored = sorted((k for k in keys if k in scores), key=lambda k: (-scores[k], -_allowance(k), k))
    out: list[tuple[str, int | None]] = []
    band_no, top = 0, None
    for k in scored:
        if top is None or top - scores[k] > band + 1e-9:
            band_no, top = band_no + 1, scores[k]
        out.append((k, band_no))
    unscored = sorted((k for k in keys if k not in scores),
                      key=lambda k: (k not in overall, -overall.get(k, 0.0), -_allowance(k), k))
    return out + [(k, None) for k in unscored]


def comparable(inputs: dict[str, Any], category: str) -> tuple[bool, list[str]]:
    """(release 2026-10-14 is comparable with 2026-10-25 in *category*, the shared models whose scores differ): it is
    comparable when at least one model has a score in both and every such model has the same score."""
    new = {r["model"]: r["scores"][category] for r in inputs["rows"]["2.0"] if category in r["scores"]}
    old = {r["model"]: r["scores"][category] for r in inputs["rows"][R1014] if category in r["scores"]}
    shared = sorted(set(new) & set(old))
    differ = [m for m in shared if abs(float(new[m]) - float(old[m])) > 1e-9]
    return bool(shared) and not differ, differ


def _served_builds(inputs: dict[str, Any], category: str) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """({route: its 2026-10-14 source ref without band}, {route: why it stays unscored}) for the SERVED_BUILDS."""
    ok, differ = comparable(inputs, category)
    rows = {r["model"]: r for r in inputs["rows"][R1014]}
    src = inputs["sources"][R1014]
    refs: dict[str, dict[str, Any]] = {}
    notes: dict[str, str] = {}
    for key, (lb_id, _why) in sorted(SERVED_BUILDS.items()):
        row = rows.get(lb_id)
        if row is None or category not in row["scores"]:
            notes[key] = f"no score of its served build {lb_id} in LiveBench release {src['release']}"
        elif not ok:
            notes[key] = (f"LiveBench release {src['release']} scored its served build {lb_id}, but that release is not "
                          f"comparable with {R2_RELEASE} in {category} ({len(differ)} models scored in both differ: "
                          "the category was re-run), so it stays unscored here")
        else:
            refs[key] = {"url": src["url"], "release": src["release"], "category": category,
                         "score": float(row["scores"][category]), "nq": int(row["nq"][category])}
    return refs, notes


def never_listed(keys: list[str]) -> list[str]:
    """Findings for listed routes that AGENTS.md invariant 6 refuses (aeos.core.model_policy, the one check)."""
    return [f"route {k} is refused: {fam} is never listed (AGENTS.md invariant 6, ADR-0129)"
            for k in keys if (fam := model_family_refusal(k))]


def build_data(inputs: dict[str, Any], inputs_sha256: str) -> dict[str, Any]:
    go_keys = sorted(go_key(m) for m in GO_ALLOWANCE if m not in GO_SUPERSEDED)
    superseded_keys = sorted(go_key(m) for m in GO_SUPERSEDED)
    free_keys = sorted(free_key(m) for m in OPENROUTER_FREE_TOOLS)
    paid_keys = sorted(OPENROUTER_PAID)
    refused = never_listed([*go_keys, *superseded_keys, *free_keys, *paid_keys])
    if refused:
        raise ValueError("; ".join(refused))
    overall = v1_overall(inputs)
    v1_src = inputs["sources"]["v1"]
    agents: dict[str, Any] = {}
    for agent, (release, category) in sorted(AGENT_CATEGORIES.items()):
        scores, nq = _scores(inputs, release, category)
        served: dict[str, dict[str, Any]] = {}
        served_notes: dict[str, str] = {}
        if release == "2.0":
            served, served_notes = _served_builds(inputs, category)
            for key, ref in served.items():
                scores.setdefault(key, ref["score"])
        nq_used = nq if nq is not None else V1_ASSUMED_NQ
        band = noise_band(nq_used)
        src = inputs["sources"][release]
        label = V1_CATEGORY_LABEL.get(category, category)
        entries: list[dict[str, Any]] = []
        tiers = (("OPENCODE_GO", _rank(go_keys, scores, band, overall) + _rank(superseded_keys, scores, band, overall)),
                 ("OPENROUTER_FREE", _rank(free_keys, scores, band, overall)),
                 ("OPENROUTER_PAID", _rank(paid_keys, scores, band, overall)))
        for tier, ranked in tiers:
            for key, band_no in ranked:
                entry: dict[str, Any] = {"route_key": key, "tier": tier, "score": scores.get(key),
                                         "allowance_usd_month": _allowance(key) or None}
                model = key.removeprefix("opencode-go/")
                if key in served:
                    ref = served[key]
                    entry["source_refs"] = [{"url": ref["url"], "release": ref["release"], "category": label,
                                             "score": ref["score"], "nq": ref["nq"], "band": band_no,
                                             "date": inputs["fetched"]}]
                    entry["served_build"] = SERVED_BUILDS[key][1]
                elif key in scores:
                    entry["source_refs"] = [{"url": src["url"], "release": src["release"], "category": label,
                                             "score": scores[key], "nq": nq, "band": band_no,
                                             "date": inputs["fetched"]}]
                else:
                    entry["source_refs"] = []
                    entry["note"] = served_notes.get(key) or NOT_MAPPED.get(
                        key, f"no score in LiveBench {release} release {src['release']}")
                    entry["order_evidence"] = (
                        {"basis": "LiveBench v1 overall", "url": v1_src["url"], "release": v1_src["release"],
                         "value": overall[key]} if key in overall else
                        {"basis": "monthly allowance USD", "value": _allowance(key) or None})
                if key.startswith("opencode-go/") and model in GO_TRANSLATION:
                    entry["serving"] = TRANSLATION_STATEMENT
                if key.startswith("opencode-go/") and model in GO_SUPERSEDED:
                    entry["overflow_only"] = (f"superseded by {go_key(GO_SUPERSEDED[model])}: listed after every other "
                                              "Go route (owner R34)")
                if key in OPENROUTER_PAID:
                    entry["paid_use"] = OPENROUTER_PAID[key]
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
        "decision": "owner decision R31, 2026-10-28 (ADR-0126): best models first, matched to each role's needs; "
                    "owner decision R34, 2026-10-28 (ADR-0129): every Go model incl. the translation candidates, the "
                    "paid OpenRouter DeepSeek route for escalation and overflow only",
        "method": {
            "release_per_category": {"2.0": R2_RELEASE, "v1": V1_RELEASE},
            "served_build": f"a route in served_builds takes the LiveBench {R1014_RELEASE} score of the build it "
                            f"serves, only in a category where {R1014_RELEASE} and {R2_RELEASE} give every model "
                            "they both score the same score (comparable); otherwise it is unscored there",
            "noise_band": "1.96 * 50 / sqrt(nq) points (95% half-width, worst case p=0.5)",
            "v1_nq": f"not published in the v1 table; {V1_ASSUMED_NQ} assumed (NOT-PROVEN)",
            "order": "best first: category score, highest first; an equal score: larger monthly allowance, then "
                     "route key (owner 2026-10-28: 'model-model terbaik saat ini yang berjalan, selanjutnya model "
                     "lainnya')",
            "band": "metadata only (source_refs[].band, for later load spreading): a band takes every model within "
                    "the noise band of its best remaining score; it never re-orders against the score",
            "unscored": "never omitted; after the scored routes of the tier: LiveBench v1 overall where present "
                        "(highest first), then larger monthly allowance, then route key (order_evidence)",
            "tiers": "OPENCODE_GO first (the superseded routes last, overflow only), then OPENROUTER_FREE (PUBLIC only: "
                     "eligibility filters it for INTERNAL), then OPENROUTER_PAID (escalation and overflow only, never "
                     "PUBLIC: paid_use)",
            "never_listed": "any Claude-family model and meta-routers (aeos.core.model_policy, AGENTS.md invariant 6); "
                            "Gemini 4 Argon and the Qwen models are listed since owner answer R34 (serving NOT PROVEN "
                            "until their probes pass)",
            "effect": "re-orders routes that are already eligible; never makes a route eligible",
        },
        "aliases": {f"{rel}:{lb}": {"route_key": key, "reason": why} for (rel, lb), (key, why) in sorted(ALIASES.items())},
        "served_builds": {key: {"livebench_id": lb, "release": R1014_RELEASE, "reason": why}
                          for key, (lb, why) in sorted(SERVED_BUILDS.items())},
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
