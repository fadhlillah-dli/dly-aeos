### Added

- **Free OpenRouter WORKER routes for PUBLIC data (owner decision C, ADR-0041 amendment 2026-10-26).** New `aeos routes
  set-pool` / `POST /api/routes/{key}/pool` moves an OpenRouter `:free` route between ARENA and WORKER; the move voids
  every earlier decision, so the owner re-activates. For `:free` WORKER routes only (other OpenRouter routes keep the
  existing rules), decision C (catalog price exactly zero, free period not over, PUBLIC only, PERSONAL only) is checked
  at the move, at activation, in routing and on every broker request (an INTERNAL run is refused). They rank first for
  PUBLIC tasks; at the account's free-model limit (`openrouter_free_rpm_limit` 20, `openrouter_free_daily_limit` 50,
  1000 after credits) the task waits (`FREE_TIER_LIMIT_REACHED`), never a Go/paid fallback; a 429 is recorded
  account-wide. Probes count under the same lock as worker requests; migration 0009 indexes that count. A run stopped
  at a provider quota no longer uses up a task attempt (bounded: 6 quota stops per owner retry decision). E2 (respx);
  live NOT-PROVEN.
