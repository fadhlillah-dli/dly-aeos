"""Cross-module contract for inventory observations (§37, §38). Owned by ``aeos.inventory``.

Observations never grant permission and never rewrite desired state.

Additive (2026-10-19, C4): runtime producers may record usage/relation observations explicitly, and
decision flows may cache an admission/lifecycle change bound to an ACTIVE human Decision. The
inventory itself also derives usage and decision-side dimensions from the owning records (run,
lease, usage ledger, publication outbox, Decision registry), so callers are never required to.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from aeos.models.inventory import ComponentObservation, ComponentReference, ComponentRelation


def record_observation(
    session: Session, *, component_id: str, dimension: str, status: str, observer: str,
    observed_version: str | None = None, observed_identity: str | None = None,
    detail: dict[str, Any] | None = None, ttl_seconds: int | None = 3600, evidence_id: str | None = None,
    scope: dict[str, Any] | None = None,
) -> ComponentObservation:
    kwargs = dict(locals())  # captured before the lazy import adds 'impl'
    from aeos.inventory.observations import record_observation as impl

    return impl(**kwargs)


def get_component(session: Session, component_id: str) -> ComponentReference | None:
    return session.get(ComponentReference, component_id)


def record_usage_observation(
    session: Session, *, component_id: str, status: str, observer: str, run_id: str | None = None,
    operation_ref: str | None = None, ttl_seconds: int | None = 900, domain: str | None = None,
    detail: dict[str, Any] | None = None,
) -> ComponentObservation:
    """Usage/liveness (§38): IN_USE needs a run/process/operation reference and a time window (ttl);
    a finished request is LAST_USED."""
    kwargs = dict(locals())
    from aeos.inventory.observations import record_usage_observation as impl

    return impl(**kwargs)


def record_relation_observation(
    session: Session, *, source_id: str, target_id: str, relation: str, observer: str,
    operation: str = "CALL", run_id: str | None = None, domain: str | None = None,
    data_description: str = "",
) -> ComponentRelation:
    """Upsert an OBSERVED-lens edge (actual traffic/event) with ``last_observed_at`` = now (§36)."""
    kwargs = dict(locals())
    from aeos.inventory.observations import record_relation_observation as impl

    return impl(**kwargs)


def set_admission(session: Session, *, component_id: str, status: str, decision_id: str, actor: str
                  ) -> ComponentReference:
    """Cache an admission state bound to an ACTIVE human Decision that decided exactly that state (audited;
    refused otherwise). The views always re-derive admission from the Decision registry."""
    from aeos.inventory.decisions import set_admission as impl

    return impl(session, component_id=component_id, status=status, decision_id=decision_id, actor=actor)


def set_lifecycle(session: Session, *, component_id: str, status: str, decision_id: str, actor: str
                  ) -> ComponentReference:
    """Cache a final-lifecycle state (DEPRECATED/QUARANTINED/RETIRING/RETIRED) bound to an ACTIVE human Decision
    that decided exactly that state. Never deletes rows or history; the views always re-derive from the registry."""
    from aeos.inventory.decisions import set_lifecycle as impl

    return impl(session, component_id=component_id, status=status, decision_id=decision_id, actor=actor)


def effective_enablement(session: Session, component_id: str, *, domain: str | None = None) -> dict[str, Any]:
    """§38 Enablement as the inventory shows it: ENABLED only with an explicit approved-config basis."""
    from aeos.inventory.views import enablement_for

    return enablement_for(session, component_id, domain=domain)
def blast_radius(session: Session, component_id: str, *, domain: str | None,
                 direction: str = "dependents") -> dict[str, Any]:
    """Who is affected if *component_id* changes/fails (``dependents``) or what it relies on (read-only)."""
    from aeos.inventory.views import blast_radius as impl

    return impl(session, component_id, domain=domain, direction=direction)


def component_visible(comp: ComponentReference, domain: str | None) -> bool:
    """§23/§38 scope rule: a PLATFORM component is shared; a PERSONAL-only / OFFICE-only one exists only in its
    own domain."""
    from aeos.inventory.views import visible

    return visible(comp, domain)


def relation_visible(rel: ComponentRelation, domain: str | None) -> bool:
    """A relation tagged with a domain is only that domain's record (untagged = shared)."""
    from aeos.inventory.views import _relation_visible

    return _relation_visible(rel, domain)


def observation_visible(obs: ComponentObservation, domain: str | None) -> bool:
    """An observation tagged with a domain is only that domain's evidence (untagged = shared)."""
    from aeos.inventory.views import _obs_visible

    return _obs_visible(obs, domain)


def tool_schema_hash(tool_defs: list[dict[str, Any]]) -> str | None:
    """§25 tool admission "schema": canonical hash of the tool set a model is offered (OpenAI ``tools`` entries
    or MCP ``tools/list`` items) - name, description and parameter schema per tool, order-independent. None when
    the set is empty (nothing observed, never a hash of nothing)."""
    from aeos.core.hashing import content_hash

    norm: dict[str, dict[str, Any]] = {}
    for t in tool_defs or []:
        if not isinstance(t, dict):
            continue
        inner = t.get("function")
        fn: dict[str, Any] = inner if isinstance(inner, dict) else t
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        norm[name] = {"name": name, "description": str(fn.get("description") or ""),
                      "parameters": fn.get("parameters") or fn.get("inputSchema") or fn.get("input_schema") or {}}
    if not norm:
        return None
    return content_hash([norm[k] for k in sorted(norm)])


def record_tool_schema_observation(session: Session, *, component_id: str, schema_hash: str, observer: str,
                                   status: str = "NOT_TESTED", evidence_id: str | None = None,
                                   tools: list[str] | None = None, ttl_seconds: int = 7 * 86400
                                   ) -> ComponentObservation:
    """A functional observation (status = the test's own result) carrying the observed tool schema hash; drift
    compares it with the hash approved at admission (``tool_schema_hash`` in the InstallManifest)."""
    from aeos.inventory.observations import record_observation as impl

    return impl(session, component_id=component_id, dimension="functional", status=status, observer=observer,
                detail={"tool_schema_hash": schema_hash, "tools": sorted(tools or [])}, ttl_seconds=ttl_seconds,
                evidence_id=evidence_id)


def _reviewed_manifest() -> tuple[dict[str, Any] | None, str | None]:
    """(data, None) of the validated reviewed InstallManifest file, or (None, error code) when it cannot be
    read/validated."""
    from aeos.core.errors import ValidationFailed
    from aeos.inventory.manifest import load_manifest

    try:
        return load_manifest().data, None
    except (ValidationFailed, OSError) as exc:
        return None, str(getattr(exc, "code", None) or type(exc).__name__)


def _manifest_component(component_id: str) -> dict[str, Any] | None:
    """The component entry of the reviewed InstallManifest file (validated load), or None when it is absent or
    the manifest cannot be read/validated (``manifest_pin`` / ``signer_pin`` tell those apart)."""
    data, _problem = _reviewed_manifest()
    for comp in (data or {}).get("components") or []:
        if isinstance(comp, dict) and comp.get("component_id") == component_id:
            return comp
    return None


def host_platform() -> tuple[str, str | None]:
    """(os, arch) of this host in the pin naming (``windows|darwin|linux``, ``amd64|arm64`` or None when no reviewed
    artifact exists for the architecture) - the ``<os>_<arch>`` key of a per-platform ``approved_digest`` and the OS
    key of a per-OS ``signature.approved_signer``."""
    from aeos.inventory.pins import host_arch, host_os

    return host_os(), host_arch()


def select_digest(raw: Any, platform: str | None = None) -> dict[str, Any]:
    """Select the approved digest for *platform* (default: this host) from a manifest ``approved_digest`` value (one
    ``sha256:`` for all platforms, or a mapping keyed ``<os>_<arch>``). See ``aeos.inventory.pins.select_digest``."""
    from aeos.inventory.pins import select_digest as impl

    return impl(raw, platform)


def signer_pin(component_id: str, *, os_name: str | None = None) -> dict[str, Any]:
    """The owner's publisher pin of *component_id* (``signature.approved_signer`` of the reviewed InstallManifest) that
    applies on this host's OS (or *os_name*): ``{"signer", "status" PINNED|NOT_PINNED|UNREADABLE, "reason"}``.

    UNREADABLE means the manifest could not be read or validated, so whether a pin exists is unknown - callers that
    gate on the pin fail closed instead of treating it as "not pinned" (read-only)."""
    from aeos.inventory.pins import select_signer

    data, problem = _reviewed_manifest()
    if data is None:
        return {"signer": None, "status": "UNREADABLE",
                "reason": f"install-manifest cannot be read/validated ({problem}): the publisher pin is unknown"}
    comp = next((c for c in data.get("components") or []
                 if isinstance(c, dict) and c.get("component_id") == component_id), None)
    sig = (comp or {}).get("signature")
    signer = select_signer(sig.get("approved_signer") if isinstance(sig, dict) else None, os_name)
    return {"signer": signer, "status": "PINNED" if signer else "NOT_PINNED", "reason": None}


def approved_signer(component_id: str, *, os_name: str | None = None) -> str | None:
    """The publisher identity the owner pinned for *component_id* in the reviewed InstallManifest
    (``signature.approved_signer``) that applies on this host's OS (or *os_name*): a plain string applies on
    every OS, a ``{windows, darwin}`` mapping per OS. None when nothing is pinned for this OS or the manifest
    cannot be read (read-only; a gate uses ``signer_pin`` to tell those two apart)."""
    return signer_pin(component_id, os_name=os_name)["signer"]


def approved_pin(component_id: str, *, session: Session | None = None) -> str | None:
    """The owner-approved version pin of *component_id* (§07 "pins approved by a human"), normalised (no leading
    ``v``): the seeded ComponentReference when a *session* is given (what the Doctor compares), else the reviewed
    InstallManifest file. None when no pin is approved (callers label their built-in default as such)."""
    from aeos.inventory.pins import normalise_pin

    if session is not None:
        ref = session.get(ComponentReference, component_id)
        return normalise_pin(ref.approved_pin) if ref is not None and ref.tombstoned_at is None else None
    return manifest_pin(component_id)["pin"]


def manifest_pin(component_id: str) -> dict[str, str | None]:
    """``{"pin", "problem"}``: the normalised ``approved_pin`` of *component_id* in the reviewed InstallManifest file
    (None when not approved), and the manifest's error code when it cannot be read/validated - the pin is then
    UNKNOWN, never "not approved" (one validated load; read-only)."""
    from aeos.inventory.pins import normalise_pin

    data, problem = _reviewed_manifest()
    comp = next((c for c in (data or {}).get("components") or []
                 if isinstance(c, dict) and c.get("component_id") == component_id), None)
    return {"pin": normalise_pin((comp or {}).get("approved_pin")), "problem": problem}


def approved_digest(component_id: str, *, session: Session | None = None, platform: str | None = None
                    ) -> dict[str, Any]:
    """The owner-approved executable digest of *component_id* for this host's ``<os>_<arch>`` (or *platform*):
    ``{"digest", "status" RECORDED|NOT_RECORDED|INVALID, "platform", "form", "recorded_platforms", "reason"}``.
    A per-platform mapping without this platform's entry is NOT_RECORDED, never a mismatch."""
    from aeos.inventory.pins import select_digest

    if session is not None:
        ref = session.get(ComponentReference, component_id)
        raw = (ref.details or {}).get("approved_digest") if ref is not None else None
    else:
        raw = (_manifest_component(component_id) or {}).get("approved_digest")
    return select_digest(raw, platform)


def snapshot_checkpoint(session: Session, snapshot_id: str, *, domain: str | None) -> dict[str, Any]:
    """Meta of one ArchitectureSnapshot in *domain* (id, label, hash, counts, ``approved`` + the human APPROVAL bound
    to its exact hash). ``NotFound`` outside the domain. Read-only (§41 checkpoint input)."""
    from aeos.inventory.snapshots import get_snapshot, snapshot_meta

    return snapshot_meta(get_snapshot(session, snapshot_id, domain=domain), session)


def snapshot_delta(session: Session, before_id: str, after_id: str, *, domain: str | None) -> dict[str, Any]:
    """Compact architecture delta between two snapshots: node/edge ids added/removed/changed and which reference
    sets (manifest, master, ADRs, contracts, decisions) changed. Read-only."""
    from aeos.inventory.snapshots import diff_snapshots

    d = diff_snapshots(session, before_id, after_id, domain=domain)

    def ids(part: dict[str, Any], key: Any) -> dict[str, list[str]]:
        return {"added": sorted(str(key(x)) for x in part.get("added") or []),
                "removed": sorted(str(key(x)) for x in part.get("removed") or []),
                "changed": sorted(str(x.get("key")) for x in part.get("changed") or [])}

    return {"before": before_id, "after": after_id, "same_hash": d["same_hash"],
            "both_approved": d["both_approved"],
            "nodes": ids(d["nodes"], lambda n: n.get("id")),
            "edges": ids(d["edges"], lambda e: f"{e.get('source')}->{e.get('target')}:{e.get('relation')}"),
            "refs_changed": sorted(d["refs_changed"])}
