#!/usr/bin/env python3
"""Probe credential-free Reddit-related endpoints without persisting payloads."""
from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

import httpx

MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MIN_REQUEST_INTERVAL_SECONDS = 2.0
USER_AGENT = "AgenticTradingDesk/1.0 (credential-free endpoint investigation)"

ENDPOINTS = {
    "apewisdom": "https://apewisdom.io/api/v1.0/filter/all-stocks/page/1",
    "reddit_rss": "https://www.reddit.com/r/wallstreetbets/search.rss?q=AAPL&restrict_sr=1&sort=new&limit=25",
    "old_reddit_json": "https://old.reddit.com/r/wallstreetbets/search.json?q=AAPL&restrict_sr=1&limit=25",
    "pushshift": "https://api.pushshift.io/reddit/submission/search/?q=AAPL&subreddit=wallstreetbets&size=25",
}


def _read_bounded(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ValueError(f"response exceeds {MAX_RESPONSE_BYTES} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _json_items(name: str, payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    if name == "apewisdom":
        items = payload.get("results")
    elif name == "old_reddit_json":
        children = payload.get("data", {}).get("children", [])
        items = [child.get("data") for child in children if isinstance(child, dict)]
    else:
        items = payload.get("data")
    return [item for item in (items or []) if isinstance(item, dict)]


def _rss_items(body: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    root = ET.fromstring(body)
    entries: list[dict[str, Any]] = []
    timestamps: list[str] = []
    for entry in root.findall("{http://www.w3.org/2005/Atom}entry"):
        updated = entry.findtext("{http://www.w3.org/2005/Atom}updated")
        entries.append({"updated": updated})
        if updated:
            timestamps.append(updated)
    return entries, timestamps


def _timestamp_summary(items: list[dict[str, Any]], extra: list[str] | None = None) -> dict[str, Any]:
    values: list[datetime] = []
    for item in items:
        for key in ("created_utc", "created", "timestamp"):
            raw = item.get(key)
            if isinstance(raw, (int, float)):
                values.append(datetime.fromtimestamp(float(raw), tz=timezone.utc))
                break
    for raw in extra or []:
        try:
            values.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        except ValueError:
            pass
    latest = max(values) if values else None
    age_hours = (datetime.now(timezone.utc) - latest).total_seconds() / 3600 if latest else None
    return {
        "latest_timestamp": latest.isoformat() if latest else None,
        "latest_age_hours": round(age_hours, 2) if age_hours is not None else None,
        "contains_recent_posts": age_hours is not None and -1 <= age_hours <= 72,
    }


def investigate() -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    last_started: float | None = None
    with httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=20.0, follow_redirects=True, trust_env=False
    ) as client:
        for name, url in ENDPOINTS.items():
            if last_started is not None:
                time.sleep(max(0.0, MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - last_started)))
            last_started = time.monotonic()
            report: dict[str, Any] = {"endpoint": name, "url": url}
            start = time.monotonic()
            try:
                with client.stream("GET", url) as response:
                    body = _read_bounded(response)
                    report.update(
                        http_status=response.status_code,
                        response_time_ms=round((time.monotonic() - start) * 1000),
                        final_url=str(response.url),
                        redirect_count=len(response.history),
                        response_bytes=len(body),
                        blocking=response.status_code in {401, 403, 429} or not body,
                    )
                    if response.status_code >= 400 or not body:
                        report.update(item_count=0, contains_recent_posts=False)
                    elif name == "reddit_rss":
                        items, timestamps = _rss_items(body)
                        report.update(item_count=len(items), **_timestamp_summary(items, timestamps))
                    else:
                        items = _json_items(name, json.loads(body))
                        report.update(item_count=len(items), **_timestamp_summary(items))
            except Exception as exc:
                report.update(
                    response_time_ms=round((time.monotonic() - start) * 1000),
                    item_count=0,
                    contains_recent_posts=False,
                    blocking=True,
                    error=f"{type(exc).__name__}: {exc}",
                )
            reports.append(report)
    return reports


if __name__ == "__main__":
    print(json.dumps(investigate(), indent=2, sort_keys=True))
