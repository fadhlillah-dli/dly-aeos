# ADR-0123 — OpenBao unseal key in the OS keyring, auto-unseal and an offline recovery copy (§26, §33)

* Status: ACCEPTED
* Date: 2026-10-27
* Deciders: owner (Fadhlillah) by delegation, decision D6 of the delegating session. The owner's words
  (2026-10-27): "Keputusan yang lain lakukan rekomendasi yang terbaik yang tidak mengganggu atau menghambat
  operasional (harus efesien dan efektif) tapi sesuai dengan praktek industri profesional". The owner can still
  record an ADR decision on this file (`aeos policy adr-decide ADR-0123 ...`, see the register README).
* Amends: [ADR-0042](ADR-0042-openbao-secret-provider-and-bootstrap.md) decision 4 (`init` printed the unseal
  keys once; `unseal` read the keyring only with `--store-unseal-in-keyring`) and its amendment of 2026-10-26
  (temporary screen). ADR-0042 itself is not edited (history is never rewritten); the former flow stays available
  as `aeos vault init --show-unseal-once`.

## Context

* **Incident 2026-10-26** (LESSONS L1, AGENTS.md invariant 12): during the first Mac setup an unseal key reached a
  chat because the terminal output of `vault init` was copied to prove the step. The 2026-10-26 amendment of
  ADR-0042 moved the keys to a temporary terminal screen, but the owner still had to read, copy and later type the
  key by hand: after every reboot OpenBao is sealed and every secret-dependent capability stays BLOCKED until
  `aeos vault unseal` is run with a key from offline storage. Each of those steps is a chance to paste the key
  into the wrong window.
* **The OS keyring already holds the AppRole bootstrap credentials** (role_id and secret_id in the service
  `<keyring_service>-bootstrap`, ADR-0042 decision 2). Whoever can read that entry logs in as the controller and
  reads every secret the controller policy allows, without the unseal key.
* Master §26: root token, unseal/recovery material and the bootstrap SecretID are not stored together with the
  application configuration, and the OS credential store holds the bootstrap. The OS keyring is not application
  configuration (`aeos.yaml`, the database, the repository and backups never contain it).
* Industry practice for a single host without an HSM or cloud KMS: no long-lived root token (generate one with
  the unseal quorum when needed); keep unseal material in the platform's protected credential store or in an
  auto-unseal mechanism, and keep an offline copy for disaster recovery.

## Decision

1. **Where the key lives.** `aeos vault init` stores every unseal key share in the OS keyring (bootstrap service,
   entries `openbao-unseal-key-<n>` and `openbao-unseal-key-count`) right after `sys/init`, before any later step
   can fail. It prints where they are and how to make a recovery copy, never the values. `--show-unseal-once` keeps
   the former flow (temporary screen, `--plain-output` outside a terminal). If the keyring refuses the write, init
   falls back to that flow so the material is never lost. On Windows, where the recovery export refuses (decision
   5), the default init stores the shares in the Credential Manager as well and shows the recovery material (the
   shares and the AppRole role_id/secret_id) once on the temporary screen: that screen is the Windows offline copy,
   so the Credential Manager never holds the only copy. Outside a terminal it refuses before anything changes,
   unless `--plain-output` is given.
2. **Root token.** As before, it is revoked once the AppRole login works, and its keyring entry is removed. Only
   while a setup stays incomplete is it kept, in the keyring only (`openbao-root-token`), never printed; the
   recovery copy then contains it so the owner can finish, or the owner resets the still empty vault.
3. **Auto-unseal.** `aeos serve` (before it starts the controller) and `aeos vault unseal` first ask OpenBao for
   its seal status. Only when it reports sealed do they read the share(s) from the keyring and unseal. Each
   attempt is an audit row `openbao.unseal` (source, trigger, outcome, number of shares applied; never a value).
   Unreachable, uninitialised or unsealed: the keyring is not read. No share in the keyring: the former behaviour
   (`serve` says "OpenBao is sealed. Next: aeos vault unseal"; `vault unseal` prompts without echo).
   `--from-keyring` makes `vault unseal` fail instead of prompting.
4. **Migration of an existing install.** `aeos vault import-unseal-key` reads the share(s) from a hidden prompt
   (getpass, entered twice; never argv, never echoed) and stores them; `--replace` overwrites stored shares. Audit
   row `openbao.unseal_key_import` with the count. `--with-approle` also restores the AppRole role_id and secret_id
   from the recovery copy at the same kind of hidden prompt (a lost keyring); `--replace` is needed to overwrite
   stored AppRole credentials.
5. **Offline recovery copy.** `aeos vault export-recovery <file>` writes the stored shares, the AppRole role_id and
   secret_id (the secret_id has no TTL and no use limit, so it stays valid with the vault's data) and a kept root
   token to a new file created with mode 0600 (`O_CREAT|O_EXCL|O_NOFOLLOW`, then `fchmod`, then a mode check). It refuses
   an existing path or link, a path inside the AEOS repository or any git work tree, and a path inside the AEOS
   data directory (backups copy that directory; §33 keeps recovery material out of them). It prints the path
   only. Audit row `openbao.recovery_export` (path, count, whether AppRole and a root token are included). On
   Windows it refuses: the code cannot guarantee an owner-only ACL (NOT-PROVEN); `vault init` shows the same
   material once instead (decision 1).
6. **Doctor and status.** `secret.openbao_health` adds one line: where the unseal key lives (OS keyring with the
   share count, or not stored) and the residual risk; the evidence field `unseal_key_location` is `os-keyring`,
   `not-stored` or `UNKNOWN`. Only the count entry is read. `aeos vault status` prints the same location.

## Threat model

* **An attacker with the owner's OS user session** (malware running as the user, an unlocked session) can read
  the keyring entries. That attacker already reaches the AppRole credentials in the same keyring service and with
  them every secret the controller may read. The unseal key adds the ability to unseal after a restart and,
  together with a copy of `<data>/openbao/raft`, to decrypt the storage offline, including `office/` paths the
  controller is not bound to. That is a wider reach inside the vault, but not a new class of attacker: the same
  session access is required. The residual risk is stated in `aeos vault init`, `import-unseal-key`, Doctor and
  this ADR.
* **Another OS user or an offline disk thief** needs the user's keyring unlocked (macOS login keychain, Windows
  DPAPI bound to the user's credentials). Not reachable without the user's password.
* **Chat and screenshot leaks** (incident class of 2026-10-26): the default flow prints nothing, and no reboot needs
  a manual unseal any more, so the material no longer passes through a terminal. The recovery copy is the one
  moment it exists outside the keyring; it is a file, never terminal output.
* **Loss of the keyring** (user profile reset, new machine with a copy of `<data>/openbao/raft`): the offline
  recovery copy restores both the unseal share(s) and the AppRole credentials
  (`vault import-unseal-key --shares <n> --with-approle`); without the AppRole credentials the vault would unseal
  but the controller could not log in, and with the root token revoked nothing could re-issue them. Without that
  copy, a vault whose keys are only in the keyring is lost; the owner makes the copy right after init or migration.
  A copy made before ADR-0123 (the shown-once unseal key only, or one exported while no AppRole was stored, which
  says so in the file) restores the unseal key but not the controller login: then re-initialise and re-enter every
  credential (runbook step 5).
* **The recovery copy itself** holds the unseal share(s) and the AppRole credentials together: whoever holds it and
  the raft data reads everything. That is no wider than the unseal key alone with the raft data; it is why the copy
  lives offline and never on the machine.
* **Root token**: not long-lived. A complete setup leaves none anywhere; generating a new one needs the unseal
  quorum and an owner decision (break-glass, NOT-PROVEN, ADR-0042 runbook step 6).

## Consequences

* Reboots no longer need a manual unseal: `aeos serve` unseals from the keyring, and `aeos doctor` shows where the
  key lives. Same machine, same OS user: one keyring compromise covers both AppRole and unseal.
* The macOS Keychain may ask once to allow the Python interpreter to read the new entries (same as the AppRole
  entries).
* `run_init(store_unseal_in_keyring=False)` remains the library default for callers that show the keys themselves
  (the live integration test); the CLI default is the keyring.
* Evidence: unit tests with fakes (in-memory keyring, respx OpenBao, monkeypatched getpass) in
  `tests/unit/test_vault_keyring_unseal.py` and `tests/unit/test_secrets_cli.py`, E2 on macOS arm64 (2026-10-27).

## NOT-PROVEN

* Any real OS keyring: tests never reach the real Keychain, Windows Credential Manager or Secret Service (L20).
  Windows Credential Manager limits a generic credential blob to 2560 bytes (the `keyring` backend stores UTF-16,
  so about 1280 characters); an unseal key share (44 base64 characters) and a root token fit by arithmetic, not
  by a Windows run.
* Auto-unseal against a real OpenBao after a real reboot, and the macOS Keychain access prompt for the entries.
* The owner-only ACL of a recovery file on Windows (the command refuses there), and that the owner's chosen
  target is not a cloud-synced folder (the command cannot detect sync clients).
* That an imported key is correct: it is checked the next time OpenBao is sealed (`serve` or `vault unseal`
  report a rejected key and name `import-unseal-key --replace`). Imported AppRole credentials are checked by
  `vault status --verify`.
* A real restore after a lost keyring (profile reset or a new machine with the raft data): proven only by a unit
  round trip with fakes (export, empty keyring, `import-unseal-key --with-approle`).
* The Windows init screen with the recovery material on a real Windows console (unit tests fake the platform).
* Windows and macOS Intel behaviour of the new code paths until CI runs them.
