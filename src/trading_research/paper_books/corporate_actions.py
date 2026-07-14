"""Book-specific corporate-action application for isolated paper books
(docs/milestone-8.md Step 18).

Supports only what Milestone 7's `evidence_providers/corporate_actions.py`
already implements: `forward_split`, `reverse_split`, `cash_dividend`. Every
other action type is explicitly rejected (`UnsupportedCorporateActionError`)
rather than silently ignored — "unsupported action type remains explicit and
unapplied." Applied once per `(book_id, action_id)`; a book that holds no
position in the symbol at the time of a dividend simply receives $0 (still
recorded as applied, so a later purchase never retroactively receives it).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from ..hashing import hash_config
from ..storage import paper_books_repositories as repo
from . import cash_ledger, positions

SUPPORTED_ACTION_TYPES = ("forward_split", "reverse_split", "cash_dividend")


class UnsupportedCorporateActionError(RuntimeError):
    """An action_type outside SUPPORTED_ACTION_TYPES — fails closed, never applied."""


def apply_corporate_action(
    conn, book_id: str, *, action_id: str, symbol: str, action_type: str, effective_date: str,
    ratio: Decimal | None = None, dividend_per_share_usd: Decimal | None = None, now: datetime,
) -> dict:
    if action_type not in SUPPORTED_ACTION_TYPES:
        raise UnsupportedCorporateActionError(
            f"action_type {action_type!r} is not one of {SUPPORTED_ACTION_TYPES} — fails closed, unapplied"
        )
    if repo.corporate_action_applied(conn, book_id, action_id):
        return {"applied": False, "reason": "already applied"}

    source_hash = hash_config({
        "book_id": book_id, "action_id": action_id, "symbol": symbol, "action_type": action_type,
        "effective_date": effective_date, "ratio": str(ratio) if ratio is not None else None,
        "dividend_per_share_usd": str(dividend_per_share_usd) if dividend_per_share_usd is not None else None,
    })

    if action_type in ("forward_split", "reverse_split"):
        if ratio is None or ratio <= 0:
            raise ValueError(f"{action_type} requires a positive ratio")
        positions.apply_forward_or_reverse_split(conn, book_id, symbol, ratio, action_id, now)
        result = {"applied": True, "action_type": action_type, "ratio": str(ratio)}
    else:  # cash_dividend
        if dividend_per_share_usd is None or dividend_per_share_usd <= 0:
            raise ValueError("cash_dividend requires a positive dividend_per_share_usd")
        position = repo.load_position(conn, book_id, symbol)
        quantity_held = Decimal(position["quantity"]) if position else Decimal("0")
        dividend_amount = quantity_held * dividend_per_share_usd
        if dividend_amount > 0:
            cash_ledger.credit_dividend(conn, book_id, action_id, symbol, dividend_amount, now)
        result = {"applied": True, "action_type": action_type, "quantity_held": str(quantity_held), "dividend_amount_usd": str(dividend_amount)}

    repo.save_corporate_action_applied(
        conn, book_id, action_id, symbol, action_type, effective_date, ratio, dividend_per_share_usd, now, source_hash,
    )
    return result
