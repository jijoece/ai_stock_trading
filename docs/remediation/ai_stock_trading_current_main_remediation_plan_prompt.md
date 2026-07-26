Work against the latest `main` branch of `jijoece/ai_stock_trading`.

Read `docs/remediation/current-main-audit-progress.md` and the previous session handoff before making changes. Revalidate each audit finding against the current code instead of assuming the original report is still accurate.

Keep this session limited to the assigned root-cause cluster. Add failing regression tests before or alongside the implementation. Preserve deterministic IDs, immutable audit history, point-in-time guarantees, and fail-closed behavior.

Do not:

* add live-trading capability;
* enable execution or promotion defaults;
* use real broker, provider, model, Reddit, SEC, or market-data credentials;
* make real external requests;
* weaken a safety invariant to make a test pass;
* silently repair conflicting persisted state.

Use fake runtimes, deterministic providers, fixtures, and temporary SQLite databases.

Before finishing:

1. Run the focused tests for the changed subsystem.
2. Run `pytest tests/ -q --tb=short`.
3. Run the `paper_runtime` test suite when relevant.
4. Run `pyright --project pyright-safety.json`.
5. Update `docs/remediation/current-main-audit-progress.md`.
6. Record the starting SHA, files changed, migrations, exact test results, remaining limitations, and recommended next session.
7. Explicitly confirm that no real provider or broker call occurred.

