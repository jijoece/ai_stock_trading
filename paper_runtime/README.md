# trading-paper-runtime (Milestone 4 isolated paper-broker process)

A standalone package, deliberately **not** installed into the main
trading-desk environment. It owns the LumiBot dependency tree and the only
credentialed connection to a paper broker (Alpaca paper by default). The
main process (`trading_research`) never imports anything from this package
and never imports `lumibot` — it talks to this process over a small,
versioned JSON Lines protocol on stdin/stdout (`paper-runtime.v1`).

See `docs/milestone4-isolated-paper-broker.md` and
`docs/adr/0002-isolated-lumibot-runtime.md` in the main repository for the
full design.

## Install (isolated environment)

```bash
python3 -m venv .venv-paper-runtime
.venv-paper-runtime/bin/pip install -e ".[dev]"
```

## Run

```bash
.venv-paper-runtime/bin/python -m trading_paper_runtime
```

Reads one JSON request object per line from stdin, writes one JSON response
object per line to stdout. All logging goes to stderr. Never writes secrets
to stdout, stderr, or logs.

## Credentials

Read only from environment variables (or a `.env` file LumiBot itself may
load) — see the main repo's `.env.example`:

* `ALPACA_API_KEY`
* `ALPACA_API_SECRET`
* `ALPACA_IS_PAPER` — must be exactly `true` (case-insensitive); any other
  value, or its absence, fails closed and blocks `submit_order` /
  `cancel_paper_order`. `health` and `capabilities` still respond (with
  boolean credential-presence flags only) even when credentials are
  missing or invalid, so the main process can detect the condition.

## Offline testing

`tests/` runs fully offline against `DeterministicBrokerGateway` — no
network, no real credentials, no LumiBot broker connection. The
LumiBot/Alpaca translation tests are guarded with
`pytest.importorskip("lumibot")` / real-credential opt-in, mirroring the
main repo's testing posture.
