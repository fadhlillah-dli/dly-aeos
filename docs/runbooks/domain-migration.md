# Runbook: Moving a project between domains (PERSONAL <-> OFFICE)

## Trigger

A project was registered in the wrong domain, or its ownership moved (e.g. a personal prototype becomes
company work). Master §04: "Domain tidak dapat diganti" — the domain of a project is immutable (DB
trigger on `projects.domain`, `DOMAIN_IMMUTABLE` on any API attempt). A move is a **migration with an
owner decision**, never an edit.

## Authority

The owner. Moving data **into OFFICE** additionally needs a recorded company authorisation (§23 default
deny); moving data **out of OFFICE** needs the company's approval for that data — AEOS cannot grant it.
No agent or model output may propose, choose or execute the target domain (U-05).

## What is never done

* `projects.domain` is never UPDATEd (the trigger refuses it; do not drop the trigger).
* Evidence, audit and decision rows of the old project are never edited or moved: they stay in the
  old project's domain partition as history (append-only, §34).
* Credentials are never copied across domain namespaces (`personal/*` vs `office/*`, §26).

## Steps

1. **Decide and record.** Write down the reason and the target domain; for OFFICE, obtain the company
   authorisation reference first. Record an owner decision in the Decision Queue (kind `POLICY`, target
   the old project id, rationale = reason + company reference) so the move is auditable.
2. **Inventory the old project** (read-only): *Riwayat & Pemulihan → Retirement* or
   `aeos ops retirement show <old-project-key>` lists repositories, integrations, external
   references, publications, attachments, evidence counts and backups that contain project files.
3. **Back up first:** `aeos ops backup create --label pre-domain-migration` and run its restore test.
4. **Create the new project** in the target domain (explicit domain choice in the UI or
   `POST /api/projects` with `domain`), with a new project key.
5. **Re-register, never copy, the external identities:** repositories (GitHub for PERSONAL, Bitbucket for
   OFFICE), integrations and their *own* credential references in the target namespace. Re-verify each
   repository (`Verifikasi identitas`); nothing is assumed from the old project.
6. **Carry over only what the decision allows:** re-upload the attachments/documents that may cross the
   boundary through intake (they are scanned again); plans are re-approved in the new project (a
   PlanVersion approval is bound to its project and hash and is not transferable).
7. **ID mapping:** add a note to the new project's description listing `old project id -> new project id`
   and the decision id from step 1.
8. **Retire the old project** through the retirement workflow (owner decision bound to the checklist hash),
   or keep it ARCHIVED as read-only history.

## Verification

* The old project still shows its original domain and history; the new project shows the target domain.
* `aeos doctor` shows no cross-domain findings; OFFICE capabilities stay BLOCKED until authorised.
* The audit trail contains the decision from step 1, the backup + restore test and both projects.

## Evidence

* The owner `POLICY` decision (step 1), audit rows `portfolio.project.create` for the new project,
  `ops.backup.create` + `RESTORE_TEST` evidence of the pre-migration backup, repository verification
  attempts of the new project, and the retirement decision of the old project.

## Known limits

* There is no automated migration tool: every step above is explicit and owner-run (safe by design).
