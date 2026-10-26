#!/usr/bin/env bash
# Run the backend suite the way CI does, with an environment check first (bash 3.2 compatible; macOS and Git Bash).
#
#   scripts/run_tests.sh [--deadline MINUTES] [--] [pytest arguments...]
#
# 1. Preflight, before pytest starts: `python -m pip check`, then `import pytest` and `import xdist`. A missing or
#    broken package stops the run and is named (exit 2); a background run no longer fails minutes in on a dependency.
# 2. pytest -q -m "not integration and not network", plus -n logical when pytest-xdist is importable, then your
#    arguments. Arguments are kept in bash arrays (never in an unquoted variable: zsh does not word-split those).
# 3. --deadline MINUTES (or AEOS_TEST_DEADLINE_MIN) stops the run after that long, using scripts/run_with_deadline.py
#    because macOS has no `timeout`. A stopped run exits 124.
# Last line of output, always: RUN_TESTS: PASS rc=<n> or RUN_TESTS: FAIL rc=<n> (grep for it; tools that hide lines
# of a long log still show it).
# Interpreter, first match wins: $AEOS_GATES_PYTHON; the .venv of this checkout; the .venv of the main checkout (a
# linked worktree has none of its own; never link one into a worktree); python3; python.
set -euo pipefail

# shellcheck disable=SC2317,SC2329  # called by the EXIT trap below (SC2317: shellcheck 0.9; SC2329: 0.10 and newer)
on_exit() {
  local code=$?
  if [ "$code" -eq 0 ]; then
    echo "RUN_TESTS: PASS rc=0"
  else
    echo "RUN_TESTS: FAIL rc=$code"
  fi
}
trap on_exit EXIT

die() {
  echo "run_tests: $*" >&2
  exit 2
}

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"

deadline="${AEOS_TEST_DEADLINE_MIN:-}"
extra=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --deadline)
      [ "$#" -ge 2 ] || die "--deadline needs a number of minutes"
      deadline=$2
      shift 2
      ;;
    --deadline=*)
      deadline=${1#--deadline=}
      shift
      ;;
    --)
      shift
      extra+=("$@")
      break
      ;;
    *)
      extra+=("$1")
      shift
      ;;
  esac
done

py=
if [ -n "${AEOS_GATES_PYTHON:-}" ]; then
  if ! { [ -f "$AEOS_GATES_PYTHON" ] && [ -x "$AEOS_GATES_PYTHON" ]; }; then
    die "AEOS_GATES_PYTHON is not an existing executable file: $AEOS_GATES_PYTHON"
  fi
  py=$AEOS_GATES_PYTHON
fi
if [ -z "$py" ]; then
  main=
  common=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || common=
  [ -n "$common" ] && [ "$(basename "$common")" = ".git" ] && main=$(dirname "$common")
  for base in "$root" "$main"; do
    [ -n "$base" ] || continue
    for cand in "$base/.venv/bin/python" "$base/.venv/Scripts/python.exe"; do
      if [ -x "$cand" ]; then py=$cand; break 2; fi
    done
  done
fi
if [ -z "$py" ]; then
  for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
      py=$cand
      echo "run_tests: warning: no project .venv found, using $cand from PATH" >&2
      break
    fi
  done
fi
[ -n "$py" ] || die "no Python found (no project .venv, no python3 or python on PATH); set AEOS_GATES_PYTHON"

if [ -n "$deadline" ]; then
  case "$deadline" in
    *[!0-9.]* | "" | . | *.*.*) die "the deadline must be a positive number of minutes, got: $deadline" ;;
  esac
fi

echo "run_tests: python $py"

# Preflight: stop BEFORE pytest, naming what is wrong.
if ! pip_out=$("$py" -m pip check 2>&1); then
  echo "run_tests: preflight failed: 'python -m pip check' reports a missing or conflicting package:" >&2
  echo "$pip_out" >&2
  die "fix the environment (for example: $py -m pip install -r requirements.lock) and run again; pytest was not started"
fi
for entry in pytest:pytest xdist:pytest-xdist; do
  module=${entry%%:*}
  package=${entry#*:}
  if ! "$py" -c "import $module" >/dev/null 2>&1; then
    die "preflight failed: the package '$package' (import $module) is missing for $py; install it ($py -m pip install $package); pytest was not started"
  fi
done

cmd=("$py" -m pytest -q -m "not integration and not network" -n logical)
if [ "${#extra[@]}" -gt 0 ]; then
  cmd+=("${extra[@]}")
fi
if [ -n "$deadline" ]; then
  cmd=("$py" "$root/scripts/run_with_deadline.py" "$deadline" -- "${cmd[@]}")
fi

echo "run_tests: ${cmd[*]}"
set +e
"${cmd[@]}"
rc=$?
set -e
exit "$rc"
