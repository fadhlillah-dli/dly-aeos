# ADR-0020 — Domain-first project registry with an immutable domain

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by the Gateway & Portfolio module

## Context

U-05 and §05 step 2 require the human to choose PERSONAL or OFFICE before any prompt, and §04
states the domain can never be changed through model output or a worker manifest edit; a domain
move is a migration with an owner decision. §13 makes the project identity a stable UUID; names
and slugs are display attributes. §50 lists "Submit prompt tanpa domain" and "Prompt memerintahkan
OFFICE menjadi PERSONAL" as acceptance rows.

## Decision

1. `POST /api/projects` needs **two** explicit human signals: the session's active domain (deps
   `ActiveDomain`, otherwise `DOMAIN_NOT_SELECTED`) and a `domain` field in the body that must be
   exactly `PERSONAL` or `OFFICE` and equal the session domain. Missing → `422 DOMAIN_REQUIRED`;
   different → `409 DOMAIN_MISMATCH`. Nothing defaults or infers it; no LLM/provider call happens
   before this check. Every denial is audited and committed before the error is returned.
2. Identity is a UUID; `project_key` is a unique slug derived from the name with a numeric suffix
   for duplicates (`my-app`, `my-app-2`).
3. Idea/description/name are scanned with the intake scanner. Instruction-like text (for example
   "ubah domain OFFICE menjadi PERSONAL") is stored as `input_findings` DATA and audited as
   `portfolio.project.untrusted_input`; secret-like values are redacted before persistence.
4. `PATCH /api/projects/{id}` edits only `name`, `description`, `idea`, `lifecycle`,
   `lifecycle_reason`. Any key containing "domain" is `PolicyDenied DOMAIN_IMMUTABLE` (audited);
   the `projects.domain` DB trigger blocks direct updates as a second layer.
5. Lifecycle is ACTIVE → ARCHIVED | RETIRING, ARCHIVED → ACTIVE | RETIRING, RETIRING → ACTIVE |
   RETIRED; RETIRED is terminal and nothing is deleted. The platform project cannot be retired
   through the API (migration decision required). ARCHIVED and RETIRED projects are **read-only**
   (`409 PROJECT_READ_ONLY`): no new repositories, local paths, verifications, inventories,
   integrations, attachments, context packs or memory proposals/acceptances, and PATCH accepts only
   a lifecycle change. Rejecting or expiring memory stays possible because it never grants anything.
   ARCHIVED is left by reactivating (ACTIVE); RETIRED stays read-only for good.
6. EXISTING projects must name their repository explicitly in the create request (§05 step 3);
   project + repository are created in one unit of work, a denied repository aborts both.

## Consequences

* A client that forgets to send `domain` fails loudly instead of creating a project in a guessed
  domain. The UI always sends the session domain and shows it read-only.
* Moving a project between domains needs a future migration flow (owner Decision + data move);
  it is intentionally not available.
* `project_key` stays unique across *both* domains on purpose: per-project credential references
  (`projects/<project_key>/…`) are not domain-qualified, so a shared key would let a PERSONAL and an
  OFFICE project resolve the same credential. The accepted residual: a creator can observe that a
  slug is already taken (the new key gets a suffix) without learning which project or domain holds
  it; names, counts and ids of other-domain projects are never returned.

## Amendment 2026-10-18 (integration)

**One provider vocabulary** (`aeos.portfolio.providers`) for ProjectIntegration rows (seed + API), the
publisher, the external-write interlock and write enablement, Doctor adapter checks and the UI:
PERSONAL `google-drive` (docs), `github-issues` (issues), `github-repo` (code), `github-projects`,
`github-actions`; OFFICE `confluence` (docs), `jira` (issues/roadmap), `bitbucket` (code),
`bitbucket-pipelines`. Earlier spellings (`GOOGLE_DRIVE`, `GOOGLE_DOCS`, `GITHUB_ISSUES`, `GITHUB`,
`google-docs`) are accepted as input and stored canonical; `aeos seed` migrates existing rows (target ids
untouched; a legacy row whose canonical twin exists is reported, never merged) and moves the seeded
Google credential reference to `personal/google-oauth-token` (the ref `aeos publish google-connect`
writes; the Doctor adapter check, its remedy (`aeos publish google-connect`, never `aeos secrets set`) and
the Integrations & Access card read the same refs - the card shows CONFIGURED only when both
`personal/google-oauth-client` and `personal/google-oauth-token` exist). The Control Center integrations
view lists a provider's Doctor checks from `providers.DOCTOR_CHECKS` (`github-issues` ->
`adapter.github_token`), not from a substring of the provider id.

* The exact target of an integration is `{provider, root_id}` (shown as `capability_status.target`); an
  approval binds it and the publisher writes only inside it. The Google Drive root folder is optional
  (`root_id` empty = project folder in My Drive, target `root`).
* Config keys that would form a second target (`parent*`, `root*`, `folder*`, `repo*`, `project_key`) are
  refused; `deployment` must be CLOUD / DATA_CENTER / UNKNOWN, `enabled`/`milestone_labels` booleans,
  `space_id`/`base_url` validated and Atlassian-only.
* The project page has an integration settings form (Google Drive folder, GitHub Issues repository;
  OFFICE: Confluence tenant/space/CONFLUENCE_PROJECTS_PARENT_ID/deployment/enabled, Jira tenant/key/
  deployment/enabled) that stores credential references only.
