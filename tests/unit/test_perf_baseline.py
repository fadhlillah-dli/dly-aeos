"""Performance baseline (master §31 G3 "performance baseline").

Measures p50/p95 latency of the key read endpoints of the in-process control plane on this machine and,
when ``DLY_AEOS_PERF_REPORT`` names a file, writes the numbers there (the CI ``browser-e2e [G3]`` job publishes
them in its step summary). The numbers are a BASELINE for comparison, REPORT_ONLY: no latency objective has
been decided by the owner (§33 SLO targets are owner decisions), so nothing passes or fails on them. The only
assertion is a sanity ceiling that catches a pathological regression (e.g. an N+1 query on every request).
"""

from __future__ import annotations

import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path

from aeos.core.paths import repo_root
from tests.conftest import AuthedClient

ENDPOINTS = ("/api/health", "/api/projects", "/api/doctor/capabilities", "/api/control-center/activity",
             "/api/decisions", "/api/ops/sli", "/api/inventory/components")
SAMPLES = 15
SANITY_CEILING_MS = 5000.0  # not an objective: a request taking longer than this is a defect on any machine


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, int(round(0.95 * len(ordered))) - 1)]


def test_read_endpoint_latency_baseline(personal_client: AuthedClient, db):
    for mod in ("aeos.policy.seed", "aeos.inventory.seed", "aeos.portfolio.seed"):
        __import__(mod, fromlist=["run"]).run(db)
    db.commit()
    results: dict[str, dict[str, float | int]] = {}
    for path in ENDPOINTS:
        assert personal_client.get(path).status_code == 200, path  # warm-up, and the endpoint really answers
        timings = []
        for _ in range(SAMPLES):
            t0 = time.perf_counter()
            r = personal_client.get(path)
            timings.append((time.perf_counter() - t0) * 1000.0)
            assert r.status_code == 200, path
        results[path] = {"n": SAMPLES, "p50_ms": round(statistics.median(timings), 2),
                         "p95_ms": round(_p95(timings), 2), "max_ms": round(max(timings), 2)}
    slow = {p: v for p, v in results.items() if float(v["p95_ms"]) > SANITY_CEILING_MS}
    assert not slow, f"pathological latency (sanity ceiling {SANITY_CEILING_MS} ms): {slow}"
    report = {"status": "REPORT_ONLY", "note": "baseline numbers; no owner latency objective decided (§33)",
              "python": sys.version.split()[0], "platform": platform.platform(), "endpoints": results}
    out = os.environ.get("DLY_AEOS_PERF_REPORT")
    if out:
        Path(out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def test_ci_publishes_the_baseline_as_a_report_in_g3():
    ci = (repo_root() / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = ci.split("browser-e2e:", 1)[1].split("\n  bootstrap:", 1)[0]
    assert "tests/unit/test_perf_baseline.py" in job and "DLY_AEOS_PERF_REPORT" in job
    assert "GITHUB_STEP_SUMMARY" in job  # published as a report, never as a pass claim
