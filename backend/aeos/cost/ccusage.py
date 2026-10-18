"""Optional ccusage estimator for native Claude Code usage (§42, S31).

ccusage reads local Claude Code usage logs. Its output is an ESTIMATE — never an invoice or a quota
authority. Dollar figures it computes are API-equivalent prices and are deliberately dropped: native
Max usage is not converted into dollars or a percentage of the subscription (§42).

The estimator never installs anything: it runs only an already-installed ``ccusage`` binary, or
``npx --no-install ccusage`` when npx is present (``--no-install`` refuses to download), with
``--offline`` so no pricing data is fetched from the network.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from typing import Any

from aeos.core.redaction import redact_text
from aeos.core.timeutil import utcnow

_SECRETISH = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
_TOKEN_FIELDS = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "cacheCreationTokens": "cache_creation_tokens",
    "cacheReadTokens": "cache_read_tokens",
    "totalTokens": "total_tokens",
}

Runner = Callable[[list[str], int, dict[str, str]], subprocess.CompletedProcess[str]]


def _default_runner(cmd: list[str], timeout: int, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, check=False)


def _clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in _SECRETISH)}
    env["npm_config_offline"] = "true"  # npx must not reach the registry
    return env


def find_command() -> list[str] | None:
    exe = shutil.which("ccusage")
    if exe:
        return [exe]
    npx = shutil.which("npx")
    if npx:
        return [npx, "--no-install", "ccusage"]
    return None


def _tokens(row: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for src, dst in _TOKEN_FIELDS.items():
        v = row.get(src)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[dst] = int(v)
    return out


def parse_daily(stdout: str) -> dict[str, Any]:
    data = json.loads(stdout)
    if not isinstance(data, dict):
        raise ValueError("unexpected ccusage output")
    days = []
    for row in data.get("daily") or []:
        if not isinstance(row, dict):
            continue
        models = row.get("modelsUsed") or row.get("models") or []
        days.append({"date": str(row.get("date", ""))[:32], **_tokens(row),
                     "models": [str(m)[:80] for m in models if isinstance(m, str)][:20]})
    totals = _tokens(data.get("totals") or {})
    return {"days": days, "totals": totals}


def ccusage_estimate(*, timeout: int = 60, runner: Runner | None = None,
                     command: list[str] | None = None) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source": "ccusage (log lokal Claude Code)", "mode": "ESTIMATE",
        "authority": "Bukan invoice dan bukan otoritas kuota (§42)",
        "dollar_figures": "Tidak ditampilkan: penggunaan native Max tidak dikonversi ke dolar (§42)",
        "scope_note": "Agregat lokal lintas sesi Claude Code; bukan per domain/proyek.",
        "checked_at": utcnow().isoformat(),
    }
    cmd = command if command is not None else find_command()
    if not cmd:
        return base | {"available": False, "status": "NOT_AVAILABLE",
                       "reason": "ccusage/npx tidak ditemukan; tidak dipasang otomatis.",
                       "remedy": "Opsional (keputusan pemilik): npm install -g ccusage, lalu jalankan ulang."}
    run = runner or _default_runner
    try:
        proc = run([*cmd, "daily", "--json", "--offline"], timeout, _clean_env())
    except subprocess.TimeoutExpired:
        return base | {"available": True, "status": "FAILED", "reason": f"ccusage timed out after {timeout}s"}
    except OSError as exc:
        return base | {"available": False, "status": "FAILED", "reason": redact_text(str(exc))[:300]}
    if proc.returncode != 0:
        return base | {"available": True, "status": "FAILED",
                       "reason": redact_text((proc.stderr or proc.stdout or "")[:400]) or f"exit {proc.returncode}"}
    try:
        parsed = parse_daily(proc.stdout)
    except (ValueError, json.JSONDecodeError) as exc:
        return base | {"available": True, "status": "FAILED", "reason": f"unparseable ccusage output: {exc}"[:300]}
    return base | {"available": True, "status": "ESTIMATE", **parsed}
