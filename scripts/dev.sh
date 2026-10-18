#!/usr/bin/env bash
# DLY AEOS development mode on macOS/Linux: starts the control plane with `aeos serve --dev` on
# 127.0.0.1:8787 (allows the Vite origin, enables API docs) and the Vite dev server on
# 127.0.0.1:5173 (proxies /api to 8787). Ctrl+C stops both. Loopback only (master section 06).
# bash 3.2 compatible.   ./scripts/dev.sh [PORT] [VITE_PORT]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PORT="${1:-8787}"
VITE_PORT="${2:-5173}"
PY=".venv/bin/python"
AEOS=".venv/bin/aeos"

if [ ! -x "$AEOS" ]; then
  echo "Virtual environment not found. Run: ./scripts/bootstrap.sh" >&2
  exit 1
fi
if [ ! -d frontend/node_modules ]; then
  echo "Frontend dependencies missing. Run: (cd frontend && npm ci)" >&2
  exit 1
fi

port_busy() {
  "$PY" -c 'import socket,sys; s=socket.socket(); s.settimeout(0.3); sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)' "$1"
}

for p in "$PORT" "$VITE_PORT"; do
  if port_busy "$p"; then
    echo "Port $p on 127.0.0.1 is already in use. Stop the other process (lsof -nP -iTCP:$p -sTCP:LISTEN) or choose another port." >&2
    exit 2
  fi
done

if [ "$PORT" != "8787" ]; then
  echo "Note: the Vite proxy in frontend/vite.config.ts targets 127.0.0.1:8787; with port $PORT use the backend URL directly or adjust the proxy."
fi
export AEOS_DEV_FRONTEND_PORT="$VITE_PORT"
echo "Starting control plane (dev) on http://127.0.0.1:$PORT ..."
"$AEOS" serve --dev --port "$PORT" &
BACKEND_PID=$!

cleanup() {
  if kill -0 "$BACKEND_PID" 2>/dev/null; then
    echo "Stopping control plane (pid $BACKEND_PID) ..."
    kill "$BACKEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

tries=0
until port_busy "$PORT"; do
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    echo "backend exited early" >&2
    exit 1
  fi
  tries=$((tries + 1))
  if [ "$tries" -gt 100 ]; then
    echo "backend did not open port $PORT within 30s" >&2
    exit 1
  fi
  sleep 0.3
done

echo "Starting Vite on http://127.0.0.1:$VITE_PORT (Ctrl+C stops both) ..."
(cd frontend && npm run dev -- --port "$VITE_PORT" --strictPort)
