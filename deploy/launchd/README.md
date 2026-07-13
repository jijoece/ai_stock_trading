# Shadow-operations launchd artifact (Milestone 7, Step 20)

This directory contains a **DEPLOYABLE SCHEDULER ARTIFACT**, not an
**ACTUAL RECURRING DEPLOYMENT**. Nothing in this directory is loaded,
activated, or run automatically by cloning or reading this repository. Both
files use an `.example` suffix specifically so `launchctl` never picks them
up by accident, and no `launchctl load` command has been run against them by
this task.

Files:

- `com.agentic-trading-desk.shadow.plist.example` — the launchd job
  definition template. `StartCalendarInterval` is commented out and
  `RunAtLoad` is `false`, so even a copy of this file with the suffix
  dropped is inert until an operator explicitly adds a real trigger.
- `run_shadow_cycle.sh.example` — the wrapper script the plist invokes. It
  `cd`s into the repository, activates the repository's own `.venv`, and
  runs `python -m trading_research.cli run-due-shadow-cycle`, redirecting
  output to a timestamped log file under an operator-configured `LOG_DIR`.

## What `run-due-shadow-cycle` is, as of this task

`shadow/scheduler.py::run_due_shadow_cycle` (the underlying Python callable)
is implemented and tested. The `run-due-shadow-cycle` CLI subcommand named
in this artifact **does not exist yet** in `cli.py` — a later task adds the
thin argparse wrapper. This artifact documents and validates the intended
invocation ahead of that CLI wiring, per this task's explicit instructions.
Until that CLI command exists, running the wrapper script will fail with an
argparse "unknown command" error — this is expected and does not indicate a
problem with the artifact itself.

## Exact activation procedure (NOT performed by this task)

1. Copy both files, dropping the `.example` suffix:
   ```bash
   cp deploy/launchd/com.agentic-trading-desk.shadow.plist.example \
      ~/Library/LaunchAgents/com.agentic-trading-desk.shadow.plist
   cp deploy/launchd/run_shadow_cycle.sh.example \
      deploy/launchd/run_shadow_cycle.sh
   chmod +x deploy/launchd/run_shadow_cycle.sh
   ```
2. Edit `run_shadow_cycle.sh`: set `REPO_DIR` to this repository's absolute
   path and `LOG_DIR` to where per-invocation logs should be written.
3. Edit the copied plist: set `ProgramArguments`' second element to the
   absolute path of your edited `run_shadow_cycle.sh`, set
   `StandardOutPath`/`StandardErrorPath` to real log paths, and uncomment +
   fill in `StartCalendarInterval` with the hour/minute you want launchd to
   fire at (this does not need to exactly match
   `config/shadow_operations.yaml`'s `schedule.intended_local_time` — the
   scheduler re-evaluates due-ness fresh on every invocation and tolerates
   launchd firing a few minutes early/late, per `shadow/schedule.py`'s
   run-window logic).
4. Load it:
   ```bash
   launchctl load ~/Library/LaunchAgents/com.agentic-trading-desk.shadow.plist
   ```
   This is the step that constitutes ACTUAL RECURRING DEPLOYMENT ACTIVATION.
   It was **not** run by this task.

## How to validate without activating

- **Syntax check** (safe, no side effects): `plutil -lint
  path/to/the.plist`. This task ran `plutil -lint` against the `.example`
  template directly and confirmed `OK`.
- **Confirm nothing is loaded**: `launchctl list | grep
  com.agentic-trading-desk` should print nothing until step 4 above is
  actually performed.
- **Run the wrapper script manually, once, in the foreground**, without
  going through launchd at all:
  ```bash
  bash deploy/launchd/run_shadow_cycle.sh   # after copying + editing, per above
  ```
  This exercises the exact same `cd` / venv-activate / CLI-invoke sequence
  launchd would use, but on demand and with output visible in your
  terminal (in addition to the log file) — the fastest way to confirm paths
  and environment loading are correct before trusting launchd with it.

## Failure behavior

This artifact deliberately does **not** rely on launchd's own
`KeepAlive`/auto-restart semantics — that key is omitted from the plist.
The reasoning: a failed or skipped invocation is instead recovered by this
repository's own catch-up logic (`shadow/schedule.py`'s
`MISSED_WITHIN_CATCHUP`/`MISSED_TOO_OLD` statuses, bounded by
`config/shadow_operations.yaml`'s `max_catch_up_cycles`) the next time the
job fires on its normal schedule, rather than by launchd immediately
relaunching a possibly-still-broken process. If you want launchd to retry
sooner than the next scheduled firing, that is an explicit operator choice
outside what this artifact configures by default.

## Working-directory assumption

`run_shadow_cycle.sh` explicitly `cd`s into `REPO_DIR` before doing
anything else. This matters because
`trading_research/config.py::load_config()` resolves `.env` as
`REPO_ROOT / ".env"` where `REPO_ROOT` is derived from the installed
package's own file location, not the process's current working directory —
so the `cd` is not strictly required for `.env` loading, but the venv
activation step (`source "$REPO_DIR/.venv/bin/activate"`) does require
`REPO_DIR` to be correct, and any code path in this repository that assumes
relative paths from the repo root depends on it too.

## Log location

Two independent logs, deliberately kept separate:

- launchd's own capture of the wrapper script's stdout/stderr, at whatever
  path the plist's `StandardOutPath`/`StandardErrorPath` name (only
  captures launchd-level failures, e.g. the script failing to even start).
- The wrapper script's own per-invocation, timestamped log file under
  `LOG_DIR` (captures the actual CLI command's output for that invocation).

Neither log path is set by default — both are placeholder paths in the
`.example` templates that an operator must fill in, and neither template
embeds any credential or secret value.

## How to deactivate

```bash
launchctl unload ~/Library/LaunchAgents/com.agentic-trading-desk.shadow.plist
```

Unloading stops future scheduled firings; it does not affect a currently
in-flight invocation (which will run to completion, respecting its own
lease TTL as usual).
