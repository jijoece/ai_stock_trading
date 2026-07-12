"""Deterministic content hashing for version-controlled configuration.

Used so every screening/scoring/risk config produces a reproducible
config_hash that can be stored on a frozen recommendation and independently
recomputed later for audit — never a random or time-based value.
"""
from __future__ import annotations

import hashlib
import json


def hash_config(data: dict) -> str:
    """SHA-256 over sorted-key JSON. Reproducible across runs and machines."""
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
