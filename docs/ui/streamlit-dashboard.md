# Streamlit Dashboard

A private, read-only Streamlit dashboard for the research and paper-trading
system. It shows evaluated candidates, decision explanations, portfolio and
paper-book status, research-cycle history, provider health, and
scheduler/pause status. It contains no trading, approval, pause/resume, or
scheduler controls.

## Architecture

```text
Authorized browser/device
        |
        v
Tailscale Serve HTTPS
        |
        v
127.0.0.1:8501
        |
        v
Streamlit dashboard (dashboard/streamlit_app.py)
        |
        v
Read-only query services (dashboard/services/*.py)
        |
        v
Existing SQLite research database (read-only, mode=ro, query_only)
```

Pages: Overview, Decisions, Research Cycles, Portfolio, System Health.
Services use short-lived, read-only SQLite connections with parameterized,
bounded queries; no query ever commits, migrates, or creates tables.

## Dependency installation

Streamlit and Pandas are declared in `pyproject.toml`. Install with the
project's existing dependency workflow (e.g. `pip install -e .` inside
`.venv`).

## Database environment variable

Set `AI_STOCK_TRADING_DB_PATH` to the existing research SQLite database file
(e.g. the file at `RESEARCH_DATABASE_PATH`/`research.sqlite3` used by the
main system). The dashboard refuses to start without it and never falls back
to a default path.

```bash
export AI_STOCK_TRADING_DB_PATH=/path/to/research.sqlite3
```

## Local startup

```bash
scripts/run_dashboard.sh
```

This verifies the database variable and file, verifies Streamlit is
installed, starts Streamlit bound to `127.0.0.1:8501` only, and writes a PID
file under `data/.dashboard_runtime/`.

Open `http://127.0.0.1:8501` locally to verify.

## Tailscale prerequisites

* Tailscale installed and signed in on this MacBook (`tailscale up`).
* This device authorized on your tailnet.
* Only devices on the same tailnet (or explicitly shared) can reach the
  Serve URL.

## Tailscale Serve setup

With the dashboard already running locally:

```bash
scripts/tailscale_serve_dashboard.sh
```

This checks that `tailscale` is installed, that Tailscale is connected, and
that `127.0.0.1:8501/_stcore/health` is healthy, then runs the equivalent of:

```bash
tailscale serve --bg 127.0.0.1:8501
```

Tailscale Funnel is never used. The dashboard is never exposed to the public
internet — only to your tailnet.

## Remote-access steps

1. Ensure the MacBook is powered on, awake, and connected to the internet.
2. Ensure `scripts/run_dashboard.sh` and `scripts/tailscale_serve_dashboard.sh`
   have been run.
3. From another device on the same tailnet, open the HTTPS Serve URL shown
   by `tailscale serve status` (typically `https://<device-name>.<tailnet>.ts.net`).
4. Access is gated by tailnet device authorization — no separate dashboard
   password is configured in this milestone.

## Status checks

```bash
scripts/dashboard_status.sh
```

Reports dashboard process status, localhost health, database existence and
last-modified time, Tailscale connection status, and Tailscale Serve status.
It never prints credentials or the full database path.

## Shutdown

```bash
scripts/stop_dashboard.sh
```

Stops only the process recorded in the dashboard PID file. It never uses
`pkill`/`killall`.

## Serve disablement

To remove only the dashboard's Serve route without affecting other Serve
configuration, use the narrow disable command reported by
`tailscale serve status` for this route (for current Tailscale clients,
typically `tailscale serve --https=443 off` when port 443 is the dashboard's
only Serve route). Do not run a full `tailscale serve reset` unless you
intend to remove every Serve route on this device.

## Security boundaries

* Streamlit listens on `127.0.0.1` only; it is never bound to `0.0.0.0`.
* Remote access exists only through Tailscale Serve; Funnel is never enabled.
* All SQLite connections are opened read-only (`mode=ro`, `PRAGMA
  query_only = ON`); no commit, schema change, or migration is possible from
  the dashboard.
* Queries are parameterized and bounded (capped result sizes/pagination).
* No SQL console, database download, or raw filesystem path is exposed.
* No credentials, tokens, account numbers, raw prompts, raw model responses,
  raw subprocess output, or unrestricted exceptions are displayed; errors are
  sanitized.
* No trading, order-placement, approval, pause/resume, or scheduler control
  exists anywhere in the dashboard.
* No real provider, broker, or external network call is made by the
  dashboard or its tests.

## Known limitations

* Decision, cycle, and portfolio views are bounded (typically 200 rows);
  very large result sets are truncated rather than loaded unboundedly.
* Portfolio valuations use only the latest persisted snapshot/position price;
  no live price lookup occurs.
* Some outcomes (duplicate-prevention success, ambiguous "still pending"
  price/order states) have no stable persisted code and map to `UNKNOWN`.
* Remote access requires the MacBook to be powered on, awake, running the
  dashboard process, and connected to Tailscale and the internet.
* No dashboard password is configured; access relies entirely on Tailscale
  tailnet authorization.

## Troubleshooting

| Symptom | Check |
|---|---|
| `run_dashboard.sh` exits immediately | Confirm `AI_STOCK_TRADING_DB_PATH` is set and the file exists. |
| Dashboard shows "Not available" everywhere | Confirm the configured database path points at the live research database, not an empty fixture. |
| `tailscale_serve_dashboard.sh` fails the health check | Start the dashboard first with `run_dashboard.sh`; confirm nothing else is bound to port 8501. |
| Remote URL unreachable | Confirm the MacBook is awake, `tailscale status` shows connected, and the remote device is authorized on the same tailnet. |
| `stop_dashboard.sh` reports no PID file | The dashboard was not started via `run_dashboard.sh`, or was already stopped. |
