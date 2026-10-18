# ADR-0072 — PERSONAL docs: drive.file scope, DOCX import, requiredRevisionId updates

* Status: PROPOSED (implementation decision inside master §04/§29, S16-S21; ACCEPTED when merged under ADR-0003)
* Date: 2026-10-18
* Deciders: seed-build implementer (Claude Code, native Max) for the owner; owner review on merge

## Context

U-04: PERSONAL documentation is Google Docs inside a Google Drive folder per project. S18 recommends
the least-privilege `drive.file` scope. S19: `documents.batchUpdate` supports `requiredRevisionId`.
S20: Drive uploads can convert DOCX to Google Docs. S21: Docs image insertion needs reachable URIs —
§29 forbids opening confidential images as public links and names DOCX-with-embedded-images as the
snapshot path. Credentials must live in the secret store (§26).

## Decision

1. OAuth installed-app flow on 127.0.0.1 (free port) requesting ONLY
   `https://www.googleapis.com/auth/drive.file`. Client config is stored once at
   `personal/google-oauth-client`, the token at `personal/google-oauth-token`; refresh happens
   in-process; values are registered for redaction and never logged or returned
   (`aeos publish google-connect --client-secret <path>`, `aeos publish google-status`).
2. The project folder is created in My Drive (or a configured root folder created/opened by the app —
   `drive.file` cannot see other folders), named after the project. Every file carries
   `appProperties` `aeos_project_id` / `aeos_logical_id` (+ op key prefix); reconciliation searches
   these markers, never titles.
3. New documents: DOCX snapshot (python-docx) uploaded with conversion to Google Docs; rendered
   diagrams are embedded inside the DOCX. The adapter has no permissions call at all — no public or
   anyone-with-link sharing (tested).
4. Updates: `documents.get` first; a revision different from the last AEOS revision → CONFLICT with
   the observed text stored as revision input, no write. Otherwise `batchUpdate` with
   `writeControl.requiredRevisionId`; a rejection is CONFLICT. The incremental path writes text,
   headings and bullets; tables/images are carried only by a new DOCX snapshot (fidelity limit).

## Consequences

* Fidelity of DOCX → Google Docs conversion (headings/tables/links/images) and revision-ID lifetime
  are NOT-PROVEN until tested against a real account (§51 P2). An unknown revision baseline is
  treated as CONFLICT (fail closed).
* Owner action required: create an OAuth client of type "Desktop app" in the GCP project, enable the
  Drive and Docs APIs, then run `aeos publish google-connect`.
