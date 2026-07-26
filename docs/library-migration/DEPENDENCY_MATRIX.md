# Dependency Matrix

Compatibility research performed 2026-07-26 via live PyPI/GitHub lookups
(current training data is stale for this purpose — every figure below was
verified against the package index, not recalled). See `DECISIONS.md` D4 for
the decisions this data produced.

## 1. Compatibility matrix

| Library | Version | Python | License | Maintenance | macOS | Offline tests | Dependency weight | Conflicts | Decision |
|---|---|---|---|---|---|---|---|---|---|
| exchange_calendars | 4.13.2 (2026-03) | `>=3.10,<4` | Apache-2.0 | Active | No native deps | Yes | Light (~5 deps) | None | **Adopt** |
| VectorBT (OSS `vectorbt`) | 1.1.0 (2026-07) | `>=3.11,<3.15` | Apache-2.0 + Commons Clause | Active, secondary to commercial vectorbt.pro | No native deps | Yes | Heavy (numpy/pandas/numba) | `pandas>=3.0.3,<4.0`, `numpy>=2.4.6` conflicts with LumiBot's `numpy<2.5.0` | **Adopt, isolated group** |
| TA-Lib | 0.7.1 (2026-07) | `>=3.9` | BSD-2-Clause | Active | Requires `brew install ta-lib` (native C library) | Yes, once C lib installed | Light wrapper, native footprint | numpy>=2 compatible | **Adopt** |
| pandas-ta (original) | 0.4.71b0 (2025-09, never left beta) | — | MIT | Facing archival past its own 2026-07-01 deadline | — | — | — | — | **Reject** |
| pandas-ta-classic (fork) | 0.6.52 (2026-06) | `>=3.10` | MIT | Active fork | No native deps | Yes | Moderate | numpy>=2.0, pandas>=2.0 | **Evaluate as fallback only** |
| QuantStats (original) | 0.0.81 (2026-01, revived after long gap) | — | Apache-2.0 | Historically stagnant | — | — | — | — | **Reject as primary** |
| quantstats-lumi (fork) | 1.1.5 (2026-06) | — | Apache-2.0 | Active (Lumiwealth, same maintainer as LumiBot) | No native deps | Yes (avoid its yfinance download helpers) | Moderate | Loose pins, no conflicts | **Adopt** |
| Empyrical (original) | archived | — | Apache | Abandoned (Quantopian) | — | — | — | — | **Reject** |
| empyrical-reloaded (fork) | 0.5.12 (2025-06) | `>=3.9` (3.10-3.13 tested) | Apache-2.0 | Slowed, standard replacement | No native deps | Yes | Light (numpy/scipy) | numpy>=2 requires pandas>=2.2.2 | **Adopt** |
| Pydantic v2 | 2.13.4 (2026-05) | 3.9-3.14 | MIT | Very active | Prebuilt wheels (Rust core) | Yes | Light | None | **Evaluate** (boundary-only, PR 2) |
| Pandera | 0.32.1 (2026-06) | 3.10-3.14 | MIT | Active | No issues | Yes | Light with `[pandas]` extra only | None known | **Defer** |
| PyArrow | 25.0.0 (2026-07) | 3.10-3.14 | Apache-2.0 | Very active | Prebuilt wheels both arches | Yes | Heavy (28-53MB wheel) | Watch numpy pin vs. numba (VectorBT dep) | **Defer** |
| LumiBot | 4.5.78 current (repo pins 4.5.74) | 3.10-3.12 declared | MIT | Active | No native deps | Yes | Heavy, already isolated | `numpy<2.5.0,>=1.20.0`, `pandas>=2.2.0` conflicts with VectorBT | **No change** — bump patch pin |
| Riskfolio-Lib | 7.3.0 | `>=3.10` | BSD-3-Clause | Active | No native issues, heavy deps | Yes | Very heavy (cvxpy, matplotlib, sklearn, statsmodels, astropy) + hard-depends on `vectorbt>=0.28.0` | Transitively pulls VectorBT's pandas/numpy chain | **Evaluate** (PR 12, advisory only) |
| SQLAlchemy | 2.0.51 (2026-06) | `>=3.7` | MIT | Very active | No issues | Yes | Light core (`typing-extensions`, `greenlet`) | None with sqlite3 | **Evaluate** (PR 13) |
| Alembic | 1.18.5 | `>=3.10` | MIT | Active (SQLAlchemy team) | No issues | Yes | Light | None | **Evaluate** (PR 13/14) |
| APScheduler | 3.11.3 stable (v4 alpha, not production-ready) | 3.8-3.14 | MIT | Active | No issues | Yes | Light (SQLite jobstore reuses SQLAlchemy) | SQLite jobstore documented unsuitable for multiple concurrent schedulers; no distributed lease/fencing | **Evaluate** (PR 14) |
| Tenacity | 9.1.4 (2026-02) | `>=3.10` | Apache-2.0 | Active | No issues | Yes | Negligible | None | **Evaluate** (PR 14) |
| Structlog | 26.1.0 (2026-06) | `>=3.10` | MIT/Apache-2.0 | Active | No issues | Yes | Minimal | None | **Adopt** (PR 15) |
| OpenTelemetry SDK/API | 1.44.0 | `>=3.10` | Apache-2.0 | Active | No issues | Yes | Light core, exporters opt-in | None | **Adopt** (PR 16, additive) |
| Hypothesis | 6.161.5 (2026-07) | 3.10-3.14 | MPL-2.0 | Very active | No issues | Yes | Light | None | **Adopt** (dev group) |
| time-machine | 3.2.0 (2025-12) | `>=3.10` | MIT | Active | Prebuilt wheels | Yes | Light | None | **Adopt** (dev group, preferred over freezegun) |
| freezegun | 1.5.5 (2025-08) | 3.8-3.13 (no 3.14) | Apache-2.0 | Active, weaker for pandas C-extension time | No issues | Yes | Light | None | **Reject as primary**, fallback note only |

## 2. Recommended Python version

**Python 3.11**, raised from the repository's current `>=3.10` floor.

Binding constraint: VectorBT 1.1.0 requires `>=3.11`. Nothing else in the
Adopt/Evaluate set requires more than 3.10. Cap the main-package CI matrix at
**3.11-3.12** for now — LumiBot only declares 3.10-3.12 support, and none of
the newly evaluated libraries require 3.13+. Revisit once LumiBot explicitly
declares 3.13 support.

`paper_runtime` is a **separately installable distribution** (existing
pattern, not new) and can independently target whatever Python/numpy/pandas
range LumiBot supports without sharing a resolved environment with VectorBT
or Riskfolio-Lib. No new isolation mechanism is required.

## 3. Dependency groups

```text
core:
  anthropic, mcp, jsonschema, PyYAML, python-dotenv, httpx, pandas,
  streamlit, vaderSentiment   (existing)
  + exchange_calendars        (PR 3)

research:
  vectorbt                    (PR 5) — isolated: pandas>=3.0/numpy>=2.4.6
                               never shares a resolved env with `paper`

indicators:
  TA-Lib                      (PR 4, default)
  pandas-ta-classic           (PR 4, optional fallback only)

backtest:
  reserved — do not populate until the LumiBot-backtest-mode import-boundary
  question in DECISIONS.md D4 is resolved

paper:
  lumibot==4.5.78              (existing, isolated — bump patch pin)

analytics:
  quantstats-lumi              (PR 11)
  empyrical-reloaded            (PR 11)
  riskfolio-lib                 (PR 12, evaluation only — pulls vectorbt
                                 transitively, keep isolated from core/paper)

observability:
  structlog                    (PR 15)
  opentelemetry-sdk, opentelemetry-api   (PR 16)

dev:
  pytest, pytest-asyncio        (existing)
  hypothesis                    (adopt now)
  time-machine                  (adopt now)
```

`pydantic`, `tenacity`, `sqlalchemy`, `alembic`, `apscheduler` are
intentionally not yet placed in any group — each remains Category B,
evaluated in its own PR before any dependency addition.

## 4. Rejected or deferred libraries

| Library | Status | Reason | What remains authoritative |
|---|---|---|---|
| pandas-ta (original) | Reject | Beta-only, never left `0.4.x`, facing archival past its own 2026-07-01 deadline | `scripts/indicators.py`, then TA-Lib after PR 4 |
| Empyrical (original) | Reject | Abandoned since Quantopian's shutdown | `evaluation/metrics.py`, then empyrical-reloaded after PR 11 |
| QuantStats (original) | Reject as primary | Long stagnation before a recent revival; quantstats-lumi shares a maintainer with the already-adopted LumiBot | `evaluation/metrics.py`, then quantstats-lumi after PR 11 |
| freezegun | Reject as primary, fallback note only | Pure-Python monkeypatching misses time-travel inside pandas' C-extension datetime internals; no 3.14 support | time-machine |
| Pandera | Defer | No current DataFrame-shaped contract exists in the repo | Existing dataclass/YAML validation |
| PyArrow | Defer | No current bulk historical-dataset storage requirement; heaviest dependency evaluated | Existing fixtures/SQLite |
| pandas-ta-classic | Evaluate only if needed | Only relevant if TA-Lib's native C-library requirement proves infeasible in some CI/macOS target | TA-Lib as primary |

## 5. Special findings called out by the migration plan

- **VectorBT / LumiBot / Riskfolio-Lib / TA-Lib / QuantStats sharing one
  Python version:** they cannot share one *resolved environment* (VectorBT's
  `pandas>=3.0.3`/`numpy>=2.4.6` conflicts with LumiBot's `numpy<2.5.0`), but
  they do not need to — LumiBot is already isolated to its own distribution.
  All other libraries here are mutually compatible under Python 3.11 with a
  shared numpy>=2.4.6/pandas>=3.0 floor. Riskfolio-Lib hard-depends on
  `vectorbt>=0.28.0`, coupling it to the same floor.
- **TA-Lib native system package:** confirmed required (`brew install
  ta-lib` on macOS; equivalent apt package on Linux CI). This must be an
  explicit CI step in PR 4, not an incidental detail.
- **pandas-ta maintenance status:** confirmed abandoned in its original
  form; `pandas-ta-classic` is the actively maintained fork.
- **Empyrical maintenance status:** confirmed the original is unmaintained;
  `empyrical-reloaded` is the standard modern replacement, though its own
  release cadence has slowed (last release 2025-06).
- **LumiBot dependency-tree conflict:** confirmed — its `numpy<2.5.0` ceiling
  is incompatible with VectorBT's `numpy>=2.4.6` floor in anything but a
  razor-thin overlap window, and untested against VectorBT's `pandas>=3.0.3`
  floor at all. Isolation via `paper_runtime` (already existing) is the
  correct mitigation, not a new requirement.
- **SQLAlchemy/Alembic vs. trigger-heavy safety tables:** SQLite triggers
  fire at the SQL-statement level regardless of ORM/Core usage, but the
  ORM's unit-of-work flush ordering and identity-map caching can mask a
  trigger-rejected write. Recommendation carried into `MASTER_PLAN.md` PR 13:
  restrict any SQLAlchemy usage of trigger-protected tables (append-only
  tables, `real_orders`) to Core-only explicit statements, never ORM
  sessions.
- **APScheduler vs. database leases/generation fencing:** APScheduler v3's
  SQLite jobstore is explicitly documented upstream as unsuitable for
  multiple concurrent schedulers sharing a store, and provides only coarse
  scheduler-level locking, not a distributed lease with fencing tokens.
  Conclusion: APScheduler can coexist for simple due-time triggering: it
  cannot replace the existing lease/generation-fencing logic.
- **Tenacity retry scoping:** confirmed decorator/context-manager scoped
  only, no global interception. It can be structurally restricted away from
  the ambiguous-broker-retry path (`external_broker.py`) simply by never
  applying it there — recommend a structural test enforcing this, analogous
  to the existing LumiBot-import AST test.
- **Pandera/PyArrow deferral:** confirmed no concrete current need exists;
  both remain deferred until the VectorBT research adapter (PR 5)
  establishes one.
