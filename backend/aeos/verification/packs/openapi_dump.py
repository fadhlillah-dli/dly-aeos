"""Print the OpenAPI document of a DLY AEOS-layout app (contract-impact diff, §16 Backend/API "contract", W6-045).

Run by the verifier through its runner with ``PYTHONPATH=<tree>/backend`` - once for the recorded base commit
and once for the candidate head - as ``python -c <this file> <output json path>``. It imports the tree's own
``aeos.api.app.create_app`` (no database, no lifecycle hooks) and writes ``app.openapi()``; the verifier compares
the two documents itself. Prints one JSON status line.

The verifier's result seal (``aeos.verification.seal``) is imported before the tree's app: the host uses only the
status sealed at the very end of this driver, which carries the sha256 of the document it wrote (``files``), so a
document rewritten afterwards (an ``atexit`` hook of the imported app) is detected instead of diffed. Absent outside
a sealed verifier step.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

try:
    import aeos_vseal as _vseal
except Exception:
    _vseal = None

if __name__ == "__main__":
    status: dict[str, object] = {"ok": False}
    try:
        from aeos.api.app import create_app

        app = create_app(testing=True, init_db=False)
        doc = app.openapi()
        with open(sys.argv[1], "w", encoding="utf-8") as fh:
            json.dump(doc, fh, sort_keys=True)
        with open(sys.argv[1], "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        status = {"ok": True, "paths": len(doc.get("paths") or {}),
                  "files": {os.path.basename(sys.argv[1]): digest}}
    except Exception as exc:  # reported, never raised to the verifier
        status = {"ok": False, "error": type(exc).__name__ + ": " + str(exc)[:300]}
    if _vseal is not None:
        _vseal.emit("result", d=json.loads(json.dumps(status, default=str)))
    print(json.dumps(status))
