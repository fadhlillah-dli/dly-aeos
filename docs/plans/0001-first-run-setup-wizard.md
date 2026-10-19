# DLY AEOS upgrade — slice 1: first-run setup wizard

Domain: PERSONAL. Project: DLY AEOS (platform, self-build per master §09).

## Problem

A fresh clone reaches the dashboard, but the owner still has to read the Readiness Doctor table and
README to work out the order of the manual setup steps of master §07 (admin account, Claude Code
login check, OpenBao install/init/unseal, worker credential, OpenCode, Google OAuth consent, GitHub
token, worker route activation, sandbox decision, first backup + restore test). Each step already
has a Doctor check and a copyable remedy, but nothing guides the owner through them in order or
shows what each step unlocks.

## Goal

A "Penyiapan awal" (first-run setup) page in the Control Center that walks the owner through the
§07 bootstrap order using only existing Doctor checks, capability gates and decision endpoints:

- one ordered step per §07 bootstrap step, each showing: why it is needed (which capability it
  unlocks), current status from the Doctor (with freshness), the exact copyable command for the
  owner's OS (Windows PowerShell / macOS), and a "Periksa ulang" button that re-runs only the
  checks of that step;
- steps that are human decisions (route activation, process-sandbox exception, write enablement)
  link to the existing decision forms with the exact target shape pre-filled — the wizard never
  records a decision itself and never runs any command;
- progress is computed from Doctor results only (never from the wizard's own state), STALE/UNKNOWN
  are shown as such, and the page works for a VIEWER read-only;
- an entry point from the Ringkasan page while any CAN_* gate needed for PERSONAL work is BLOCKED.

## Non-goals

No auto-install, no secret entry in the browser, no OFFICE steps beyond showing that OFFICE stays
blocked until a company authorisation is recorded.

## Acceptance (examples)

- With an empty data dir every step shows MISSING with its remedy, in §07 order.
- After `aeos admin create` and a Doctor re-run, step 1 shows VERIFIED with its check time.
- A VIEWER sees the page but no action buttons; a direct POST from a VIEWER is refused (403).
- Re-check of one step re-runs only that step's checks and updates freshness.
- No secret value, token or password is ever rendered or sent by the page.
