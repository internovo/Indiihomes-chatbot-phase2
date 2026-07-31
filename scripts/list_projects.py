"""Safe, read-only lookup of a few real projects from the IndiHomes
backend, for when there's no recent lead data to pull a real
project_code from (e.g. dry_run_leads.py returns 0 leads).

Prints the FULL raw project dict (not just a guessed "code" field) -
fetch_project's docstring warns the backend wants the project's `id`
field specifically, not `projectCode`/`projectName`, so this shows
every id-shaped field rather than assuming which one is right.

Calls the same fetch_filtered_projects wrapper the rest of the service
uses - no writes, no side effects.

Usage:
    python scripts/list_projects.py
    python scripts/list_projects.py --limit 3
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.indihomes_client import get_indihomes_client  # noqa: E402


async def main(limit: int):
    client = get_indihomes_client()
    print(f"Fetching up to {limit} project(s) ...")
    try:
        projects = await client.fetch_filtered_projects({"limit": limit})
    except Exception as exc:  # noqa: BLE001 - this is a diagnostic script
        print(f"!! request failed: {exc}")
        return

    if not projects:
        print("Backend returned 0 projects - check INDIHOMES_API_KEY / INDIHOMES_BASE_URL in .env.")
        return

    print(f"Got {len(projects)} project(s). Full raw dict for each, so we can see every id-shaped "
          f"field rather than guess which one fetch_project actually wants:\n")
    for p in projects:
        print(json.dumps(p, indent=2, default=str))
        print("-" * 60)

    print("\nLook for an 'id' / '_id' field specifically - fetch_project sends whatever you give it "
          "as `id`, and per its docstring that's NOT the same as projectCode/projectName. Then test with:")
    print("  python scripts/smoke_test_endpoints.py --project-code <the id value> --phone <real phone>")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args()
    asyncio.run(main(args.limit))
