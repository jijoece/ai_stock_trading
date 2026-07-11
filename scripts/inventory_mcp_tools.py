#!/usr/bin/env python3
"""Inventory Robinhood + Reddit MCP tool capabilities and write sanitized reports.

Outputs:
    research-input/robinhood-tools.json
    research-input/reddit-tools.json
    research-input/mcp-inventory-summary.md

No Robinhood or Reddit write tool is ever called by this script.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_research.config import load_config  # noqa: E402
from trading_research.logging_config import configure_logging, get_logger  # noqa: E402
from trading_research.mcp import robinhood_inventory, reddit_adapter  # noqa: E402
from trading_research.mcp.capability_inventory import (  # noqa: E402
    write_inventory_json,
    write_summary_markdown,
)
from trading_research.storage import database, repositories  # noqa: E402

log = get_logger("scripts.inventory_mcp_tools")


def main() -> int:
    config = load_config()
    configure_logging(config.log_level)

    output_dir = Path(__file__).resolve().parents[1] / "research-input"

    rh_inv = robinhood_inventory.inventory(config)
    rh_path = write_inventory_json(rh_inv, output_dir)
    log.info("Wrote %s", rh_path)

    try:
        reddit_inv = reddit_adapter.build_reddit_capability_inventory(config)
        reddit_path = write_inventory_json(reddit_inv, output_dir)
        log.info("Wrote %s", reddit_path)
        inventories = [rh_inv, reddit_inv]
    except Exception as exc:  # noqa: BLE001 — surfaced to the operator, not swallowed
        log.error("Reddit MCP inventory failed: %s", exc)
        inventories = [rh_inv]

    summary_path = write_summary_markdown(inventories, output_dir)
    log.info("Wrote %s", summary_path)

    with database.session(config.research_database_path) as conn:
        for inv in inventories:
            repositories.save_capability_inventory(
                conn, inv.server_name, inv.inventory_timestamp, inv.to_dict()
            )

    print(f"Inventoried {len(inventories)} MCP server(s). See {output_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
