### Fixed

- **A PUBLIC-only free WORKER route now makes the worker readiness check ready (decision C).** `route.registry` (and
  so `CAN_RUN_WORKER` and `aeos work agent-enable`) judged eligibility for PERSONAL/INTERNAL only, so a free OpenRouter
  WORKER TRIAL that may serve PUBLIC data only never counted, although ADR-0041's decision C amendment and
  OWNER_ACTIONS promised it serves PUBLIC tasks at once (found on the owner's Mac on 2026-10-27: "1 route
  APPROVED/TRIAL tetapi tidak ada yang eligible ... INTERNAL not allowed"). The check now evaluates INTERNAL and then
  PUBLIC, as the agent enable probe already did; with only PUBLIC routes it reports CONFIGURED for "PERSONAL/PUBLIC
  saja" and says that INTERNAL tasks wait for a route that allows INTERNAL. Routing is unchanged: an INTERNAL task
  never reaches a free route. Bite test: red on the old check with the owner's exact message, green now. E2.
