# ADR-0093 — Universal Python dependency lockfile (requirements.lock)

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); implemented by the Operations & Reliability module

## Context

Master §07 step 1 asks for "versi Python/Node/package manager sesuai lockfile/compatibility yang
dipin", and DLY AEOS must install identically on Windows and macOS (and Linux in CI). The frontend
already pins through `package-lock.json`; the backend only had lower bounds in `pyproject.toml`.

## Decision

1. `requirements.lock` is generated with
   `uv pip compile pyproject.toml --universal --extra dev -o requirements.lock`: one file with exact
   versions and environment markers valid for every supported OS and Python ≥ 3.11.
2. Bootstrap installs `pip install -r requirements.lock`, then the project with `--no-deps`, then
   runs `pip check` (warning on drift). Without a lock it falls back to `pip install -e ".[dev]"`.
3. `uv` is a developer tool installed only into the venv when regenerating; it is not a runtime
   dependency.
4. Hash pinning (`--generate-hashes`) is deferred: it forces hash-checking mode for every install
   path and must be validated on all three OSes first.

## Consequences

* Any change to `[project].dependencies` or the `dev` extra must regenerate and commit the lock in
  the same change; modules merged separately require a regeneration by the integrator.
* CI can switch to installing from the lock for reproducible builds (integrator change to the
  protected workflow).
