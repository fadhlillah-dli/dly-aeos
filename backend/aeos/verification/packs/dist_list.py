"""List the distributions an interpreter sees (per-candidate dependency set identity, §09 Zone 2; W6-031).

Run with the per-candidate virtual environment's interpreter through the verifier runner. Reads package metadata
only; prints one JSON line ``{"pins": ["name==version", ...], "executable": ..., "prefix": ...}``.
"""

from __future__ import annotations

import importlib.metadata
import json
import sys

if __name__ == "__main__":
    pins: set[str] = set()
    for dist in importlib.metadata.distributions():
        name = str((dist.metadata.get_all("Name") or [""])[0] or "").strip().lower()
        if name:
            pins.add(f"{name}=={dist.version}")
    print(json.dumps({"pins": sorted(pins), "executable": sys.executable, "prefix": sys.prefix}))
