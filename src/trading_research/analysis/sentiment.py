"""Deterministic sentiment aggregation over classified Reddit records.

Division of labor (architecture §15): a Classifier labels individual texts
(the ONLY step an LLM may perform, later, behind the same interface); Python
computes every count, rate, window, and aggregate. The keyword classifier
below is a deterministic stand-in so the pipeline runs offline — it is not a
claim about NLP quality.

All input text is untrusted (sentiment, not fact).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol

BULLISH = "bullish"
BEARISH = "bearish"
NEUTRAL = "neutral"


class Classifier(Protocol):
    def classify(self, text: str) -> str:
        """Return one of: bullish | bearish | neutral."""
        ...


class KeywordClassifier:
    """Deterministic keyword-count classifier (offline stand-in for the LLM)."""

    _BULL = frozenset({"buy", "bullish", "moon", "calls", "long", "undervalued", "breakout", "beat"})
    _BEAR = frozenset({"sell", "bearish", "puts", "short", "overvalued", "dump", "miss", "bagholder"})

    def classify(self, text: str) -> str:
        tokens = [t.strip(".,!?:;()[]'\"$").lower() for t in text.split()]
        bull = sum(t in self._BULL for t in tokens)
        bear = sum(t in self._BEAR for t in tokens)
        if bull > bear:
            return BULLISH
        if bear > bull:
            return BEARISH
        return NEUTRAL


@dataclass(frozen=True)
class RedditRecord:
    """One post or comment, already ticker-attributed and injection-annotated."""

    record_id: str
    record_type: str  # "post" | "comment"
    symbol: str
    author: str
    subreddit: str
    created_utc: float
    text: str
    engagement: int = 0  # e.g. score + num_comments
    is_duplicate: bool = False


@dataclass(frozen=True)
class MentionAggregate:
    symbol: str
    window_start: float
    window_end: float
    unique_posts: int
    unique_comments: int
    unique_authors: int
    engagement_weighted: int
    bullish: int
    bearish: int
    neutral: int
    duplicates_excluded: int
    subreddit_distribution: dict[str, int] = field(default_factory=dict)
    mention_growth: float | None = None  # vs. prior window; None when prior is empty

    @property
    def total_mentions(self) -> int:
        return self.unique_posts + self.unique_comments

    @property
    def net_sentiment(self) -> float:
        """(bullish - bearish) / classified, in [-1, 1]; 0 when nothing classified."""
        classified = self.bullish + self.bearish + self.neutral
        if classified == 0:
            return 0.0
        return (self.bullish - self.bearish) / classified


def aggregate(
    records: list[RedditRecord],
    symbol: str,
    window_start: float,
    window_end: float,
    classifier: Classifier | None = None,
    prior_window_mentions: int | None = None,
) -> MentionAggregate:
    """Aggregate one symbol's records within [window_start, window_end).

    Duplicates are excluded from every count. `prior_window_mentions` (the
    same metric computed over the preceding window) enables growth; growth is
    None when the prior window had no mentions — never fabricated.
    """
    classifier = classifier or KeywordClassifier()

    in_window = [
        r
        for r in records
        if r.symbol == symbol and window_start <= r.created_utc < window_end
    ]
    dupes = sum(1 for r in in_window if r.is_duplicate)
    live = [r for r in in_window if not r.is_duplicate]

    sentiments = Counter(classifier.classify(r.text) for r in live)
    subreddits = Counter(r.subreddit for r in live)

    total = len(live)
    growth: float | None = None
    if prior_window_mentions is not None and prior_window_mentions > 0:
        growth = (total - prior_window_mentions) / prior_window_mentions

    return MentionAggregate(
        symbol=symbol,
        window_start=window_start,
        window_end=window_end,
        unique_posts=sum(1 for r in live if r.record_type == "post"),
        unique_comments=sum(1 for r in live if r.record_type == "comment"),
        unique_authors=len({r.author for r in live}),
        engagement_weighted=sum(max(r.engagement, 0) for r in live),
        bullish=sentiments[BULLISH],
        bearish=sentiments[BEARISH],
        neutral=sentiments[NEUTRAL],
        duplicates_excluded=dupes,
        subreddit_distribution=dict(subreddits),
        mention_growth=growth,
    )
