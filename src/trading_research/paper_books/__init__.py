"""Isolated baseline/enhanced paper-book subsystem (Milestone 8, docs/milestone-8.md).

This package is a wholly new, additive subsystem beside the existing
Milestone 3/4 global paper ledger (`paper/ledger.py`) — see
`docs/adr/0006-isolated-paper-books-and-portfolio-evaluation.md` Decision 1.
Nothing in this package imports `paper/ledger.py`, `execution/models.py`, or
mutates any `simulated_*`/`paper_cash_state` table; those remain completely
unchanged. Nothing in this package imports `anthropic`, `research.orchestration`,
or any Claude-facing module — Claude never selects a book, never sizes an
order, and never overrides a risk decision (docs/milestone-8.md "Authority
model").
"""
