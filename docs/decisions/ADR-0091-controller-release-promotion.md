# ADR-0091 — Released controller artifacts, human-only promotion and rollback

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by the Operations & Reliability module

## Context

Master §09 separates the trusted released controller, the candidate build workspace and the
accepted release, and requires "artifact/config hash, versioned migrations, pre-upgrade backup,
compatibility check, health/function smoke tests dan recovery. Kegagalan upgrade tidak boleh
menghilangkan rilis controller lama atau evidence." §40: promotion decisions are bound to hashes;
"Rollback kode tidak menjamin rollback data"; new controllers never load themselves into the trusted
process. §50: "Upgrade controller gagal → Rilis lama dan data/evidence tetap dapat dipulihkan".

## Decision

1. `aeos ops release build` creates `<data>/releases/<version>-<sha12>.zip` with `git archive` from a
   commit reachable from `main` in a clean working tree, plus a provenance manifest (commit, tree
   hash, artifact SHA-256, schema head parsed statically with `ast`, built_at, builder). Candidate
   code is never imported into the trusted process.
2. `aeos ops release promote` / `rollback` are CLI-only (no HTTP route), refuse inside worker or
   candidate contexts, require an active SUPER_ADMIN `--operator` and typed confirmation, and record
   a `PROMOTION` Decision bound to the artifact hash (fail closed if it cannot be recorded).
3. Promotion order: integrity → compatibility (database revision must be the release head or an
   ancestor; newer databases are refused) → pre-promote backup + restore test → smoke test in a
   separate process (extracted artifact's app factory, migrated *copy* of the database, temporary
   loopback port, `GET /api/health` must be ok) → live migration only when the schema changes and
   the server is stopped → `ACTIVE.json` updated with the previous release kept.
4. Rollback moves the pointer to the previous release after the same checks and is refused when
   the database is ahead of that release, naming the pre-promote backup to restore first.
5. Any failure leaves the active pointer unchanged, invalidates the decision, raises a HIGH alert and
   is audited.

## Consequences

* The active pointer identifies the trusted release; running the controller and the UI directly
  from the extracted artifact (instead of the repository checkout) is a later step (NOT-PROVEN).
* Smoke tests prove startup + health against migrated data, not full functional correctness; CI
  and the acceptance suite remain the functional evidence.
