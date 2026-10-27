# Runbook: OpenBao unseal and recovery

## Trigger

After a reboot OpenBao is sealed (doctor: secret store FAILED/UNKNOWN; *Integrasi & Akses* shows
credential status UNKNOWN), the AppRole/agent token expired, the OpenBao data directory was lost or
corrupted, or unseal/recovery material must be re-created, for example because an unseal key was
exposed (pasted into a chat or an AI assistant, captured in a screenshot, written to a file).

## Authority

Owner only. Root token, unseal keys and recovery keys are never stored with the application
configuration, in the AEOS database, in backups, in the repository or in chat (§26). Since
[ADR-0123](../decisions/ADR-0123-openbao-unseal-key-in-os-keyring.md) the unseal key share(s) live in the OS
keyring (the same service as the AppRole credentials) plus one offline recovery copy that you keep. Every
operation that needs a secret fails closed while OpenBao is unavailable — that is intended.

## Where the unseal key lives

* `./aeos vault status` prints the line `unseal key`: either the number of shares in the OS keyring, or `not
  stored`. `./aeos doctor -g SECRET` says the same under `secret.openbao_health`, with the residual risk: anyone who
  acts as your OS user on this machine can read the key, as they can the AppRole credentials already there.
* `./aeos serve` unseals a sealed OpenBao from the keyring before it starts; `./aeos vault unseal` does the same
  and prompts (hidden) only when no key is stored. Each attempt is an audit row `openbao.unseal`, without a value.
* The offline copy is a file that `./aeos vault export-recovery` creates owner-only (0600) outside every
  repository and outside the AEOS data directory. It holds the unseal key share(s) and the AppRole role_id and
  secret_id, and it is the only way back if the keyring is lost (profile reset, new machine): see "Lost keyring"
  below. On Windows the export refuses (owner-only ACL NOT-PROVEN): a plain `.\aeos.ps1 vault init` shows the same
  material once on a temporary screen instead, and that is the Windows offline copy.

## One-time migration of an existing install (macOS, zsh)

For a vault initialised before ADR-0123, whose unseal key is only in your offline storage. Run these in a
terminal on the Mac, from the repository root. `import-unseal-key` asks for the key twice at a hidden prompt:
paste it from your password manager there, never into a chat, a file or the command line, then clear the
clipboard. `export-recovery` prints only the path of the new file.

```bash
./aeos vault import-unseal-key
./aeos vault status
./aeos vault export-recovery "$HOME/aeos-openbao-recovery.txt"
```

Move the file offline, then delete it from the disk: copy it to an encrypted USB drive (or put its content into
your password manager entry), check the copy, and remove the original. `$HOME` itself is not synced by iCloud
Drive; never choose Desktop or Documents when "Desktop & Documents Folders" sync is on.

```bash
cp "$HOME/aeos-openbao-recovery.txt" "/Volumes/AEOS-RECOVERY/aeos-openbao-recovery.txt"
rm "$HOME/aeos-openbao-recovery.txt"
```

`/Volumes/AEOS-RECOVERY` is the name of your encrypted USB volume; use your own volume's name. To prove the
imported key now instead of at the next reboot, restart the vault and unseal it from the keyring; the last line
must say `Unsealed with the key from the OS keyring`:

```bash
./aeos vault stop
./aeos vault start
./aeos vault unseal
```

A rejected key ends with `aeos vault import-unseal-key --replace`: run that with the right key.

## Lost keyring (profile reset, or a new Mac with the OpenBao data)

The keyring entries are gone but `<data>/openbao/raft` is still there (or was copied to the new machine). Take
the recovery copy from offline storage. `import-unseal-key` asks for each unseal key share and then, with
`--with-approle`, for the AppRole role_id and secret_id, each twice at a hidden prompt: paste each value from the
recovery copy there only. Use the `--shares` number the recovery copy names (1 by default). `vault status --verify`
proves the controller login with the restored AppRole credentials.

```bash
./aeos vault start
./aeos vault import-unseal-key --shares 1 --with-approle
./aeos vault unseal
./aeos vault status --verify
```

A recovery copy that says `approle: NOT in the OS keyring`, or an unseal key kept from before ADR-0123, restores
the unseal key only (`./aeos vault import-unseal-key`): the controller cannot log in, and with the root token
revoked nothing re-issues the AppRole credentials. Then follow step 5 (re-initialise and re-enter every
credential), and make a new recovery copy right after.

## Steps

1. **Status**: `aeos vault --help` lists the vault commands of this installation. The service
   inventory (`GET /api/ops/services`) shows OpenBao as CONNECTED (sealed/unsealed) or UNREACHABLE —
   CONNECTED is not a functional verification.
2. **Start** OpenBao if it is not running: `aeos vault start`.
3. **Unseal**: `aeos vault unseal`. With the key in the OS keyring it unseals without a prompt (so does
   `aeos serve`). Without one it asks for the required number of unseal key shares at a hidden prompt: take them
   from your offline recovery copy, then put them back into the keyring with `aeos vault import-unseal-key`
   (`--with-approle` when the AppRole credentials were lost too, see "Lost keyring").
   Do not paste keys into files, chats or terminals that log history.
4. **Re-authenticate the controller** (AppRole/agent) if its token expired — follow the prompt of
   `aeos vault` / `aeos secrets` (the bootstrap SecretID is delivered by the OS credential store,
   not by config files).
5. **Data loss**: if the OpenBao data directory is gone, AEOS backups cannot restore it (by design).
   Restore OpenBao from its own backup (OpenBao snapshot / data directory copy made with OpenBao
   tooling) or re-initialise (`aeos vault init`) and re-enter every credential
   ([credential-rotation](credential-rotation.md) step 4). Re-initialising produces new unseal
   keys: store them offline immediately.
6. **Exposed unseal key.** The unseal key quorum unseals the vault after a restart and, together with a
   copy of the storage (`<data>/openbao/raft`), decrypts every stored secret. It does not mint a root
   token on its own: on the AEOS listener config with OpenBao 2.7 the unauthenticated generate-root
   endpoint is disabled (`sys/generate-root/attempt` answered 405, `sys/generate-root-token/attempt`
   and `sys/rotate/keys/init` answered 403 without a token on 2026-10-26). Replace the keys anyway.
   * *Right after `vault init`, before any `secrets set`*: reset the empty vault with `aeos vault stop`,
     delete `<data>/openbao/raft`, `aeos vault start`, `aeos vault init`. The AppRole credentials and the
     unseal key share(s) in the OS keyring are replaced by the new init; make a new recovery copy and destroy the
     old one. Do not judge emptiness by doctor `secret.status`: it does
     not count `office/` and `projects/` references.
   * *Secrets stored*: key rotation needs a token with `sudo` on `sys/rotate/keys`, and AEOS revokes the
     root token after init, so there is no supported in-place rotation (break-glass with a new root
     token is an owner decision and NOT-PROVEN). Re-initialise as in step 5 and re-enter every
     credential.
7. **Re-run the doctor**: `aeos doctor`; secret-dependent capabilities stay BLOCKED until their
   checks pass.

## Verification

* Doctor secret check VERIFIED with a fresh timestamp; `GET /api/control-center/integrations` shows
  credential status CONFIGURED for the expected references (values are never shown).
* A test operation that needs a secret (e.g. the GitHub adapter dry-run) succeeds.

## Evidence

* Doctor results and audit rows for secret-store access; OpenBao's own audit device if enabled.
* Record *that* unseal keys were re-created and where they are kept (location, not content).
* Audit rows `openbao.unseal`, `openbao.unseal_key_import` and `openbao.recovery_export` (counts, trigger,
  outcome and the recovery file path; never a value).
