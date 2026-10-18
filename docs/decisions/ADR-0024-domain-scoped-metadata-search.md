# ADR-0024 — Domain-scoped metadata search on SQLite LIKE

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by the Gateway & Portfolio module

## Context

§11 chooses simple metadata/FTS search first; §35 requires project-scoped/global metadata search
that is authorised, and "metadata list, jumlah baris, pagination, search … tunduk pada AUTH yang
sama; menyembunyikan nama di layar saja tidak cukup". §50: a viewer searching Office inventory
must not see metadata or counts.

## Decision

1. `GET /api/search?q=&kind=&limit=&offset=&project=` searches projects, plans, task specs, components,
   decisions, ADRs, documents, attachments (filename), repositories and memory with
   case-insensitive `LIKE` (wildcards escaped) — no FTS5 virtual tables in V1.
2. Every query is filtered on the backend by the ACTIVE domain and the principal's
   `project_scope`; counts, totals and pagination are computed with the same filter. Global records
   (ADRs, decisions without domain/project, components without a domain/project scope) are
   included; domain- or project-scoped components only when that domain/project is visible (a
   component scoped to an OFFICE project is neither listed nor counted in a PERSONAL search).
   `project=<id>` makes the search project-scoped (that project's rows plus global records); a
   project outside the domain/scope is a 404.
3. Ordering is deterministic per kind so offset pagination is stable; responses carry
   `source`, `checked_at` and `mode`.

## Consequences

* LIKE scans are adequate for the local single-user metadata volume; FTS5 can be added behind the
  same endpoint later without changing scoping rules.
