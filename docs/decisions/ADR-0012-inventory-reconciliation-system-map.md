# ADR-0012 — Inventory reconciliation, drift and the three System Map lenses

* Status: ACCEPTED (seed build under ADR-0003)
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by the seed build (native Claude, ADR-0003)

## Context

Master §36-§38 require separate DESIGN / CONFIG / OBSERVED lenses, six drift classes, separate status
dimensions with time and source, an auditable non-LLM reconciliation that never installs or adopts,
coverage gaps instead of "everything mapped", domain separation, UNKNOWN for uninstrumented nodes and
edges, and immutable ArchitectureSnapshots for render/diff/audit.

## Decision

1. **Probes** (`aeos.inventory.probes`) are read-only and bounded: list-argv subprocesses with timeouts
   and process-tree kill (psutil), loopback-only HTTP unless a caller explicitly opts into a remote
   read-only request, child environments scrubbed of `ANTHROPIC_*`/alternative-provider variables, and
   paths reduced to logical names (`<repo>`, `<data>`, `<home>/.../x`). Only binaries named by the
   manifest are looked up; candidates/references are not probed before an admission decision.
2. **Managed scope** reads are limited to named repository files: `pyproject.toml`,
   `requirements.lock`, `frontend/package-lock.json`, `.github/workflows/*`, `.vscode/extensions.json`
   and MCP/agent configs a non-bare session would load (`.mcp.json`, `.claude/settings.json`,
   `.vscode/mcp.json`, `opencode.json[c]`). Lockfile entries are *declarations* (build dependencies),
   not running processes. MCP servers configured in scope but absent from the manifest become
   `unregistered:mcp:<name>` observations — never adopted, never given access.
3. **Observations** record dimension, status, observer, version/identity, logical location, redacted
   operation reference, scope (run id) and expiry; each emits `inventory.observation`. Unknown
   dimensions/statuses are rejected.
4. **CONFIG lens** requires an explicit basis: enablement by decision, APPROVED/TRIAL model routes,
   project integrations in the viewer's domain, enabled agent definitions, or being part of the running
   released controller (platform service/module/datastore/runtime/manifest/repository observed
   INSTALLED). Default settings are not enablement.
5. **Drift** is computed from fresh observations only: planned-but-missing, installed-but-disabled
   (installed without CONFIG basis — "Terpasang tetapi tidak aktif"), enabled-but-unverified,
   resolved-version-mismatch (approved pin, repo identity, manifest hash), observed-but-unregistered and
   obsolete-reference (live relations/observations pointing at tombstoned components). Findings are
   persisted, deduplicated and resolved (never deleted); a scoped refresh only resolves the refreshed
   component's own classes; each component keeps its own probe status and time.
6. **System Map** nodes are component references; OBSERVED-lens design edges between observed nodes
   are labelled UNKNOWN ("tidak diinstrumentasi"). PERSONAL sessions never see OFFICE-only nodes or edges
   (and vice versa). Blast radius walks typed dependency edges (excluding `alternative_to`/`replaces`).
7. **ArchitectureSnapshot** refs hold manifest record + hash, master hash, ADR hashes, frozen contract
   hashes, active decisions and the DESIGN/CONFIG nodes/edges of the snapshot's domain; the hash excludes
   the timestamp so identical states compare equal. The table is append-only through SQLite triggers
   installed with the table (same names `aeos.core.db` would use).
8. **Inventory changes invalidate readiness** (§08, §38 "Drift dan kegagalan"). When reconciliation
   sees the installation/configuration status, resolved version or identity of a component change
   relative to its own previous observation, it calls `aeos.doctor.facade.invalidate` before recording
   the new observation, with only the invalidators that depend on it: `binary:<name>` for binary
   probes, `repo:head` / `repo:origin` for the platform repository (a new commit does not stale the
   origin check), otherwise `component:<id>`. A new InstallManifest content hash invalidates
   `manifest`. Changes are announced with `inventory.component.changed` and listed in the run summary.
9. **OBSERVED lens precedence.** The most significant fresh fact wins by dimension (usage >
   connectivity > installation > declaration > discovery), not by timestamp, and a fresh
   NOT_INSTALLED/REMOVED/INSTALL_FAILED is never hidden by a later lockfile declaration (§50 "Tool
   ditemukan dalam README atau lockfile"). Probe subprocesses never inherit `ANTHROPIC_*` variables,
   even when the caller passes no explicit environment.

## Consequences

* Until runtime modules emit usage observations, usage stays NEVER_OBSERVED and the OBSERVED lens shows
  mostly installation/declaration facts — by design, not a gap in honesty.
* Many platform tools show "installed-but-disabled" (INFO) until enablement decisions exist; that is the
  truthful state of the seed.
* Python packages have no lockfile yet; the coverage view reports `requirements.lock` as a gap.

## Amendment 2026-10-18 (integration)

* Platform-module implementation status (`implemented` FULL/PARTIAL/SKELETON) and the Doctor's
  `module_present` share one rule, `aeos.inventory.probes.source_is_placeholder`: a module is a
  placeholder only when the first line of its docstring carries the foundation marker
  `PLACEHOLDER —` (or `–`, `-`, `:`). The earlier "word anywhere in the first 2 KiB" test would report a
  real module as a skeleton as soon as its docstring mentioned placeholders.
* Reconciliation keeps calling `aeos.doctor.facade.invalidate` for changed binaries/identities (item 8);
  the Doctor now shows `secret:<ref>` invalidators as `secret-ref <ref>` in stored reasons, events and the
  observations it expires (ADR-0011 amendment), so redaction never rewrites them.

## Amendment 2026-10-19 (C4 audit closure)

The decision-side and usage dimensions are now projections of the records that own them (no second state):

* **Admission / final lifecycle** (`aeos.inventory.decisions`) are derived from the Decision registry for the
  viewer's domain: ChangeProposal decisions on *candidates* (TRIAL -> TRIAL_APPROVED, PROMOTION -> APPROVED,
  REJECTION -> REJECTED, open proposal -> UNDER_REVIEW) and on *existing* components of REMOVE/REPLACE
  (PROMOTION -> RETIRING, ACCEPTED outcome -> RETIRED/DEPRECATED, ROLLED_BACK -> ACTIVE), plus explicit component
  decisions (`target_type="component"`, e.g. SUSPENDED/QUARANTINED via `POST /api/inventory/components/{id}/decision`,
  recorded through the policy facade, bound to the manifest hash). Rows are never deleted; tombstones show RETIRED.
  `set_admission`/`set_lifecycle` (facade) cache a value only for an ACTIVE human decision that binds the component
  (current manifest hash / current proposal baseline+candidate hashes) and that decided exactly that value.
  The cached columns are never read back as authority: a revoked or stale decision stops counting at once. A direct
  component decision can only restrict (SUSPENDED/REJECTED/UNDER_REVIEW, QUARANTINED/DEPRECATED/RETIRING/RETIRED;
  OFFICE-only components get OFFICE-tagged decisions); TRIAL_APPROVED/APPROVED come from the proposal flow only and
  a restriction is lifted by revoking its decision.
* **Enablement** is ENABLED only with a human-decided basis: explicit decision, APPROVED/TRIAL route, enabled agent
  definition, a project integration whose provider write enablement is effective, or the running released
  controller. An integration record alone stays DISABLED (CONFIG lens: CONFIGURED) — the seeded catalog therefore
  still shows DISABLED everywhere (§50 3.1).
* **Usage / OBSERVED edges** (`aeos.inventory.runtime`) come from WorkerRun+Lease (IN_USE while the lease is valid,
  STALE after it expires, LAST_USED when finished), the usage ledger written by the broker/bridge (LAST_USED, broker
  -> provider edge), SUCCEEDED LIVE publication operations and Claude capability tests — each with its run/operation
  reference, domain-scoped and limited to the projects the viewer may see (`allowed_project_ids`, like the
  workforce views). Producers may also call `record_usage_observation` / `record_relation_observation`. Views read
  the latest observation *visible to the session's domain* (`latest_observations(visible_to=domain)`): an
  OFFICE-tagged observation of a shared component never reaches a PERSONAL view. Routes follow the routing
  admission rule: an empty `eligible_domains` serves no domain (no enablement, no route node, no auth scope).
* **Identity of the reviewed artifact**: the manifest schema gains `approved_digest`, `distribution`, `signature`,
  `ports`, `config_refs`, `compatibility`, `tool_schema_hash` and `cost.free_until` (validated: Settings keys,
  loopback ports, no guessed distribution URL). The reconciler records the sha256 of a probed executable as its
  observed identity; a digest that differs from `approved_digest` is `resolved-version-mismatch` (key `digest`).
  Fingerprints include a cached content digest (re-verified every 30 min) plus inode/ctime.
* **Drift severity** comes from the reviewed `policy/inventory-drift.yaml` (defaults identical to the old constants).
* **Records as nodes**: approved routes, roles of enabled agents and project repositories (CONFIG/OBSERVED), and the
  services declared by an approved PlanVersion / repository markers (DESIGN, with a selected project) appear on the
  map and in Tools & Repos with their record source. The map accepts `project_id`, `environment` and `snapshot`
  (renders an ArchitectureSnapshot); snapshots can be approved (human APPROVAL bound to the snapshot hash) and the
  diff highlights added/removed/changed edges on the graph.
* **Dependencies / SBOM**: `requirements.lock` and `package-lock.json` are itemised (direct/transitive, parents,
  "digunakan oleh build X") behind `GET /api/inventory/components/{id}/dependencies`; `GET /api/inventory/sbom` and
  `aeos inventory sbom` produce CycloneDX 1.5 from the same lockfiles; CycloneDX files under `sbom/` are read as a
  declaration source. Container images are an explicit coverage gap.
* **API**: drift and snapshot lists are paginated; one reconciliation per scope at a time (`RECONCILE_IN_PROGRESS`);
  a full refresh within 10 s of the previous one is refused (`REFRESH_TOO_SOON`); optional TTL-driven polling
  (`aeos inventory poll --enable`, off by default, max 5 components per minute).

Honest limits kept: no probe recomputes a tool/MCP schema (would require running the tool), signature verification
of release artifacts is not implemented (recorded as UNKNOWN / NOT_VERIFIED), PROMOTE does not yet require the
digest or capability-test evidence (proposals module), and SBOM generation in CI is not wired (protected workflows).
