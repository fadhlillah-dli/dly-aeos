# ADR-0094 — CI gates, explicit gate mapping, supply-chain reports and security tooling

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

| Need | Tool / job | Gate | Status |
|---|---|---|---|
| Lint | ruff (backend), tsc (frontend) | G1 | enforced |
| Type check | mypy with a ratcheted baseline (`pyproject.toml`; the baseline may only shrink, `tests/unit/test_ci_gates.py`) | G1 | enforced for every non-baselined module |
| Secret scan | `scripts/secret_scan.py` (runtime redaction patterns) | G1 | enforced |
| PII in templates | `scripts/secret_scan.py` PII/tenant-host scan of `blueprints/`, `contracts/`, `docs/` | G1 | enforced |
| Controlled tests | pytest (unit, acceptance, integration-git) | G2 | enforced |
| Bootstrap | `bootstrap` job runs both scripts for real on Linux/Windows + a negative case | G2 | enforced |
| UI tests | vitest + build | G3 | enforced |
| Browser + basic a11y | `browser-e2e` job: Playwright smoke over every nav route (no page/console errors, 390 px overflow, dark-mode contrast, labels) | G3 | enforced |
| API contract | `tests/unit/test_api_envelope_contract.py` (route snapshot only grows; list endpoints state source + freshness) | G2/G3 | enforced |
| SBOM | `scripts/supply_chain.py sbom` (CycloneDX 1.5 from the lockfiles, offline, deterministic) | report | produced every run |
| Licences | `scripts/supply_chain.py licenses` | report | REPORT_ONLY - no allow-list decided |
| Dependency vulnerabilities | pip-audit 2.7.3 (requirements.lock), `npm audit --omit=dev` | report | NOT-PROVEN until the owner sets thresholds |
| SAST | bandit 1.7.10 | report | report-only until findings are triaged |
| IaC scan | - | - | NOT_APPLICABLE: the repository has no infrastructure-as-code |
| DAST | - | - | NOT_APPLICABLE: no deployed target exists; the control plane binds 127.0.0.1 only (§06) |
| Load tests | - | - | NOT_APPLICABLE: single-owner local control plane, no deployed service or SLO-bound traffic |

**Explicit gate mapping.** Every CI job name carries its gates in brackets (`[G1,G2]`, `[G3]`) or
`[NO-GATE]` for report-only jobs. `aeos.delivery.provider.classify_check` uses the tag when present
and falls back to the name heuristics only for untagged provider checks.

**Hash pinning** of `requirements.lock` (`uv pip compile --generate-hashes`, `pip install
--require-hashes`) is deferred: it needs a networked regeneration by the owner and changes how the
bootstrap installs; until then Python dependencies are pinned by exact version (ADR-0093).

## Consequences

* Report-only jobs never make a gate PASS; their output is in the job log and step summary.
* Moving a report to enforced needs an owner decision on thresholds/allow-lists (and then the
  `[NO-GATE]` tag changes to the gate it belongs to).
* IaC/DAST/load become applicable when a deployed target or infrastructure code exists (§31 G7/G8).
