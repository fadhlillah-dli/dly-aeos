### Fixed

- **Components the owner admitted for a trial are probed; candidates still are not (owner request R28).**
  Reconciliation probed only REQUIRED/OPTIONAL components, so the catalog components approved for TRIAL kept their
  installation UNKNOWN after the install and Refresh could not change it. A full run now also probes every component
  that a human admitted (TRIAL_APPROVED/APPROVED, derived from the Decision registry as the views derive it, PERSONAL
  or platform decisions only, never an OFFICE-only component) and lists them as `scope.admitted`; the poll loop follows
  the same set. A scoped refresh of a DISCOVERED/UNDER_REVIEW candidate is SKIPPED, and the views count a candidate
  with a probe that no human admitted (or whose trial was rejected or revoked) as a not-probed candidate, not as a
  NEVER_PROBED or STALE_OBSERVATIONS gap; its installation reason says it is not admitted instead of asking for a
  reconcile that cannot cover it, and its next action is no longer RUN_RECONCILE or REFRESH (it is
  REVIEW_CANDIDATE again while it is DISCOVERED/UNDER_REVIEW). The InstallManifest gives `pi`, `context7`
  (`context7-mcp`), `codebase-memory-mcp`, `rtk`, `ccusage`, `copier` and `temporal` (`temporal-server`, `tdbg`) a
  `--version` binary probe; on macOS arm64 their version calls wrote no file in an empty home and needed no network.
  `serena --version` did write a file (it created `~/.serena/memories/global`), so `serena`, `goose` (its version call
  writes a CLI log), `promptfoo` (config, database, logs and an online update check) and `toolhive` (`thv version`
  writes `updates.json` and checks online) get a presence-only binary probe (`version_args: []`): the binary is
  located on PATH or through its settings override and never run, so it is INSTALLED with its version UNKNOWN and
  the reason, or NOT_INSTALLED; its installation claim is static evidence (E1, a lookup and a file digest), never the
  E3 of an executed probe. `litellm`, `deep-agents`, `opentelemetry`, `langfuse`, `superpowers`,
  `agency-agents`, `gstack`, `hermes` and `langgraph` keep `none` (`litellm` runs from a venv outside PATH and no
  Settings field names its location yet, although a binary probe can take one from `aeos.yaml`; the others are a
  library in a separate venv, a compose file, a text archive, or not installed). The `sops` probe passes
  `--disable-version-check`, so it no longer asks GitHub for the latest release. A new
  Doctor check, `inventory.admitted_unprobed`, lists admitted components without a probe (CONFIGURED, informational,
  in no capability), and such a component's next action is ADD_PROBE (add a manifest probe in a PR) instead of the
  unsatisfiable INSTALL_TRIAL "pasang lalu probe" while its installation is UNKNOWN. A TRIAL/PROMOTION decision on a change_proposal now invalidates
  `component:<id>` of its components and `inventory:admission` instead of `config:routes`/`model:route`, so it no
  longer makes the route and bridge checks STALE; a TRIAL on a model_route still does. Bite tests: red on the old
  code with their own messages, near misses green. E2 on macOS arm64; NOT-PROVEN on Windows and macOS Intel, where
  the npm shims and binary names are not checked.
