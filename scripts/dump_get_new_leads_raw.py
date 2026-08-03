"""Diagnostic-only: hits GET /get-new-leads directly and prints the
RAW response body, completely bypassing indihomes_client.get_new_leads()'s
own extraction logic (data.get("leads", ...)). If the real response
wraps the lead array under a different key than "leads", or nests it
further, that extraction would silently return [] on a perfectly
successful 200 OK - this script exists to rule that in or out directly
rather than trusting our own parsing.

Usage:
    python scripts/dump_get_new_leads_raw.py
    python scripts/dump_get_new_leads_raw.py --after 2026-07-01T00:00:00.000Z
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from config import get_settings  # noqa: E402


async def main(after_date: str):
    settings = get_settings()
    base_url = settings.indihomes_base_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if settings.indihomes_api_key:
        headers["Authorization"] = f"Bearer {settings.indihomes_api_key}"

    url = f"{base_url}/get-new-leads"
    print(f"GET {url}?afterDate={after_date}")

    async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as client:
        resp = await client.get(url, params={"afterDate": after_date}, headers=headers)

    print(f"Status: {resp.status_code}")
    print(f"Raw body (first 3000 chars):")
    text = resp.text
    print(text[:3000])
    if len(text) > 3000:
        print(f"... ({len(text)} total chars, truncated)")

    try:
        data = resp.json()
        print(f"\nTop-level type: {type(data).__name__}")
        if isinstance(data, dict):
            print(f"Top-level keys: {list(data.keys())}")
            for key, value in data.items():
                if isinstance(value, list):
                    print(f"  data[{key!r}] is a list of {len(value)} item(s)")
                elif isinstance(value, dict):
                    print(f"  data[{key!r}] is a dict with keys: {list(value.keys())}")
        elif isinstance(data, list):
            print(f"Top-level is a list of {len(data)} item(s)")
    except Exception as exc:  # noqa: BLE001
        print(f"Could not parse as JSON: {exc}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--after", dest="after_date", default="2026-07-01T00:00:00.000Z")
    args = parser.parse_args()
    asyncio.run(main(args.after_date))
