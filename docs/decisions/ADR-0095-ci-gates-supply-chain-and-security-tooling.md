# ADR-0095 — CI gates, explicit gate mapping, supply-chain reports and security tooling

* Status: PROPOSED
* Date: 2026-10-19
* Deciders: owner (Fadhlillah) - review pending; written by the implementation agent

## Context

Master §11 lists type checking and relevant UI/browser tests; §31 G1 asks for schema/manifest,
format/lint/type, secret scan and dependency/license/SBOM "as needed", G3 for browser/UI/a11y,
contract and security scenarios; §45 asks to choose the minimum security/test tools per stack and
never to invent scan results. Before this decision CI ran ruff, the secret scan, pytest, tsc, vitest
and the build; gate membership was guessed from job names.

## Decision

| Need | Tool / job | Gate | Status | Licence (source) |
|---|---|---|---|---|
| Lint | ruff (backend), tsc (frontend) | G1 | enforced | ruff 0.17.1: MIT (installed metadata `License-Expression`); typescript 7.0.2: Apache-2.0 (`frontend/package-lock.json` `license`) |
| Type check | mypy with a ratcheted baseline (`pyproject.toml`; the baseline may only shrink, `tests/unit/test_ci_gates.py`) | G1 | enforced for every non-baselined module | mypy 2.4.0: MIT (installed metadata `License-Expression`) |
| Secret scan | `scripts/secret_scan.py` (runtime redaction patterns) | G1 | enforced | NOT_APPLICABLE: own code (`pyproject.toml` license text "Proprietary") |
| PII in templates | `scripts/secret_scan.py` PII/tenant-host scan of `blueprints/`, `contracts/`, `docs/` | G1 | enforced | NOT_APPLICABLE: own code (`pyproject.toml` license text "Proprietary") |
| Controlled tests | pytest (unit, acceptance, integration-git) | G2 | enforced | pytest 9.1.1: MIT (installed metadata `License-Expression`) |
| Bootstrap | `bootstrap` job runs both scripts for real on Linux/Windows + a negative case | G2 | enforced | NOT_APPLICABLE: own scripts |
| Restore test | `tests/unit/test_operations_backup.py` (restore-test roundtrip with evidence only on success, corrupt/tampered archive detected, live restore takes a pre-restore backup, refused while the server runs or in a worker context) and `tests/acceptance/test_operations.py`, inside the backend job | G2 | enforced | pytest 9.1.1: MIT (installed metadata `License-Expression`); the restore code is own code |
| UI tests | vitest + build | G3 | enforced | vitest 5.0.5: MIT; vite 8.3.4: MIT (`frontend/package-lock.json` `license`) |
| Browser + basic a11y | `browser-e2e` job: Playwright smoke over every nav route (no page/console errors, 390 px overflow, dark-mode contrast, labels) | G3 | enforced | playwright 1.64.0: Apache-2.0 (installed metadata `License-Expression`); the downloaded Chromium build's own licence: UNKNOWN (not verified) |
| API contract | `tests/unit/test_api_envelope_contract.py` (route snapshot only grows; list, claim and project/component-scoped endpoints state source + freshness, no ratchet; every /api response carries version/schema/settings-hash headers) | G2/G3 | enforced | pytest 9.1.1: MIT (own tests) |
| Security scenarios | the trusted acceptance suite's security rows (`tests/acceptance/test_auth_boundary.py`, `test_untrusted_input.py`, `test_domain_scope.py`, `test_containment.py`) inside the backend job | G2 (counted as the G3 security scenarios) | enforced; container containment BLOCKED (no Docker, MATRIX 1.12/1.13) | pytest 9.1.1: MIT (own tests) |
| Performance baseline | `tests/unit/test_perf_baseline.py` in the `browser-e2e` job: p50/p95 of key read endpoints written to the step summary | G3 report | REPORT_ONLY: no latency objective decided by the owner (§33); only a pathological sanity ceiling fails | pytest 9.1.1: MIT (own test) |
| SBOM | `scripts/supply_chain.py sbom` (CycloneDX 1.5 from the lockfiles, offline, deterministic) | report | produced every run | NOT_APPLICABLE: own script |
| Licences | `scripts/supply_chain.py licenses --policy policy/supply-chain.yaml` (installed Python distributions + frontend lockfile `license` fields) | report | REPORT_ONLY - `policy/supply-chain.yaml` is UNDECIDED (no allow-list) | NOT_APPLICABLE: own script |
| Dependency vulnerabilities | pip-audit 2.7.3 (requirements.lock, `-f json`), `npm audit --omit=dev --json` | report | NOT-PROVEN until the owner sets thresholds | pip-audit 2.7.3: classifier "License :: OSI Approved :: Apache Software License", no SPDX/License field (PyPI JSON record pypi.org/pypi/pip-audit/2.7.3/json, read 2026-10-25); npm audit: UNKNOWN for the CI copy (the npm bundled with Node 24 on the runner, version not pinned; the local npm 11.12.1 `package.json` says Artistic-2.0) |
| SAST | bandit 1.7.10 (`-f json`, no severity flag) | report | report-only until findings are triaged and thresholds decided | bandit 1.7.10: License field "Apache-2.0 license", classifier "License :: OSI Approved :: Apache Software License" (PyPI JSON record pypi.org/pypi/bandit/1.7.10/json, read 2026-10-25) |
| Scan policy evaluation | `scripts/supply_chain.py evaluate --policy policy/supply-chain.yaml` over the licence/pip-audit/npm-audit/bandit reports | report | REPORT_ONLY while the policy is UNDECIDED; once the owner records values a violation, an UNKNOWN licence, a finding of unknown severity or a missing/unreadable scanner report fails the job | NOT_APPLICABLE: own script |
| IaC scan | - | - | NOT_APPLICABLE: the repository has no infrastructure-as-code | NOT_APPLICABLE: no tool selected |
| DAST | - | - | NOT_APPLICABLE: no deployed target exists; the control plane binds 127.0.0.1 only (§06) | NOT_APPLICABLE: no tool selected |
| Load tests | - | - | NOT_APPLICABLE: single-owner local control plane, no deployed service or SLO-bound traffic | NOT_APPLICABLE: no tool selected |

**Tool licences (§45 "versi, license dan gate diverifikasi").** The licence column is read from each tool's own
package metadata - `importlib.metadata` in the pinned environment, the `license` field `npm` records in
`frontend/package-lock.json`, or the PyPI JSON record of the pinned release for tools CI installs on the fly -
and written verbatim (a classifier is quoted as a classifier, never mapped to an SPDX id). Where no metadata
could be read it says UNKNOWN. `tests/unit/test_ci_gates.py` re-reads the installed metadata and fails when
the table disagrees with it.

**Scan policy (§31 G1, §48 CI controls).** Thresholds and the licence allow-list are owner decisions and live in
`policy/supply-chain.yaml` (protected path), currently `status: UNDECIDED` with every value null. The CI
report steps run under `shell: bash` (`-eo pipefail`), so a crashed tool is a failed step, never a masked
success; the evaluation treats a missing or unreadable report as such. Severity filtering is done only by the
evaluator from the owner's values - no `--audit-level`/`-ll` threshold is chosen in the workflow.

**Explicit gate mapping.** Every CI job name carries its gates in brackets (`[G1,G2]`, `[G3]`) or
`[NO-GATE]` for report-only jobs. `aeos.delivery.provider.classify_check` uses the tag when present
and falls back to the name heuristics only for untagged provider checks.

**Hash pinning** of `requirements.lock` (`uv pip compile --generate-hashes`, `pip install
--require-hashes`) is deferred: it needs a networked regeneration by the owner and changes how the
bootstrap installs; until then Python dependencies are pinned by exact version (ADR-0093).

## Consequences

* Report-only jobs never make a gate PASS; their output is in the job log and step summary.
* Moving a report to enforced needs an owner decision on thresholds/allow-lists, recorded in
  `policy/supply-chain.yaml` (status DECIDED + decision_ref) in the reviewed PR that accepts this ADR - and
  then the `[NO-GATE]` tag changes to the gate it belongs to. No code change is needed for that step.
* IaC/DAST/load become applicable when a deployed target or infrastructure code exists (§31 G7/G8).
* External enforcement (§48) stays outside what CI can prove: strong worker isolation needs the container sandbox (NOT-PROVEN on a host without Docker; MATRIX 1.12/1.13 BLOCKED), and provider-side branch protection / required checks are NOT_ENFORCEABLE on GitHub Free (ADR-0063). The SBOM and dependency scans above are evidence for the owner, not enforcement.
