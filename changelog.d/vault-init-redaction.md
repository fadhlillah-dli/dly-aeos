### Security

- **Vault/OpenBao init output is detected and redacted.** The shared credential patterns
  (`aeos.core.redaction`, reused by `scripts/secret_scan.py` and the verifier binary scan) now match the labelled
  forms `Unseal Key <n>: <base64>`, `Recovery Key <n>: <base64>`, `Initial Root Token: <token>`, the
  `unseal_keys_b64` / `unseal_keys_hex` JSON arrays, the `hvb.` / `hvr.` token prefixes (`hvs.` now covers the
  full token alphabet) and the short OpenBao recovery token `r.<24+ alphanumerics>` (not after a letter, digit
  or dot). Runtime redaction matches over the whole text, so a pretty-printed `operator init -format=json` is
  redacted; `scripts/secret_scan.py` now also runs the patterns over each whole file (`multiline_findings`),
  while its per-line pass and the verifier added-line scan cannot see a multi-line form. The label is required,
  so bare base64 such as lockfile hashes never matches (a unit test runs every one of the new patterns, the recovery token pattern included, over all tracked files and requires 0 matches). `AGENTS.md`
  invariant 12 states that a secret seen in chat is burned: never quote or store it, rotate or re-key it per the
  runbook, touch credentials only on explicit instruction. This prevents the incident class where an unseal key
  is pasted into chat. Evidence: E1 static plus E2 unit run on macOS arm64 (synthetic labelled samples red,
  unlabelled near-misses green); NOT-PROVEN on Windows and Intel macOS until CI runs there.
