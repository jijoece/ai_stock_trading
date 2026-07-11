from trading_research.analysis.ticker_extractor import counted_symbols, extract_mentions
from trading_research.universe.tickers import default_universe

U = default_universe()


def test_cashtag_counts_even_for_ambiguous_symbol():
    mentions = extract_mentions("I bought $AI yesterday", U)
    assert [m.symbol for m in mentions] == ["AI"]
    assert mentions[0].is_cashtag and mentions[0].counted


def test_bare_ambiguous_word_without_context_not_counted():
    # "AI" and "FOR" and "SO" as plain English — no finance context.
    text = "SO the answer is obvious, AI will change everything FOR everyone"
    assert counted_symbols(text, U) == set()


def test_bare_ambiguous_with_finance_context_counted():
    text = "AI stock earnings look great, price target raised"
    assert "AI" in counted_symbols(text, U)


def test_company_name_co_mention_confirms_ambiguous():
    text = "Gartner reported today and IT jumped"
    assert "IT" in counted_symbols(text, U)


def test_unknown_symbol_never_matches():
    assert counted_symbols("$ZZZZZ is going up, ZZZZZ to the moon", U) == set()


def test_unambiguous_bare_symbol_counts():
    assert "PLTR" in counted_symbols("PLTR keeps grinding higher", U)


def test_otc_symbol_rejected():
    assert counted_symbols("$SHELCO is a gem", U) == set()


def test_lowercase_cashtag_matches():
    assert "AAPL" in counted_symbols("loading up on $aapl", U)


def test_ambiguous_flag_preserved_on_cashtag():
    (m,) = extract_mentions("$ON semiconductor play", U)
    assert m.ambiguous is True and m.counted is True


def test_empty_text():
    assert extract_mentions("", U) == []


def test_cashtag_confidence_is_high():
    (m,) = extract_mentions("I am researching $AI before earnings", U)
    assert m.symbol == "AI" and m.counted
    assert m.confidence == "high"
    assert m.rejection_reason is None


def test_unambiguous_bare_symbol_confidence_is_medium():
    (m,) = extract_mentions("PLTR keeps grinding higher", U)
    assert m.confidence == "medium"
    assert m.rejection_reason is None


def test_ambiguous_confirmed_confidence_is_low():
    text = "AI stock earnings look great, price target raised"
    mentions = [m for m in extract_mentions(text, U) if m.symbol == "AI"]
    (m,) = mentions
    assert m.confidence == "low"
    assert m.counted
    assert m.rejection_reason is None


def test_ambiguous_rejected_has_confidence_and_reason():
    text = "SO the answer is obvious, AI will change everything FOR everyone"
    mentions = extract_mentions(text, U)
    for m in mentions:
        assert not m.counted
        assert m.confidence == "rejected"
        assert m.rejection_reason == "ambiguous symbol without contextual confirmation"


def test_mention_has_start_and_end_span():
    text = "loading up on $aapl today"
    (m,) = extract_mentions(text, U)
    assert text[m.start:m.end].upper() == "$AAPL"


def test_false_positive_it_looks_difficult():
    assert counted_symbols("IT looks difficult", U) == set()


def test_false_positive_turn_it_on():
    assert counted_symbols("Turn it ON", U) == set()


def test_false_positive_all_investors():
    assert counted_symbols("ALL investors should be careful", U) == set()


def test_company_name_reference_c3ai():
    text = "C3.ai, ticker AI, reported revenue growth"
    assert "AI" in counted_symbols(text, U)


def test_sofi_appears_in_discussions():
    text = "$SOFI appears in several discussions"
    assert "SOFI" in counted_symbols(text, U)
