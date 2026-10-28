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
