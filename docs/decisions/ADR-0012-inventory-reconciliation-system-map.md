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
