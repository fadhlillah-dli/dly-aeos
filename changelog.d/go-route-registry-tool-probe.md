### Added

- **OpenCode Go WATCHLIST/WORKER move, two new Go candidates and a tool-call probe (owner decision R31 of
  2026-10-28, ADR-0125).** `aeos routes set-pool` / `POST /api/routes/{key}/pool` now also moves an OpenCode Go route
  between WATCHLIST and WORKER, only while it is CANDIDATE with no decision of any kind or status
  (`ROUTE_POOL_MOVE_NOT_CANDIDATE`, `ROUTE_POOL_MOVE_HAS_DECISIONS`), so no owner decision is ever voided. `aeos seed`
  adds `opencode-go/kimi-k3` and `opencode-go/step-5-preview` as WORKER CANDIDATE routes with tool support UNKNOWN;
  every earlier seeded route keeps its configuration hash (pinned by a test). New `aeos broker probe-route --tools` /
  `POST /api/broker/routes/{key}/tool-probe` sends one synthetic PUBLIC request with one trivial function (the
  feasibility probe's gates: activated, PERSONAL, no money) and records PASS/FAIL evidence bound to the route's
  configuration hash; `routing.facade.tool_capable` (supports_tools YES, or such a PASS for the current hash) replaces
  the plain `supports_tools` check in routing, and the probe never changes `supports_tools`. E2 (respx, macOS arm64);
  live Go tool calls NOT-PROVEN. The newest probe the model actually answered decides for the current configuration: a later answered FAIL revokes an earlier PASS, while a rate-limited or unanswered probe neither grants nor revokes (bite test).
