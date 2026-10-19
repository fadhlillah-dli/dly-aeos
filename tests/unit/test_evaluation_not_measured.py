"""§44 "Jangan menilai dari banyaknya agent, pesan atau baris kode": the computed metrics carry no agent,
message or line-of-code count, and the leaderboard ranking ignores them even when samples report them."""

from __future__ import annotations

from typing import Any

from aeos.evaluation import definitions as D
from aeos.evaluation import service

FORBIDDEN = ("agent", "message", "loc", "line")


def _keys(obj: Any, prefix: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.append(f"{prefix}{k}")
            out.extend(_keys(v, f"{prefix}{k}."))
    return out


def _samples(status: str, n: int, *, agents: int, messages: int, loc: int) -> list[dict[str, Any]]:
    return [{"status": status, "first_pass": True, "agent_count": agents, "message_count": messages,
             "lines_of_code": loc, "agents": agents, "messages": messages, "loc": loc} for _ in range(n)]


def test_metrics_never_contain_agent_message_or_loc_counts():
    metrics = service.compute_metrics(_samples("ACCEPTED", 3, agents=40, messages=900, loc=12000))
    leaf = [k.rsplit(".", 1)[-1].lower() for k in _keys(metrics)]
    hits = [k for k in leaf if any(f in k for f in FORBIDDEN)]
    assert hits == [], hits
    assert {"jumlah agent", "jumlah pesan", "jumlah baris kode"} <= set(D.NOT_MEASURED)


def test_ranking_ignores_volume_even_when_samples_report_it():
    def entry(h: str, samples: list[dict[str, Any]]) -> dict[str, Any]:
        m = service.compute_metrics(samples)
        # a leaderboard entry is keyed by configuration + route binding + topology (C5 leaderboard entries)
        return {"entry_key": service._entry_key(h, None, None), "configuration_hash": h, "sample_size": m["sample_size"],
                "final_acceptance_rate": m["outcome"]["final_acceptance_rate"],
                "acceptance_ci95": m["outcome"]["acceptance_ci95"]}

    busy = entry("busy", _samples("ACCEPTED", 2, agents=50, messages=5000, loc=90000)
                 + _samples("FAILED", 8, agents=50, messages=5000, loc=90000))
    lean = entry("lean", _samples("ACCEPTED", 9, agents=1, messages=3, loc=10)
                 + _samples("FAILED", 1, agents=1, messages=3, loc=10))
    entries = [busy, lean]
    service._rank(entries)
    assert [e["configuration_hash"] for e in entries] == ["lean", "busy"]
    assert lean["rank"] < busy["rank"]
