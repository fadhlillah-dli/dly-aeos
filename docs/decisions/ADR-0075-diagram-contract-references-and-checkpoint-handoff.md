# ADR-0075 — Diagram checks against the project's own contracts; checkpoint hand-off to planning

* Status: PROPOSED
* Date: 2026-10-26
* Deciders: owner (Fadhlillah) — proposed by the wave-7 docs-engine gap-closing change (cluster H)

## Context

§16/§29 require diagrams to be checked against their sources (contract/manifest, API flow and integration tests,
referenced schema/migrations, threat model and data inventory, pipeline/IaC and the verified target). Only the
platform project was checked, by node/table name only; product diagrams always stayed UNVERIFIED although AEOS
already reads a registered checkout read-only at a commit (ADR-0022). Product runbooks, product VA reports and the
platform's own restore test never reached their documents, and recording a documentation checkpoint (ADR-0114 §4)
needed a raw API call while its Tech Writer draft never became work.

## Decision

1. **References at a recorded commit (`aeos.docs_engine.references`).** Platform: SQLAlchemy metadata (tables,
   columns, foreign keys), InstallManifest components and their DESIGN relations, `contracts/api/routes.json`,
   and from the platform repository at HEAD (git object database, commit named in the notes) the CI workflow
   jobs, deploy files and `docs/runbooks`. Product: the registered checkout after the domain checks every content
   read uses, at the merge commit of the newest merged PR or else the last inventory commit (never the working
   tree): OpenAPI paths, `schema.prisma` / SQL / Alembic migrations, compose/package/chart/Kubernetes names, CI
   jobs/stages/triggers, IaC kinds, the plan's `interface_contracts[].contract_paths` and `docs/runbooks`.
   Sensitive file names are never read; no checkout / no recorded commit / a refused domain check = UNVERIFIED with
   the reason. The commit is named in the rendered body but is not a render input on its own: the same facts (and
   the same runbook text) at a newer commit do not re-render a document, so an unrelated commit never creates new
   revisions to publish or a "material" checkpoint delta.
2. **Stricter checks.** Component links must be registered relations (platform); ERD relationship lines must be
   backed by a foreign key, a junction table or an `<entity>_id` column; cicd/deployment nodes must be pipeline
   jobs/stages, registered environments or detected IaC/deploy references. Anything else is MISMATCH. A sequence
   diagram is VERIFIED only when every integration test it names (`%% test: <node id>` or in
   `verification_source`) PASSED in a trusted verifier `TEST_REPORT` of the trusted suite at an ACCEPTED head.
3. **Plan schema 1, revision 4 (additive, nested).** Each `operations.runbooks[]` entry gains `prerequisites`,
   `procedure`, `verification`, `rollback`, `escalation` (AI_MAINTAINED proposals, labelled; a reviewed repository
   runbook wins). Older entries are upgraded empty. Space on the Claude CLI command line was made by shortening
   five schema annotations without changing their meaning.
4. **External VA reports** (`aeos docs register-va-report`): same intake/provenance rules as PT reports, plus
   required tools and readable content (an empty scan is not a result). This is the VA path for products.
5. **Platform restore test.** The platform project's documents read the platform-level `RESTORE_TEST` evidence
   (origin `restore-test`, no project/domain, no per-domain counts).
6. **Checkpoints from the CLI and the Control Center.** `aeos docs checkpoint <plan_version_id> --kind --ref` and a
   Dokumen & Publikasi > Checkpoint tab: offers for fully accepted milestones / approved snapshots (never an
   automatic record), the immutable packages, "Minta review Claude" (advisory) and "Tambahkan ke versi rencana
   berikutnya": the writer draft becomes a task of a NEW human-edit plan version with an EMPTY write scope (the
   owner sets it before approving). That task names exactly the chosen package (`doc_checkpoint:<revision id>`, an
   immutable revision), so a package recorded later for the same milestone/snapshot never replaces what the owner
   approved; the generic `documentation_checkpoint:<project>:<kind>:<ref>` label a planner may use resolves to the
   newest recorded package. Both resolve in the task compiler to the hash-bound package content.
   `aeos docs checkpoints <project_id>` lists the same offers and packages read-only.
7. **The accepted writer output reaches the narrative.** Once that TaskSpec (its checkpoint input resolved, never
   an unresolved label) is ACCEPTED, its change record becomes part of the input of `03-architecture-overview`:
   the next render names the change record, PR, merge commit and package. The plan's own prose and diagram
   sources still change only through an owner-approved plan version.

## Consequences

* Diagrams that name unregistered nodes, links, relations, routes or jobs now show MISMATCH with the names.
* The narrative prose itself still changes only through an owner-approved plan version; AI output never becomes
  the narrative or a status by itself.
* An automatic npm-audit collector for the platform frontend is not part of this change (inventory module); an
  npm audit report can be registered as an external VA report meanwhile.
