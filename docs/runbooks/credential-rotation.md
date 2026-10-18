# Runbook: Credential rotation

## Trigger

A credential is expiring (Control Center *Integrasi & Akses* shows "segera kedaluwarsa"), a doctor
check reports EXPIRED/EXPIRED_AUTH, a secret may have leaked (found in a log, commit, screenshot or
chat), or a scheduled rotation is due. A suspected leak is also an incident: start with
[incident-containment](incident-containment.md) if the credential could be abused right now.

## Authority

Owner only. Rotation at the provider (GitHub, OpenRouter, Google, Atlassian) is a manual human
action in the provider's console; AEOS never rotates or revokes provider credentials by itself.

## Steps

1. **Identify the reference**, never the value: `aeos secrets --help` lists the status commands;
   the UI shows ref, backend, updated/expiry only (§26).
2. **Revoke active capabilities that use it.** Worker capabilities are short-lived broker grants;
   an emergency stop revokes all of them at once. For a single run, cancel it
   ([cancel-runaway-run](cancel-runaway-run.md)).
3. **Create the new credential at the provider** with the same least-privilege scope (e.g. a
   fine-grained GitHub token limited to the platform repository; an OpenRouter key with a spend
   limit).
4. **Store it** without echoing it to the terminal history:
   - GitHub: `gh auth token | ./aeos secrets set personal/github --stdin` (macOS/Linux);
     on Windows pipe into the program directly:
     `gh auth token | .\.venv\Scripts\aeos.exe secrets set personal/github --stdin`
   - OpenRouter worker key: `aeos secrets set workers/openrouter` (prompts without echo)
   - Google OAuth: re-run `aeos publish google-connect --client-secret <file>` and complete consent.
5. **Revoke the old credential at the provider** once the new one is verified.
6. **Re-run the doctor** for the affected capability (`aeos doctor`). Rotation invalidates the
   related checks; they must pass again before the capability is used (§08).
7. **If the secret leaked into a file or commit**: remove it from the working tree, rotate (step
   3-5) — rewriting history is not enough because the old value must be considered compromised.
   Run `python scripts/secret_scan.py` to confirm no tracked file still contains it.

## Verification

* Doctor check for the provider is CONNECTED/VERIFIED again with a fresh timestamp.
* The old credential is rejected by the provider (test with the provider's own tooling).
* No secret value appears in an audit export (*Riwayat & Pemulihan* → *Ekspor* → audit, or
  `GET /api/ops/export?kind=audit`; exports are redacted again before they leave the server).

## Evidence

* Audit: secret store write (`secrets.*`), doctor run, capability revocations.
* Provider-side audit log of the revocation (keep a reference, not a screenshot containing the key).
