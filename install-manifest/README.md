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

`aeos seed` loads or upserts the catalog (DESIGN only, never "installed"). `aeos inventory reconcile` runs the
read-only probes and records observations and drift (no install, no LLM). `aeos inventory list` and
`aeos inventory show <id>` show the separate status dimensions per component. `aeos doctor` runs the readiness
checks, which share the same component ids.

```
aeos seed
aeos inventory reconcile
aeos inventory list
aeos inventory show <id>
aeos doctor
```

Loading the manifest never marks anything installed, enabled, verified or in use; those come from
dated observations and human decisions (§37, §38).

Structured identity fields (§07 "digest, port, configuration references, compatibility", §25 tool
admission, §37 distinct source/distribution/executable identities), all optional and never guessed:

| Field | Meaning | Checked against |
|---|---|---|
| `approved_digest` | `sha256:` of the reviewed executable/artifact (needs `approved_pin`): one value for every platform, or a mapping keyed `<os>_<arch>` (`windows_amd64`, `darwin_arm64`, `darwin_amd64`, `linux_amd64`, ...) because the executable differs per platform. A platform without an entry is NOT_RECORDED there, never a mismatch | digest the reconciler observes for the probed binary on THIS platform (drift `resolved-version-mismatch`, key `digest`); `aeos vault install` refuses an OpenBao binary that differs from this platform's entry |
| `distribution` | `{kind, url, digest, channel, note}` of the release asset / package | `url` must appear in the master |
| `signature` | `{method, available, verified, verified_at, result, approved_signer, note}`; `UNKNOWN`/`NOT_VERIFIED` stated explicitly. `approved_signer` (owner pin) is one publisher identity or a per-OS mapping `{windows: <Authenticode subject / CN / O>, darwin: <TeamIdentifier>}`; once pinned for an OS, anything but a VALID OS signature by exactly that signer is BLOCKED there. While this manifest fails validation the pin is UNKNOWN (never "not pinned") and the Claude bridge is BLOCKED too | `VERIFIED` needs `verified_at`; the Claude bridge and Doctor `claude.binary` apply the pin |
| `ports` | loopback ports with the Settings key that configures them | Settings keys; host must be loopback |
| `config_refs` | `settings:<key>` or `file:<repo-relative path>` the component reads | Settings keys / relative paths |
| `compatibility` | `{os, arch, min_version, max_version, source, note}` | — |
| `tool_schema_hash` | hash of the tool/MCP schema surface recorded at admission | a schema hash reported by an observation (no probe recomputes it) |
| `cost.free_until` | free-tier expiry date or `UNKNOWN` | — |

**Which pins are recorded** ([OWNER_ACTIONS.md](../docs/OWNER_ACTIONS.md) 1.12): a value is pinned only when drift
from it is caught without blocking a capability on a routine update, or when it records the pin the runtime already
holds (so the pin adds no new block). The `claude-code` entry has only its publisher
pin (it updates itself). `opencode` has `approved_pin` 1.19.3, the same version as the worker runtime default
(`PINNED_VERSION`), so runtime behaviour is unchanged while the inventory shows a human-approved pin and its drift
instead of NO_PIN/UNKNOWN. Drift from that pin blocks `CAN_RUN_WORKER`, so OpenCode self-update is turned off on each
machine. Its `tool_schema_hash` stays unset: none observed yet. `openbao` has its version pin. `approved_pin` applies
on every platform; digests and signers are recorded per platform as each machine is reviewed.
`tests/unit/test_inventory_pins.py` checks this rule.

`aeos inventory sbom` writes a CycloneDX 1.5 SBOM from `requirements.lock` and `frontend/package-lock.json`.
`aeos inventory poll` is an optional TTL-driven refresh, off by default; `--enable` or `--disable` changes it and
needs `--reason`.

```
aeos inventory sbom --out sbom/aeos.cdx.json
aeos inventory poll --enable --reason "<why>"
aeos inventory poll --disable --reason "<why>"
```
