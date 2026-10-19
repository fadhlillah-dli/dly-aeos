"""Bootstrap scripts (master §06/§07): a failed required step stops the bootstrap with a non-zero exit.

Windows PowerShell 5.1 does not stop on a native non-zero exit even with ``$ErrorActionPreference =
'Stop'``, so every required native call in bootstrap.ps1 must check ``$LASTEXITCODE``; bootstrap.sh
relies on ``set -euo pipefail``. The CI ``bootstrap`` job runs both scripts for real (and a negative
case) - see .github/workflows/ci.yml."""

from __future__ import annotations

import re

from aeos.core.paths import repo_root

REQUIRED_STOP = ("db upgrade", "seed")


def _lines(name: str) -> list[str]:
    return (repo_root() / "scripts" / name).read_text(encoding="utf-8").splitlines()


def test_powershell_bootstrap_checks_every_native_exit_code():
    lines = _lines("bootstrap.ps1")
    native = [i for i, ln in enumerate(lines) if re.search(r"^\s*&\s+(\.\\\.venv\\Scripts\\(aeos|python)\.exe|npm\.cmd)", ln)]
    assert native, "no native calls found"
    unchecked = []
    for i in native:
        cmd = lines[i].strip()
        if "--upgrade pip" in cmd or "component_status.py" in cmd:
            continue  # best effort / status only
        follow = next((ln for ln in lines[i + 1:] if ln.strip()), "")
        if "$LASTEXITCODE" not in follow:
            unchecked.append(cmd)
    assert not unchecked, unchecked
    for step in REQUIRED_STOP:
        idx = next(i for i, ln in enumerate(lines) if f"aeos.exe {step}" in ln)
        assert "exit 1" in lines[idx + 1], f"'{step}' failure must stop the bootstrap"


def test_shell_bootstrap_stops_on_error():
    lines = _lines("bootstrap.sh")
    assert "set -euo pipefail" in lines
    for step in REQUIRED_STOP:
        line = next(ln for ln in lines if f"aeos {step}" in ln)
        assert "|| true" not in line, f"'{step}' failure must stop the bootstrap"


def test_ci_runs_both_bootstrap_scripts_including_a_failure_case():
    ci = (repo_root() / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "scripts/bootstrap.sh --no-doctor" in ci and ".\\scripts\\bootstrap.ps1 -NoDoctor" in ci
    assert "bootstrap must fail" in ci  # negative case: an invalid data dir makes db upgrade fail
