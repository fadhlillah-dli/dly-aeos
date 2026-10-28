### Fixed

- **Components the owner admitted for a trial are probed; candidates still are not (owner request R28).**
  Reconciliation probed only REQUIRED/OPTIONAL components, so the catalog components approved for TRIAL kept their
  installation UNKNOWN after the install and Refresh could not change it. A full run now also probes every component
  that a human admitted (TRIAL_APPROVED/APPROVED, derived from the Decision registry as the views derive it, PERSONAL
  or platform decisions only, never an OFFICE-only component) and lists them as `scope.admitted`; the poll loop follows
  the same set. A scoped refresh of a DISCOVERED/UNDER_REVIEW candidate is SKIPPED. The InstallManifest gives `pi`,
  `serena`, `context7` (`context7-mcp`), `codebase-memory-mcp`, `rtk`, `ccusage`, `copier` and `temporal`
  (`temporal-server`, `tdbg`) a `--version` binary probe, checked read-only on macOS arm64 (no file written in an
  empty home, no network needed). `goose`, `promptfoo` and `toolhive` keep probe `none` with the reason (their version
  call writes a log, config or update cache, or calls the network); `litellm`, `deep-agents`, `opentelemetry`,
  `langfuse`, `superpowers`, `agency-agents`, `gstack`, `hermes` and `langgraph` keep `none` (no portable probe or
  not installed). The `sops` probe passes `--disable-version-check`, so it no longer asks GitHub for the latest
  release. A new Doctor check, `inventory.admitted_unprobed`, lists admitted components without a probe (CONFIGURED,
  informational, in no capability). A TRIAL/PROMOTION decision on a change_proposal now invalidates
  `component:<id>` of its components and `inventory:admission` instead of `config:routes`/`model:route`, so it no
  longer makes the route and bridge checks STALE; a TRIAL on a model_route still does. Bite tests: red on the old
  code with their own messages, near misses green. E2 on macOS arm64; NOT-PROVEN on Windows and macOS Intel, where
  the npm shims and binary names are not checked.
