import pytest

from trading_research.universe.tickers import (
    Security,
    TickerUniverse,
    UnknownSymbolError,
    default_universe,
    normalize_symbol,
)

U = default_universe()


def test_normalize_strips_and_uppercases():
    assert normalize_symbol("  sofi  ") == "SOFI"


def test_normalize_strips_leading_cashtag():
    assert normalize_symbol("$aapl") == "AAPL"


def test_normalize_rejects_empty():
    with pytest.raises(UnknownSymbolError):
        normalize_symbol("   ")


def test_normalize_rejects_embedded_whitespace():
    with pytest.raises(UnknownSymbolError):
        normalize_symbol("AA PL")


def test_known_symbol_is_valid():
    assert U.is_valid("AAPL")
    assert U.is_valid("aapl")  # case-insensitive


def test_unknown_symbol_is_not_valid():
    assert not U.is_valid("ZZZZZ")


def test_otc_symbol_is_not_valid():
    assert not U.is_valid("SHELCO")


def test_inactive_symbol_is_not_valid():
    assert not U.is_valid("GONEQ")


def test_require_returns_security_for_valid_symbol():
    sec = U.require("aapl")
    assert sec.symbol == "AAPL"
    assert sec.name == "Apple Inc"


def test_require_raises_for_unknown_symbol():
    with pytest.raises(UnknownSymbolError):
        U.require("ZZZZZ")


def test_require_fails_closed_for_otc():
    with pytest.raises(UnknownSymbolError):
        U.require("SHELCO")


def test_ambiguous_symbols_flagged():
    for sym in ("AI", "IT", "ON", "ALL", "SO", "A", "FOR", "ARE"):
        assert U.is_ambiguous(sym), f"{sym} should be flagged ambiguous"


def test_unambiguous_symbol_not_flagged():
    assert not U.is_ambiguous("PLTR")


def test_from_csv_round_trip(tmp_path):
    csv_path = tmp_path / "universe.csv"
    csv_path.write_text(
        "symbol,name,exchange,sector,is_otc,is_active,source\n"
        "ZTST,Test Corp,NASDAQ,Technology,0,1,unit-test\n"
        "ZOTC,OTC Corp,OTC,,1,1,unit-test\n"
        "ZOLD,Delisted Corp,NYSE,,0,0,unit-test\n"
    )
    universe = TickerUniverse.from_csv(csv_path)
    assert universe.is_valid("ZTST")
    assert not universe.is_valid("ZOTC")
    assert not universe.is_valid("ZOLD")
    sec = universe.get("ZTST")
    assert sec.source == "unit-test"


def test_from_csv_normalizes_symbol(tmp_path):
    csv_path = tmp_path / "universe.csv"
    csv_path.write_text("symbol,name,exchange\n  ztst  ,Test Corp,NASDAQ\n")
    universe = TickerUniverse.from_csv(csv_path)
    assert universe.is_valid("ZTST")


def test_name_tokens_used_for_contextual_confirmation():
    tokens = U.name_tokens("IT")
    assert "gartner" in tokens
    assert "inc" not in tokens  # corporate suffix stripped


def test_name_tokens_exclude_symbols_own_spelling():
    # ON Semiconductor Corp: "on" must not appear in its own name tokens,
    # or every bare "on" would self-confirm as a ticker mention via the
    # company-name co-mention rule (see ticker_extractor._has_context).
    tokens = U.name_tokens("ON")
    assert "on" not in tokens
    assert "semiconductor" in tokens


def test_security_defaults():
    sec = Security(symbol="XYZ", name="Xyz Inc", exchange="NYSE")
    assert sec.is_active is True
    assert sec.source == "seed"
    assert sec.is_otc is False
