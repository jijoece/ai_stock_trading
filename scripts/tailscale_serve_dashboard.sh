#!/usr/bin/env bash
# Expose the local read-only dashboard privately via Tailscale Serve.
# Never uses Tailscale Funnel; never binds outside 127.0.0.1.
set -euo pipefail

if ! command -v tailscale >/dev/null 2>&1; then
  echo "The 'tailscale' command was not found on PATH." >&2
  exit 1
fi

if ! tailscale status >/dev/null 2>&1; then
  echo "Tailscale is not connected. Run 'tailscale up' first." >&2
  exit 1
fi

if ! curl --fail --silent --max-time 5 http://127.0.0.1:8501/_stcore/health >/dev/null; then
  echo "The local Streamlit dashboard is not healthy at 127.0.0.1:8501." >&2
  echo "Start it first with scripts/run_dashboard.sh." >&2
  exit 1
fi

echo "Configuring Tailscale Serve for the local dashboard (127.0.0.1:8501)..."
tailscale serve --bg 127.0.0.1:8501

echo
echo "Tailscale Serve status:"
tailscale serve status
