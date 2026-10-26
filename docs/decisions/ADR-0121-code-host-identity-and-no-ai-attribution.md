# ADR-0121 — Owner identity on code hosts and no AI attribution (§06, §09, §23, §25, §26, §31)

* Status: ACCEPTED
* Date: 2026-10-26
* Deciders: owner (Fadhlillah) — standing owner rule given on 2026-10-26 for DLY AEOS and every project it creates
  or manages. The rule is the owner's. The enforcement design below was built and reviewed in wave 7b (lane
  `ident`). The owner can still record an ADR decision on this file's content
  (`aeos policy adr-decide ADR-0121 ...`, see the register README).

## Context

The owner gave a standing rule on 2026-10-26:

1. Every activity on **GitHub** (commits as author and committer, pushes, pull requests, issues, comments) uses
   the owner's GitHub identity. Every activity on **Bitbucket** uses the owner's company identity. These are
   two different e-mail addresses.
2. **No AI appears on GitHub, Bitbucket or the company's Jira/Confluence.** No AI is named as author, committer,
   co-author or contributor, and no attribution line names one. That excludes `Co-Authored-By:` trailers naming
   an AI or a bot, "Generated with/by <AI tool>" footers, robot emoji attribution, AI vendor and bot noreply
   addresses, and model, route or agent names used as attribution in commit messages, PR titles and bodies,
   issues, comments or pages.
3. Provenance (run id, agent, route, model) stays inside DLY AEOS's own records. It is not written to the code
   host.

Before this decision, one identity (`commit_author_name` / `commit_author_email`) served every host. When it was
unset, worker candidates were committed as `AEOS Runtime <aeos-runtime@localhost>` and could still be pushed.
Every candidate commit carried `AEOS-Agent`, `AEOS-Route`, `AEOS-Engine` and `AEOS-Prompt-Revision` trailers,
and the PR evidence bundle named the reviewer tool and model. Nothing checked outbound text for attribution.

The platform repository is public (ADR-0115), so the owner's real addresses must never appear in code, tests,
docs, fixtures or commit history.

## Decision

1. **Per-host owner identity, local only.** `commit_identities.github` and `commit_identities.bitbucket`
   (`name`, `email`) are set in the owner's local `aeos.yaml` in the OS config directory, or in
   `AEOS_COMMIT_IDENTITIES` as JSON. They are never committed: `/aeos.yaml` and `config/aeos.yaml` are gitignored
   as a guard, and `config/aeos.example.yaml` and the docs carry placeholders only. `commit_author_*` remains a
   backward-compatible fallback for **github only**. Bitbucket never falls back. Validation follows the earlier
   workspace rules and also refuses AI and bot identities. A project's code host is Bitbucket for OFFICE or a
   registered Bitbucket repository, and GitHub otherwise (`aeos.core.commit_identity.provider_for`).
2. **Worker candidate commits** are made with the project's code-host identity as both author and committer.
   They carry no agent, route, engine, model or prompt-revision trailer; the subject keeps the `run <id8>` link.
   Without a valid identity for the host, the runtime still commits locally with the runtime identity, but
   `run.caps.commit_identity.pushable` is `false` and the reason is recorded. A commit the worker made itself
   with a foreign or AI identity also marks the candidate not pushable. The run record keeps the provider, the
   config key, the status and the pushable flag, never the address.
3. **Broker push gate** (`broker/git_broker.py` for GitHub, `broker/bitbucket_broker.py` for Bitbucket).
   `push_from_mirror` requires a commit policy. A missing identity is refused (`COMMIT_IDENTITY_MISSING`) before
   the grant is used or the credential is read. Before the push, the broker fetches the remote's branches into
   `refs/aeos-remote/heads/*` in its own temporary mirror, using the push credential. It then checks every commit
   of the head that no remote branch reaches: author e-mail equals committer e-mail equals the configured
   identity, the identity is not an AI or bot, and the message carries no AI attribution. Unpushed history
   below the run's base is therefore checked, and history already on the remote is not. More than 1000 such
   commits is refused (`COMMIT_RANGE_UNBOUNDED`). A remote whose branches cannot be read fails the push (FAILED,
   nothing sent). Each refusal (`COMMIT_IDENTITY_MISMATCH`, `AI_ATTRIBUTION_IN_COMMIT`, and the codes above)
   leaves a DENIED audit row and a FAILED outbox row, and nothing reaches the remote.
4. **One outbound text guard** (`aeos.core.attribution`: `find_ai_attribution`, `assert_no_ai_attribution`).
   It checks every outbound text before the request: PR titles and bodies on GitHub and Bitbucket (before the
   branch is pushed, and again in the broker before any credential is read), GitHub issues and labels, Jira
   issues, and Confluence pages and folders, including kept human-written sections. It **refuses, never
   strips** (`AI_ATTRIBUTION_IN_TEXT`, with an Indonesian remedy), so the owner removes the text and sends it
   again. Text is normalised before matching (NFKC, invisible characters removed, Cyrillic and Greek look-alikes
   read as Latin), and the caller's text is never changed. The guard looks for attribution, not mentions: a
   module name such as `fix(claude_bridge): ...` or "route deepseek requests through the broker" is ordinary
   text. A trailer naming a human called Claude is refused too (fail closed).
5. **Neutral renderer labels.** The PR evidence bundle shows roles ("Supervisor review", "independent verifier",
   "Worker run", "Prepared by DLY AEOS delivery"), not tool or model names. The reviewer identity and model stay
   in the review record and the bundle data.
6. **Provenance stays inside AEOS**: run rows and caps, EvidenceArtifact, review and acceptance records, the
   broker audit trail. None of it is written to the code host.
7. **Readiness.** Doctor checks `git.commit_identity.github` and `git.commit_identity.bitbucket` report
   CONFIGURED, MISSING or FAILED (invalid, including an AI or bot identity). The Bitbucket check is
   NOT_APPLICABLE while no Bitbucket repository is registered. The remedy names the `aeos.yaml` key, never an
   address. `git.commit_identity.github` is in the critical tier of `CAN_PREPARE_PR`, and OFFICE delivery
   readiness lists a missing or invalid company identity as a reason. The rule is AGENTS.md invariant 11; the
   operating guide is docs/OPERATIONS.md "Commit identity and no AI attribution".

## Consequences / not decided here

* **Owner step:** set both identities in the local `aeos.yaml`, then run `aeos doctor`. Until
  `git.commit_identity.github` shows CONFIGURED, `CAN_PREPARE_PR` is BLOCKED and the broker refuses every push.
* CONFIGURED is not VERIFIED: reading `aeos.yaml` does not prove that the address belongs to the account. The
  Doctor fingerprint is an unkeyed SHA-256 of the configured values, so someone who can read the local Doctor
  API could confirm a guessed address (the data stays local).
* Candidates committed before this decision still carry the old `AEOS-*` trailers and are refused at push
  (`AI_ATTRIBUTION_IN_COMMIT`); those tasks are re-run.
* Local history with unpushed commits by another address, or with AI trailers, is refused at push. The owner
  pushes or rewrites that history first.
* `pushable` on the run comes from the runtime's base..head check only. The broker re-checks the full unpushed
  range, so a candidate marked pushable can still be refused at push time.
* Remote tags are not fetched. A commit reachable only from a remote tag is checked anyway, which errs toward
  refusing. Every push now also fetches the remote's branches, which costs a download on large company
  repositories with many branches.
* No comment or PR-update write path exists today. When one is added, it must call the same guard.
