"""Milestone 11.3 Part 30: `scripts/macro_pillar.py::extract_closes` must
validate the complete series before conversion — reject mixed floats/
mappings, mappings missing `close`, strings, `None`, `NaN`, infinity — with
a bounded domain error, not a raw `KeyError`/`TypeError`. `scripts/` has no
`__init__.py` (it's a set of standalone CLI scripts, per
`run-agentic-trading-desk` skill convention), so the module is loaded
directly from its file path."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "macro_pillar.py"


def _load_macro_pillar():
    spec = importlib.util.spec_from_file_location("macro_pillar_under_test", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["macro_pillar_under_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def macro_pillar():
    return _load_macro_pillar()


def test_plain_float_series_accepted(macro_pillar):
    result = macro_pillar.extract_closes({"RSP": [1.0, 2.0, 3]}, "RSP")
    assert result == [1.0, 2.0, 3.0]


def test_dict_shaped_series_accepted(macro_pillar):
    result = macro_pillar.extract_closes({"RSP": [{"close": 1.0}, {"close": 2}]}, "RSP")
    assert result == [1.0, 2.0]


def test_missing_symbol_returns_none_not_an_error(macro_pillar):
    assert macro_pillar.extract_closes({}, "RSP") is None


def test_empty_list_returns_none_not_an_error(macro_pillar):
    assert macro_pillar.extract_closes({"RSP": []}, "RSP") is None


def test_non_list_series_rejected(macro_pillar):
    with pytest.raises(macro_pillar.MarketDataShapeError):
        macro_pillar.extract_closes({"RSP": "not-a-list"}, "RSP")


def test_mixed_float_and_dict_rejected(macro_pillar):
    with pytest.raises(macro_pillar.MarketDataShapeError):
        macro_pillar.extract_closes({"RSP": [1.0, {"close": 2.0}]}, "RSP")


def test_dict_missing_close_key_rejected(macro_pillar):
    with pytest.raises(macro_pillar.MarketDataShapeError):
        macro_pillar.extract_closes({"RSP": [{"open": 1.0}]}, "RSP")


def test_string_element_rejected(macro_pillar):
    with pytest.raises(macro_pillar.MarketDataShapeError):
        macro_pillar.extract_closes({"RSP": ["1.0", "2.0"]}, "RSP")


def test_none_element_rejected(macro_pillar):
    with pytest.raises(macro_pillar.MarketDataShapeError):
        macro_pillar.extract_closes({"RSP": [1.0, None]}, "RSP")


def test_nan_value_rejected(macro_pillar):
    with pytest.raises(macro_pillar.MarketDataShapeError):
        macro_pillar.extract_closes({"RSP": [1.0, float("nan")]}, "RSP")


def test_infinity_value_rejected(macro_pillar):
    with pytest.raises(macro_pillar.MarketDataShapeError):
        macro_pillar.extract_closes({"RSP": [1.0, float("inf")]}, "RSP")


def test_nan_inside_dict_shape_rejected(macro_pillar):
    with pytest.raises(macro_pillar.MarketDataShapeError):
        macro_pillar.extract_closes({"RSP": [{"close": float("nan")}]}, "RSP")


def test_bool_element_rejected_not_silently_treated_as_0_or_1(macro_pillar):
    with pytest.raises(macro_pillar.MarketDataShapeError):
        macro_pillar.extract_closes({"RSP": [1.0, True]}, "RSP")


def test_bool_inside_dict_close_rejected(macro_pillar):
    with pytest.raises(macro_pillar.MarketDataShapeError):
        macro_pillar.extract_closes({"RSP": [{"close": True}]}, "RSP")


def test_score_macro_end_to_end_still_works_with_flexible_shapes(macro_pillar):
    """Regression: the full `score_macro` entry point still runs end-to-end
    over a realistic (long enough for default fast/slow/slope windows)
    dict-shaped series without raising."""
    n = 260
    series = {
        sym: [{"close": 100.0 + i * 0.1} for i in range(n)]
        for sym in ("RSP", "SPY", "HYG", "LQD", "IWM", "TLT", "XLY", "XLP")
    }
    result = macro_pillar.score_macro({"series": series})
    assert result.composite is not None
