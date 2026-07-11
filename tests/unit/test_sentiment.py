from trading_research.analysis.sentiment import (
    BEARISH,
    BULLISH,
    NEUTRAL,
    KeywordClassifier,
    RedditRecord,
    aggregate,
)

T0 = 1_800_000_000.0
DAY = 86_400.0


def rec(rid, rtype, author, created, text, engagement=10, dup=False, sub="stocks"):
    return RedditRecord(rid, rtype, "SOFI", author, sub, created, text, engagement, dup)


RECORDS = [
    rec("p1", "post", "a", T0 + 100, "buy SOFI, undervalued breakout", 50),
    rec("p2", "post", "b", T0 + 200, "SOFI puts, overvalued dump", 30),
    rec("c1", "comment", "a", T0 + 300, "just numbers, no opinion", 5),
    rec("p3", "post", "spam", T0 + 400, "buy buy buy moon", 2, dup=True),
    rec("c2", "comment", "c", T0 - 500, "buy calls", 5),  # before window
]


def test_classifier_labels():
    c = KeywordClassifier()
    assert c.classify("time to buy, very bullish, calls") == BULLISH
    assert c.classify("sell it all, bearish, puts") == BEARISH
    assert c.classify("mixed buy and sell signals") == NEUTRAL


def test_window_and_duplicate_exclusion():
    agg = aggregate(RECORDS, "SOFI", T0, T0 + DAY)
    assert agg.unique_posts == 2          # p3 excluded (dup), c2 out of window
    assert agg.unique_comments == 1
    assert agg.duplicates_excluded == 1
    assert agg.unique_authors == 2        # a, b (a appears twice)
    assert agg.total_mentions == 3


def test_sentiment_counts_and_net():
    agg = aggregate(RECORDS, "SOFI", T0, T0 + DAY)
    assert (agg.bullish, agg.bearish, agg.neutral) == (1, 1, 1)
    assert agg.net_sentiment == 0.0


def test_engagement_weighting():
    agg = aggregate(RECORDS, "SOFI", T0, T0 + DAY)
    assert agg.engagement_weighted == 85  # 50 + 30 + 5


def test_growth_none_when_no_prior():
    agg = aggregate(RECORDS, "SOFI", T0, T0 + DAY, prior_window_mentions=0)
    assert agg.mention_growth is None
    agg2 = aggregate(RECORDS, "SOFI", T0, T0 + DAY)
    assert agg2.mention_growth is None


def test_growth_computed_against_prior():
    agg = aggregate(RECORDS, "SOFI", T0, T0 + DAY, prior_window_mentions=2)
    assert agg.mention_growth == 0.5  # 3 vs 2


def test_symbol_filter():
    agg = aggregate(RECORDS, "PLTR", T0, T0 + DAY)
    assert agg.total_mentions == 0
    assert agg.net_sentiment == 0.0


def test_subreddit_distribution():
    agg = aggregate(RECORDS, "SOFI", T0, T0 + DAY)
    assert agg.subreddit_distribution == {"stocks": 3}
