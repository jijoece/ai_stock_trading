#!/usr/bin/env python3

import argparse
import json
from pathlib import Path
from typing import Any

import anthropic


OUTPUT_DIR = Path("research-results")
DEFAULT_METADATA = OUTPUT_DIR / "latest-batch.json"


def resolve_batch_id(batch_id: str | None) -> str:
    if batch_id:
        return batch_id

    if not DEFAULT_METADATA.exists():
        raise SystemExit(
            "No batch ID supplied and latest-batch.json does not exist."
        )

    metadata = json.loads(DEFAULT_METADATA.read_text(encoding="utf-8"))
    return metadata["batch_id"]


def extract_text(message: Any) -> str:
    parts: list[str] = []

    for block in message.content:
        if block.type == "text":
            parts.append(block.text)

    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-id")
    args = parser.parse_args()

    batch_id = resolve_batch_id(args.batch_id)

    client = anthropic.Anthropic()
    batch = client.messages.batches.retrieve(batch_id)

    if batch.processing_status != "ended":
        raise SystemExit(
            f"Batch is not complete. Current status: "
            f"{batch.processing_status}"
        )

    raw_path = OUTPUT_DIR / f"{batch_id}-results.jsonl"
    extracted_dir = OUTPUT_DIR / batch_id
    extracted_dir.mkdir(parents=True, exist_ok=True)

    with raw_path.open("w", encoding="utf-8") as raw_file:
        for response in client.messages.batches.results(batch_id):
            raw_file.write(response.model_dump_json())
            raw_file.write("\n")

            custom_id = response.custom_id
            result = response.result

            if result.type == "succeeded":
                text = extract_text(result.message)
                output_path = extracted_dir / f"{custom_id}.md"
                output_path.write_text(text, encoding="utf-8")
                print(f"Saved: {output_path}")

            elif result.type == "errored":
                error_path = extracted_dir / f"{custom_id}.error.json"
                error_path.write_text(
                    json.dumps(
                        result.model_dump(mode="json"),
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                print(f"Errored: {custom_id}")

            else:
                print(f"{custom_id}: {result.type}")

    print(f"Raw JSONL: {raw_path}")


if __name__ == "__main__":
    main()