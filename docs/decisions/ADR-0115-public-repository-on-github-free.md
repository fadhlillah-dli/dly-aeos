# ADR-0115 — Platform repository is public on GitHub Free: free CI on all three OSes, provider rules and security features

* Status: ACCEPTED
* Date: 2026-10-25
* Deciders: owner (Fadhlillah) — "jadikan repo public saja jika memang itu solusi yang free, dan juga di
  settingan kita harus menyesuaikan agar bisa menggunakan akun free"

## Context

`fadhlillah-dli/dly-aeos` was a private repository on GitHub Free. Private repositories get a monthly
quota of Actions minutes, with macOS runners billed at 10x and Windows at 2x. On 2026-10-25 the push run
on `main` after PR #7 (merge commit ecf8b87; 32652a1 before the 2026-10-26 history normalisation, see
[docs/evidence/commit-id-map.md](../evidence/commit-id-map.md)) never started: every job was refused with *"recent account
payments have failed or your spending limit needs to be increased"*. PR #7 itself had passed all its
checks, and the merge commit has the same tree as the tested PR head, but no further PR could get CI.
Private repositories on GitHub Free also cannot enforce branch protection or rulesets (ADR-0063
reports this as `NOT_ENFORCEABLE`). Standard GitHub-hosted runners are free for public repositories,
and rulesets, secret scanning with push protection, Dependabot alerts and CodeQL default setup are
available to public repositories on the Free plan.

## Decision

1. **Visibility public** (owner decision above). Before the switch the whole history was scanned, not
   only the tracked tree: 2,767 blobs on all 8 branches, the secret-scan patterns plus Google OAuth,
   OpenRouter, GitHub, Atlassian, OpenBao, AWS and private-key shapes, sensitive file names (`.env`, keys,
   OAuth client files, databases, `aeos.yaml`) and commit messages. There were no findings. The repository
   never contained credentials, `.env` files, local databases or the owner's `aeos.yaml` (all gitignored,
   AGENTS.md "no secrets anywhere").
2. **CI on all three operating systems for every pull request and every push to `main`.** The
   macOS smoke subset is removed. The backend suite and the bootstrap now run in full on
   `ubuntu-latest`, `windows-latest` and `macos-latest`. The bootstrap had no macOS job before. Only
   standard runner labels are used: larger runners are billed even for public repositories.
3. **Free repository security features enabled:** secret scanning with push protection, Dependabot
   alerts (no automatic Dependabot PRs), "require actions pinned to a full-length commit SHA" (already the
   supply-chain policy of ci.yml) and CodeQL default setup (actions, JavaScript/TypeScript, Python).
   These complement the local G1 secret scan and the report-only supply-chain job (ADR-0095); they do not
   replace them. Default workflow permissions stay read-only, and workflows cannot approve pull requests.
4. **Ruleset on `main`** (now enforceable on Free): no deletion, no force push, changes only through a
   pull request, and the CI gate jobs must pass, with the required checks pinned to the GitHub Actions app
   (integration id 15368). There are no bypass actors, so not even the owner's own token can push to
   `main` directly. Zero required approvals: the repository has one human account, GitHub forbids
   approving one's own pull request, and the seed-build merge delegation (ADR-0003) runs as that
   account. Merge stays a human-account action (G7 checks `merged_by` is a `User`). AEOS's own provider
   check (ADR-0063 §3) now reads this ruleset as `CONFIGURED` instead of `NOT_ENFORCEABLE`.
   `enforcement_tested` stays NOT-PROVEN until the bypass test runs against the real repository.

## Consequences

* CI no longer depends on billing. The push run on `main` after PR #7 was re-run and started at no cost.
* **Everything in the repository is public**, including the full history, ADRs and `docs/evidence`.
  The author and committer email of every commit is public as well (commit metadata, not file
  contents). Future commits can use the GitHub `users.noreply.github.com` address if the owner sets
  it as the commit identity (`git config user.email`, or the commit identity in `aeos.yaml`). Rewriting
  existing history to remove the address is not done: it would need a force push to `main`.
* Pull requests from forks run CI without secrets and with a read-only token, as before (the workflow
  uses `pull_request`, never `pull_request_target`). GitHub's default approval requirement for
  first-time contributors' workflow runs stays on.
* Products managed by AEOS are unaffected: each project's repository keeps its own visibility, and
  AEOS reads each provider's real entitlements per repository (ADR-0063). OFFICE code stays on the
  company code host (ADR-0111).
* The owner can undo each part in the repository settings: Settings -> General -> Danger Zone for
  visibility, Settings -> Rules -> Rulesets for the ruleset, and Settings -> Code security for the
  security features.

## Amendment 2026-10-26 — Intel macOS and Command Prompt

1. The backend suite and the bootstrap also run on `macos-26-intel` (x86_64). `macos-latest` is Apple
   Silicon (arm64), and both Mac architectures are target hosts, so neither one stands in for the other.
   The Intel runner is a standard runner, free for public repositories.
2. `aeos.cmd` starts the CLI from Command Prompt (cmd.exe), where `.\aeos.ps1` opens in an editor instead
   of running. `tests/unit/test_bootstrap_scripts.py` runs it on Windows, including the case where the
   virtual environment is missing.
