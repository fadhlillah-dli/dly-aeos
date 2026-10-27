### Security

- **The OpenBao unseal key lives in the OS keyring and is never printed; reboots unseal themselves (ADR-0123).**
  An unseal key reached a chat on 2026-10-26 because `vault init` output was copied (LESSONS L1). `aeos vault init`
  now stores every unseal key share in the OS keyring service that already holds the AppRole credentials, before
  any later step can fail, and prints only where they are; the root token is revoked (kept in the keyring only
  while setup is incomplete). `aeos serve` and `aeos vault unseal` unseal a sealed OpenBao from the keyring and
  audit each attempt without a value; with no stored key both behave as before. New `aeos vault
  import-unseal-key` (hidden prompt, one-time migration) and `aeos vault export-recovery <file>` (new 0600 file
  outside every git repository and the data directory, path printed only; refused on Windows, owner-only ACL
  NOT-PROVEN). Doctor `secret.openbao_health` and `aeos vault status` name where the key lives and the residual
  risk (same machine, same OS user). `--show-unseal-once` keeps the former flow. Evidence E2 on macOS arm64 with
  fakes only; a real keyring, a real reboot and Windows are NOT-PROVEN.
