"""Data models for Batch API requests and lifecycle tracking."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum


class BatchRequestStatus(str, Enum):
    SUCCEEDED = "succeeded"
    ERRORED = "errored"
    CANCELED = "canceled"
    EXPIRED = "expired"
    MISSING = "missing"


class ValidationStatus(str, Enum):
    PENDING = "pending"
    VALID = "valid"
    INVALID_JSON = "invalid_json"
    SCHEMA_FAILED = "schema_failed"
    TRUNCATED = "truncated"


def make_custom_id(run_id: str, workstream_id: str, version: int = 1) -> str:
    """research-<run_id>-<workstream_id>-v<version>, matching the spec's example format."""
    return f"research-{run_id}-{workstream_id}-v{version}"


@dataclass
class BatchRequestRecord:
    custom_id: str
    workstream_id: str
    run_id: str
    prompt_version: str
    prompt_hash: str
    input_hash: str
    source_set_hash: str
    submitted_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    retry_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BatchJobRecord:
    batch_id: str
    run_id: str
    model: str
    status: str
    created_at: str
    completed_at: str | None = None
    request_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)
