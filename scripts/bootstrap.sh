#!/usr/bin/env bash
# DLY AEOS bootstrap for macOS/Linux (master §07 order 0-1): verifies prerequisites, creates the
# repository virtual environment, installs pinned dependencies, builds the Control Center and runs
# the Readiness Doctor. Never installs system software silently: missing tools are reported with
# the official install command for you to run.
#   ./scripts/bootstrap.sh [--skip-frontend] [--no-doctor]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
SKIP_FRONTEND=0
NO_DOCTOR=0
for a in "$@"; do
  case "$a" in
    --skip-frontend) SKIP_FRONTEND=1 ;;
    --no-doctor) NO_DOCTOR=1 ;;
  esac
done

OS="$(uname -s)"
hint() {
  if [ "$OS" = "Darwin" ]; then echo "brew install $1"; else echo "use your package manager to install $1"; fi
}
missing=0
need() {
  if command -v "$1" >/dev/null 2>&1; then
    echo "  OK       $1 -> $(command -v "$1")"
  else
    echo "  MISSING  $1"; echo "           install: $(hint "$2")"; missing=1
  fi
}

echo "DLY AEOS bootstrap ($OS)"
echo "Step 0: prerequisites"
need git git
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "  MISSING  python3 (>=3.11)"; echo "           install: $(hint python@3.13)"; missing=1
else
  if "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "  OK       $PY $("$PY" -c 'import sys;print("%d.%d" % sys.version_info[:2])')"
  else
    echo "  TOO OLD  $PY (need >=3.11): $(hint python@3.13)"; missing=1
  fi
fi
if [ "$SKIP_FRONTEND" = 0 ]; then
  need node node
  if command -v node >/dev/null 2>&1; then
    major="$(node --version | sed 's/^v//' | cut -d. -f1)"
    if [ "$major" -lt 20 ]; then echo "  TOO OLD  node (need >=20)"; missing=1; fi
  fi
fi
if [ "$missing" = 1 ]; then
  echo "Install the missing prerequisites above and re-run this script."
  exit 1
fi

echo "Step 1: Python virtual environment (.venv)"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip --quiet
if [ -f requirements.lock ]; then .venv/bin/python -m pip install --quiet -r requirements.lock; fi
.venv/bin/python -m pip install --quiet -e ".[dev]"

if [ "$SKIP_FRONTEND" = 0 ]; then
  echo "Step 2: Control Center frontend (npm ci + build)"
  (cd frontend && npm ci --no-audit --no-fund && npm run build)
fi

echo "Step 3: local database"
.venv/bin/aeos db upgrade
.venv/bin/aeos seed

if [ "$NO_DOCTOR" = 0 ]; then
  echo "Step 4: Readiness Doctor"
  .venv/bin/aeos doctor || true
fi

chmod +x ./aeos 2>/dev/null || true
echo
echo "Next steps:"
echo "  ./aeos admin create      # first SUPER_ADMIN (no default credentials)"
echo "  ./aeos serve             # http://127.0.0.1:8787"
