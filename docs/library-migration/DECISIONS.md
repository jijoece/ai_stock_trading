# Migration Decisions

This document records every decision that changes, narrows, or reconciles an
accepted ADR, plus the library-adoption decisions established during PR 0
planning. It is the authoritative record for "why is the migration plan not
following `docs/milestones/rebuild/plan.md` literally in this area."

Governing principle (established 2026-07-26):

```text
Replace generic commodity infrastructure with open-source libraries.

Preserve project-specific domain infrastructure when it encodes safety,
book isolation, accounting invariants, auditability, operator authorization,
or ambiguous-side-effect recovery that the proposed library does not provide.
```

Any change that contradicts an accepted ADR must, before implementation:

1. identify the exact ADR decision being changed;
2. explain what repository evidence invalidates the original decision;
3. prove the replacement satisfies every original requirement;
4. include parity, safety, migration, and failure-recovery tests;
5. add a new ADR explicitly superseding the previous ADR;
6. be approved before implementation.

Until those conditions are met for a given item, the accepted ADR remains
authoritative and the original `plan.md` PR is not implemented as written.

---

## D1 — ADR 0006 (isolated paper books): custom accounting preserved

**Original plan.md item:** PR 9 "LumiBot portfolio and paper-accounting
integration" (make LumiBot authoritative for orders, fills, cash, positions,
portfolio value) and PR 10 "Remove custom generic ledgers."

**Status:** Rejected as originally scoped. No superseding ADR written or
required — no repository evidence invalidates ADR 0006's original reasoning.

**Reasoning:** `paper_books/` (`cash_ledger.py`, `positions.py`,
`valuation.py`, `reconciliation.py`) implements `book_id`-scoped isolation
(BASELINE/ENHANCED), FIFO lot accounting, a settled/reserved/available cash
model, point-in-time valuation with staleness flags, and immutable
append-only fill/cash evidence. LumiBot's paper-broker gateway
(`paper_runtime/.../lumibot_gateway.py`) keys state per runtime *process*,
not per `book_id` — ADR 0006 Decision 9 recorded this explicitly as the
reason Milestone 8 built an in-process simulator instead of routing through
`paper_runtime`. Nothing discovered during PR 0 inventory changes that fact.

**Revised scope:**

```text
LumiBot remains authoritative only for the isolated broker/runtime boundary.

paper_books remains authoritative for:
  logical book accounting, local cash, lots, positions, reservations,
  book-level valuation, book-level reconciliation, experiment isolation.
```

Replacement PRs: **PR 9 — Strengthen the LumiBot runtime normalization
contract** (LumiBot may supply normalized orders/statuses/fills/positions/
account snapshots) and **PR 10 — Broker-to-paper_books reconciliation parity
tests** (the application reconciles those observations into `paper_books`;
LumiBot never mutates book state directly). The custom book ledger is not a
removal target.

**Do not delete** `paper_books/cash_ledger.py`, `paper_books/positions.py`,
book-scoped lot accounting, cash/share reservations, book valuation, or
external reconciliation state unless a future *approved* ADR demonstrates
exact replacement semantics.

---

## D2 — ADR 0001 (frozen dataclasses): Pydantic narrowed to trust boundaries

**Original plan.md item:** PR 2 "Canonical data contracts" (introduce
Pydantic models broadly, per the original migration sequence).

**Status:** Narrowed, not superseded. No new ADR required unless a later PR
proposes expanding Pydantic beyond the boundary scope below.

**Reasoning:** ADR 0001 Decision 3 rejected `pydantic.BaseModel` for internal
contracts specifically because the repository had no other pydantic
dependency and dataclasses with `__post_init__` already provided the needed
fail-closed validation. That reasoning is undisturbed — every domain type
inventoried in PR 0 (`models/trading_models.py`,
`analysis/screener.py::ScreeningConfig`, `analysis/scorer.py::ScoringConfig`,
`recommendations/builder.py::FrozenRecommendation`, `paper_books/models.py`)
is still a `@dataclass(frozen=True)`.

**Revised scope — permitted Pydantic uses:**

```text
untrusted dict / YAML / JSON
        |
Pydantic boundary model
        |
explicit conversion
        |
frozen domain dataclass
```

YAML/environment configuration loading, CLI request validation, external
provider response parsing, broker/runtime JSONL message validation, and
API/serialized DTO validation are in scope. Internal domain code (strategy,
risk, accounting, persistence) continues to receive only frozen dataclasses.
Pydantic must never be passed through those layers merely for uniformity.

Replacement PR: **PR 2 — Introduce boundary validation without replacing
domain models**, comparing current hand-written validation against a
Pydantic boundary implementation on: dependency/performance impact,
error-message behavior, unknown-field rejection, secret-field handling.
Adopt only where it produces a clear reduction in custom boundary-validation
code. A new ADR documenting limited Pydantic adoption is required **only if**
PR 2 is actually implemented.

---

## D3 — Hard safety layer: preserved regardless of any library adoption

No ADR conflict — this is a standing constraint restated for this migration.
The following must remain enforced at the application's authoritative
gateway, never delegated to a library default:

```text
$50 maximum single trade
$150 maximum submitted paper notional per UTC day
10% maximum symbol allocation
no options / no margin / no shorting / whole shares only
LIMIT orders only / DAY time in force
paper environment verification
account fingerprint verification
explicit operator preview and submit
exact approval phrase
never auto-retry an ambiguous broker transmission
authoritative NOT_FOUND evidence required before retry
attempt-specific retry limits
cash and share reservations
append-only event and reconciliation evidence
```

Authoritative files: `paper_books/config.py` (limit values),
`paper_books/external_broker.py` (enforcement gateway, ambiguous-submission
recovery, fingerprint verification). These are excluded from every removal
manifest in this migration.

---

## D4 — Library adoption decisions (dependency-compatibility pass, 2026-07-26)

Full research detail in `DEPENDENCY_MATRIX.md`. Summary of decisions that
diverge from `plan.md`'s literal library list:

| Item | plan.md said | Decision | Reason |
|---|---|---|---|
| Empyrical | "Empyrical-compatible package" | Use **empyrical-reloaded** (stefan-jansen fork), not original `empyrical` | Original is abandoned (Quantopian archive) |
| QuantStats | "QuantStats" | Use **quantstats-lumi** (Lumiwealth fork), not original `quantstats` | Same maintainer as already-adopted LumiBot; more consistent release cadence |
| Indicators | "VectorBT indicators or TA-Lib" | **TA-Lib** primary; **pandas-ta-classic** evaluated only as a fallback if TA-Lib's native C-library requirement blocks a target environment | Original `pandas-ta` is functionally abandoned (beta-only, facing archival) |
| Time control (new, not in original plan.md) | — | Adopt **time-machine** over freezegun for test suites | freezegun's monkeypatch approach misses C-extension/pandas datetime internals; no Python 3.14 support |
| Python floor | Not specified in plan.md | Raise from `>=3.10` to `>=3.11` | VectorBT 1.1.0 requires `>=3.11`; nothing else in the adopted set requires more |
| Pandera, PyArrow | PR 2 / dataset storage | **Defer** — no concrete DataFrame-contract or bulk-storage need exists yet | Adding either speculatively increases install weight (PyArrow wheel 28–53MB) with no current consumer |

### Open items requiring resolution before implementation (not before PR 0)

1. **LumiBot backtest-mode import boundary (blocks PR 6 start, not PR 0).**
   ADR 0001's AST-enforced constraint
   (`test_no_lumibot_import_outside_runtime_package`) currently limits
   LumiBot imports to `runtime/lumibot/`. Using LumiBot's backtest mode for
   PR 6/7/8 parity work requires deciding whether that evaluation happens
   inside the existing `paper_runtime` boundary (preferred, no boundary
   change) or requires extending the enforced import boundary to a second
   package. Must be resolved and recorded here before PR 6 implementation
   begins.
2. **VectorBT license note.** The open-source `vectorbt` package is
   Apache-2.0 **with Commons Clause** (a fair-code restriction on reselling
   the software, not a restriction on internal research/paper-trading use).
   This is compatible with this repository's MIT-licensed, non-commercial
   internal use, but must be recorded explicitly here rather than silently
   assumed. No blocker; documentation only.
