"""Print the OpenAPI document of a DLY AEOS-layout app (contract-impact diff, §16 Backend/API "contract", W6-045).

Run by the verifier through its runner with ``PYTHONPATH=<tree>/backend`` - once for the recorded base commit
and once for the candidate head - as ``python -c <this file> <output json path>``. It imports the tree's own
``aeos.api.app.create_app`` (no database, no lifecycle hooks) and writes ``app.openapi()``; the verifier compares
the two documents itself. Prints one JSON status line.
"""

from __future__ import annotations

import json
import sys

if __name__ == "__main__":
    status: dict[str, object] = {"ok": False}
    try:
        from aeos.api.app import create_app

        app = create_app(testing=True, init_db=False)
        doc = app.openapi()
        with open(sys.argv[1], "w", encoding="utf-8") as fh:
            json.dump(doc, fh, sort_keys=True)
        status = {"ok": True, "paths": len(doc.get("paths") or {})}
    except Exception as exc:  # reported, never raised to the verifier
        status = {"ok": False, "error": type(exc).__name__ + ": " + str(exc)[:300]}
    print(json.dumps(status))
