# ADR-0002 — Stack and cross-platform local runtime (Windows + macOS)

* Status: ACCEPTED
* Date: 2026-10-18
* Deciders: owner (Fadhlillah); requirement "harus berjalan di Windows dan Mac"

## Context

Master §11 recommends Python/FastAPI/Pydantic/SQLAlchemy/Alembic/httpx, React/TypeScript/Vite/
Tailwind/TanStack Query/React Router and SQLite for V1. The repo had no prior stack to preserve.
The owner requires the platform to run on Windows and macOS.

## Decision

* Backend: Python ≥3.11 (developed on 3.13), FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, httpx,
  argon2-cffi, keyring (Windows Credential Manager / macOS Keychain), platformdirs, psutil, typer.
* Frontend: React 19, TypeScript, Vite, Tailwind v4, TanStack Query, React Router, @xyflow/react
  (System Map / DAG), mermaid (diagram rendering). Served by the backend from one origin
  `http://127.0.0.1:8787`; dev mode uses Vite on 5173 with an explicit origin allow-list.
* Data dir per OS via platformdirs (never the repo): `%LOCALAPPDATA%\DLY-AEOS`,
  `~/Library/Application Support/DLY-AEOS`, `$XDG_DATA_HOME/DLY-AEOS`.
* Entry points: `scripts/bootstrap.ps1` (Windows) and `scripts/bootstrap.sh` (macOS/Linux);
  launchers `aeos.ps1` / `aeos`; VS Code tasks per OS.
* CI matrix: ubuntu, windows, macos (backend) + frontend build; actions pinned to commit SHAs.
* No shell=True, pathlib everywhere, psutil for process trees, ASCII CLI output.

## Consequences

* macOS behaviour is verified by CI (E2) — not by an interactive run on a Mac (no Mac available in
  this session). First real macOS run is an owner step listed in the runbook.
* Strong worker isolation differs per OS (Docker Desktop / VM); see the sandbox ADR.
