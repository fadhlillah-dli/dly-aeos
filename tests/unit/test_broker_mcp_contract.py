"""§11/§25 "MCP: curated AEOS MCP interface": not implemented yet, so it is registered as a PLANNED design
gap in the InstallManifest (System Map / Tools view) — never as an installed or active capability."""

from __future__ import annotations

from sqlalchemy import select

from aeos.inventory import manifest as mf
from aeos.models.inventory import ComponentReference


def test_mcp_interface_is_a_planned_design_gap_not_a_capability(db):
    load = mf.load_manifest()
    comps = {c["component_id"]: c for c in load.data["components"]}
    mcp = comps["aeos-mcp-interface"]
    assert mcp["design_label"] == "PLANNED" and mcp["requirement"] == "OPTIONAL"
    assert mcp["identity_verified"] == "UNVERIFIED" and mcp["source_url"] is None
    assert mcp["probe"]["type"] == "none" and "install" not in mcp["operations"]
    assert {r["target"] for r in mcp["relations"]} == {"capability-broker"}
    mf.upsert_components(db, load.data, manifest_hash=load.content_hash)
    db.commit()
    ref = db.scalar(select(ComponentReference).where(ComponentReference.component_id == "aeos-mcp-interface"))
    assert ref is not None and ref.mode == "DESIGN"
    assert ref.enablement == "DISABLED" and ref.admission_status == "DISCOVERED"
