"""Deterministic sentiment aggregation over classified Reddit records.

Division of labor (architecture §15): a Classifier labels individual texts
(the ONLY step an LLM may perform, later, behind the same interface); Python
computes every count, rate, window, and aggregate. The keyword classifier
below is a deterministic stand-in so the pipeline runs offline — it is not a
claim about NLP quality. A record may also arrive pre-classified (e.g. a
fixture built from an earlier Claude classification batch, or hand-labeled
test data) via `RedditRecord.classification`; when present, aggregate() uses
it directly and never re-derives the label from raw text.

All input text is untrusted (sentiment, not fact).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol

BULLISH = "bullish"
BEARISH = "bearish"
NEUTRAL = "neutral"

# Accounts younger than this are counted toward new-account concentration.
# Documented default; not user/LLM configurable at runtime.
NEW_ACCOUNT_AGE_DAYS_THRESHOLD = 30.0

# Share of in-window records showing duplicate/cross-post/repeated-link
# signals above which the aggregate is flagged as possible promotion.
# Documented heuristic default, deliberately conservative (high precision
# over recall) since this flag is supplementary, not a screening gate.
PROMOTION_RISK_RATIO_THRESHOLD = 0.30


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
class Classification:
    """A pre-computed label for one record — from a fixture or, later, a
    bounded Claude classification call. Never produced by aggregate() itself.
    """

    label: str  # bullish | bearish | neutral
    confidence: str | None = None  # "high" | "medium" | "low" | None (unavailable)
    catalyst_phrases: tuple[str, ...] = ()
    risk_phrases: tuple[str, ...] = ()


@dataclass(frozen=True)
class RedditRecord:
    """One post or comment, already ticker-attributed and injection-annotated.

    Extraction metadata (is_cashtag/ambiguous/context_confirmed) mirrors
    `reddit_ticker_mentions` columns and is expected to already be populated
    by analysis/ticker_extractor.py before a record reaches this module —
    aggregate() only counts, it never re-runs extraction.
    """

    record_id: str
    record_type: str  # "post" | "comment"
    symbol: str
    author: str
    subreddit: str
    created_utc: float
    text: str
    engagement: int = 0  # e.g. score + num_comments
    is_duplicate: bool = False
    is_cashtag: bool = False
    ambiguous: bool = False
    context_confirmed: bool = True
    is_cross_post: bool = False
    link_url: str | None = None
    author_account_age_days: float | None = None
    classification: Classification | None = None


@dataclass(frozen=True)
class MentionAggregate:
    symbol: str
    window_start: float
    window_end: float
    record_count: int
    unique_posts: int
    unique_comments: int
    unique_authors: int
    engagement_weighted: int
    bullish: int
    bearish: int
    neutral: int
    duplicates_excluded: int
    mention_velocity: float
    weighted_sentiment_score: float
    duplicate_post_count: int = 0
    duplicate_comment_count: int = 0
    cross_post_count: int = 0
    repeated_link_count: int = 0
    promotion_risk_flag: bool = False
    ambiguous_ticker_count: int = 0
    context_confirmed_ticker_count: int = 0
    cashtag_mention_count: int = 0
    new_account_concentration: float | None = None  # None when no account-age metadata is present
    catalyst_phrases: tuple[str, ...] = ()
    risk_phrases: tuple[str, ...] = ()
    price_discussion_lead_lag: str | None = None  # None unless price_points was supplied
    subreddit_distribution: dict[str, int] = field(default_factory=dict)
    sentiment_confidence_distribution: dict[str, int] = field(default_factory=dict)
    mention_growth: float | None = None  # vs. prior window; None when prior is empty/unknown

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


def _label_for(record: RedditRecord, classifier: Classifier) -> str:
    if record.classification is not None:
        return record.classification.label
    return classifier.classify(record.text)


def _lead_lag(
    live: list[RedditRecord],
    window_start: float,
    window_end: float,
    price_points: list[tuple[float, float]] | None,
) -> str | None:
    """Deterministic, coarse indicator of whether discussion growth or a price
    move came first within the window. None when price data isn't supplied —
    never fabricated (per 1B.2: "where suitable timestamped price data is
    supplied").
    """
    if not price_points or window_end <= window_start:
        return None
    midpoint = (window_start + window_end) / 2
    first_half = sum(1 for r in live if r.created_utc < midpoint)
    second_half = len(live) - first_half
    discussion_grew = second_half > first_half

    points = sorted(p for p in price_points if window_start <= p[0] <= window_end)
    if len(points) < 2:
        return "inconclusive"
    before = [p for p in points if p[0] < midpoint]
    after = [p for p in points if p[0] >= midpoint]
    if not before or not after:
        return "inconclusive"
    price_moved_second_half = abs(after[-1][1] - after[0][1]) > abs(before[-1][1] - before[0][1])

    if discussion_grew and price_moved_second_half:
        return "discussion_led"
    if not discussion_grew and not price_moved_second_half:
        return "price_led"
    return "inconclusive"


def aggregate(
    records: list[RedditRecord],
    symbol: str,
    window_start: float,
    window_end: float,
    classifier: Classifier | None = None,
    prior_window_mentions: int | None = None,
    price_points: list[tuple[float, float]] | None = None,
) -> MentionAggregate:
    """Aggregate one symbol's records within [window_start, window_end).

    Duplicates are excluded from every count except `record_count` (the raw
    observation count for the window, kept for auditability) and
    `duplicates_excluded`/`duplicate_*_count`. `prior_window_mentions` (the
    same metric computed over the preceding window) enables growth; growth is
    None when the prior window is empty/unknown — never fabricated. Empty
    input returns a valid zero-data aggregate (bullish=bearish=neutral=0,
    net_sentiment=0.0, empty confidence distribution) — a fully unavailable
    read, not a bullish or bearish conclusion.
    """
    classifier = classifier or KeywordClassifier()

    in_window = [
        r
        for r in records
        if r.symbol == symbol and window_start <= r.created_utc < window_end
    ]
    dup_records = [r for r in in_window if r.is_duplicate]
    live = [r for r in in_window if not r.is_duplicate]

    labels = [_label_for(r, classifier) for r in live]
    sentiments = Counter(labels)
    subreddits = Counter(r.subreddit for r in live)
    confidences = Counter(
        (r.classification.confidence if r.classification and r.classification.confidence else "unknown")
        for r in live
    )

    total = len(live)
    growth: float | None = None
    if prior_window_mentions is not None and prior_window_mentions > 0:
        growth = (total - prior_window_mentions) / prior_window_mentions

    duration_hours = (window_end - window_start) / 3600.0
    velocity = (total / duration_hours) if duration_hours > 0 else 0.0

    engagement_weighted = sum(max(r.engagement, 0) for r in live)
    if engagement_weighted > 0:
        signed = sum(
            max(r.engagement, 0) * (1 if lbl == BULLISH else -1 if lbl == BEARISH else 0)
            for r, lbl in zip(live, labels)
        )
        weighted_sentiment_score = signed / engagement_weighted
    else:
        weighted_sentiment_score = 0.0

    link_counts = Counter(r.link_url for r in live if r.link_url)
    repeated_link_count = sum(c - 1 for c in link_counts.values() if c > 1)
    cross_post_count = sum(1 for r in live if r.is_cross_post)
    duplicate_post_count = sum(1 for r in dup_records if r.record_type == "post")
    duplicate_comment_count = sum(1 for r in dup_records if r.record_type == "comment")

    promo_signal_count = len(dup_records) + cross_post_count + repeated_link_count
    promotion_risk_flag = bool(in_window) and (promo_signal_count / len(in_window)) >= PROMOTION_RISK_RATIO_THRESHOLD

    ages = [r.author_account_age_days for r in live if r.author_account_age_days is not None]
    new_account_concentration = (
        sum(1 for a in ages if a < NEW_ACCOUNT_AGE_DAYS_THRESHOLD) / len(ages) if ages else None
    )

    catalyst_phrases: list[str] = []
    risk_phrases: list[str] = []
    for r in live:
        if r.classification is None:
            continue
        for p in r.classification.catalyst_phrases:
            if p not in catalyst_phrases:
                catalyst_phrases.append(p)
        for p in r.classification.risk_phrases:
            if p not in risk_phrases:
                risk_phrases.append(p)

    return MentionAggregate(
        symbol=symbol,
        window_start=window_start,
        window_end=window_end,
        record_count=len(in_window),
        unique_posts=sum(1 for r in live if r.record_type == "post"),
        unique_comments=sum(1 for r in live if r.record_type == "comment"),
        unique_authors=len({r.author for r in live}),
        engagement_weighted=engagement_weighted,
        bullish=sentiments[BULLISH],
        bearish=sentiments[BEARISH],
        neutral=sentiments[NEUTRAL],
        duplicates_excluded=len(dup_records),
        mention_velocity=round(velocity, 4),
        weighted_sentiment_score=round(weighted_sentiment_score, 4),
        duplicate_post_count=duplicate_post_count,
        duplicate_comment_count=duplicate_comment_count,
        cross_post_count=cross_post_count,
        repeated_link_count=repeated_link_count,
        promotion_risk_flag=promotion_risk_flag,
        ambiguous_ticker_count=sum(1 for r in live if r.ambiguous),
        context_confirmed_ticker_count=sum(1 for r in live if r.ambiguous and r.context_confirmed),
        cashtag_mention_count=sum(1 for r in live if r.is_cashtag),
        new_account_concentration=(
            round(new_account_concentration, 4) if new_account_concentration is not None else None
        ),
        catalyst_phrases=tuple(catalyst_phrases),
        risk_phrases=tuple(risk_phrases),
        price_discussion_lead_lag=_lead_lag(live, window_start, window_end, price_points),
        subreddit_distribution=dict(subreddits),
        sentiment_confidence_distribution=dict(confidences) if live else {},
        mention_growth=growth,
    )


def aggregate_windows(
    records: list[RedditRecord],
    symbol: str,
    window_starts: list[float],
    window_size: float,
    classifier: Classifier | None = None,
) -> list[MentionAggregate]:
    """Aggregate the same symbol over several consecutive, explicit windows.

    `window_starts` must be sorted ascending; each window is
    [start, start + window_size). Growth for window i is computed against
    window i-1's total_mentions automatically (chained), so callers get
    mention-growth-over-time without recomputing prior windows by hand.
    """
    classifier = classifier or KeywordClassifier()
    results: list[MentionAggregate] = []
    prior_total: int | None = None
    for start in window_starts:
        agg = aggregate(
            records, symbol, start, start + window_size,
            classifier=classifier, prior_window_mentions=prior_total,
        )
        results.append(agg)
        prior_total = agg.total_mentions
    return results
