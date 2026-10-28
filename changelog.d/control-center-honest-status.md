### Fixed

- **Control Center shows activated items as active (R28).** Tools & Repos and Models & Routes drew trial-activated items
  as not active: `StatusBadge` had no spec for `TRIAL`, `UNTESTED`, `TRIAL_APPROVED` (a grey raw code with a question
  mark, like a candidate) and a dozen more backend values; a test now enumerates the backend enums (a fixture that
  `tests/unit/test_status_vocabulary.py` keeps equal to them) and fails with "StatusBadge has no spec for <X>". The Models
  page states "Aktif: N dari M rute (TRIAL/APPROVED) · sisanya katalog kandidat", counted from the rows its table
  shows (SUPERVISOR routes have their own section and are in neither N nor M; same active rule as the rows), lists active routes
  first, labels a catalogue row "Katalog, belum diaktifkan" and shows "s/d <date>" only from the active decision's
  `expires_at` (never invented); a route counts and shows as active only while its status is TRIAL/APPROVED **and** an ACTIVE decision
  bound to the current config hash exists (`counts.approved` now requires it; an expired or invalidated decision shows
  "TRIAL · keputusan kedaluwarsa/tidak berlaku" in the warn tone, never green); an OpenRouter `:free` route shows today's usage against the daily limit and the decision C
  status (drawer block and table badge). The Tools default view shows the admission badge (TRIAL / Disetujui) in the
  component cell; `next_action` for an approved or trial-approved component that is not installed is the new
  `INSTALL_TRIAL`, no longer "pantau observasi"; its label follows the admission ("Trial disetujui: pasang lalu probe" for
  TRIAL_APPROVED, "Disetujui: pasang lalu probe" for APPROVED; the frontend shows the API label as is). One shared event to query map
  (`lib/eventInvalidation.ts`, installed in AppShell, no polling, staleTime 15 s) replaces the per-page filters: decision,
  proposal and policy events refresh inventory, routes and decisions; `route.*` routes and inventory; `inventory.*`
  inventory; a task heartbeat nothing. Inventory enablement is unchanged (master section 38: a TRIAL decision is not an
  enablement basis). E2 (unit and vitest on macOS arm64); the live Control Center on a real host is NOT-PROVEN.
- **A route the router cannot authenticate is not shown as active (R28, incident 2026-10-28).** A TRIAL route with an
  ACTIVE decision but no stored key was green "Aktif · TRIAL", counted and sorted first while Doctor
  `CAN_RUN_WORKER` was BLOCKED. `/api/routes` items carry a `credential` block (`state` PRESENT, MISSING, UNAVAILABLE
  or NOT_NEEDED, `ref`, `reason`, `domain`) from the routing service's own credential rules, one secret-store status
  lookup per reference and list call (metadata only, never a value, never a provider call). The block is evaluated in
  the session's domain and only for a route eligible there: otherwise it is `NOT_ELIGIBLE_IN_DOMAIN` with nothing
  looked up (an OFFICE-only route in a PERSONAL session reads "TRIAL · aktif hanya di domain lain", not "tidak dapat
  dipakai", and no remedy). A reference name private to another domain (`office/` outside OFFICE) or to a project the
  viewer cannot see is never quoted (a path problem keeps its reason with the name replaced; an otherwise usable
  `projects/` reference is `WITHHELD`: neither its name nor its status). The same rule (one owner,
  `routing.service.withheld_refs`) covers every route payload: the route detail `config`
  (`mode_params.credential_ref`), the `/api/routes/eligibility` rejection reasons and the decision rationales show
  `<office/ reference, name withheld>` (or the `projects/` equivalent) to a session that may not see the name, the
  session's selected domain deciding over a queried one; OFFICE sessions and project members still see it, and a
  `workers/` name is quoted as before (these two leaks were pre-existing, found in review; LESSONS L12 strike 2).
  `counts.approved` and the Models page count a route as active only with PRESENT or
  NOT_NEEDED and otherwise show "Aktif, tidak dapat dipakai: kunci belum disimpan" (or "... vault terkunci"), not
  sorted first, with `aeos secrets set <ref>` (or `aeos vault unseal`) in the drawer. `secret.presence_changed` and
  `secret.store_changed` (source `secrets`) now refresh routes, route detail and Readiness; the Models page refetches
  once at the nearest decision expiry (clamped to 1 s to 24 h; no event fires at the moment a decision expires, and a
  test pins one `decision.expired` per decision when the server reconciles); with a Pool/Status/Domain filter the
  registry-wide header adds " · ditampilkan: X". E2 (pytest and vitest on macOS arm64); the live Control Center on a
  real host, Windows and macOS Intel are NOT-PROVEN.
