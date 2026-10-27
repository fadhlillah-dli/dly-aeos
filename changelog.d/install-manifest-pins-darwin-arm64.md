### Security

- **macOS publisher pin for Claude Code; OpenBao version pin (OWNER_ACTIONS 1.12, darwin_arm64).** Under the owner's
  decision of 2026-10-27, the reviewed install-manifest pins only values whose drift is caught without blocking a
  capability on a routine update. The `claude-code` entry gets `signature.approved_signer` `darwin: Q6L2SF6YDW` (the
  Anthropic PBC TeamIdentifier, read with `codesign` on the owner's M4). An unsigned or foreign-signed Claude binary is
  now BLOCKED on macOS by the bridge and by `claude.binary`. Claude Code gets no version or digest pin, because it
  updates itself. OpenCode gets no manifest pin. The worker runtime keeps its built-in default pin 1.19.3, and drift
  from it fails `worker.opencode` and blocks `CAN_RUN_WORKER`. So 1.12 tells the owner to turn OpenCode self-update
  off (`OPENCODE_DISABLE_AUTOUPDATE=1`) on each machine. OpenBao is pinned to 2.7.1. Its observed darwin_arm64 digest is not recorded yet, because a digest pin makes OpenBao
  change proposals need a candidate digest, and the trusted acceptance suite does not supply one. Git, Node.js and
  Python stay unpinned. `reviewed_by` names the owner. `windows_amd64` and `darwin_amd64` stay NOT_RECORDED.
  `tests/unit/test_inventory_pins.py` checks the rule with bite fixtures. Tests outside the signature module see the
  publisher as not pinned (their Claude binaries are fakes). Evidence E2 on macOS arm64 (targeted tests); E3 for the
  observed signer and version on the owner's M4; NOT-PROVEN on Windows and macOS Intel until CI runs.
- **A transient code-signature failure no longer blocks the Claude bridge for the life of the process.** With the
  darwin publisher pinned, a check that could not decide (`codesign` failed to start or timed out, an unrecognised
  answer, or a VALID report without its signer) BLOCKS the call it gates, by design. The result cache per (path,
  fingerprint) used to keep that UNKNOWN until a restart. Now only a definitive verdict is cached (INVALID, UNSIGNED,
  or VALID with its signer, so a definite mismatch stays BLOCKED), and an undecided check runs again on the next call.
  Bite tests in `tests/unit/test_claude_bridge_signature.py`. Evidence E2 on macOS arm64; NOT-PROVEN on Windows until
  CI runs.
