"""`aeos seed` hook: load the reviewed InstallManifest into the DESIGN lens (idempotent).

Seeded rows are DESIGN/CANDIDATE-labelled catalog entries. They are never installation,
connection, enablement, evaluation or usage evidence (§37 "Seed awal boleh mengisi katalog
desain ... tidak boleh otomatis menjadi installation evidence").
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from aeos.inventory.manifest import apply_manifest


def run(session: Session) -> dict[str, Any]:
    result = apply_manifest(session, actor="seed")
    return {
        "manifest_hash": result["content_hash"],
        "components": result["components"],
        "created": result["created"],
        "updated": result["updated"],
        "tombstoned": result["tombstoned"],
        "relations_created": result["relations_created"],
        "stored_new_record": result["stored_new_record"],
    }
