"""Deterministic content hashing for version-controlled configuration.

Used so every screening/scoring/risk config produces a reproducible
config_hash that can be stored on a frozen recommendation and independently
recomputed later for audit — never a random or time-based value.

`hash_config` no longer accepts `json.dumps(..., default=str)`'s unrestricted
fallback (Milestone 11.3 Part 27): that silently stringified *any* object
(`Path`, `set`, a custom class instance, a `datetime`) with no guarantee the
resulting string was stable, complete, or even reflected the object's real
state — two configurations that differ only in an unsupported field could
still hash identically, and vice versa. Only an explicit set of canonical
types is accepted; anything else raises `ConfigHashError` immediately rather
than being silently absorbed into the hash.
"""
from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal


class ConfigHashError(TypeError):
    """Raised when a value passed to `hash_config` is not one of the
    supported canonical types, or is a numeric type that is not finite."""


def _canonicalize(value: object, *, path: str) -> object:
    if value is None or type(value) is str:
        return value
    if type(value) is bool:
        return value
    if type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ConfigHashError(f"{path}: float must be finite — got {value!r}")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ConfigHashError(f"{path}: Decimal must be finite — got {value!r}")
        # format(..., 'f') on the normalized value collapses construction
        # differences (e.g. Decimal("1.50") vs Decimal("1.5")) to one stable
        # fixed-point string, and never emits scientific notation.
        return format(value.normalize(), "f")
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item, path=f"{path}[{i}]") for i, item in enumerate(value)]
    if isinstance(value, dict):
        canonical = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ConfigHashError(f"{path}: mapping keys must be strings — got key {key!r} of type {type(key).__name__}")
            canonical[key] = _canonicalize(item, path=f"{path}.{key}")
        return canonical
    raise ConfigHashError(
        f"{path}: unsupported type {type(value).__name__} for config hashing — "
        "only None/bool/int/float/Decimal/str/list/tuple/dict-with-string-keys are accepted; "
        "normalize this value (e.g. Path -> str, datetime -> explicit isoformat string, set -> sorted list) "
        "before passing it to hash_config"
    )


def hash_config(data: dict) -> str:
    """SHA-256 over sorted-key JSON of a canonicalized copy of `data`.
    Reproducible across runs and machines: unsupported/non-finite values
    fail loudly instead of being silently stringified. Callers remain
    responsible for excluding secrets/credentials from `data`."""
    canonical = _canonicalize(data, path="$")
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, allow_nan=False).encode()).hexdigest()
