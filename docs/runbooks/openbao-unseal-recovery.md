# Runbook: OpenBao unseal and recovery

## Trigger

After a reboot OpenBao is sealed (doctor: secret store FAILED/UNKNOWN; *Integrasi & Akses* shows
credential status UNKNOWN), the AppRole/agent token expired, the OpenBao data directory was lost or
corrupted, or unseal/recovery material must be re-created.

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
6. **Re-run the doctor**: `aeos doctor`; secret-dependent capabilities stay BLOCKED until their
   checks pass.

## Verification

* Doctor secret check VERIFIED with a fresh timestamp; `GET /api/control-center/integrations` shows
  credential status CONFIGURED for the expected references (values are never shown).
* A test operation that needs a secret (e.g. the GitHub adapter dry-run) succeeds.

## Evidence

* Doctor results and audit rows for secret-store access; OpenBao's own audit device if enabled.
* Record *that* unseal keys were re-created and where they are kept (location, not content).
