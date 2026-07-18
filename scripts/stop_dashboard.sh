#!/usr/bin/env bash
# Stop only the dashboard process identified by the dashboard PID file.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="$REPO_ROOT/data/.dashboard_runtime"
PID_FILE="$RUNTIME_DIR/dashboard.pid"

if [ ! -f "$PID_FILE" ]; then
  echo "No dashboard PID file found; dashboard is not running." >&2
  exit 0
fi

DASHBOARD_PID="$(cat "$PID_FILE")"

if ! kill -0 "$DASHBOARD_PID" 2>/dev/null; then
  echo "Dashboard process $DASHBOARD_PID is not running; removing stale PID file." >&2
  rm -f "$PID_FILE"
  exit 0
fi

kill "$DASHBOARD_PID"
rm -f "$PID_FILE"
echo "Stopped dashboard process $DASHBOARD_PID."
echo "Note: this does not disable any Tailscale Serve route. Run"
echo "  tailscale serve --https=443 off"
echo "to remove the dashboard's Serve route if it was configured."
