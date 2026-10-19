# ADR-0094 — Product 1 from schema-v1 plans; human-governed content survives publication

* Status: ACCEPTED (implementation); items marked *owner decision* below are open
* Date: 2026-10-19
* Deciders: owner (Fadhlillah); implemented by the Documentation & Visuals and Publisher modules

## Context

An audit of master §27–§32 found that the Product 1 renderer read a key layout the planning schema
(`aeos.planning.contracts`, schema v1) forbids. Every approved plan therefore produced mostly
"documentation pending" documents, RFC/TDD/threat-model/release/runbook documents were never created,
and every renderer test used a fixture the schema rejects. Publication additionally replaced whole
Google Docs / Confluence bodies, which deletes HUMAN_GOVERNED content (sign-off, approval, decisions)
that a human wrote in the official document after an acknowledged edit.

## Decision

1. **One seam.** `docs_engine.plan_content.normalise_schema_v1` maps schema v1 onto the renderer's
   internal layout (applied by `build_view`). Values the schema marks `UNKNOWN` are absent, never names.
   `tests/unit/test_docs_engine_schema_seam.py` fills every schema leaf with a token and fails when a
   leaf is not rendered (exempt: task execution fields, facts, material questions).
2. **Needs derived from approved data only.** A feature whose tasks carry risk class R2+ gets a
   per-change Security Assessment; a PRD `related_work` entry starting with `RFC`/`TDD` creates that
   document. Schema v1 has no per-feature personal-data flag, so DPIA stays NOT STARTED with that reason
   (*planning-schema change, owner decision*).
3. **§28 timings from real records.** Release Plans per `release.release_plans[]`; runbooks per
   `operations.runbooks[]`; Operations documents are DRAFT proposals until trusted
   `PRODUCTION_VALIDATION` evidence exists; change records per broker-opened PR (DRAFT until the
   acceptance is ACCEPTED); improvements per project-scoped ChangeProposal; VA/PT documents only from
   trusted `VA_REPORT`/`PT_REPORT` evidence; the Retirement plan from the read-only retirement checklist.
   `aeos docs release-candidate` records the consistent snapshot (commit + approved version hash + all
   document content hashes) and is refused while required Release Plan sections are pending.
4. **DYNAMIC means reference.** Dynamic sections name the live source and the ids to look up; they never
   carry a status word (no "NOT STARTED", no result column).
5. **Human-governed content is carried, never auto-merged.** On update the provider reads the current
   document, splits it at the document type's unique section headings and carries every line of a
   HUMAN_GOVERNED section that AEOS did not write into the new body under an explicit marker. This runs
   only when the document can hold human content (an acknowledged human edit, or human lines carried
   before). If such a section cannot be located the update is a CONFLICT.
6. **SUPERSEDED at the provider.** A published document dropped by a newer approved version receives a
   revision-controlled SUPERSEDED banner update; nothing is deleted.
7. **Gates.** OFFICE targets are BLOCKED unless `CAN_USE_OFFICE_INTEGRATIONS` is READY (no OFFICE read or
   write), and the gate is asked again at execute/retry (an authorisation withdrawn after the preview stops
   the run). For PERSONAL, a `CAN_PUBLISH_PERSONAL` gate BLOCKED by observed results (a critical check that
   ran and is not ok — FAILED or MISSING — or a required check that FAILED) refuses execute/retry;
   UNKNOWN/STALE (never run / invalidated) is shown in the preview while the external-write interlock keeps
   enforcing every write.
8. **Proof of duplicate prevention.** `aeos publish idempotency-test` runs the timeout/partial/replay
   tests against loopback fakes and records trusted `PUBLICATION_IDEMPOTENCY_TEST` evidence only when all
   pass on a committed tree (uncommitted code under `backend/`/`tests/` → nothing recorded: the evidence
   must name the code it ran). The Doctor check `recovery.queue` reports worker runs LEASED/RUNNING without
   an ACTIVE lease and publication operations IN_FLIGHT beyond the stale window (§08 "lease/queue restart").
9. **A published DRAFT stays DRAFT.** Publishing a DRAFT of the approved version (operations proposal,
   change record before acceptance) puts it in the official folder with its DRAFT label but never turns it
   into PUBLISHED; only an APPROVED document becomes PUBLISHED.

## Consequences / open owner decisions

* Full READY enforcement of `CAN_PUBLISH_PERSONAL` needs the trusted acceptance suite (read-only for
  workers) to establish Doctor results in its publication fixtures — *owner decision*.
* A "replace with new DOCX snapshot" operation for lossy Google updates needs its own operation kind in
  the policy catalog/interlock (protected path) — *owner decision*; until then the preview names the loss
  and how to prepare a local DOCX snapshot.
* Screenshot capture needs an approved browser-automation collector (verification, protected path);
  documents reference trusted `SCREENSHOT` evidence only.
* Diagrams stay UNVERIFIED unless a real check exists (ERD vs the platform schema; trust boundaries vs the
  threat model). Context/sequence/CI-deployment diagrams are not verified against contracts, integration
  tests or IaC.
* Product incidents/problems need a project-scoped incident record in operations; the folder says so.
* Model/route rollback readiness (§08 Recovery "config/model rollback") is not checked by the Doctor yet;
  it belongs to the routing module (previous approved route configuration retained).
* The fine-grained PAT needs **Contents: read/write** on the platform repository: the git broker pushes
  `aeos/*` task branches with the same `personal/github` token (sha-pinned, never forced); `main` stays
  protected by a branch rule so merge remains human-only.
