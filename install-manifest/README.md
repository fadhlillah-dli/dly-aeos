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

Structured identity fields (§07 "digest, port, configuration references, compatibility", §25 tool
admission, §37 distinct source/distribution/executable identities), all optional and never guessed:

| Field | Meaning | Checked against |
|---|---|---|
| `approved_digest` | `sha256:` of the reviewed executable/artifact (needs `approved_pin`): one value for every platform, or a mapping keyed `<os>_<arch>` (`windows_amd64`, `darwin_arm64`, `darwin_amd64`, `linux_amd64`, ...) because the executable differs per platform. A platform without an entry is NOT_RECORDED there, never a mismatch | digest the reconciler observes for the probed binary on THIS platform (drift `resolved-version-mismatch`, key `digest`); `aeos vault install` refuses an OpenBao binary that differs from this platform's entry |
| `distribution` | `{kind, url, digest, channel, note}` of the release asset / package | `url` must appear in the master |
| `signature` | `{method, available, verified, verified_at, result, approved_signer, note}`; `UNKNOWN`/`NOT_VERIFIED` stated explicitly. `approved_signer` (owner pin) is one publisher identity or a per-OS mapping `{windows: <Authenticode subject / CN / O>, darwin: <TeamIdentifier>}`; once pinned for an OS, anything but a VALID OS signature by exactly that signer is BLOCKED there | `VERIFIED` needs `verified_at`; the Claude bridge and Doctor `claude.binary` apply the pin |
| `ports` | loopback ports with the Settings key that configures them | Settings keys; host must be loopback |
| `config_refs` | `settings:<key>` or `file:<repo-relative path>` the component reads | Settings keys / relative paths |
| `compatibility` | `{os, arch, min_version, max_version, source, note}` | — |
| `tool_schema_hash` | hash of the tool/MCP schema surface recorded at admission | a schema hash reported by an observation (no probe recomputes it) |
| `cost.free_until` | free-tier expiry date or `UNKNOWN` | — |

```
aeos inventory sbom --out sbom/aeos.cdx.json   # CycloneDX 1.5 from requirements.lock + frontend/package-lock.json
aeos inventory poll [--enable|--disable --reason ...]  # optional TTL-driven refresh (off by default)
```
