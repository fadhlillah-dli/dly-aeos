# ADR-0071 — Product 1 document model, content types and hash-based delta rendering

* Status: PROPOSED (implementation decision inside master §27/§28/§41; ACCEPTED when merged under ADR-0003)
* Date: 2026-10-18
* Deciders: seed-build implementer (Claude Code, native Max) for the owner; owner review on merge

## Context

§27 fixes the PRODUCT 1 structure (00 Product Home … 12 Retirement + Templates), distinguishes
folder / document / section, keeps Test Strategy inside the Test Plan and Release Notes inside the
Release Plan (U-08), and forbids fake PT/VA/UAT/restore results or approvals. §28 defines when
documents are created and the four content types. §41 requires hash/diff-driven updates instead of
regenerating the whole Product 1, plus a visible "documentation pending" label.

## Decision

1. `aeos.docs_engine.product1` is the single code source of the tree: folders only where a node holds
   several documents; Product Home, Product Overview, Development and Retirement are single
   documents with sections. Every section carries STATIC / DYNAMIC / AI_MAINTAINED / HUMAN_GOVERNED.
   The twelve templates in `blueprints/product1/templates/` are generated from the same spec and a
   test keeps them in sync.
2. The renderer is deterministic and LLM-free: approved plan content (DATA) is laid into sections.
   Missing content → "documentation pending"; DYNAMIC sections reference the live source (tracker,
   verifier evidence) instead of copying status; HUMAN_GOVERNED sections are left for humans and the
   renderer refuses to fill them; nodes whose §28 time has not come are NOT STARTED / NOT APPLICABLE
   with reason and accountable owner (a role is never assigned to an invented person).
3. Each document has an `input_hash` over exactly the plan slice it uses; an unchanged input hash
   means the document is not re-rendered at all. `content_hash` over the Markdown drives the
   publication delta. `DocumentRecord.source_plan_version_id` is the version that last changed the
   content. Only APPROVED versions update DocumentRecords; other versions can be previewed locally
   (`aeos docs render`).
4. `DocumentRevision` stores rendered snapshots (CURRENT/SUPERSEDED) and human edits observed at the
   provider (HUMAN_EDIT_INPUT, OPEN/ACKNOWLEDGED) as revision input.
5. Diagrams are Mermaid source in the plan, rendered with optional mermaid-cli when installed; when
   absent/failing the visual is VISUAL_PENDING with the diffable source kept — never a placeholder
   image. Diagrams are DESIGN artefacts, never runtime proof. Screenshot capture is NOT-PROVEN.

## Consequences

* The PlanVersion content schema is owned by planning; the renderer reads it tolerantly (documented
  aliases in `plan_content.py`). Unknown keys never silently become content.
* Section-level delta inside a document is not attempted: a changed document is updated as a whole
  under revision control (ADR-0072); human-edited documents are protected by CONFLICT.

## Amendment (completeness review, 2026-10-18)

6. **§28 feature approval "threat/assessment".** When the approved plan gives a feature
   `security_assessment{...}` (or `needs_security_assessment`) and/or `threat_model{...}`/`threats[]`,
   the renderer adds `04-security-assessments/security-assessment-<feature>` and
   `04-threat-models/threat-model-<feature>`. Findings are DYNAMIC (only from real reviews/scans);
   residual risk and approval are HUMAN_GOVERNED. Nothing is created when the plan gives nothing.
7. **Last reviewed (§27).** `POST /api/documents/{id}/review` (SUPER_ADMIN) records that a human read
   one exact `content_hash` (stale view → 409 STALE_CONTENT). A newer render shows REVIEW_STALE. This
   is review metadata only — never a plan/publication approval — and is not written into the body
   (that would change the hash it refers to). Renderer version 2 reflects this metadata wording.
8. **Logical ids from plan data** (RFC/TDD keys) are slugged and de-duplicated, so they are always
   valid single path segments and two documents never collapse into one. The local preview mirrors
   the folder nesting of the Product 1 tree.
