# InstallManifest (master §07, §13, §37, §45)

`manifest.yaml` is the reviewed component catalog — the **DESIGN lens** of Tools & Repos and the
System Map. It lists every §45 tool/repository/method (one component per tool), the runtime pieces this
repository uses and one entry per §10 control-plane module, with official source (only URLs that appear
in the master), identity status, requirement, design label, bootstrap order (§07 0-11), approved pin
(null = UNKNOWN), read-only probe, capability tests, access, cost, operations, §45 placement and typed
relations. `schema.json` is the JSON Schema; `aeos.inventory.manifest` adds semantic checks
(URLs/S-ids must exist in the master, no guessed licenses, no `latest` pins, no install action for
UNVERIFIED/candidate entries, relation targets exist). See ADR-0010.

This directory is a protected path (§09): workers may only propose edits; a human reviews them.

```
aeos seed                         # load/upsert the catalog (DESIGN only, never "installed")
aeos inventory reconcile          # read-only probes -> observations + drift (no install, no LLM)
aeos inventory list | show <id>   # separate status dimensions per component
aeos doctor                       # readiness checks sharing the same component ids
```

Loading the manifest never marks anything installed, enabled, verified or in use; those come from
dated observations and human decisions (§37, §38).
