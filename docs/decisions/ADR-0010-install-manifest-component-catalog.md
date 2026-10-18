# ADR-0010 — InstallManifest as the reviewed component catalog (DESIGN lens)

* Status: ACCEPTED (seed build under ADR-0003; human review of the manifest content remains required)
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by the seed build (native Claude, ADR-0003)

## Context

Master §07/§13 require tool selection and versions to live in a reviewed InstallManifest that records
official source, pin/digest, license, compatibility, configuration references, ports, network and
credential scope, health probe, capability test, cost and uninstall/recovery notes. §37/§45 require
every catalogued tool, repository and method to stay discoverable, with labels that distinguish design
from runtime, and forbid guessed URLs, licenses, maintainers or "latest" versions. §36 requires typed,
provenanced relations for the System Map.

## Decision

1. `install-manifest/manifest.yaml` (protected path, §09) is the single reviewed catalog; it is validated
   by `install-manifest/schema.json` (JSON Schema 2020-12) plus semantic rules in
   `aeos.inventory.manifest`:
   * `source_url` is either a URL that appears verbatim in the master document or `null`;
   * `identity_verified` is `VERIFIED` only with such a canonical URL; otherwise `UNVERIFIED`;
   * `license` stays `null` (the master states none); `approved_pin: null` means UNKNOWN; `latest` is
     rejected; UNVERIFIED, CANDIDATE and REFERENCE_ONLY entries can never carry an install operation;
   * every `source_refs` id must exist in master §52; health probes may only target loopback settings.
2. One component per tool: every §45 row is split (e.g. Serena / Context7, Gas Town / Ruflo,
   MetaGPT / ChatDev / CrewAI), plus the runtime pieces this repository actually uses and one
   `platform_module` per §10 control-plane module, so the §36 logical map can be drawn from records.
   100 components at adoption.
3. Domains are `PLATFORM`, `PERSONAL` or `OFFICE`; PERSONAL-only (GitHub, Google) and OFFICE-only
   (Bitbucket, Jira, Confluence) components never appear in the other domain's views.
4. Relations use the §36 vocabulary (`depends_on`, `calls`, `reads`, `writes`, `publishes_to`,
   `verifies`, `stores_in`, `runs_with`, `implements`, `governed_by`, `replaces`) plus `alternative_to`
   from §37 ("Depends-on/used-by/replaces/alternative-to"). Each carries data description, operation,
   boundary and identity reference; provenance is the manifest content hash.
5. Loading (`aeos seed`) stores an `InstallManifestRecord` per content hash and upserts
   `ComponentReference` rows in mode DESIGN. It never sets admission, enablement, lifecycle or any
   observed dimension; decision-side columns survive reloads; removed components and relations are
   tombstoned, never deleted.

## Consequences

* The dashboard can show the full catalog from day one without implying installation (§50 row
  "Katalog blueprint ... tidak otomatis INSTALLED/ENABLED/IN_USE").
* Candidate identities that the master leaves open (DDorchestrator, codebase-memory-mcp, Goose, Pi,
  OpenHands, Deep Agents, Docker MCP Gateway, Infisical, Langfuse, OpenTelemetry, Promptfoo, Copier)
  stay UNVERIFIED with `null` URLs until research records a canonical source.
* Pins are UNKNOWN until a human decision approves them after capability tests; version drift is only
  computed against approved pins.
* Changing the manifest is a reviewed change (protected path); the Doctor reports when the file
  differs from the loaded record.
