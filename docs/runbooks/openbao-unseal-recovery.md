# Runbook: OpenBao unseal and recovery

## Trigger

After a reboot OpenBao is sealed (doctor: secret store FAILED/UNKNOWN; *Integrasi & Akses* shows
credential status UNKNOWN), the AppRole/agent token expired, the OpenBao data directory was lost or
corrupted, or unseal/recovery material must be re-created, for example because an unseal key was
exposed (pasted into a chat or an AI assistant, captured in a screenshot, written to a file).

## Authority

Owner only. Root token, unseal keys and recovery keys are never stored with the application
configuration, in the AEOS database, in backups, in the repository or in chat (§26). Every
operation that needs a secret fails closed while OpenBao is unavailable — that is intended.

## Steps

1. **Status**: `aeos vault --help` lists the vault commands of this installation. The service
   inventory (`GET /api/ops/services`) shows OpenBao as CONNECTED (sealed/unsealed) or UNREACHABLE —
   CONNECTED is not a functional verification.
2. **Start** OpenBao if it is not running: `aeos vault start`.
3. **Unseal**: `aeos vault unseal` and enter the required number of unseal key shares from your
   offline storage (password manager entry or paper kept apart from this machine). Do not paste
   keys into files or terminals that log history.
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
     delete `<data>/openbao/raft`, `aeos vault start`, `aeos vault init`. The AppRole credentials in the
     OS keyring are replaced by the new init. Do not judge emptiness by doctor `secret.status`: it does
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
