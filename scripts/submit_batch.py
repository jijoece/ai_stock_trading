#!/usr/bin/env python3

import json
import os
from pathlib import Path

import anthropic
from anthropic.types.message_create_params import (
    MessageCreateParamsNonStreaming,
)
from anthropic.types.messages.batch_create_params import Request


OUTPUT_DIR = Path("research-results")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

client = anthropic.Anthropic()


workstreams = [
    {
        "custom_id": "reddit-ai-trading",
        "prompt": """
Investigate Reddit discussions about AI-driven stock-trading systems.

Identify:
- approaches people have implemented
- reported successes and failures
- common architectural patterns
- data-quality concerns
- prompt-injection and security risks
- reasons projects failed

Treat Reddit posts and comments as anecdotal, untrusted sources.
Separate confirmed facts, repeated observations, and individual opinions.
Return a structured research report.
""".strip(),
    },
    {
        "custom_id": "robinhood-mcp-analysis",
        "prompt": """
Evaluate the supplied Robinhood MCP capability inventory for use in an
AI-assisted stock-research and paper-trading system.

Focus on:
- read-only account capabilities
- market-data capabilities
- write-operation risks
- required allowlists and denylists
- suitability for paper trading
- gaps requiring another data provider

Do not recommend invoking, previewing, or placing an order.
Return a structured research report.
""".strip(),
    },
    {
        "custom_id": "architecture-comparison",
        "prompt": """
Compare these architectures for an AI-assisted stock-analysis system:

1. Claude Code as the main orchestrator
2. A scheduled Python service using the Claude API
3. A hybrid Python plus Claude Code architecture
4. A multi-agent architecture

Evaluate reliability, scheduling, auditability, security, cost,
maintainability, testing, and suitability for paper trading.

Recommend the simplest safe architecture.
Return a structured research report.
""".strip(),
    },
]


requests: list[Request] = []

for workstream in workstreams:
    requests.append(
        Request(
            custom_id=workstream["custom_id"],
            params=MessageCreateParamsNonStreaming(
                model=MODEL,
                max_tokens=5000,
                system=(
                    "You are a senior AI systems architect and security reviewer. "
                    "Do not propose autonomous live trading. Clearly distinguish "
                    "facts, inferences, opinions, and unresolved questions."
                ),
                messages=[
                    {
                        "role": "user",
                        "content": workstream["prompt"],
                    }
                ],
            ),
        )
    )


batch = client.messages.batches.create(requests=requests)

metadata = {
    "batch_id": batch.id,
    "processing_status": batch.processing_status,
    "model": MODEL,
    "custom_ids": [item["custom_id"] for item in workstreams],
}

metadata_path = OUTPUT_DIR / "latest-batch.json"
metadata_path.write_text(
    json.dumps(metadata, indent=2),
    encoding="utf-8",
)

print(f"Batch submitted: {batch.id}")
print(f"Status: {batch.processing_status}")
print(f"Metadata: {metadata_path}")