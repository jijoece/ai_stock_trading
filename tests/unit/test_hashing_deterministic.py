"""Milestone 11.3 Part 27: deterministic config hashing — no unrestricted
`json.dumps(..., default=str)` fallback, canonical types only."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.hashing import ConfigHashError, hash_config


def test_equivalent_key_order_hashes_identically():
    a = {"b": 1, "a": 2}
    b = {"a": 2, "b": 1}
    assert hash_config(a) == hash_config(b)


def test_stable_decimal_representation_regardless_of_construction():
    a = {"x": Decimal("1.50")}
    b = {"x": Decimal("1.5")}
    assert hash_config(a) == hash_config(b)


def test_finite_decimal_accepted():
    assert hash_config({"x": Decimal("3.14")}) == hash_config({"x": Decimal("3.14")})


def test_nonfinite_decimal_rejected():
    with pytest.raises(ConfigHashError):
        hash_config({"x": Decimal("NaN")})
    with pytest.raises(ConfigHashError):
        hash_config({"x": Decimal("Infinity")})


def test_nonfinite_float_rejected():
    with pytest.raises(ConfigHashError):
        hash_config({"x": float("nan")})
    with pytest.raises(ConfigHashError):
        hash_config({"x": float("inf")})


def test_finite_float_accepted():
    assert hash_config({"x": 0.5}) == hash_config({"x": 0.5})


def test_path_rejected():
    with pytest.raises(ConfigHashError):
        hash_config({"x": Path("/tmp/foo")})


def test_set_rejected():
    with pytest.raises(ConfigHashError):
        hash_config({"x": {1, 2, 3}})


def test_datetime_rejected_unless_normalized():
    from datetime import datetime, timezone
    with pytest.raises(ConfigHashError):
        hash_config({"x": datetime(2026, 1, 1, tzinfo=timezone.utc)})
    # Explicit normalization (isoformat string) is fine.
    assert hash_config({"x": "2026-01-01T00:00:00+00:00"})


def test_custom_object_rejected():
    class Widget:
        pass
    with pytest.raises(ConfigHashError):
        hash_config({"x": Widget()})


def test_non_string_mapping_key_rejected():
    with pytest.raises(ConfigHashError):
        hash_config({1: "a"})


def test_nested_structures_hash_deterministically():
    payload = {"a": [1, 2, {"b": Decimal("2.00")}], "c": None, "d": True}
    assert hash_config(payload) == hash_config(payload)


def test_tuple_and_list_hash_identically():
    assert hash_config({"x": (1, 2, 3)}) == hash_config({"x": [1, 2, 3]})


def test_result_is_stable_sha256_hex():
    result = hash_config({"a": 1})
    assert len(result) == 64
    assert all(c in "0123456789abcdef" for c in result)
