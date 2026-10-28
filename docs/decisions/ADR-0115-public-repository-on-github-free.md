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

## Amendment 2026-10-26 — host platforms (Intel macOS added, Linux removed) and Command Prompt

1. The backend suite and the bootstrap also run on `macos-26-intel` (x86_64). `macos-latest` is Apple
   Silicon (arm64), and both Mac architectures are target hosts, so neither one stands in for the other.
   The Intel runner is a standard runner, free for public repositories.
2. `aeos.cmd` starts the CLI from Command Prompt (cmd.exe), where `.\aeos.ps1` opens in an editor instead
   of running. `tests/unit/test_bootstrap_scripts.py` runs it on Windows, including the case where the
   virtual environment is missing.
3. **Linux removed from the host matrix (owner decision 2026-10-26).** The owner runs only Windows and macOS.
   The backend suite and the bootstrap now run on `windows-latest`, `macos-latest` and `macos-26-intel`.
   Linux host support becomes NOT-PROVEN: `bootstrap.sh`, XDG paths and the `sys.platform` branches stay
   in place but no longer run in CI. Two checks that used to run only on Linux moved to the macOS arm64
   job: mypy now checks both `--platform darwin` and `--platform win32`, which are the code paths the
   owner's hosts execute, and the real trusted-suite verifier test moved there too. The frontend, browser
   e2e and supply-chain jobs stay on `ubuntu-latest` runners. They check platform-neutral artifacts, not
   Linux host support, and finish fastest there. The CI wall clock is set by the slowest job, the Windows
   suite, so removing the Linux jobs saves runner time rather than waiting time.

## Amendment 2026-10-26 (2) — CI in about ten minutes: shards, parallel workers and aggregator checks

**Context (measured 2026-10-26).** The critical path of every CI run was the serial backend pytest step:
2,751-2,790 s on `windows-latest`, 2,535-2,722 s on `macos-26-intel` and 1,824 s on `macos-latest`, where the
real trusted-suite verifier step added another 470 s in the same job. Installs took 49-146 s without a pip cache,
and the Playwright browser install took up to 876 s once (no cache). A run took 47-66 minutes. macOS queue waits of
16-40 minutes came from several pull-request runs competing for the 5 concurrent macOS jobs of GitHub Free. Locally
(Apple Silicon) the suite took 1,053 s serially and 340 s with pytest-xdist `-n 4` (4,047 passed, 0 failed, 3.1x),
so it is parallel-safe on macOS. The concurrency group `ci-${{ github.ref }}` with `cancel-in-progress: true`
cancelled a push run on `main` when the next merge landed: the merge commit of PR #14 never got a CI PASS, and
`aeos ops release accept` needs one for each `main` commit.

**Decision.**

1. **Policy unchanged.** Every pull request and every push to `main` still runs the full backend suite on all
   three host OSes. Sharding splits the suite and selects nothing. On each OS, two shard jobs run
   `pytest -m "not integration and not network" --shard K/2`, the selection the single job ran. Together they
   run every test exactly once. `--shard K/N` (`tests/conftest.py`, also the `AEOS_TEST_SHARD` variable) assigns
   whole test files. It packs the files collected for the run into N shards with greedy longest-processing-time
   by measured seconds (`tests/ci_durations.json`). A file without a measured time gets the median weight. Ties
   are broken by a SHA-256 of the path, so the split does not depend on the OS, the input order or
   `PYTHONHASHSEED`. Every collected file lands in exactly one shard, and a collection error fails both shards.
   The code lives in `tests/conftest.py` because the verifier's copy of the trusted suite carries only
   `tests/__init__.py`, `tests/conftest.py` and `tests/acceptance` (`aeos.verification.suite`). Without
   `--shard` it changes nothing. It does change the trusted suite hash, as any change to that file does.
2. **Parallel workers.** `pytest-xdist` joins the dev extra (ADR-0093 amendment 2026-10-26). Each shard runs with
   `-n logical`, one worker per logical CPU. The plain `-n auto` counts physical cores when psutil is installed
   (it is a runtime dependency), which is half the vCPUs of the 4-vCPU Windows runner.
3. **G1 static checks once per OS.** ruff and the secret/PII scan run in shard 1 of each OS. mypy runs
   `--platform darwin` and `--platform win32` in shard 1 on `macos-latest`. The gate tag comes from the matrix:
   shard 1 is named `backend shard 1/2 [G1,G2] (<os>)` and shard 2 `backend shard 2/2 [G2] (<os>)`. A G1 step runs
   only where the name claims G1 (`if: contains(matrix.gates, 'G1')`).
4. **The real trusted-suite verifier run** has its own job, `backend verifier [G2] (macos-latest)`, so it runs
   next to the shards instead of after the suite.
5. **The required check names stay.** The ruleset on `main` keeps its 8 required contexts: the three
   `backend [G1,G2] (<os>)` names, the three `bootstrap [G2] (<os>)` names, `browser e2e + a11y smoke [G3]` and
   `frontend [G1,G3]`. Each `backend [G1,G2] (<os>)` is now an aggregator job on `ubuntu-latest`. It `needs` every
   backend job of its OS: both shards, and on `macos-latest` also the verifier job. It runs with `if: always()`
   and passes only when every needed job ended in `success`. A failed, skipped or cancelled job fails it, so the
   required name can never pass on a partial run. The aggregator runs no test itself. The bootstrap, frontend,
   browser e2e and supply-chain jobs keep their names. `tests/unit/test_ci_gates.py` pins all of this. It checks
   that each required name is reported by exactly one job, the aggregator wiring and behaviour, that the shards
   1..N exist per OS with the same selection and identical steps, the G1 placement, the verifier job, the
   concurrency rule and the caches.
6. **Concurrency.** `cancel-in-progress` is true only for `pull_request` runs. Every push run gets a group of its
   own (`github.run_id`), so a `main` run is never cancelled, and never replaced while pending, which a shared
   group would do even without cancel-in-progress.
7. **Caches** hold public downloads only. They hold no token, no data directory and no `.venv`. The pip cache of
   `actions/setup-python` is keyed on `requirements.lock` in every Python job, including the bootstrap, whose own
   `.venv` pip reads the same user-level cache. The npm cache of `actions/setup-node` is keyed on
   `frontend/package-lock.json` wherever `npm ci` runs, including the bootstrap. The Playwright browsers
   (`~/.cache/ms-playwright`) are cached by the installed Playwright version through `actions/cache`. The
   bootstrap still installs everything from the lockfiles, so a cache changes download time only.
8. **Rebalancing.** Each shard uploads its JUnit XML as the artifact `junit-backend-<os>-shard-<k>`, kept for
   14 days. `python scripts/ci_durations.py <reports...>` rebuilds `tests/ci_durations.json` from them, summing
   seconds per file. The committed weights come from the local 2026-10-26 run (Apple Silicon, `-n 4`). They
   should be replaced with the first CI run's reports, normally both `windows-latest` shards.
9. **Job timeouts.** Every job sets `timeout-minutes` (backend shards 25, macOS verifier 20, aggregators 5,
   bootstrap 15, frontend 15, browser e2e 20, supply-chain 20). The default is 360 minutes, and a hung macOS job
   would hold one of GitHub Free's 5 account-wide macOS slots for hours. `tests/unit/test_ci_gates.py` requires
   the key on every job.
10. **New action pins** (full commit SHAs of released tags, both older than two weeks):
   `actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9` (v7.0.2, released 2026-10-07) and
   `actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9` (v6.1.0, released 2026-06-26). The other pins
   are unchanged.

**Expected timings (NOT-PROVEN until the first CI run on the new layout).** These are estimates. Each shard
carries half the measured serial time, about 1,390 s on Windows and 1,270-1,360 s on Intel macOS. With 4 workers
at the local 3.1x speed-up that is about 450 s per shard. On `macos-latest` (3 vCPU) it is 912 s / ~2.5, about
370 s. A warm pip cache adds about 60-90 s of install. The verifier job is about 470 s plus install. The
aggregators add about 10-20 s plus an ubuntu queue slot. With free runners, a pull-request run should therefore
take about 10 minutes. Caveats:

* A run now has 7 macOS jobs (4 shards, the verifier and 2 bootstraps) against GitHub Free's 5 concurrent macOS
  jobs, so up to 2 of them wait for a free slot. The bootstraps take 1-3 minutes, so expect 10-12 minutes. macOS
  runner time per run roughly halves, which also shortens the queue when several runs compete.
* The first run after a change to `requirements.lock`, `frontend/package-lock.json` or the Playwright version has
  cold caches.
* Parallel safety is proven only on macOS. On Windows and Intel macOS it is NOT-PROVEN until the first run. If a
  test turns out to share state across workers, the fallbacks are `--dist loadfile` for that OS or a third shard.
* Branch protection is owner configuration. This change does not touch the ruleset: the required names keep
  existing, now reported by the aggregators.

## Amendment 2026-10-27 — rebalanced shards: static job, per-OS weights, 3 Windows shards

**Context (measured, GitHub run 40424256888, the last green run of PR #26, per-job wall time including setup).**
The layout above ran in 11.1 minutes (first job created to last job completed), which is above the goal of about ten.
Backend shard 1 / shard 2: `windows-latest` 9.3 / 8.8 min, `macos-latest` 11.0 / 5.5, `macos-26-intel` 9.6 / 7.6; the
verifier 8.0. Three causes: (a) shard 1 of every OS also ran the G1 static checks, 58 s on `macos-latest` (mypy for
both platforms 49 s, secret scan 9 s) and 10-16 s elsewhere; (b) `tests/ci_durations.json` still held the seconds
of a local run made before the test speedups of PR #26, and one split of those weights was used on three OSes whose
files cost differently: the same two shards ran 1,216 s and 697 s of test time on `macos-latest` (G2 step 534 s and
295 s) but 1,615 s and 1,569 s on `windows-latest`; (c) the macOS jobs of one run add up to 2,743 s on 5 concurrent
slots, so the bootstrap of Intel macOS waited 338 s for a slot.

**Decision.**

1. **G1 runs once, in its own job.** `static checks [G1] (ubuntu-latest)` (job id `static-checks`) runs ruff,
   mypy `--platform darwin` and `--platform win32` and the secret scan once, on `ubuntu-latest`; it consumes no
   macOS slot. These checks are platform-neutral (they read files; `--platform` selects the code path to check and
   needs no such host). The alternative, a fixed head-start offset for shard 1 in the packer, would need a
   different offset per OS (58 s against 10-16 s) and a hook change in a trusted file; the job needs neither, and
   every shard becomes the same job (no `gates` matrix key, no `if:` on the G1 steps). The required semantics
   hold: each of the three aggregators `backend [G1,G2] (<os>)` now also `needs` `static-checks` and lists its
   result, so a failed, skipped or cancelled static job fails all three required names. mypy on Linux is
   proven by run 40427552409 (PR #27): the job took 62 s, the mypy steps 30 s in all. Each mypy platform is its own step
   (`G1 static — mypy darwin`, `... mypy win32`) and the static job's steps carry no `shell:` override: two commands in
   one `run` block under `shell: bash {0}` (no `-e`) would let a darwin failure pass when win32 passes.
2. **Per-OS weights.** `scripts/ci_durations.py --os LABEL FILE...` (once per OS) reads each OS alone, scales it to
   the mean of the OS totals and averages the files, so one split fits all three OSes. A split from one OS alone
   leaves the others uneven (packed from Windows only: `macos-latest` 486 s / 343 s predicted). The committed weights are
   rebuilt from the 6 JUnit reports of run 40424256888.
3. **Windows: 3 shards** (`backend shard K/3 [G2] (windows-latest)`); Windows jobs do not use the 5 macOS slots. The
   macOS hosts stay at 2 shards: 7 macOS jobs already compete for 5 slots, a third shard adds about 50 s of setup
   per extra job and would raise the macOS slot time. The shard job names carry the matrix (`shard`, `shards`)
   and are not required checks; `backend [G1,G2] (windows-latest)` needs the whole shard job, which fails when any
   of its three legs fails.
4. Every test still runs on all three OSes: `--shard K/N` assigns whole files, and
   `tests/unit/test_ci_shards.py` collects the whole suite with the exact `-m` expression of the CI shard step
   (read from `ci.yml`) and proves that for 2 and 3 shards the node ids of all shards are disjoint, no file is in two
   shards, and their union equals the unsharded collection (about 12 s).
5. The workflow structure is pinned in `tests/unit/test_ci_gates.py`, not compared with `origin/main` (a pull request
   checkout has no `origin/main`): every non-shard job id with its name (`JOB_NAMES`) next to `REQUIRED_CHECKS`; no
   `actions/checkout` of a backend job carries a `with:` key (`ref` could point G1 or a shard at `main`); and the
   checkout, setup-python and install steps of the static job equal those of the shard jobs and the verifier.

**Predicted shard test seconds** (per-file seconds of run 40424256888 times each OS's measured wall/CPU-second
ratio; the same model gives 482 / 469, 527 / 302 and 465 / 437 for the old split against the measured 482 / 469,
534 / 295 and 496 / 406):

| OS | old shards (measured G2 step) | new shards (predicted G2 step) |
|---|---|---|
| windows-latest | 482 / 469 s | 296 / 311 / 344 s |
| macos-latest | 534 / 295 s | 432 / 397 s |
| macos-26-intel | 496 / 406 s | 450 / 452 s |

**Measured (GitHub run 40427552409, PR #27, replaces the estimate that stood here).** Whole run 10 min 47 s. The static
job took 62 s (the mypy steps 30 s of it). All backend required checks were done at 9.6 min; the last required check
to finish was the Intel bootstrap, which waited for a macOS slot. G2 step seconds per shard (predicted in brackets):

| OS | measured G2 step | predicted |
|---|---|---|
| windows-latest | 372 / 338 / 375 s | 296 / 311 / 344 s |
| macos-latest | 457 / 354 s | 432 / 397 s |
| macos-26-intel | 472 / 383 s | 450 / 452 s |

The macOS jobs add up to about 2,669 s on 5 slots (8.9 minutes at perfect packing), so the macOS slots set the floor, as
estimated. The prediction was low for Windows by 27-76 s per shard and the split is not perfectly even (Windows 338 to
375 s, macOS 354 to 472 s); a further rebalance from the reports of this run is possible but gains little against the
slot floor. The remaining levers, none taken here, are the serial verifier run (434 s of its 479 s) and the Intel
bootstrap (169 s).

**Open items (record only).** 26 tests marked `integration` or `network` run in no CI job (the shards select `not
integration and not network`; the verifier and browser jobs run only their named files). This gap predates this change.

## Amendment 2026-10-29 — 3 shards on macos-26-intel

Main went red after PR #52 without a failing test: on `macos-26-intel` shard 2/2 passed all 3,295 tests in 23 min 4 s,
and the job then hit `timeout-minutes: 25` during its last steps and was cancelled. The same shards took 11 and 16 min
on the PR run an hour earlier, so the Intel runners vary up to about 2x while `windows-latest` and `macos-latest`
stayed steady. Two shards left no margin for that variance (LESSONS L15: a timing assumption, fixed, not re-run).
Decision: `macos-26-intel` runs 3 shards, like `windows-latest`; the timeout stays 25 minutes, so a real hang still
fails. A third Intel shard uses one more macOS slot; jobs that wait for a slot queue, and queue time does not count
toward the timeout.

## Amendment 2026-10-27 — merge policy: merge commit only, two more required checks, settings as code

**Decision** (owner, 2026-10-27: "Keputusan yang lain lakukan rekomendasi yang terbaik yang tidak mengganggu atau
menghambat operasional (harus efesien dan efektif) tapi sesuai dengan praktek industri profesional"; the delegating
session made these choices under it):

1. **A merge commit is the only merge method.** The repository allows merge commits and neither squash nor rebase
   merges; the ruleset on `main` lists `allowed_merge_methods: ["merge"]`. Reason: `scripts/ci_status.py --after-merge`
   finds the CI run of the merge commit by its SHA, and release provenance (`scripts/release_check.py`, the tag on the
   merge commit) names that commit. A squash or rebase merge writes a new commit whose SHA no pull request run has
   tested, so the check after the merge could not say which commit it judged. The merge commit title and message
   settings stay as they were (title from the merge message, body from the pull request title).
2. **`commit subjects [G1]` and `pr title [G1]` are required checks** next to the eight existing ones. Before, a
   subject or title that broke the Conventional Commits or no-attribution rule failed only a non-required job and the
   verdict of `ci_status.py --required-only` stayed GREEN. With a merge commit the pull request title becomes the body
   of the commit on `main`, so it needs the same gate as a commit subject. Both are `ubuntu-latest` jobs with a
   5-minute timeout, so they add no macOS slot.
3. **Branches are not required to be up to date (`strict_required_status_checks_policy` stays false).** Strict mode
   would re-run the 7 macOS jobs of every open pull request after each merge, and GitHub Free has 5 concurrent macOS
   jobs, so a few open pull requests would queue for hours (CONTRIBUTING.md "Merging", step 6). The risk strict mode
   removes, two individually green pull requests that break `main` together, is covered after the fact:
   `ci_status.py --after-merge` judges the merge commit and, while `main` is RED, nothing else is merged.
4. **Head branches are deleted on merge** (`delete_branch_on_merge`): merged branches are not reused, and the local
   `worktree.py list` already reports merged and pushed state from the remote-tracking refs.
5. **Release tags `v*` are protected** by a tag ruleset: no deletion, no non-fast-forward update (a force push) and
   no update at all (GitHub's `update` rule, "Restrict updates"). A published release tag is what `release_check.py`
   and the provenance record point at; moving it silently would break both. The `update` rule is needed because
   `git tag -f v1.2.0 <descendant> && git push -f` onto a descendant is a fast-forward, which `non_fast_forward` alone
   allows. Creating a tag stays allowed, so a release only ever creates a tag. (Review 2026-10-27: the first version of
   this amendment left `update` out.)
6. **Settings as code.** `.github/repository-settings.json`, `.github/rulesets/main.json` and
   `.github/rulesets/release-tags.json` hold the settings; `scripts/github_settings.py --check` compares them with the
   live repository (read-only, `GITHUB_SETTINGS: OK|DRIFT|ERROR`) and `--apply --yes` writes them. It never runs in CI:
   changing the merge gate needs the owner's login, and a workflow with that permission would be a way around the
   gate. A change to the policy is therefore a reviewed pull request first and an `--apply` second.
   `tests/unit/test_ci_gates.py` keeps `REQUIRED_CHECKS` equal to the contexts of `main.json` and each name equal to
   exactly one job of one workflow, so the ruleset cannot require a check that no job reports.

7. **`pr title [G1]` reports on `main` too.** `ci_status.py --after-merge` reads its required contexts from the live
   ruleset and finds the check runs of the merge commit. `pr-title.yml` therefore also runs on a push to `main`, where
   its job is skipped (`if: github.event_name == 'pull_request'`): a skipped check run counts as OK, exactly as for
   `commit subjects [G1]` in `ci.yml`. Without it the context would never exist on a merge commit and the post-merge
   verification, which replaces strict mode (item 3), could never turn GREEN. `tests/unit/test_ci_gates.py` requires
   every required check to be reported by a workflow that runs on a push to `main`.

**Unchanged:** no bypass actors, no required approvals (a single owner), pull request required, no force push or
deletion of `main`, the status checks are those of the GitHub Actions app.

**Evidence and NOT-PROVEN.** E2 on macOS arm64: unit tests with a fake API prove the comparison and the apply order
(PATCH, PUT, POST) against fixture state; a ruleset that allows squash and a missing required context are each DRIFT
naming `allowed_merge_methods` or the context, the identical state is OK, `--apply` without `--yes` sends nothing.
NOT-PROVEN: that GitHub accepts the written files, in particular the `update` rule without parameters in a tag ruleset (the live `--apply` has not run), that the skipped `pr title [G1]` run appears on the first merge commit after this change, that the live state then reads
back as OK without a server-side default making a permanent DRIFT, that the merge button then offers only a merge
commit, and everything on Windows and macOS Intel until CI has run the tests there.
