"""Confidence-calibration metrics (docs/milestone-6.md Step 17).

Model `confidence` (`research/models.py::ResearchDecision.confidence`, a
`Decimal` in `[0, 1]`) is not a probability unless it is empirically
calibrated against realized outcomes — this module buckets decisions by
confidence and reports the *observed* hit rate and return distribution per
bucket, never presenting a bucket with an inadequate sample
(docs/milestone-6.md: "Do not present confidence calibration with inadequate
samples").
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

DEFAULT_BUCKET_EDGES = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
DEFAULT_MIN_BUCKET_SAMPLE_SIZE = 10

STATUS_OK = "OK"
STATUS_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class CalibrationInput(NamedTuple):
    """One research decision's confidence paired with its eventual trading
    outcome. `net_return` is `None` when the paired recommendation never
    reached a COMPLETED/PARTIALLY_FILLED evaluation — such decisions still
    count toward `incomplete_rate`, never silently dropped."""

    confidence: float
    net_return: float | None
    analysis_incomplete: bool


@dataclass(frozen=True)
class CalibrationBucket:
    bucket_low: float
    bucket_high: float
    status: str
    sample_size: int
    hit_rate: float | None
    average_return: float | None
    incomplete_rate: float | None
    reason: str | None = None


def _bucket_index(confidence: float, edges: tuple[float, ...]) -> int:
    for i in range(len(edges) - 1):
        low, high = edges[i], edges[i + 1]
        if (low <= confidence < high) or (i == len(edges) - 2 and confidence == high):
            return i
    return len(edges) - 2  # clamp — confidence is validated in [0, 1] upstream


def compute_calibration(
    inputs: list[CalibrationInput], *, bucket_edges: tuple[float, ...] = DEFAULT_BUCKET_EDGES,
    min_sample_size: int = DEFAULT_MIN_BUCKET_SAMPLE_SIZE,
) -> tuple[CalibrationBucket, ...]:
    if len(bucket_edges) < 2:
        raise ValueError("bucket_edges must define at least one bucket")

    buckets: list[list[CalibrationInput]] = [[] for _ in range(len(bucket_edges) - 1)]
    for item in inputs:
        buckets[_bucket_index(item.confidence, bucket_edges)].append(item)

    results = []
    for i, items in enumerate(buckets):
        low, high = bucket_edges[i], bucket_edges[i + 1]
        if len(items) < min_sample_size:
            results.append(CalibrationBucket(
                bucket_low=low, bucket_high=high, status=STATUS_INSUFFICIENT_DATA, sample_size=len(items),
                hit_rate=None, average_return=None, incomplete_rate=None,
                reason=f"need at least {min_sample_size} decisions in this bucket, have {len(items)}",
            ))
            continue
        incomplete = sum(1 for i2 in items if i2.analysis_incomplete)
        completed = [i2 for i2 in items if i2.net_return is not None]
        hit_rate = (sum(1 for i2 in completed if i2.net_return > 0) / len(completed)) if completed else None
        average_return = (sum(i2.net_return for i2 in completed) / len(completed)) if completed else None
        results.append(CalibrationBucket(
            bucket_low=low, bucket_high=high, status=STATUS_OK, sample_size=len(items),
            hit_rate=hit_rate, average_return=average_return, incomplete_rate=incomplete / len(items),
        ))
    return tuple(results)
