#!/usr/bin/env bash
# Report dashboard process, health, database, and Tailscale Serve status.
# Prints no credentials and no full database path.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="$REPO_ROOT/data/.dashboard_runtime"
PID_FILE="$RUNTIME_DIR/dashboard.pid"

echo "== Dashboard process =="
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "running (PID $(cat "$PID_FILE"))"
else
  echo "not running"
fi

echo
echo "== Localhost health =="
if curl --fail --silent --max-time 5 http://127.0.0.1:8501/_stcore/health >/dev/null 2>&1; then
  echo "ok"
else
  echo "unreachable"
fi

echo
echo "== Database =="
if [ -n "${AI_STOCK_TRADING_DB_PATH:-}" ] && [ -f "$AI_STOCK_TRADING_DB_PATH" ]; then
  echo "exists"
  echo "last modified: $(stat -f '%Sm' "$AI_STOCK_TRADING_DB_PATH" 2>/dev/null || stat -c '%y' "$AI_STOCK_TRADING_DB_PATH" 2>/dev/null)"
else
  echo "not found or AI_STOCK_TRADING_DB_PATH not set"
fi

echo
echo "== Tailscale connection =="
if command -v tailscale >/dev/null 2>&1; then
  tailscale status --self 2>&1 | head -1 || echo "unavailable"
else
  echo "tailscale command not found"
fi

echo
echo "== Tailscale Serve status =="
if command -v tailscale >/dev/null 2>&1; then
  tailscale serve status 2>&1 || echo "unavailable"
else
  echo "tailscale command not found"
fi
