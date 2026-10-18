# ADR-0073 — OFFICE Atlassian adapters: disabled by default, Cloud-only, real folders or BLOCKED

* Status: PROPOSED (implementation decision inside master §04/§23/§29, S13-S15; ACCEPTED when merged under ADR-0003)
* Date: 2026-10-18
* Deciders: seed-build implementer (Claude Code, native Max) for the owner; owner review on merge

## Context

U-03: OFFICE uses Bitbucket + Confluence + Jira. §23: OFFICE is default deny until a recorded company
authorisation exists. §04: Atlassian Cloud is an assumption, Data Center needs a capability check
and its own adapter; the Confluence parent is configured as `CONFLUENCE_PROJECTS_PARENT_ID` (no
legacy alias). §29/§50: a dedicated project folder is mandatory; if the tenant cannot provide
folders the run is blocked — the structure is never silently changed.

## Decision

1. Confluence, Jira and Bitbucket adapters are implemented against the documented Cloud REST APIs
   (Confluence v2 folders/pages/properties + v1 attachments, Jira v3 issues + `search/jql`,
   Bitbucket 2.0 repository/PR read) and are exercised only with respx-mocked HTTP.
2. They are disabled unless the project integration has `enabled: true` (boolean) **and** every
   operation passes the external-write interlock, which owns the company-authorisation check.
3. `deployment` must be verified `CLOUD`; `DATA_CENTER` → NOT_SUPPORTED; unknown → BLOCKED.
   At execution the adapter probes the space (API v2) and the parent folder; a parent that is a page
   or a tenant without folder support → BLOCKED "needs action"; no page is created as a substitute.
4. Pages use storage format; rendered diagrams become attachments; Mermaid source is shown as a code
   macro next to a "visual pending" note — raw Mermaid is never assumed to render.
   Reconciliation requires the AEOS content property (title-only matches go to human review).
5. Bitbucket is read-only (repository/PR references). No adapter has a merge operation (U-09).
6. The adapters call the documented REST APIs directly; the Atlassian Remote MCP server (S13/S14) is
   not used for writes and is not assumed to work on Data Center.

## Consequences

* Real tenant behaviour, permissions and folder support are NOT-PROVEN (§51 P2) until the company
  authorises and provides tenant/space/parent/project IDs and a scoped credential at
  `office/atlassian` (`office/bitbucket` for Bitbucket).
* A Data Center deployment needs a separate adapter and ADR.

## Amendment (completeness review, 2026-10-18)

7. Confluence titles are unique per space and several projects share one
   `CONFLUENCE_PROJECTS_PARENT_ID`, so every page and sub-folder title carries the project key
   (`00. Product Home [<key>]`); the project folder keeps the project name. Create, update and
   reconcile use the same title function.
8. The capability probe re-validates the configured parent against the configured space: a parent
   folder in another space is BLOCKED "needs action" (§04: validated against tenant, space, access and
   target; never a second target).
9. Jira reconciliation uses the `search/jql` API, which may be eventually consistent right after a
   create; absence proof on a real tenant is NOT-PROVEN.
