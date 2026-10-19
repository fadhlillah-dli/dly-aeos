# Evidence: the first DLY AEOS project planned by DLY AEOS (E3, 2026-10-19)

* **What:** the platform project `dly-aeos` (PERSONAL, registered by `aeos seed`, master §09) planned
  through the real 4-stage pipeline (Opus → Fable → Opus → Fable) via the official Claude Code
  binary 2.1.302 with the owner's native Max login — no Claude API, no `--bare`.
* **Input:** [docs/plans/0001-first-run-setup-wizard.md](../../plans/0001-first-run-setup-wizard.md).
* **Where:** a separate local data directory with a temporary admin (random password, never
  stored) — the owner's real data directory was not used. `plan run` took 21 minutes.
* **Result:** state `READY_FOR_HUMAN_REVIEW`; final reviewer verdict PASS-STATIC (a review verdict,
  **not** an approval). Resolved models per stage are in [run-metadata.json](run-metadata.json):
  Opus stages → `claude-opus-5-5`, Fable stages → `claude-fable-5-1`, mode NATIVE.
* **Content:** [plan-version-2.json](plan-version-2.json) (the synthesised PlanVersion: 4 features,
  12 TaskSpec drafts, 3 milestones and every Product 1 section) and
  [product1-preview/](product1-preview/INDEX.md) (local Markdown render by `aeos docs render`,
  48 documents in 21 folders; Mermaid visuals VISUAL_PENDING because mermaid-cli is not installed).
* **Finding fixed on the way (E3 only):** Claude Code's `--json-schema` rejects a
  `"$schema": ".../draft/2020-12/schema"` declaration. The bridge now leaves meta-schema keys out of
  the CLI argument (`aeos.claude_bridge.parsing.cli_schema`) and still validates the output against
  the full schema.
* **Not proven here:** owner approval, publication to Google Docs/GitHub Issues, and worker
  execution — those need the owner's account, credentials and decisions.

Scope of this evidence: this machine (Windows 11), this commit, this account. It is not a claim
about any other environment.
