"""Deterministic ticker-mention extraction from untrusted text.

Rules (in priority order):
1. Cashtags (`$AAPL`) count as a mention iff the symbol is in the verified
   universe. Cashtags of ambiguous symbols count too — the `$` is the context.
2. Bare uppercase tokens count iff in the universe AND either the symbol is
   not ambiguous, or the surrounding text contextually confirms it (finance
   vocabulary nearby or a company-name token co-mentioned).
3. Anything not in the universe is never a mention, no matter how it's written.

This module never calls an LLM and never mutates the source text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..universe.tickers import TickerUniverse

_CASHTAG = re.compile(r"\$([A-Za-z]{1,5})\b")
_BARE = re.compile(r"(?<![\w$])([A-Z]{1,5})(?![\w])")

# Finance vocabulary that confirms an ambiguous bare symbol is being used as
# a ticker. Matched case-insensitively within the confirmation window.
_CONTEXT_WORDS = frozenset(
    {
        "stock", "stocks", "shares", "share", "ticker", "calls", "puts",
        "earnings", "eps", "revenue", "guidance", "dividend", "buy", "sell",
        "long", "short", "position", "positions", "portfolio", "price",
        "target", "premarket", "aftermarket", "float", "squeeze", "ipo",
        "market", "cap", "valuation", "chart", "support", "resistance",
        "bullish", "bearish", "moon", "bag", "bags", "yolo", "dd",
    }
)

_CONTEXT_WINDOW = 60  # characters on each side of a bare mention


# Deterministic confidence categories (no model involved):
#   high     — explicit cashtag; the `$` is unambiguous ticker intent
#   medium   — bare symbol that collides with no English word
#   low      — bare ambiguous symbol, rescued by contextual confirmation
#   rejected — bare ambiguous symbol with no confirmation (not counted)
CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
CONFIDENCE_REJECTED = "rejected"

REJECTION_AMBIGUOUS_NO_CONTEXT = "ambiguous symbol without contextual confirmation"


@dataclass(frozen=True)
class TickerMention:
    symbol: str
    is_cashtag: bool
    ambiguous: bool
    context_confirmed: bool
    start: int
    end: int = -1
    confidence: str = ""
    rejection_reason: str | None = None

    @property
    def counted(self) -> bool:
        """Whether this mention should count toward aggregates."""
        return self.is_cashtag or not self.ambiguous or self.context_confirmed


def _has_context(text: str, start: int, end: int, name_tokens: set[str]) -> bool:
    lo = max(0, start - _CONTEXT_WINDOW)
    hi = min(len(text), end + _CONTEXT_WINDOW)
    window_tokens = {t.strip(".,!?:;()[]'\"").lower() for t in text[lo:hi].split()}
    if window_tokens & _CONTEXT_WORDS:
        return True
    # Company-name co-mention anywhere in the text confirms the symbol.
    if name_tokens:
        text_tokens = {t.strip(".,!?:;()[]'\"").lower() for t in text.split()}
        if text_tokens & name_tokens:
            return True
    return False


def extract_mentions(text: str, universe: TickerUniverse) -> list[TickerMention]:
    if not text:
        return []

    mentions: list[TickerMention] = []
    seen_spans: set[tuple[int, str]] = set()

    for m in _CASHTAG.finditer(text):
        symbol = m.group(1).upper()
        if not universe.is_valid(symbol):
            continue
        mentions.append(
            TickerMention(
                symbol=symbol,
                is_cashtag=True,
                ambiguous=universe.is_ambiguous(symbol),
                context_confirmed=True,
                start=m.start(),
                end=m.end(),
                confidence=CONFIDENCE_HIGH,
            )
        )
        seen_spans.add((m.start(1), symbol))

    for m in _BARE.finditer(text):
        symbol = m.group(1)
        if (m.start(1), symbol) in seen_spans:
            continue  # already captured as a cashtag
        if not universe.is_valid(symbol):
            continue
        ambiguous = universe.is_ambiguous(symbol)
        confirmed = True
        if ambiguous:
            confirmed = _has_context(text, m.start(), m.end(), universe.name_tokens(symbol))
        if not ambiguous:
            confidence, rejection = CONFIDENCE_MEDIUM, None
        elif confirmed:
            confidence, rejection = CONFIDENCE_LOW, None
        else:
            confidence, rejection = CONFIDENCE_REJECTED, REJECTION_AMBIGUOUS_NO_CONTEXT
        mentions.append(
            TickerMention(
                symbol=symbol,
                is_cashtag=False,
                ambiguous=ambiguous,
                context_confirmed=confirmed,
                start=m.start(),
                end=m.end(),
                confidence=confidence,
                rejection_reason=rejection,
            )
        )

    return mentions


def counted_symbols(text: str, universe: TickerUniverse) -> set[str]:
    """Convenience: the set of symbols with at least one counted mention."""
    return {m.symbol for m in extract_mentions(text, universe) if m.counted}
