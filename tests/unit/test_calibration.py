"""Unit tests for evaluation/calibration.py — Milestone 6 docs/milestone-6.md
Step 22 category J (confidence calibration)."""
from __future__ import annotations

from trading_research.evaluation.calibration import (
    STATUS_INSUFFICIENT_DATA,
    STATUS_OK,
    CalibrationInput,
    compute_calibration,
)


def test_bucket_with_insufficient_sample_is_not_presented():
    inputs = [CalibrationInput(confidence=0.9, net_return=0.05, analysis_incomplete=False)] * 3
    buckets = compute_calibration(inputs, min_sample_size=10)
    high_bucket = next(b for b in buckets if b.bucket_low <= 0.9 < b.bucket_high or b.bucket_high == 0.9)
    assert high_bucket.status == STATUS_INSUFFICIENT_DATA
    assert high_bucket.hit_rate is None


def test_bucket_with_adequate_sample_reports_hit_rate():
    inputs = (
        [CalibrationInput(confidence=0.85, net_return=0.02, analysis_incomplete=False) for _ in range(8)]
        + [CalibrationInput(confidence=0.85, net_return=-0.01, analysis_incomplete=False) for _ in range(2)]
    )
    buckets = compute_calibration(inputs, min_sample_size=10)
    high_bucket = next(b for b in buckets if b.bucket_low <= 0.85 < b.bucket_high)
    assert high_bucket.status == STATUS_OK
    assert high_bucket.hit_rate == 0.8
    assert high_bucket.sample_size == 10


def test_incomplete_analyses_counted_but_not_dropped():
    inputs = [CalibrationInput(confidence=0.5, net_return=None, analysis_incomplete=True) for _ in range(10)]
    buckets = compute_calibration(inputs, min_sample_size=10)
    mid_bucket = next(b for b in buckets if b.bucket_low <= 0.5 < b.bucket_high)
    assert mid_bucket.status == STATUS_OK
    assert mid_bucket.incomplete_rate == 1.0
    assert mid_bucket.hit_rate is None  # no completed evaluations to compute a hit rate from


def test_confidence_at_exact_upper_bound_falls_in_top_bucket():
    inputs = [CalibrationInput(confidence=1.0, net_return=0.01, analysis_incomplete=False) for _ in range(10)]
    buckets = compute_calibration(inputs, min_sample_size=10)
    assert buckets[-1].sample_size == 10
