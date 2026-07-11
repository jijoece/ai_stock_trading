#!/usr/bin/env python3

import argparse
import json
from pathlib import Path

import anthropic


DEFAULT_METADATA = Path("research-results/latest-batch.json")


def resolve_batch_id(batch_id: str | None) -> str:
    if batch_id:
        return batch_id

    if not DEFAULT_METADATA.exists():
        raise SystemExit(
            "No batch ID supplied and "
            "research-results/latest-batch.json does not exist."
        )

    metadata = json.loads(DEFAULT_METADATA.read_text(encoding="utf-8"))
    return metadata["batch_id"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-id")
    args = parser.parse_args()

    batch_id = resolve_batch_id(args.batch_id)

    client = anthropic.Anthropic()
    batch = client.messages.batches.retrieve(batch_id)

    print(f"Batch ID: {batch.id}")
    print(f"Status: {batch.processing_status}")
    print(f"Counts: {batch.request_counts}")
    print(f"Created: {batch.created_at}")
    print(f"Expires: {batch.expires_at}")
    print(f"Results URL: {batch.results_url}")


if __name__ == "__main__":
    main()