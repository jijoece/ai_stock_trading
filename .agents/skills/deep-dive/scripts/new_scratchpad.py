#!/usr/bin/env python3
"""Create a deep-dive investigation scratchpad from the template.

Usage:
    python3 new_scratchpad.py "<short topic>"

Prints the path to the created file so it can be opened/edited immediately.
"""
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

TEMPLATE = """# Deep Dive: {topic}

## Investigation Question
{topic}

## Mode
<mcp-analysis | repo-analysis | trading-research | agent-architecture | risk-review | website-research | implementation-plan | general>

## Current Status
Started {timestamp}

## Known Facts
-

## Assumptions
-

## Unknowns
-

## Hypotheses
-

## Sources to Check
- [ ] local code/files
- [ ] Git history
- [ ] MCP servers
- [ ] websites
- [ ] official documentation
- [ ] brokerage documentation
- [ ] market data sources
- [ ] news/social/reddit sources

## MCPs Used
-

## Websites Reviewed
-

## Files Reviewed
-

## Commands Run
-

## Evidence Collected
-

## Key Findings
-

## Risks / Caveats
-

## Decisions / Conclusions
-

## Open Questions
-

## Final Summary Draft

"""


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "investigation"


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: new_scratchpad.py \"<short topic>\"", file=sys.stderr)
        raise SystemExit(1)

    topic = " ".join(sys.argv[1:]).strip()
    ts = datetime.now(timezone.utc)
    stamp = ts.strftime("%Y%m%d-%H%M%S")
    slug = slugify(topic)

    out_dir = Path(__file__).resolve().parents[3] / "scratchpads" / "deep-dive"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{stamp}-{slug}.md"

    out_path.write_text(
        TEMPLATE.format(topic=topic, timestamp=ts.strftime("%Y-%m-%d %H:%M UTC")),
        encoding="utf-8",
    )
    print(out_path)


if __name__ == "__main__":
    main()
