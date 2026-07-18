#!/usr/bin/env bash
# Start the read-only Streamlit dashboard bound to 127.0.0.1 only.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

RUNTIME_DIR="$REPO_ROOT/data/.dashboard_runtime"
PID_FILE="$RUNTIME_DIR/dashboard.pid"
LOG_FILE="$RUNTIME_DIR/dashboard.log"
mkdir -p "$RUNTIME_DIR"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Dashboard already running (PID $(cat "$PID_FILE"))." >&2
  exit 1
fi

if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
  PYTHON="$REPO_ROOT/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

if [ -z "${AI_STOCK_TRADING_DB_PATH:-}" ]; then
  echo "AI_STOCK_TRADING_DB_PATH is not set. Set it to the research database path." >&2
  exit 1
fi

if [ ! -f "$AI_STOCK_TRADING_DB_PATH" ]; then
  echo "The configured dashboard database file does not exist." >&2
  exit 1
fi

if ! "$PYTHON" -c "import streamlit" >/dev/null 2>&1; then
  echo "Streamlit is not installed in the selected Python environment." >&2
  exit 1
fi

nohup "$PYTHON" -m streamlit run dashboard/streamlit_app.py \
  --server.address 127.0.0.1 \
  --server.port 8501 \
  >"$LOG_FILE" 2>&1 &

DASHBOARD_PID=$!
echo "$DASHBOARD_PID" >"$PID_FILE"
echo "Dashboard starting (PID $DASHBOARD_PID) on http://127.0.0.1:8501"
echo "Logs: $LOG_FILE"
