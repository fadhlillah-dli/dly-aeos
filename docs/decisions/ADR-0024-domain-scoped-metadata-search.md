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

## Amendment 2026-10-18 (integration)

Seam W4 (Control Center UI end to end, real browser). Two search contracts had grown on either
side of the seam: the Control Center "Ringkasan" card called `GET /api/control-center/search`
(control-center projection: `{items: [{type, link}], truncated}`, no counts, no pagination, no
project scope), while this ADR defines `GET /api/search` as the domain-scoped metadata search.

Reconciliation:

1. `GET /api/search` (this ADR) is the single metadata-search contract consumed by the UI. The
   global search box in the AppShell header (§35 "pencarian … global") and the Ringkasan card
   (now with an explicit "Hanya proyek aktif" toggle -> `project=<id>`) both call it and render
   `kind`, `title`, `subtitle`, `status`, `url`, `total`, `counts` and `has_more` as returned. The
   shared client types live in `frontend/src/lib/search.ts`.
2. `GET /api/control-center/search` is kept unchanged for backward compatibility (API clients and
   its unit tests); it has no UI consumer any more and must not gain one. New searchable kinds are
   added to `/api/search` with the same backend scoping rules (rows, counts and pagination filtered
   by the active domain and project scope).
3. Results are links into existing pages; the box never infers or switches the domain and is
   disabled until the human has chosen one (U-05).
