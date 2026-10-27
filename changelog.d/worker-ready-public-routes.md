### Fixed

- **A PUBLIC-only free WORKER route now makes the worker readiness check ready (decision C).** `route.registry` (and
  so `CAN_RUN_WORKER` and `aeos work agent-enable`) judged eligibility for PERSONAL/INTERNAL only, so a free OpenRouter
  WORKER TRIAL that may serve PUBLIC data only never counted, although ADR-0041's decision C amendment and
  OWNER_ACTIONS promised it serves PUBLIC tasks at once (found on the owner's Mac on 2026-10-27: "1 route
  APPROVED/TRIAL tetapi tidak ada yang eligible ... INTERNAL not allowed"). The check now evaluates INTERNAL and then
  PUBLIC, as the agent enable probe already did; with only PUBLIC routes it reports CONFIGURED for "PERSONAL/PUBLIC
  saja" and says that INTERNAL tasks wait for a route that allows INTERNAL. Routing is unchanged: an INTERNAL task
  never reaches a free route. Bite test: red on the old check with the owner's exact message, green now. E2.
- **The memory-cap test's hog keeps its pages resident.** Its diagnostics showed two more full-suite failures with the
  cap never firing (peak RSS 63.6 MB zero-filled, then 78.1 MB with random data, exit 0): on macOS the process
  sandbox caps memory with the runtime watchdog's RSS sample, and under memory pressure macOS compresses or swaps out
  pages a process does not touch. The hog now writes one byte per page of every block it holds on each pass, as a
  busy process does. Known gap, recorded: a worker that sits idle on swapped-out memory can stay under the RSS cap on
  macOS (the container sandbox is capped by `docker --memory`). E2; the swap cause is inferred, not reproduced.
