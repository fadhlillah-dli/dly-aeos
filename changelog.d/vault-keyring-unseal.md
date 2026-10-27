### Security

- **The OpenBao unseal key lives in the OS keyring and is never printed; reboots unseal themselves (ADR-0123).** An
  unseal key reached a chat on 2026-10-26 because `vault init` output was copied (LESSONS L1). `aeos vault init` now
  stores every unseal key share in the OS keyring service that already holds the AppRole credentials, before any
  later step can fail, and prints only where they are; the root token is revoked (kept in the keyring only while
  setup is incomplete). `aeos serve` and `aeos vault unseal` unseal a sealed OpenBao from the keyring and audit each
  attempt without a value (serve writes that row only into a database already at the migration head and never
  migrates as a side effect; otherwise the row is skipped with a logged reason); with no stored key `vault unseal`
  prompts as before and `serve` prints `OpenBao is sealed. Next: aeos vault unseal`. New `aeos vault
  import-unseal-key` (hidden prompt, one-time migration; `--with-approle` restores the AppRole credentials after a
  lost keyring) and `aeos vault export-recovery <file>` (the unseal key share(s) and the AppRole role_id/secret_id
  to a new 0600 file outside every git repository and the data directory, path printed only; refused on Windows,
  owner-only ACL NOT-PROVEN, where `vault init` shows the same material once instead). Doctor
  `secret.openbao_health` and `aeos vault status` name where the key lives and the residual risk (same machine, same
  OS user); missing AppRole credentials on an initialised vault name the lost-keyring path (`import-unseal-key
  --with-approle` from the offline copy) instead of the refused `vault init`. The recovery copy and the Windows init
  screen name `--with-approle` only when they hold the AppRole credentials. A non-interactive keyring-mode init
  proves a keyring write before `sys/init` and otherwise changes nothing. The runbook deletes the exported file only
  in `cp && cmp && rm`. `--show-unseal-once` keeps the former flow and removes unseal key shares of an earlier vault
  from the keyring after a successful init. Evidence E2 on macOS arm64 with fakes only; a real keyring, a real
  reboot and Windows are NOT-PROVEN.
